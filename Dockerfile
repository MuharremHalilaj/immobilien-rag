FROM python:3.12-slim

WORKDIR /app

# tesseract-ocr + deutsches Sprachpaket für den OCR-Fallback bei
# eingescannten Objektunterlagen (siehe pdf_lader.py) -- ohne dieses
# Systempaket würde pytesseract in Produktion mit "tesseract is not
# installed" fehlschlagen, auch wenn requirements.txt erfüllt ist.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# *.py statt einer festen Dateiliste: neue Root-Module (wie zuletzt
# db.py/protokoll.py/extraktion.py) landen automatisch im Image, ohne
# dass hier jedes Mal eine Zeile ergänzt werden muss -- das genau war
# der Bug, der den Produktions-Deploy stillschweigend zum Absturz
# gebracht hat (ModuleNotFoundError: extraktion), siehe Testlauf-Doku.
COPY *.py ./
COPY frontend/ frontend/
COPY data_pdf/ data_pdf/

ENV NLTK_DISABLE_IMPORT_SECURITY=1

# $PORT wird von Render zur Laufzeit vorgegeben; 8000 als lokaler Fallback
# (z.B. für `docker run -p 8000:8000 ...` ohne Render).
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
