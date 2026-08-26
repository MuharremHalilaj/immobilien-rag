"""
Objektunterlagen-Assistent (Stufe 2)

Liest alle Dokumente aus data_pdf/ ein, baut daraus einen Vektor-Index in
Postgres (pgvector, siehe docker-compose.yml) und beantwortet Fragen
dazu interaktiv in der Konsole, jeweils mit Quellenangabe.
"""

import os
import time

# nltk (Abhängigkeit von LlamaIndex für Satzsegmentierung beim Chunking)
# bringt seit 2026 einen Schutz gegen Modul-Hijacking aus dem aktuellen
# Arbeitsverzeichnis mit (CWE-427). Der schlägt hier fälschlich an, weil
# unser eigenes Projektverzeichnis (kein fremder/unsicherer Code) in
# sys.path steht. Offizieller Opt-out laut nltk-Dokumentation
# (nltk/inisec.py) — muss vor dem ersten nltk-Import gesetzt sein.
os.environ.setdefault("NLTK_DISABLE_IMPORT_SECURITY", "1")

import tiktoken
from dotenv import load_dotenv
import psycopg2

from llama_index.core import (
    SimpleDirectoryReader,
    VectorStoreIndex,
    StorageContext,
    Settings,
    PromptTemplate,
)
from llama_index.core.callbacks import CallbackManager, TokenCountingHandler
from llama_index.core.postprocessor import LLMRerank
from llama_index.core.vector_stores import MetadataFilter, MetadataFilters
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.llms.openai import OpenAI
from llama_index.vector_stores.postgres import PGVectorStore

import extraktion
import protokoll
import zusammenfassung
from db import verbindungsparameter as _pg_verbindungsparameter
from pdf_lader import OCRFallbackPDFReader

# API-Key aus .env laden (siehe .env-Datei, nicht in Git)
load_dotenv()

if not os.getenv("OPENAI_API_KEY"):
    raise RuntimeError(
        "OPENAI_API_KEY nicht gefunden. Bitte in der .env-Datei setzen "
        "(OPENAI_API_KEY=sk-...)."
    )

# Günstige, für dieses Projektstadium ausreichend starke Modelle statt
# der teureren LlamaIndex-Standardmodelle.
Settings.embed_model = OpenAIEmbedding(model="text-embedding-3-small")
Settings.llm = OpenAI(model="gpt-4o-mini")

DATA_DIR = "data_pdf"

# Postgres/pgvector-Zugangsdaten aus .env (siehe docker-compose.yml für
# den lokalen Container). embed_dim=1536 passt zu text-embedding-3-small.
# Neuer Tabellenname (v2), weil der PDF-Corpus (32 Dokumente, 8 Objekte,
# 4 Dokumenttypen) den alten Text-Corpus (9 Dokumente) ersetzt — die
# alte Tabelle "data_immobilien_chunks" bleibt unberührt in Postgres
# bestehen, wird aber von main.py nicht mehr verwendet.
PG_TABLE_NAME = "immobilien_chunks_v2"
PG_EMBED_DIM = 1536

# Wie viele Chunks pro Frage aus dem Vektorspeicher geholt werden.
# Bei 8 Objekten kann eine objektübergreifende Vergleichsfrage im
# Prinzip Kontext aus allen 8 Energieausweisen brauchen — SIMILARITY_TOP_K
# wird daher unten, nach dem ersten Indexaufbau, anhand der tatsächlichen
# Chunk-Zahl kalibriert (siehe Kommentar bei der Zuweisung weiter unten).
SIMILARITY_TOP_K = 12

# Für Fragen, die auf genau ein Objekt gefiltert sind (siehe
# beantworte_frage): breiter abrufen + immer per LLM neu ranken, weil
# reale Objekte mit vielen Dokumenten (Grundbuchauszüge, Protokolle,
# Teilungserklärungen ...) dieselbe Adresse in fast jedem Chunk
# wiederholen und einzelne Fakten sonst untergehen -- siehe Kommentar
# bei beantworte_frage() und docs/testergebnisse.md.
SIMILARITY_TOP_K_OBJEKT_GEFILTERT = 60
RERANK_TOP_N_OBJEKT_GEFILTERT = 8

# Optionales Reranking: holt wie bisher SIMILARITY_TOP_K Chunks per
# Vektor-Ähnlichkeit, lässt sie danach zusätzlich vom LLM nach
# tatsächlicher Relevanz zur Frage neu bewerten und behält nur die
# RERANK_TOP_N besten für die Antwortgenerierung (LLMRerank-
# Postprocessor, siehe beantworte_frage()). Per Env-Var togglebar, da
# noch nicht klar war, ob sich das beim aktuellen Corpus-Umfang (~40
# Chunks) überhaupt lohnt, oder nur zusätzliche Kosten/Latenz ohne
# messbaren Nutzen verursacht -- siehe docs/testergebnisse.md für den
# Vergleich mit/ohne Reranking.
AKTIVIERE_RERANKING = os.getenv("AKTIVIERE_RERANKING", "false").lower() == "true"
RERANK_TOP_N = 5

# Angepasster Antwort-Prompt: weist das Modell explizit an, Widersprüche
# zwischen Quellen offenzulegen statt sich stillschweigend für eine Angabe
# zu entscheiden, und nichts zu erfinden, was nicht im Kontext steht.
QA_PROMPT = PromptTemplate(
    "Kontextinformationen aus den Objektunterlagen sind unten angegeben.\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Beantworte die folgende Frage ausschließlich anhand der obigen "
    "Kontextinformationen.\n"
    "- Wenn verschiedene Quellen im Kontext unterschiedliche Angaben zum "
    "gleichen Sachverhalt machen, weise das explizit aus: nenne beide Werte "
    "und die jeweilige Quelle (Dateiname).\n"
    "- Verschiedene Dokumente können denselben Sachverhalt unterschiedlich "
    "benennen (z. B. 'Baujahr' im Exposé vs. 'Baujahr Gebäude' im "
    "Energieausweis). Prüfe bei Zahlen- und Datumsangaben gezielt, ob eine "
    "andere Quelle einen abweichenden Wert zum inhaltlich selben Punkt "
    "nennt — auch wenn die Bezeichnung nicht identisch ist — und behandle "
    "das wie einen Widerspruch.\n"
    "- Vorsicht bei Verwechslungen: Urkundenrollennummern (z. B. 'UR-Nr. "
    "884/1998'), Aktenzeichen, Bescheinigungsnummern, Grundbuchblatt-"
    "Bezeichnungen und Beurkundungs-/Änderungs-/Freigabedaten sind KEINE "
    "Baujahre, Kaufpreise oder sonstigen Kennzahlen, auch wenn eine "
    "Zahl darin wie ein Jahr oder Betrag aussieht — behandle sie nicht "
    "als abweichenden Wert zum selben Sachverhalt, nur weil beide "
    "Zahlen enthalten.\n"
    "- Wenn die Information nicht im Kontext enthalten ist, sage das "
    "ausdrücklich, anstatt zu raten oder Informationen zu erfinden.\n"
    "Frage: {query_str}\n"
    "Antwort: "
)


def _objekt_metadata(datei_pfad: str) -> dict:
    """
    file_metadata-Callback für SimpleDirectoryReader: extrahiert den
    Objektnamen aus dem Dateinamen (z.B. "gartenhof" aus
    "objekt2_gartenhof_expose.pdf") und legt ihn als eigenes
    Metadatenfeld "objekt_name" ab. Damit lässt sich Retrieval bei
    Fragen zu einem konkreten Objekt gezielt filtern (siehe
    _erkenne_objekt / beantworte_frage) statt über den ganzen Corpus zu
    suchen — vermeidet semantisches Rauschen zwischen sehr ähnlich
    aufgebauten Objekten (siehe Regression in docs/testergebnisse.md).
    """
    dateiname = os.path.basename(datei_pfad)
    teile = dateiname.split("_")
    objekt_name = teile[1] if len(teile) > 1 else "unbekannt"
    return {"objekt_name": objekt_name}


def _baue_pgvector_store() -> PGVectorStore:
    """
    Verbindet sich mit dem Postgres-Container (siehe docker-compose.yml)
    und liefert einen PGVectorStore. LlamaIndex legt darüber automatisch
    die Tabelle "data_<PG_TABLE_NAME>" an (inkl. pgvector-Extension),
    falls sie noch nicht existiert.
    """
    params = _pg_verbindungsparameter()
    return PGVectorStore.from_params(
        host=params["host"],
        port=params["port"],
        database=params["database"],
        user=params["user"],
        password=params["password"],
        table_name=PG_TABLE_NAME,
        embed_dim=PG_EMBED_DIM,
    )


def _anzahl_vorhandener_chunks() -> int:
    """
    Prüft direkt per SQL, ob in Postgres schon eingebettete Chunks liegen.
    Damit entscheidet baue_index(), ob der Index neu gebaut (und dabei
    kostenpflichtig neu embedded) oder einfach aus der Datenbank geladen
    werden kann.
    """
    tabelle = f"data_{PG_TABLE_NAME}"
    params = _pg_verbindungsparameter()
    conn = psycopg2.connect(**params)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_name = %s)",
                (tabelle,),
            )
            if not cur.fetchone()[0]:
                return 0
            cur.execute(f'SELECT COUNT(*) FROM "{tabelle}"')
            return cur.fetchone()[0]
    finally:
        conn.close()


def _bekannte_objektnamen() -> list[str]:
    """
    Liest die Menge aller vorkommenden objekt_name-Werte direkt per SQL
    aus Postgres — dynamisch statt hart codiert, damit neue/entfernte
    Objekte in data_pdf/ nicht manuell nachgepflegt werden müssen.
    """
    tabelle = f"data_{PG_TABLE_NAME}"
    conn = psycopg2.connect(**_pg_verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT DISTINCT metadata_->>\'objekt_name\' FROM "{tabelle}"'
            )
            return [row[0] for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


def _normalisiere_fuer_erkennung(text: str) -> str:
    """
    Gleiche Normalisierung wie _slug() in api.py (ß->ss, äöü->aou),
    damit z.B. "Stadelmannstraße" in einer Frage mit dem Slug-Bestandteil
    "stadelmannstrasse" übereinstimmt.
    """
    ersetzungen = str.maketrans("äöü", "aou")
    return text.lower().replace("ß", "ss").translate(ersetzungen)


def _erkenne_objekt(frage: str, bekannte_objekte: list[str]) -> str | None:
    """
    Prüft, ob die Frage genau einen bekannten Objektnamen enthält. Bei
    genau einem Treffer wird dieser für eine gezielte Metadaten-Filterung
    zurückgegeben. Bei keinem Treffer (allgemeine Frage) oder mehreren
    Treffern (z.B. Vergleichsfragen über mehrere Objekte) wird None
    zurückgegeben — dann sucht beantworte_frage() ungefiltert über den
    ganzen Corpus, wie bisher.

    Objektnamen aus einem Wort (unser synthetischer Testcorpus, z.B.
    "sonnenblick") müssen komplett in der Frage vorkommen. Mehrteilige
    Objektnamen aus echten Adressen (z.B. "aschaffenburg-
    stadelmannstrasse-15") würden mit einem exakten Volltreffer so gut
    wie nie matchen, weil kaum jemand eine Frage im Slug-Wortlaut
    inkl. Bindestrichen formuliert -- hier reicht ein Großteil der
    Bestandteile (alle bis auf höchstens einen, z.B. eine ausgelassene
    Hausnummer).
    """
    frage_norm = _normalisiere_fuer_erkennung(frage)
    treffer = []
    for name in bekannte_objekte:
        bestandteile = [
            _normalisiere_fuer_erkennung(teil) for teil in name.split("-") if teil
        ]
        gefunden = sum(1 for teil in bestandteile if teil in frage_norm)
        mindestanzahl = max(1, len(bestandteile) - 1)
        if gefunden >= mindestanzahl:
            treffer.append(name)
    return treffer[0] if len(treffer) == 1 else None


_TOKEN_ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")


def beantworte_frage(
    index: VectorStoreIndex, frage: str, bekannte_objekte: list[str], herkunft: str
):
    """
    Zentrale Anfrage-Funktion, genutzt von interaktive_schleife(), der
    Web-API (api.py) und vom Testkatalog (tests/testfragen.py) — stellt
    sicher, dass alle drei denselben Filterungs- und Prompt-Mechanismus
    verwenden. herkunft ("konsole"/"web"/"test") kennzeichnet im
    Anfrage-Protokoll (siehe protokoll.py), woher eine Anfrage kam,
    damit sich z.B. Testläufe von echter Nutzung unterscheiden lassen.

    Wird genau ein Objektname in der Frage erkannt, filtert die
    Vektorsuche gezielt auf dessen Dokumente (objekt_name-Metadatenfeld),
    statt über alle 8 Objekte hinweg zu suchen. Das reduziert semantisches
    Rauschen zwischen den strukturell sehr ähnlichen Objektunterlagen
    (siehe Regression in docs/testergebnisse.md). Bei Fragen ohne
    erkennbaren einzelnen Objektnamen (z.B. "Welches Objekt hat die beste
    Energieeffizienzklasse?") bleibt die Suche ungefiltert über den
    gesamten Corpus, damit objektübergreifende Vergleiche weiter
    funktionieren.
    """
    objekt = _erkenne_objekt(frage, bekannte_objekte)
    filters = None
    if objekt is not None:
        filters = MetadataFilters(
            filters=[MetadataFilter(key="objekt_name", value=objekt)]
        )

    # Token-Zählung läuft über Settings.callback_manager (global) statt
    # über die Query-Engine selbst: Settings.llm/Settings.embed_model
    # sind geteilte Singletons, die Callback-Events nur über den
    # globalen Callback-Manager feuern — ein callback_manager, der erst
    # nach dem Bau der Query-Engine gesetzt wird, erreicht sie nicht
    # (getestet: Token-Zahlen blieben dabei durchgehend 0).
    # Bekannte Einschränkung: Da Settings.callback_manager global ist,
    # können sich Zählungen bei echt gleichzeitigen Anfragen theoretisch
    # überschneiden. Bei der erwarteten Nutzung (einzelne Vertriebspartner,
    # keine Massenlast) ist das vernachlässigbar und für ein hartes
    # Locking (das alle Anfragen serialisieren würde) nicht gerechtfertigt.
    token_zaehler = TokenCountingHandler(tokenizer=_TOKEN_ENCODER.encode)
    urspruenglicher_callback_manager = Settings.callback_manager
    Settings.callback_manager = CallbackManager([token_zaehler])
    try:
        # Bei Filterung auf genau ein Objekt: viel breiter abrufen und
        # per LLM neu ranken statt sich auf reine Embedding-Ähnlichkeit
        # zu verlassen. Grund: Objekte mit vielen Dokumenten (bei
        # unseren 3 echten Test-Objekten 57-188 statt der 5 Chunks je
        # synthetischem Objekt) wiederholen dieselbe Adresse in fast
        # jedem Chunk -- die Embedding-Ähnlichkeit einzelner Fakten wie
        # "Baujahr 1950" unterscheidet sich davon kaum, wodurch der
        # richtige Chunk selbst mit SIMILARITY_TOP_K=12 nicht unter die
        # Top-Treffer kam (gemessen: Rang 42 von 107). LLMRerank holt
        # ihn zuverlässig auf Platz 1 -- siehe docs/testergebnisse.md.
        # Für die ungefilterte, objektübergreifende Suche bleibt es
        # beim bewusst schmaleren SIMILARITY_TOP_K/optionalen
        # AKTIVIERE_RERANKING (siehe Kommentar oben, andere Kalibrierung
        # für einen ganz anderen Anwendungsfall).
        if filters is not None:
            similarity_top_k = SIMILARITY_TOP_K_OBJEKT_GEFILTERT
            node_postprocessors = [
                LLMRerank(top_n=RERANK_TOP_N_OBJEKT_GEFILTERT, llm=Settings.llm)
            ]
        else:
            similarity_top_k = SIMILARITY_TOP_K
            node_postprocessors = []
            if AKTIVIERE_RERANKING:
                node_postprocessors.append(
                    LLMRerank(top_n=RERANK_TOP_N, llm=Settings.llm)
                )

        query_engine = index.as_query_engine(
            similarity_top_k=similarity_top_k,
            filters=filters,
            node_postprocessors=node_postprocessors,
        )
        query_engine.update_prompts(
            {"response_synthesizer:text_qa_template": QA_PROMPT}
        )
        start = time.perf_counter()
        antwort = query_engine.query(frage)
        latenz_ms = int((time.perf_counter() - start) * 1000)
    finally:
        Settings.callback_manager = urspruenglicher_callback_manager

    quellen = [
        {"dateiname": node.metadata.get("file_name", "unbekannt"), "score": node.score}
        for node in antwort.source_nodes
    ]
    protokoll.eintrag_schreiben(
        herkunft=herkunft,
        frage=frage,
        antwort=str(antwort),
        objekt_filter=objekt,
        quellen=quellen,
        prompt_tokens=token_zaehler.prompt_llm_token_count,
        completion_tokens=token_zaehler.completion_llm_token_count,
        # Bleibt in der Praxis meist 0: PGVectorStore ruft die Query-
        # Embedding-Erzeugung offenbar auf einem Pfad auf, der keine
        # Callback-Events feuert (LLM-Tokens werden dagegen zuverlässig
        # erfasst). Fällt kaum ins Gewicht — Embeddings sind laut
        # OpenAI-Preisliste ca. 7x günstiger als LLM-Tokens, und pro
        # Anfrage steht nur die kurze Frage selbst zur Embedding an.
        embedding_tokens=token_zaehler.total_embedding_token_count,
        latenz_ms=latenz_ms,
    )

    return antwort, objekt


def baue_index() -> VectorStoreIndex:
    """
    Liefert den Vektor-Index — entweder aus Postgres geladen oder neu
    gebaut und dort gespeichert.

    Persistenz: Anders als bei der lokalen SimpleVectorStore-Variante ist
    die Datenbank selbst der persistente Speicher — es gibt keinen
    separaten "storage/"-Ordner mehr. Enthält die Tabelle bereits Chunks,
    wird direkt darauf aufgesetzt (VectorStoreIndex.from_vector_store),
    ohne erneut zu embedden. Wichtig: Wenn sich Dateien in data/ ändern,
    merkt das System das NICHT von selbst — dazu die Tabelle leeren
    (z.B. `docker compose down -v` für einen kompletten Reset) oder
    PG_TABLE_NAME ändern, damit neu gebaut wird.

    Ablauf beim Neubau (Chunking + Embedding) ist identisch zur
    SimpleVectorStore-Variante:
    1. SimpleDirectoryReader liest jede Datei als ein "Document"-Objekt
       ein (Text + Metadaten, u.a. der Dateiname unter "file_name").
    2. VectorStoreIndex.from_documents() übernimmt intern zwei Schritte:
       a) Chunking: Jedes Document wird in kleinere "Nodes" (Textabschnitte)
          zerlegt. Standardmäßig verwendet LlamaIndex dafür den
          SentenceSplitter mit fester Chunk-Größe und Overlap
          (Standard: chunk_size=1024 Tokens, chunk_overlap=200 Tokens).
       b) Embedding: Für jeden Chunk wird über die OpenAI-Embedding-API
          ein Vektor berechnet (hier: text-embedding-3-small). Die
          Vektoren werden diesmal nicht mehr lokal im Arbeitsspeicher,
          sondern über den PGVectorStore direkt in Postgres geschrieben.
    """
    protokoll.sicherstelle_tabelle()
    extraktion.sicherstelle_tabelle()
    zusammenfassung.sicherstelle_tabelle()
    vector_store = _baue_pgvector_store()

    anzahl = _anzahl_vorhandener_chunks()
    if anzahl > 0:
        print(f"Lade bestehenden Index aus Postgres ({anzahl} Chunks) ...")
        return VectorStoreIndex.from_vector_store(vector_store)

    dokumente = SimpleDirectoryReader(
        DATA_DIR,
        file_metadata=_objekt_metadata,
        file_extractor={".pdf": OCRFallbackPDFReader()},
    ).load_data()
    print(f"{len(dokumente)} Dokument(e) aus '{DATA_DIR}/' eingelesen.")

    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_documents(
        dokumente, storage_context=storage_context
    )
    print("Index in Postgres (pgvector) gespeichert.")

    _extrahiere_kennzahlen_je_datei(dokumente)

    objekt_namen = {doc.metadata.get("objekt_name", "unbekannt") for doc in dokumente}
    print(f"Erzeuge Objekt-Zusammenfassungen für {len(objekt_namen)} Objekt(e) ...")
    for objekt_name in objekt_namen:
        zusammenfassung.erzeuge_und_speichere(objekt_name)

    return index


def zusammenfassung_backfill() -> int:
    """
    Erzeugt/aktualisiert die Objekt-Zusammenfassung (zusammenfassung.py)
    für alle aktuell bekannten Objekte, unabhängig davon, ob der
    Vektor-Index gerade neu gebaut wurde. Für den Fall, dass Objekte schon
    vor Einführung dieses Features hochgeladen wurden (z.B. über
    api.py: POST /api/admin/zusammenfassung-backfill, Basic-Auth-geschützt).
    """
    bekannte_objekte = _bekannte_objektnamen()
    for objekt_name in bekannte_objekte:
        zusammenfassung.erzeuge_und_speichere(objekt_name)
    return len(bekannte_objekte)


def kennzahlen_backfill() -> int:
    """
    Holt die Kennzahlen-Extraktion (extraktion.py) nachträglich für
    alle Dateien in DATA_DIR nach, ohne den Vektor-Index anzufassen.
    Für den Fall, dass baue_index() beim letzten Aufbau auf eine
    bereits gefüllte Chunk-Tabelle traf und die automatische Extraktion
    deshalb übersprungen hat (z.B. weil extraktion.py erst nachträglich
    hinzugefügt wurde) — sonst müsste man dafür die komplette
    Vektor-Tabelle leeren und neu embedden. Aufrufbar über
    api.py: POST /api/admin/kennzahlen-backfill (Basic-Auth-geschützt).
    Gibt die Anzahl der verarbeiteten Dateien zurück.
    """
    dokumente = SimpleDirectoryReader(
        DATA_DIR,
        file_metadata=_objekt_metadata,
        file_extractor={".pdf": OCRFallbackPDFReader()},
    ).load_data()
    _extrahiere_kennzahlen_je_datei(dokumente)
    return len({doc.metadata.get("file_name", "unbekannt") for doc in dokumente})


def _extrahiere_kennzahlen_je_datei(dokumente: list) -> None:
    """
    Strukturierte Kennzahlen (Kaufpreis, Wohnfläche, ...) laufen pro
    Datei, nicht pro Document/Seite: PDFReader erzeugt ein Document je
    PDF-Seite (siehe baue_index-Docstring), und mehrseitige Dokumente
    (z.B. Teilungserklärung) sollen als ein zusammenhängender Text
    extrahiert werden statt Seite für Seite mit sich gegenseitig
    überschreibenden Ergebnissen (siehe extraktion.py: dateiname ist
    eindeutiger Schlüssel je Datensatz).
    """
    texte_je_datei: dict[str, list[str]] = {}
    objekt_je_datei: dict[str, str] = {}
    for doc in dokumente:
        dateiname = doc.metadata.get("file_name", "unbekannt")
        texte_je_datei.setdefault(dateiname, []).append(doc.text)
        objekt_je_datei[dateiname] = doc.metadata.get("objekt_name", "unbekannt")

    print(f"Extrahiere Kennzahlen aus {len(texte_je_datei)} Datei(en) ...")
    for dateiname, texte in texte_je_datei.items():
        extraktion.extrahiere_und_speichere(
            objekt_je_datei[dateiname], dateiname, "\n".join(texte)
        )


def interaktive_schleife(index: VectorStoreIndex) -> None:
    """
    Erlaubt es, wiederholt Fragen an den Index zu stellen.

    beantworte_frage() (siehe oben) läuft dabei in zwei Schritten ab:
    1. Retrieval: Die Frage wird ebenfalls in einen Vektor umgewandelt
       (Embedding). Per Kosinus-Ähnlichkeit werden die "similarity_top_k"
       ähnlichsten Chunks per pgvector aus Postgres geholt — LlamaIndex-
       Standard wäre 2; wir setzen SIMILARITY_TOP_K=12 (siehe oben), weil
       2 Chunks für objektübergreifende Vergleichsfragen nicht reichen.
       Wird in der Frage genau ein Objektname erkannt, wird die Suche
       zusätzlich per Metadaten-Filter auf dessen Dokumente eingegrenzt.
    2. Generation: Die gefundenen Chunks werden zusammen mit der Frage
       als Kontext an das LLM (hier: gpt-4o-mini, siehe Settings.llm)
       geschickt, das daraus die Antwort formuliert.

    Die Quellenangabe kommt aus response.source_nodes: Jeder verwendete
    Chunk (Node) trägt die Metadaten seines Ursprungs-Dokuments (u.a.
    file_name, objekt_name) mit sich, weil SimpleDirectoryReader diese
    beim Einlesen an jedes Document angehängt hat und sie beim Chunking
    an die Nodes vererbt werden.
    """
    bekannte_objekte = _bekannte_objektnamen()

    print("\nObjektunterlagen-Assistent bereit. Stelle deine Fragen.")
    print("Zum Beenden 'exit' oder 'quit' eingeben.\n")

    while True:
        frage = input("Frage: ").strip()
        if not frage:
            continue
        if frage.lower() in {"exit", "quit"}:
            print("Auf Wiedersehen.")
            break

        antwort, objekt = beantworte_frage(index, frage, bekannte_objekte, herkunft="konsole")

        if objekt:
            print(f"\n[Retrieval gefiltert auf Objekt: {objekt}]")
        print(f"\nAntwort: {antwort}\n")
        print("Quellen:")
        for node in antwort.source_nodes:
            dateiname = node.metadata.get("file_name", "unbekannt")
            score = node.score
            print(f"  - {dateiname} (Ähnlichkeit: {score:.3f})")
        print()


if __name__ == "__main__":
    index = baue_index()
    interaktive_schleife(index)
