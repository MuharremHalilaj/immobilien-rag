"""
Lädt Objektunterlagen aus einem lokalen Ordner direkt in eine
Postgres-Datenbank -- ohne über den Web-Server zu laufen.

Grund: OCR (Tesseract) ist speicherhungrig und hat den Render-
Produktions-Container (512 MB RAM) zum Absturz gebracht (siehe
docs/testergebnisse.md). Läuft OCR stattdessen hier lokal, wird nur der
eigene Rechner belastet -- übertragen werden am Ende nur die fertigen
Texte und Embedding-Vektoren per Datenbankverbindung, was für die
Datenbank selbst eine leichte Operation ist (siehe Chat-Erklärung dazu).

Nutzung:
    python scripts/lokal_ingest.py --objekt "Musterstraße 12" \
        --verzeichnis data_pdf_real/musterstrasse-12

    python scripts/lokal_ingest.py --objekt "Musterstraße 12" \
        --verzeichnis data_pdf_real/musterstrasse-12 --ziel prod

--ziel prod erwartet eine Datei .env.prod im Projektwurzelverzeichnis
mit den POSTGRES_*-Zugangsdaten der Produktionsdatenbank (siehe Render-
Dashboard -> Datenbank -> "External Connection", Format wie
.env.example). Diese Datei ist bewusst NICHT eingecheckt (.gitignore).
"""

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Projektwurzel statt scripts/ in sys.path[0] -- beim Aufruf per
# "python scripts/lokal_ingest.py" landet sonst nur der scripts/-Ordner
# im Pfad und "import main" (main.py liegt im Projektwurzelverzeichnis)
# schlägt fehl.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _slug(text: str) -> str:
    """Gleiche Normalisierung wie _slug() in api.py."""
    ersetzungen = str.maketrans("äöü", "aou")
    text = text.strip().lower().replace("ß", "ss").translate(ersetzungen)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "objekt"


def main_ausfuehren() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objekt", required=True, help="Objektname (wird zu einem Slug normalisiert)")
    parser.add_argument("--verzeichnis", required=True, type=Path, help="Ordner mit den PDF-Dateien des Objekts")
    parser.add_argument("--ziel", choices=["lokal", "prod"], default="lokal")
    args = parser.parse_args()

    if not args.verzeichnis.is_dir():
        sys.exit(f"Verzeichnis nicht gefunden: {args.verzeichnis}")

    pdf_dateien = sorted(args.verzeichnis.glob("*.pdf"))
    if not pdf_dateien:
        sys.exit(f"Keine PDF-Dateien in {args.verzeichnis} gefunden.")

    if args.ziel == "prod":
        prod_env = Path(".env.prod")
        if not prod_env.is_file():
            sys.exit(
                ".env.prod nicht gefunden. Lege sie mit den Produktions-"
                "Zugangsdaten an (Render-Dashboard -> Datenbank -> "
                "'External Connection'), Format wie .env.example."
            )
        # Muss VOR dem main-Import und mit override=True passieren: main.py
        # ruft selbst load_dotenv() ohne override auf, das lässt bereits
        # gesetzte Variablen unangetastet -- ohne diese Reihenfolge würden
        # die Produktions-Zugangsdaten hier also gar nicht wirksam.
        load_dotenv(dotenv_path=prod_env, override=True)
        ziel_beschreibung = "PRODUKTIONS-Datenbank"
    else:
        ziel_beschreibung = "lokale Datenbank"

    print(f"Ziel: {ziel_beschreibung} -- {len(pdf_dateien)} Datei(en) für '{args.objekt}':")
    for pfad in pdf_dateien:
        print(f"  - {pfad.name}")

    if input("Fortfahren? [j/N] ").strip().lower() != "j":
        sys.exit("Abgebrochen.")

    # Import erst hier, nach load_dotenv(--ziel prod): main.py liest beim
    # eigenen Import die POSTGRES_*-Umgebungsvariablen aus.
    import main
    import zusammenfassung
    from llama_index.core import VectorStoreIndex
    from pdf_lader import OCRFallbackPDFReader

    objekt_slug = _slug(args.objekt)

    main.protokoll.sicherstelle_tabelle()
    main.extraktion.sicherstelle_tabelle()
    zusammenfassung.sicherstelle_tabelle()

    vector_store = main._baue_pgvector_store()
    index = VectorStoreIndex.from_vector_store(vector_store)

    for pdf_pfad in pdf_dateien:
        print(f"Verarbeite {pdf_pfad.name} ...")
        try:
            seiten = OCRFallbackPDFReader().load_data(pdf_pfad)
        except Exception as fehler:
            print(f"  Übersprungen (kein gültiges PDF): {fehler}")
            continue

        for seite in seiten:
            seite.metadata["objekt_name"] = objekt_slug
            seite.metadata["file_name"] = pdf_pfad.name
            index.insert(seite)

        voller_text = "\n".join(seite.text for seite in seiten)
        main.extraktion.extrahiere_und_speichere(objekt_slug, pdf_pfad.name, voller_text)

    print(f"Erzeuge Objekt-Zusammenfassung für '{objekt_slug}' ...")
    zusammenfassung.erzeuge_und_speichere(objekt_slug)

    print(f"Fertig: '{objekt_slug}' mit {len(pdf_dateien)} Datei(en) in die {ziel_beschreibung} geladen.")


if __name__ == "__main__":
    main_ausfuehren()
