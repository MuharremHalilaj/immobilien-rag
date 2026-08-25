"""
PDF-Reader mit OCR-Fallback für eingescannte Objektunterlagen.

Ausgangslage: unser Test-Corpus (data_pdf/) ist mit reportlab erzeugt und
hat eine echte Textebene, daher hat pypdf (über llama_index PDFReader)
dort nie Probleme gemacht. Echte Objektunterlagen sind aber oft
eingescannt oder abfotografiert (Grundbuchauszüge, unterschriebene
Protokolle, alte Energieausweise) -- pypdf liefert für solche Seiten
stillschweigend einen leeren String zurück, kein Fehler. Die Folge: der
Upload meldet "erfolgreich", aber weder die Kennzahlen-Extraktion noch
die Chat-Antworten haben irgendeinen Inhalt zur Verfügung (siehe
docs/testergebnisse.md, Abschnitt zum ersten Test mit echten Daten).

Dieser Reader prüft pro Seite, ob pypdf/pymupdf genug Text liefert. Wenn
nicht, wird die Seite als Bild gerendert und per Tesseract-OCR gelesen.
Erzeugt wie llama_index's PDFReader ein Document pro Seite, damit er als
Drop-in-Ersatz in SimpleDirectoryReader (file_extractor) und beim
manuellen Upload (api.py) funktioniert.
"""

import io
from pathlib import Path

import pymupdf
import pytesseract
from llama_index.core import Document
from llama_index.core.readers.base import BaseReader
from PIL import Image

# Unterhalb dieser Zeichenzahl gilt eine Seite als "kein Text" (Scan) --
# ein paar Kopfzeilen-Reste reichen nicht, um auf OCR zu verzichten.
_MINDEST_ZEICHEN_PRO_SEITE = 20

# 300 DPI ist ein guter Kompromiss zwischen OCR-Genauigkeit und
# Laufzeit/Speicher für die typischerweise DIN-A4-großen Dokumente hier.
_OCR_DPI = 300

# Objektunterlagen sind durchgehend deutschsprachig.
_OCR_SPRACHE = "deu"


class OCRFallbackPDFReader(BaseReader):
    def load_data(
        self, file: Path, extra_info: dict | None = None
    ) -> list[Document]:
        pfad = Path(file)
        pdf = pymupdf.open(str(pfad))
        dokumente = []
        try:
            for seiten_index in range(len(pdf)):
                seite = pdf[seiten_index]
                text = seite.get_text().strip()
                ocr_verwendet = False
                if len(text) < _MINDEST_ZEICHEN_PRO_SEITE:
                    text = self._ocr_seite(seite, pfad, seiten_index)
                    ocr_verwendet = True

                metadata = {
                    "file_name": pfad.name,
                    "page_label": str(seiten_index + 1),
                    "ocr_verwendet": ocr_verwendet,
                }
                if extra_info:
                    metadata.update(extra_info)
                dokumente.append(Document(text=text, metadata=metadata))
        finally:
            pdf.close()
        return dokumente

    def _ocr_seite(self, seite, pfad: Path, seiten_index: int) -> str:
        try:
            pix = seite.get_pixmap(dpi=_OCR_DPI)
            bild = Image.open(io.BytesIO(pix.tobytes("png")))
            return pytesseract.image_to_string(bild, lang=_OCR_SPRACHE)
        except Exception as fehler:
            print(
                f"[OCR fehlgeschlagen für {pfad.name}, Seite "
                f"{seiten_index + 1}: {fehler}]"
            )
            return ""
