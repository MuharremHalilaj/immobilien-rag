"""
Web-API für den Objektunterlagen-Assistenten.

Dünner FastAPI-Wrapper um main.py: baut den Index einmal beim Start
und beantwortet Fragen über einen HTTP-Endpunkt, statt wie
interaktive_schleife() in main.py über die Konsole. Nutzt dieselbe
beantworte_frage()-Funktion wie die Konsolen-Variante und der
Testkatalog (tests/testfragen.py) — Filterung, Prompt und
Antwortverhalten sind identisch.
"""

import base64
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import extraktion
from pdf_lader import OCRFallbackPDFReader
from main import DATA_DIR, baue_index, beantworte_frage, kennzahlen_backfill, _bekannte_objektnamen

# Wird beim Start einmal befüllt (siehe lifespan unten), damit der Index
# nicht bei jeder Anfrage neu geladen wird.
zustand: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    zustand["index"] = baue_index()
    zustand["bekannte_objekte"] = _bekannte_objektnamen()
    yield


app = FastAPI(title="Objektunterlagen-Assistent", lifespan=lifespan)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Einfacher Zugriffsschutz für die öffentlich erreichbare Deployment-
    Instanz (siehe render.yaml / README, Abschnitt Deployment). Greift
    nur, wenn BASIC_AUTH_USER und BASIC_AUTH_PASSWORD gesetzt sind — im
    lokalen Betrieb (keine dieser Variablen in .env) bleibt die App wie
    bisher ungeschützt erreichbar.
    """

    async def dispatch(self, request, call_next):
        erwarteter_benutzer = os.getenv("BASIC_AUTH_USER")
        erwartetes_passwort = os.getenv("BASIC_AUTH_PASSWORD")
        if not erwarteter_benutzer or not erwartetes_passwort:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                benutzer, passwort = (
                    base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
                )
            except Exception:
                benutzer, passwort = "", ""
            if secrets.compare_digest(
                benutzer, erwarteter_benutzer
            ) and secrets.compare_digest(passwort, erwartetes_passwort):
                return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Objektunterlagen-Assistent"'},
        )


app.add_middleware(BasicAuthMiddleware)


class FrageRequest(BaseModel):
    frage: str

    @field_validator("frage")
    @classmethod
    def frage_nicht_leer(cls, wert: str) -> str:
        """
        Leere/Whitespace-Fragen ohne diesen Check liefen bisher
        ungebremst bis zum Embedding-/LLM-Call durch -- kostet echtes
        Geld für eine sinnlose Anfrage und liefert bedeutungslose
        Quellen mit Score 0.0 zurück. Das Frontend verhindert das zwar
        clientseitig, aber ein direkter API-Call umging das bisher.
        """
        wert = wert.strip()
        if not wert:
            raise ValueError("Frage darf nicht leer sein.")
        return wert


class Quelle(BaseModel):
    dateiname: str
    score: float


class FrageResponse(BaseModel):
    antwort: str
    objekt: str | None
    quellen: list[Quelle]


@app.post("/api/frage")
def frage_stellen(request: FrageRequest) -> FrageResponse:
    antwort, objekt = beantworte_frage(
        zustand["index"], request.frage, zustand["bekannte_objekte"], herkunft="web"
    )
    quellen = [
        Quelle(
            dateiname=node.metadata.get("file_name", "unbekannt"),
            score=node.score or 0.0,
        )
        for node in antwort.source_nodes
    ]
    return FrageResponse(antwort=str(antwort), objekt=objekt, quellen=quellen)


@app.get("/api/objekte")
def objekte_auflisten() -> list[str]:
    return sorted(zustand["bekannte_objekte"])


@app.get("/api/kennzahlen")
def kennzahlen_auflisten() -> list[dict]:
    """
    Strukturiert extrahierte Kennzahlen (Kaufpreis, Wohnfläche, ...) je
    Dokument, siehe extraktion.py -- bewusst pro Quelldokument, nicht
    pro Objekt zusammengeführt, damit Widersprüche zwischen Quellen
    (z.B. abweichende Wohnflächen-Angabe) sichtbar bleiben.
    """
    return extraktion.alle_kennzahlen()


@app.get("/api/kennzahlen/{objekt_name}")
def kennzahlen_fuer_objekt(objekt_name: str) -> list[dict]:
    return extraktion.kennzahlen_fuer_objekt(objekt_name)


@app.post("/api/admin/kennzahlen-backfill")
def kennzahlen_backfill_auslösen() -> dict:
    """
    Holt die Kennzahlen-Extraktion nachträglich für alle Dateien in
    DATA_DIR nach (main.kennzahlen_backfill) -- für den Fall, dass der
    Vektor-Index schon vor Einführung von extraktion.py gebaut wurde
    und die automatische Extraktion beim Start deshalb übersprungen
    wurde. Rührt den Vektor-Index nicht an, kostet daher nur die
    (deutlich günstigeren) Extraktions-LLM-Calls, kein erneutes
    Embedding. Wie alle Routen durch die Basic-Auth-Middleware
    geschützt (siehe BasicAuthMiddleware oben).
    """
    anzahl = kennzahlen_backfill()
    return {"verarbeitete_dateien": anzahl}


def _slug(text: str) -> str:
    """
    Wandelt einen frei eingegebenen Objektnamen in eine dateiname- und
    metadatentaugliche Kurzform um (z.B. "Musterstraße 12" ->
    "musterstrasse-12"). Dieselbe Form wird als objekt_name-Metadatenfeld
    verwendet, damit Metadaten-Filterung (_erkenne_objekt in main.py) und
    Dateiname konsistent bleiben.
    """
    ersetzungen = str.maketrans("äöü", "aou")
    text = text.strip().lower().replace("ß", "ss").translate(ersetzungen)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "objekt"


class UploadErgebnis(BaseModel):
    dateiname: str
    seiten: int = 0
    fehler: str | None = None


class UploadResponse(BaseModel):
    objekt: str
    hochgeladen: list[UploadErgebnis]


def _datei_verarbeiten(
    zielpfad: Path, inhalt: bytes, objekt_slug: str, original_dateiname: str
) -> UploadErgebnis:
    """
    Der eigentlich teure Teil pro Datei (OCR-Fallback beim Lesen,
    Embedding beim Index-Insert, LLM-Aufruf bei der Kennzahlen-
    Extraktion) — synchron, damit er über run_in_threadpool() aus der
    async-Route ausgelagert werden kann (siehe dokumente_hochladen).
    """
    zielpfad.write_bytes(inhalt)

    try:
        seiten = OCRFallbackPDFReader().load_data(zielpfad)
    except Exception:
        zielpfad.unlink(missing_ok=True)
        return UploadErgebnis(
            dateiname=original_dateiname,
            fehler="Datei konnte nicht als PDF gelesen werden (beschädigt oder kein gültiges PDF).",
        )

    for seite in seiten:
        seite.metadata["objekt_name"] = objekt_slug
        seite.metadata["file_name"] = zielpfad.name
        zustand["index"].insert(seite)

    voller_text = "\n".join(seite.text for seite in seiten)
    extraktion.extrahiere_und_speichere(objekt_slug, zielpfad.name, voller_text)

    return UploadErgebnis(dateiname=zielpfad.name, seiten=len(seiten))


@app.post("/api/upload")
async def dokumente_hochladen(
    objekt_name: str = Form(...), dateien: list[UploadFile] = File(...)
) -> UploadResponse:
    """
    Nimmt mehrere PDFs für ein Objekt entgegen, speichert sie in
    DATA_DIR (wie der bestehende Corpus) und fügt sie inkrementell in
    den laufenden Index ein (index.insert), statt den kompletten Index
    neu zu bauen. Der Objektname wird zu einem Slug normalisiert
    (siehe _slug) und sowohl im Dateinamen als auch im
    objekt_name-Metadatenfeld verwendet, damit ein späterer kompletter
    Neuaufbau (SimpleDirectoryReader + _objekt_metadata in main.py) den
    gleichen Objektnamen wieder erkennt.

    Eine defekte/keine-echte-PDF-Datei bricht nicht den ganzen Batch ab
    (500 Internal Server Error): PDFReader wirft dafür einen
    unabgefangenen Fehler, der vorher den gesamten Request zum Absturz
    brachte, auch wenn andere Dateien im selben Batch gültig waren.
    Stattdessen wird die fehlerhafte Datei einzeln als Fehler im
    Ergebnis markiert (fehler-Feld), die übrigen Dateien werden normal
    verarbeitet, und die bereits geschriebene Datei wird wieder
    gelöscht statt als Datenmüll liegen zu bleiben.

    Die eigentliche Verarbeitung pro Datei läuft über run_in_threadpool
    in einem Worker-Thread, nicht direkt im async-Handler: OCR
    (pytesseract) ist reine, blockierende CPU-Arbeit, die bei einem
    direkten Aufruf hier die komplette Event-Loop anhält -- damit wäre
    der Server für ALLE Nutzer eingefroren, solange nur eine einzige
    eingescannte Datei verarbeitet wird, nicht nur für den Hochladenden
    (beobachtet beim ersten Produktions-Upload eines eingescannten
    Energieausweises: der komplette Dienst wurde für die Dauer der
    OCR-Verarbeitung unerreichbar, siehe docs/testergebnisse.md).
    """
    objekt_slug = _slug(objekt_name)
    ergebnisse = []
    mindestens_eine_erfolgreich = False

    for datei in dateien:
        original_name = Path(datei.filename or "dokument.pdf").stem
        original_name = re.sub(r"[^a-zA-Z0-9-]+", "-", original_name).strip("-")
        zielpfad = Path(DATA_DIR) / f"hochgeladen_{objekt_slug}_{original_name}.pdf"
        if zielpfad.exists():
            zielpfad = zielpfad.with_stem(f"{zielpfad.stem}_{int(time.time() * 1000)}")

        inhalt = await datei.read()
        ergebnis = await run_in_threadpool(
            _datei_verarbeiten,
            zielpfad,
            inhalt,
            objekt_slug,
            datei.filename or "unbekannt",
        )
        ergebnisse.append(ergebnis)
        if ergebnis.fehler is None:
            mindestens_eine_erfolgreich = True

    if mindestens_eine_erfolgreich and objekt_slug not in zustand["bekannte_objekte"]:
        zustand["bekannte_objekte"].append(objekt_slug)

    return UploadResponse(objekt=objekt_slug, hochgeladen=ergebnisse)


# Frontend als statische Dateien ausliefern (index.html unter "/").
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
