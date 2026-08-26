"""
Objekt-Zusammenfassung: fasst alle Dokumente eines Objekts zu einer
strukturierten, für ein Kundengespräch schnell überfliegbaren Übersicht
zusammen -- Ergänzung zu extraktion.py (dort: 7 feste Kennzahlen PRO
DOKUMENT) um alles andere Wichtige aus den Dokumenten, einmal PRO OBJEKT.

Läuft nach jedem Upload für das betroffene Objekt neu (siehe
erzeuge_und_speichere, aufgerufen aus api.py), nicht bei jedem
Dashboard-Aufruf -- die Detailansicht soll sofort aus der Datenbank
geladen sein, nicht auf einen LLM-Call warten (siehe docs/testergebnisse.md,
Abschnitt zur Live-Gesprächs-Nutzung).
"""

import psycopg2
import psycopg2.extras
import tiktoken
from pydantic import BaseModel, Field

from llama_index.core import PromptTemplate, Settings

from db import verbindungsparameter

TABELLE = "objekt_zusammenfassungen"

# Bleibt deutlich unter dem 128k-Kontextfenster von gpt-4o-mini, auch
# nach Abzug von Prompt-Text und Platz für die Antwort. Reale Objekte
# mit vielen Dokumenten (z.B. Mainz mit 188 Chunks) können diesen Wert
# überschreiten -- siehe _text_mit_budget für den Umgang damit.
_MAX_KONTEXT_TOKENS = 100_000
_TOKEN_ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")

ZUSAMMENFASSUNGS_PROMPT = PromptTemplate(
    "Die folgenden Textauszüge stammen aus allen Dokumenten (Exposé, "
    "Energieausweis, Protokolle, Teilungserklärung, Grundbuchauszug, "
    "Gutachten, Rechnungen, ...), die für ein einzelnes Immobilienobjekt "
    "vorliegen. Erstelle daraus eine Übersicht für einen Vertriebspartner, "
    "der sich in einem laufenden Kundengespräch schnell einen verlässlichen "
    "Überblick verschaffen muss.\n\n"
    "Wichtig: Erfinde nichts, was nicht in den Auszügen steht. Wenn eine "
    "übliche Angabe (z.B. Kaufpreis) fehlt, lass sie einfach weg statt zu "
    "raten. Wenn zwei Dokumente sich bei einer Angabe widersprechen "
    "(z.B. abweichende Wohnfläche in Exposé vs. Energieausweis), trage das "
    "explizit unter offene_punkte ein statt dich für einen Wert zu "
    "entscheiden.\n\n"
    "- kurzueberblick: 2-3 Sätze Gesamtüberblick über das Objekt.\n"
    "- eckdaten: kurze Stichpunkte zu den wichtigsten Fakten (Kaufpreis, "
    "Wohnfläche, Zimmer, Baujahr, Energieeffizienzklasse, Hausgeld, Lage "
    "-- was jeweils vorhanden ist), jeweils mit Angabe, aus welchem "
    "Dokument die Angabe stammt.\n"
    "- besonderheiten: alles, was für ein Verkaufsgespräch relevant sein "
    "könnte und nicht in die Eckdaten passt -- Rechte/Lasten, geplante "
    "Modernisierungen, Zustand, Beschlüsse aus Protokollen, "
    "Sondernutzungsrechte, Auflagen.\n"
    "- offene_punkte: Widersprüche zwischen Quellen ODER wichtige Angaben, "
    "die in den Dokumenten fehlen.\n\n"
    "Textauszüge:\n{text}"
)


class ObjektZusammenfassung(BaseModel):
    kurzueberblick: str = Field(description="2-3 Sätze Gesamtüberblick über das Objekt")
    eckdaten: list[str] = Field(default_factory=list, description="Wichtigste Fakten als kurze Stichpunkte mit Quellenangabe")
    besonderheiten: list[str] = Field(default_factory=list, description="Für ein Verkaufsgespräch relevante Besonderheiten")
    offene_punkte: list[str] = Field(default_factory=list, description="Widersprüche zwischen Quellen oder fehlende wichtige Angaben")


def sicherstelle_tabelle() -> None:
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABELLE} (
                    objekt_name TEXT PRIMARY KEY,
                    kurzueberblick TEXT,
                    eckdaten JSONB,
                    besonderheiten JSONB,
                    offene_punkte JSONB,
                    anzahl_dokumente INT,
                    text_gekuerzt BOOLEAN NOT NULL DEFAULT false,
                    aktualisiert_am TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def _text_mit_budget(objekt_name: str) -> tuple[str, int, bool]:
    """
    Holt alle Chunk-Texte des Objekts aus der Vektor-Tabelle und hängt sie
    aneinander, bis _MAX_KONTEXT_TOKENS erreicht ist. Gekürzt statt
    abgebrochen, damit ein Objekt mit sehr vielen Dokumenten trotzdem eine
    (dann unvollständige, aber klar als solche markierte) Zusammenfassung
    bekommt statt gar keine.

    Lokaler Import von PG_TABLE_NAME wegen Zirkelbezug: main.py importiert
    dieses Modul (siehe main.baue_index), daher kann hier nicht auf
    Modulebene aus main importiert werden.
    """
    from main import PG_TABLE_NAME

    tabelle = f"data_{PG_TABLE_NAME}"
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT text, metadata_->>\'file_name\' FROM "{tabelle}" '
                f"WHERE metadata_->>'objekt_name' = %s ORDER BY metadata_->>'file_name'",
                (objekt_name,),
            )
            zeilen = cur.fetchall()
    finally:
        conn.close()

    dateinamen = {name for _, name in zeilen if name}
    teile = []
    token_summe = 0
    gekuerzt = False
    for text, _ in zeilen:
        token_anzahl = len(_TOKEN_ENCODER.encode(text))
        if token_summe + token_anzahl > _MAX_KONTEXT_TOKENS:
            gekuerzt = True
            break
        teile.append(text)
        token_summe += token_anzahl

    return "\n\n---\n\n".join(teile), len(dateinamen), gekuerzt


def erzeuge_und_speichere(objekt_name: str) -> None:
    """
    Läuft fehlertolerant wie extraktion.extrahiere_und_speichere: ein
    Fehlschlag hier darf den Upload nicht scheitern lassen, die
    Zusammenfassung ist eine Zusatzfunktion.
    """
    text, anzahl_dokumente, gekuerzt = _text_mit_budget(objekt_name)
    if not text:
        return

    if gekuerzt:
        print(
            f"[Zusammenfassung für '{objekt_name}': Kontext-Budget "
            f"({_MAX_KONTEXT_TOKENS} Tokens) überschritten, Zusammenfassung "
            "basiert nur auf einem Teil der Dokumente]"
        )

    try:
        zusammenfassung = Settings.llm.structured_predict(
            ObjektZusammenfassung, ZUSAMMENFASSUNGS_PROMPT, text=text
        )
    except Exception as fehler:
        print(f"[Zusammenfassung für '{objekt_name}' fehlgeschlagen: {fehler}]")
        return

    try:
        conn = psycopg2.connect(**verbindungsparameter())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {TABELLE}
                        (objekt_name, kurzueberblick, eckdaten, besonderheiten,
                         offene_punkte, anzahl_dokumente, text_gekuerzt)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (objekt_name) DO UPDATE SET
                        kurzueberblick = EXCLUDED.kurzueberblick,
                        eckdaten = EXCLUDED.eckdaten,
                        besonderheiten = EXCLUDED.besonderheiten,
                        offene_punkte = EXCLUDED.offene_punkte,
                        anzahl_dokumente = EXCLUDED.anzahl_dokumente,
                        text_gekuerzt = EXCLUDED.text_gekuerzt,
                        aktualisiert_am = now()
                    """,
                    (
                        objekt_name,
                        zusammenfassung.kurzueberblick,
                        psycopg2.extras.Json(zusammenfassung.eckdaten),
                        psycopg2.extras.Json(zusammenfassung.besonderheiten),
                        psycopg2.extras.Json(zusammenfassung.offene_punkte),
                        anzahl_dokumente,
                        gekuerzt,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as fehler:
        print(f"[Zusammenfassung-Speicherung fehlgeschlagen für '{objekt_name}': {fehler}]")


def zusammenfassung_fuer_objekt(objekt_name: str) -> dict | None:
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT objekt_name, kurzueberblick, eckdaten, besonderheiten, "
                f"offene_punkte, anzahl_dokumente, text_gekuerzt, aktualisiert_am "
                f"FROM {TABELLE} WHERE objekt_name = %s",
                (objekt_name,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            spalten = [
                "objekt_name", "kurzueberblick", "eckdaten", "besonderheiten",
                "offene_punkte", "anzahl_dokumente", "text_gekuerzt", "aktualisiert_am",
            ]
            eintrag = dict(zip(spalten, row))
            eintrag["aktualisiert_am"] = eintrag["aktualisiert_am"].isoformat()
            return eintrag
    finally:
        conn.close()
