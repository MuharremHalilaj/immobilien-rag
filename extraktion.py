"""
Strukturierte Kennzahlen-Extraktion aus Objektunterlagen -- ergänzt die
freitext-basierte RAG-Suche um klassisch durchsuchbare/aggregierbare
Felder (Kaufpreis, Wohnfläche, ...). Läuft einmalig pro Dokument bei
dessen Einlesen (main.py: baue_index / api.py: Upload), nicht pro
Anfrage -- analog zum Chunking/Embedding.

Bewusst pro Dokument statt pro Objekt zusammengeführt: Exposé und
Energieausweis nennen z.B. teils leicht abweichende Wohnflächen (siehe
docs/testergebnisse.md). Ein zusammengeführter Datensatz würde diesen
Widerspruch stillschweigend auflösen -- widerspricht der Grundidee
dieses Projekts, Widersprüche zwischen Quellen sichtbar zu halten.
"""

import re

from pydantic import BaseModel, Field
import psycopg2

from llama_index.core import PromptTemplate, Settings

from db import verbindungsparameter

TABELLE = "objekt_kennzahlen"

EXTRAKTIONS_PROMPT = PromptTemplate(
    "Extrahiere die folgenden Kennzahlen aus diesem Auszug einer "
    "Immobilien-Objektunterlage. Übernimm einen Wert NUR, wenn er im "
    "Text unmittelbar neben einer eindeutigen Beschriftung steht, die "
    "genau dieses Feld benennt (z.B. 'Baujahr: 1985', 'Kaufpreis: "
    "310.000 EUR'). Lasse ein Feld leer (null), wenn kein so "
    "beschrifteter Wert im Text steht -- rate nicht, leite nichts aus "
    "dem Kontext ab und übernimm keine Werte aus anderen Objekten.\n\n"
    "Immobilien-Objektunterlagen enthalten viele Zahlen, die wie ein "
    "gesuchter Wert AUSSEHEN, es aber nicht sind: Urkundenrollennummern "
    "(z.B. 'UR-Nr. 884/1998'), Grundbuchblatt-Bezeichnungen und "
    "-Nummern, Aktenzeichen, Beurkundungs-/Änderungs-/Freigabedaten, "
    "Rechnungs- oder Auftragsnummern, Kontostände oder Auftragssummen "
    "aus Handwerker-/Dienstleistungsrechnungen. Eine vierstellige Zahl "
    "ist nur dann ein Baujahr, ein Geldbetrag nur dann ein Kaufpreis, "
    "wenn der Text das Wort 'Baujahr'/'erbaut'/'errichtet' bzw. "
    "'Kaufpreis'/'Kaufsumme' direkt davor oder danach verwendet -- "
    "niemals aus der Nähe zu einem Datum, einer Nummer oder einem "
    "anderen Betrag im selben Absatz ableiten.\n\n{text}"
)


class ObjektKennzahlen(BaseModel):
    kaufpreis_eur: float | None = Field(None, description="Kaufpreis in Euro")
    wohnflaeche_qm: float | None = Field(None, description="Wohnfläche in Quadratmetern")
    zimmer: float | None = Field(None, description="Anzahl Zimmer")
    baujahr: int | None = Field(None, description="Baujahr des Gebäudes")
    energieeffizienzklasse: str | None = Field(
        None, description="Energieeffizienzklasse, z.B. 'A+', 'B', 'D'"
    )
    hausgeld_eur_monatlich: float | None = Field(None, description="Monatliches Hausgeld in Euro")
    etage: str | None = Field(None, description="Stockwerk/Etage der Einheit")


def sicherstelle_tabelle() -> None:
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABELLE} (
                    id SERIAL PRIMARY KEY,
                    objekt_name TEXT NOT NULL,
                    dateiname TEXT NOT NULL UNIQUE,
                    kaufpreis_eur NUMERIC,
                    wohnflaeche_qm NUMERIC,
                    zimmer NUMERIC,
                    baujahr INT,
                    energieeffizienzklasse TEXT,
                    hausgeld_eur_monatlich NUMERIC,
                    etage TEXT,
                    extrahiert_am TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


# Felder mit einfacher Präsenzprüfung: der Wert muss irgendwo im Text
# als Ziffernfolge auftauchen. Reicht für Felder, bei denen wir in der
# Praxis keine Verwechslungsfälle gesehen haben.
_PRUEFBARE_ZAHLENFELDER = {"wohnflaeche_qm", "zimmer", "hausgeld_eur_monatlich"}

# Felder mit Label-Pflicht: hier reicht "Zahl kommt im Dokument vor"
# nicht -- Objektunterlagen sind voll von Zahlen, die wie eine
# Jahreszahl oder ein Geldbetrag aussehen, es aber nicht sind
# (Urkundenrollennummern, Grundbuchblatt-Bezeichnungen,
# Beurkundungsdaten, Auftragssummen, ...). Erst bestätigt, wenn eines
# der Label-Wörter in der Nähe der Zahl im Text steht. Deckt sowohl
# Verwechslungen (Zahl kommt vor, aber falsch interpretiert) als auch
# reine Erfindungen (Zahl kommt gar nicht vor) ab -- beide Fälle traten
# beim ersten Test mit echten Objektunterlagen auf, siehe
# docs/testergebnisse.md.
_LABELWOERTER = {
    "baujahr": ("baujahr", "erbaut", "errichtet"),
    "kaufpreis_eur": ("kaufpreis", "kaufsumme", "gesamtkaufpreis"),
}
# 80 statt z.B. 50 Zeichen, weil OCR bei tabellarischen Energieausweisen
# die Lesereihenfolge von Wert und Label durcheinanderbringen kann
# (siehe docs/testergebnisse.md, Mainz-Energieausweis: 57 Zeichen
# Abstand zwischen "1980" und "Baujahr Gebäude").
_LABEL_FENSTER = 80  # Zeichen vor/nach dem Zahlwert, in denen ein Label stehen muss


def _kommt_im_text_vor(wert: float | int, text: str) -> bool:
    ziffern_wert = str(int(wert))
    ziffern_text = re.sub(r"[^0-9]", "", text)
    return ziffern_wert in ziffern_text


def _text_positionen(ziffern_wert: str, text: str) -> list[int]:
    """
    Fundstellen von ziffern_wert im Originaltext -- sowohl als
    zusammenhängende Ziffernfolge als auch mit deutschen
    Tausenderpunkten alle drei Stellen (z.B. "319458" -> auch
    "319.458"), da Layout/OCR Tausendertrennzeichen unterschiedlich
    setzen.
    """
    kandidaten = {ziffern_wert}
    if len(ziffern_wert) > 3:
        gruppen, rest = [], ziffern_wert
        while len(rest) > 3:
            gruppen.insert(0, rest[-3:])
            rest = rest[:-3]
        gruppen.insert(0, rest)
        kandidaten.add(".".join(gruppen))

    positionen = []
    for kandidat in kandidaten:
        start = 0
        while (idx := text.find(kandidat, start)) != -1:
            positionen.append(idx)
            start = idx + 1
    return positionen


def _mit_label_belegt(wert: float | int, feld: str, text: str) -> bool:
    ziffern_wert = str(int(wert))
    text_klein = text.lower()
    for idx in _text_positionen(ziffern_wert, text):
        fenster = text_klein[
            max(0, idx - _LABEL_FENSTER) : idx + len(ziffern_wert) + _LABEL_FENSTER
        ]
        if any(label in fenster for label in _LABELWOERTER[feld]):
            return True
    return False


def _gegen_halluzination_absichern(
    kennzahlen: "ObjektKennzahlen", dateiname: str, text: str
) -> "ObjektKennzahlen":
    for feld in _PRUEFBARE_ZAHLENFELDER:
        wert = getattr(kennzahlen, feld)
        if wert is not None and not _kommt_im_text_vor(wert, text):
            print(
                f"[Kennzahlen-Absicherung: '{feld}'={wert} für "
                f"{dateiname} kommt im Quelltext nicht vor -- verworfen]"
            )
            setattr(kennzahlen, feld, None)

    for feld in _LABELWOERTER:
        wert = getattr(kennzahlen, feld)
        if wert is not None and not _mit_label_belegt(wert, feld, text):
            print(
                f"[Kennzahlen-Absicherung: '{feld}'={wert} für "
                f"{dateiname} steht in keiner Nähe zu "
                f"{_LABELWOERTER[feld]} -- verworfen]"
            )
            setattr(kennzahlen, feld, None)

    return kennzahlen


def extrahiere_und_speichere(objekt_name: str, dateiname: str, text: str) -> None:
    """
    Läuft fehlertolerant: ein Problem bei der Extraktion (z.B. Timeout,
    unerwartetes LLM-Format) darf nie den Ingestion-Vorgang abbrechen --
    die Kennzahlen sind eine Zusatzfunktion, kein kritischer Pfad wie
    Chunking/Embedding.
    """
    try:
        kennzahlen = Settings.llm.structured_predict(
            ObjektKennzahlen, EXTRAKTIONS_PROMPT, text=text
        )
        kennzahlen = _gegen_halluzination_absichern(kennzahlen, dateiname, text)
    except Exception as fehler:
        print(f"[Kennzahlen-Extraktion fehlgeschlagen für {dateiname}: {fehler}]")
        return

    try:
        conn = psycopg2.connect(**verbindungsparameter())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {TABELLE}
                        (objekt_name, dateiname, kaufpreis_eur, wohnflaeche_qm,
                         zimmer, baujahr, energieeffizienzklasse,
                         hausgeld_eur_monatlich, etage)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dateiname) DO UPDATE SET
                        objekt_name = EXCLUDED.objekt_name,
                        kaufpreis_eur = EXCLUDED.kaufpreis_eur,
                        wohnflaeche_qm = EXCLUDED.wohnflaeche_qm,
                        zimmer = EXCLUDED.zimmer,
                        baujahr = EXCLUDED.baujahr,
                        energieeffizienzklasse = EXCLUDED.energieeffizienzklasse,
                        hausgeld_eur_monatlich = EXCLUDED.hausgeld_eur_monatlich,
                        etage = EXCLUDED.etage,
                        extrahiert_am = now()
                    """,
                    (
                        objekt_name,
                        dateiname,
                        kennzahlen.kaufpreis_eur,
                        kennzahlen.wohnflaeche_qm,
                        kennzahlen.zimmer,
                        kennzahlen.baujahr,
                        kennzahlen.energieeffizienzklasse,
                        kennzahlen.hausgeld_eur_monatlich,
                        kennzahlen.etage,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as fehler:
        print(f"[Kennzahlen-Speicherung fehlgeschlagen für {dateiname}: {fehler}]")


_SPALTEN = [
    "objekt_name",
    "dateiname",
    "kaufpreis_eur",
    "wohnflaeche_qm",
    "zimmer",
    "baujahr",
    "energieeffizienzklasse",
    "hausgeld_eur_monatlich",
    "etage",
]

# NUMERIC-Spalten kommen von psycopg2 als decimal.Decimal zurück und
# werden von FastAPI dadurch als String statt als Zahl serialisiert
# (z.B. "112.0" statt 112.0) -- im Frontend sah man dadurch hässliche
# Werte wie "112.0 m²" statt "112 m²". Explizite float()-Konvertierung
# hier behebt das an der Quelle, ohne das Frontend anzufassen.
_NUMERISCHE_FELDER = {"kaufpreis_eur", "wohnflaeche_qm", "zimmer", "hausgeld_eur_monatlich"}


def _zeile_aufbereiten(row: tuple) -> dict:
    eintrag = dict(zip(_SPALTEN, row))
    for feld in _NUMERISCHE_FELDER:
        if eintrag[feld] is not None:
            eintrag[feld] = float(eintrag[feld])
    return eintrag


def kennzahlen_fuer_objekt(objekt_name: str) -> list[dict]:
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_SPALTEN)} FROM {TABELLE} "
                "WHERE objekt_name = %s ORDER BY dateiname",
                (objekt_name,),
            )
            return [_zeile_aufbereiten(row) for row in cur.fetchall()]
    finally:
        conn.close()


def alle_kennzahlen() -> list[dict]:
    conn = psycopg2.connect(**verbindungsparameter())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_SPALTEN)} FROM {TABELLE} "
                "ORDER BY objekt_name, dateiname"
            )
            return [_zeile_aufbereiten(row) for row in cur.fetchall()]
    finally:
        conn.close()
