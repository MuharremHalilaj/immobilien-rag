# Testergebnisse — Objektunterlagen-Assistent

Dieses Dokument protokolliert Testläufe des RAG-Systems mit Zeitstempel,
damit sich ein Ergebnis-Snapshot später einem bestimmten Code-Stand
zuordnen lässt. Quelle der Fragen: `tests/testfragen.py`. Neue Läufe
werden **oben** als neuer Abschnitt ergänzt, ältere Läufe bleiben
vollständig erhalten (kein Überschreiben/Löschen). Ab diesem Lauf wird
jeder Testfall mit vollständiger Frage, Erwartung, Antwort, Quellen und
Richter-Begründung dokumentiert, nicht nur als Verweis auf frühere
Einträge.

---

## Lauf vom 2026-08-26 (Fortsetzung) — Produktions-Upload der 3 echten Objekte: Render Free Tier zu ressourcenschwach für OCR

**Git-Commit:** `39b6d70` ("Upload-Endpunkt: OCR blockiert nicht mehr den
ganzen Server, DPI gesenkt").

**Ziel:** Die 3 lokal bereits verifizierten echten Objekte (siehe Lauf
oben) über denselben Weg wie ein echter Nutzer-Upload — also über die
Produktions-Weboberfläche selbst, nicht per Direktzugriff auf die
Datenbank — nach Render laden, damit exakt der Pfad getestet wird, der
später für Bonorum/Vertriebspartner-Uploads genutzt würde.

**Vorab gefundener und behobener Bug:** Der Upload-Endpunkt
(`api.py: dokumente_hochladen`) rief die OCR-Verarbeitung
(`pytesseract`, reine CPU-Arbeit) bisher direkt und blockierend
innerhalb der `async def`-Route auf. Das hält die komplette
uvicorn-Event-Loop an, solange eine Datei OCR braucht — nicht nur für
den Hochladenden, sondern für **alle** gleichzeitigen Nutzer der Seite.
Fix: die Verarbeitung pro Datei läuft jetzt über
`starlette.concurrency.run_in_threadpool` in einem Worker-Thread.
Zusätzlich OCR-Auflösung von 300 auf 200 DPI gesenkt (an der
Aschaffenburg-Energieausweis-Testseite verifiziert: "Baujahr Gebäude
1950" bleibt bei 200 DPI zuverlässig lesbar), um die Rechenlast pro
Seite zu senken.

**Test 1 — Threadpool-Fix funktioniert:** Ein Upload der 6-seitigen,
komplett gescannten Energieausweis-Datei (Objekt Aschaffenburg) wurde
im Hintergrund gestartet; parallel dazu wurde `/api/objekte` alle 8
Sekunden abgefragt. Ergebnis: durchgehend `HTTP 200`, der Server blieb
für andere Anfragen voll erreichbar, während die OCR-Verarbeitung lief
— der Fix wirkt wie beabsichtigt.

**Test 1 — aber trotzdem gescheitert:** Der Upload selbst kam nach 280
Sekunden nicht durch (Client-Timeout, `curl` Exit-Code 28). Ein erneuter
Versuch, den Fortschritt zu prüfen, führte kurz danach zu einem
kompletten Ausfall des Dienstes: **~9 Minuten durchgehend `HTTP 000`**
auf jede Anfrage, deutlich länger und ohne die Selbstheilung des ersten
Einfrierens vor dem Threadpool-Fix (das erholte sich nach ~90s). Nach
einer Pause war der Dienst von selbst wieder erreichbar (`HTTP 200`),
aber ohne dass die Datei verarbeitet wurde — `/api/kennzahlen/
aschaffenburg-stadelmannstrasse-15` blieb leer (`[]`), das Objekt wurde
nicht einmal in `bekannte_objekte` aufgenommen. Kein Datenmüll, aber
auch kein Ergebnis.

**Einschätzung:** Der Threadpool-Fix behebt das reine
Nebenläufigkeits-Problem (Server nicht mehr für alle blockiert), löst
aber nicht das eigentliche Ressourcenproblem — der Render-Free-Tier-
Container (typ. 512 MB RAM, stark gedrosselte CPU) scheint beim
mehrseitigen OCR-Rendern (pymupdf-Pixmaps + pytesseract) an seine
Speicher-/Rechengrenze zu stoßen und abzustürzen. Auf zwei
unabhängigen Versuchen beobachtet, kein Einzelfall.

**Entscheidung (mit User):** Kein weiteres Erzwingen über den
Live-Upload-Endpunkt heute. Zwei Optionen für morgen besprochen, noch
nicht entschieden:
1. Die bereits lokal fertig verifizierten Daten (Text, Embeddings,
   Kennzahlen — siehe Lauf oben) direkt in die Produktions-Postgres-DB
   einspielen, ohne den Live-Server mit OCR zu belasten (braucht
   Render-Postgres-Zugangsdaten vom User).
2. Render-Plan-Upgrade (z.B. Starter-Tier) für dauerhaft belastbare
   OCR-Uploads über die UI — relevant unabhängig von Option 1, da
   Bonorum/Vertriebspartner künftig selbst PDFs hochladen sollen.

**Offener Punkt für morgen:** Produktions-Upload der 3 echten Objekte
noch nicht abgeschlossen. Code-Fixes (OCR, Halluzinations-Absicherung,
Objekterkennung, Retrieval-Breite, Threadpool) sind gepusht und in
Produktion aktiv, nur die eigentlichen Objektdaten fehlen dort noch.

---

## Lauf vom 2026-08-26 — Erster Test mit echten Objektunterlagen: OCR, Halluzinations-Absicherung, Objekterkennung und Retrieval-Breite gefixt

**Git-Commit:** `75e91a4` ("OCR-Fallback und Fixes für echte Objektunterlagen
(Halluzination, Objekterkennung, Retrieval)").

**Auslöser:** Erster Test des Systems mit echten (nicht selbst erzeugten)
Objektunterlagen zu 3 realen Objekten (Aschaffenburg Stadelmannstraße 15,
Mainz Münsterstraße "AMC1", Bickenbach Ringstraße 43-45; 40 PDF-Dateien,
285 Seiten) über die Produktions-Oberfläche. Ergebnis: Upload lief ohne
Fehler durch, aber weder das Kennzahlen-Dashboard noch der Chatbot
hatten irgendeine Information zu den neuen Objekten. Vier voneinander
unabhängige Ursachen gefunden und behoben:

### 1. Kein OCR-Fallback — 14 von 40 echten Dateien waren reine Scans

`pypdf` (über `PDFReader`) liefert für eingescannte Seiten ohne
Textebene stillschweigend einen leeren String statt eines Fehlers — u.a.
betroffen: beide Energieausweise, die Teilungserklärung AMC1,
Abgeschlossenheitsbescheinigungen. Fix: neuer `pdf_lader.py` mit
`OCRFallbackPDFReader` (pymupdf zum Rendern, pytesseract mit deutschem
Sprachpaket für OCR), greift automatisch pro Seite, wenn `pypdf`/pymupdf
weniger als 20 Zeichen extrahiert. Ersetzt `PDFReader` sowohl in
`api.py` (Upload) als auch in `main.py` (`SimpleDirectoryReader`
`file_extractor`). `requirements.txt` (`pymupdf`, `pytesseract`) und
`Dockerfile` (`tesseract-ocr`, `tesseract-ocr-deu` per `apt-get`) für
Produktion ergänzt.

### 2. Kennzahlen-Extraktion halluzinierte Werte, die im Text gar nicht vorkommen

Direkter Textabgleich zeigte zwei Fehlerbilder: (a) reine Erfindung —
z.B. `kaufpreis_eur=319458.03` aus einer Auftragsbestätigung, obwohl die
Zahl an keiner Stelle im Dokument steht; (b) Verwechslung — z.B.
`baujahr=1955` aus "Darmstadt Bickenbach **1955**" (einer
Grundbuchblatt-Bezeichnung) oder `baujahr=1981` aus "Nr. 01 der
Urkundenrolle für **1981**" — dasselbe Muster wie der früher dokumentierte
Sonnenblick-Bug, nur dass der damalige Prompt-Fix zu eng gefasst war
(nur "Urkundenrollennummer" explizit ausgeschlossen). Fix, zweistufig:
- `EXTRAKTIONS_PROMPT` (`extraktion.py`) verlangt jetzt allgemein eine
  eindeutige Beschriftung direkt neben dem Wert, nicht nur den
  Ausschluss eines einzelnen bekannten Verwechslungsmusters.
- Zusätzliches Code-Sicherheitsnetz `_gegen_halluzination_absichern()`:
  für `baujahr`/`kaufpreis_eur` muss der Wert im Text nachweislich in
  einem 80-Zeichen-Fenster neben einem Label-Wort stehen
  (`baujahr`/`erbaut`/`errichtet` bzw.
  `kaufpreis`/`kaufsumme`/`gesamtkaufpreis`), sonst wird das Feld auf
  `null` zurückgesetzt und der Vorgang geloggt. Das 80-Zeichen-Fenster
  (statt z.B. 50) wurde nötig, weil OCR bei tabellarischen
  Energieausweisen Wert und Label in vertauschter Reihenfolge liest
  (gemessener Abstand: 57 Zeichen zwischen "1980" und "Baujahr
  Gebäude" im Mainz-Energieausweis). Ergebnis nach Fix: alle 3 real
  ermittelten Baujahr-Werte (1950 Aschaffenburg, 1970 Bickenbach, 1980
  Mainz) stehen nachweislich direkt neben "Baujahr Gebäude" im
  jeweiligen Energieausweis; kein einziger Kaufpreis mehr im Datensatz
  (keine der 40 echten Dateien nennt tatsächlich einen Kaufpreis).

### 3. Objekterkennung (`_erkenne_objekt`) verlangte den kompletten Slug wortwörtlich

Funktionierte nur für einwortige synthetische Objektnamen
("sonnenblick"). Bei mehrteiligen echten Adress-Slugs
("aschaffenburg-stadelmannstrasse-15") matcht praktisch keine natürlich
formulierte Frage den kompletten Bindestrich-Slug — Fragen zu echten
Objekten fielen auf die ungefilterte Suche über den gesamten Corpus
zurück. Fix: `_erkenne_objekt` prüft jetzt pro Objekt-Slug seine
einzelnen Bestandteile (Split an "-") und verlangt, dass mindestens
alle bis auf einen Bestandteil in der (gleich normalisierten: ß→ss,
äöü→aou) Frage vorkommen — deckt z.B. eine ausgelassene Hausnummer ab.
Beide Seiten (Frage und Objekt-Bestandteile) müssen dieselbe
Normalisierung durchlaufen; ein erster Versuch, nur die Frage zu
normalisieren, brach die Erkennung des synthetischen Objekts
"ahornhöhe" (Testfall 13), weil "ahornhöhe" (roh, mit ö) dann nicht mehr
gegen die normalisierte Frage ("ahornhohe") matchte — im selben Schritt
korrigiert.

### 4. `SIMILARITY_TOP_K=12` reichte bei echten Objekten mit vielen Dokumenten nicht

12 war spezifisch für den synthetischen Corpus kalibriert (8 Objekte, je
~5 Chunks). Echte Objekte haben deutlich mehr Dokumente und Chunks (57
für Bickenbach, 107 für Aschaffenburg, 188 für Mainz) — und wiederholen
dieselbe Adresse in fast jedem Chunk, wodurch sich einzelne Fakten
("Baujahr 1950") per reiner Embedding-Ähnlichkeit kaum von der Masse
abheben. Gemessen: der richtige Baujahr-Chunk für Aschaffenburg landete
bei `similarity_top_k=12` gar nicht und selbst bei 50 erst auf Rang 42
von 107. Fix: bei Fragen mit erkanntem Objektfilter jetzt
`similarity_top_k=60` plus verpflichtendes `LLMRerank` auf die 8
relevantesten Chunks (`SIMILARITY_TOP_K_OBJEKT_GEFILTERT`,
`RERANK_TOP_N_OBJEKT_GEFILTERT` in `main.py`) — gemessen: hebt den
richtigen Chunk zuverlässig auf Rang 1. Betrifft ausdrücklich nur den
gefilterten Einzelobjekt-Fall; die ungefilterte, objektübergreifende
Suche bleibt unverändert bei `SIMILARITY_TOP_K=12` (siehe Lauf vom
2026-08-06 unten — dort separat gemessen, dass Reranking dem
ungefilterten Fall schadet).

**Zusätzlich beim Fix entdeckt:** Der QA-Prompt für Chat-Antworten
(nicht nur die strukturierte Extraktion) unterlag demselben
Verwechslungsmuster — eine Testfrage nach dem Baujahr Aschaffenburg
erzeugte einen erfundenen "Widerspruch" zwischen dem echten Baujahr
(1950) und einer Bescheinigungsnummer ("536/1948 vom 09.10.1948") aus
der Teilungserklärung. `QA_PROMPT` in `main.py` um denselben
Verwechslungs-Hinweis ergänzt (Urkundenrollennummern,
Bescheinigungsnummern, Grundbuchblatt-Bezeichnungen,
Beurkundungsdaten sind keine abweichenden Werte zum selben
Sachverhalt).

### Verifikation

Nach allen Fixes: Kennzahlen-Dashboard zeigt für alle 3 echten Objekte
plausible, textlich belegte Werte; folgende Chat-Fragen (direkt gegen
`beantworte_frage()`, herkunft="test") liefern korrekte, belegte
Antworten:

- *"Welches Baujahr hat das Gebäude in der Stadelmannstraße 15 in
  Aschaffenburg?"* → 1950 (Energieausweis), mit korrekter Erkennung,
  dass die Bescheinigungsnummer 536/1948 **kein** abweichendes Baujahr
  ist.
- *"Welches Baujahr hat das Objekt Ringstraße 43-45 in Bickenbach?"* →
  1970 (Energieausweis).
- *"Wie hoch ist das Hausgeld für die Wohnung in der Münsterstraße in
  Mainz (AMC1)?"* → 402,00 €/Monat (Vorauszahlung), mit Aufschlüsselung
  aus der Hausgeldabrechnung 2024.

Vollständiger Testkatalog (`tests/testfragen.py`) danach erneut
gelaufen: **12/13 bestanden.** Einziger Fehlschlag: Testfall 7
(objektübergreifender Vergleich aller 8 synthetischen
Energieausweise) — Ursache ist **kein Code-Bug**, sondern dass die 3
echten Objekte testweise in dieselbe lokale Datenbank wie der
synthetische Demo-Corpus geladen wurden. Die viel größeren echten
Objekte (57-188 Chunks) verdrängen die synthetischen (je 5 Chunks) in
der ungefilterten, für 8 kleine Objekte kalibrierten Cross-Objekt-Suche.
Bestätigt die bereits zuvor gegebene Empfehlung, echte Objektdaten in
Produktion getrennt vom Demo-Corpus zu halten (separate Tabelle o.ä.),
statt beides zu vermischen — noch nicht umgesetzt, offener
Folgeschritt.

**Nebenbefund (keine Code-Änderung):** Beim erneuten Extrahieren fiel
`wohnflaeche_qm`/`zimmer` für eine Datei zwischen zwei Läufen
unterschiedlich aus (mal gefüllt, mal leer) — bekanntes
LLM-Nichtdeterminismus-Phänomen (Temperatur nicht auf 0 gepinnt, siehe
bereits dokumentierte Testfall-12-Flakiness), keine neue Ursache.

---

## Lauf vom 2026-08-06 06:38 (CEST) — Reranking objektiv gemessen: hilft hier nicht, bleibt standardmäßig aus

**Git-Commit:** `c187ea801ffaabe620fe9a954575ba938ca586c5` ("Retrieval-Qualität:
LLM-Reranking implementiert und gemessen, standardmäßig deaktiviert")

**Ziel dieses Laufs:** Retrieval-Qualität systematisch verbessern
(Hybrid-Suche oder Reranking) und mit dem bestehenden LLM-Richter-
Testkatalog **objektiv messen**, ob es tatsächlich hilft — nicht blind
aktivieren. Hybrid-Suche hätte einen kompletten Neuaufbau der
Vektor-Tabelle (inkl. Produktionsinstanz auf Render) erfordert, daher
zunächst **LLM-Reranking** umgesetzt: `SIMILARITY_TOP_K=12` Chunks wie
bisher per Vektor-Ähnlichkeit holen, danach zusätzlich per
`LLMRerank`-Postprocessor (LlamaIndex-Bordmittel, nutzt das bestehende
LLM, keine neue Abhängigkeit) auf `RERANK_TOP_N=5` die relevantesten
eindampfen. Umschaltbar über die Umgebungsvariable
`AKTIVIERE_RERANKING` (siehe `main.py`), Standard: aus.

**Ergebnis: Reranking verschlechtert die Trefferquote (12/13 statt
13/13) und wird daher NICHT standardmäßig aktiviert.**

Betroffen ist ausschließlich Testfall 7 (objektübergreifender
Vergleich über alle 8 Energieausweise): Mit `RERANK_TOP_N=5` werden
nach dem Reranking nur noch 5 der ursprünglich 12 abgerufenen Chunks
für die Antwortgenerierung behalten — bei einer Frage, die Kontext aus
möglichst vielen der 8 Objekte braucht, gehen dadurch 3–4 Objekte
verloren, obwohl `SIMILARITY_TOP_K=12` ursprünglich genau für solche
Vergleichsfragen hochgesetzt wurde (siehe Kommentar in `main.py`).
Interessanterweise war Reranking dabei nicht einmal spürbar teurer
oder langsamer (siehe Protokoll-Vergleich unten) — das Problem ist
nicht Kosten/Latenz, sondern zu aggressives Aussieben von Kontext bei
Fragen, die breite Abdeckung statt hoher Präzision brauchen.

**Quantitativer Vergleich** (aus `anfrage_protokoll`, beide Läufe
direkt nacheinander mit identischem Code-Stand außer der
Reranking-Umschaltung):

| | Ø Latenz | Summe geschätzte Kosten | Ø Prompt-Tokens | Ergebnis |
|---|---|---|---|---|
| Ohne Reranking (Baseline) | 2996 ms | 0,00877 $ | 4159 | 13/13 |
| Mit Reranking | 2624 ms | 0,00867 $ | 4108 | 12/13 |

**Einordnung:** Bei einem größeren Corpus (deutlich mehr als ~40
Chunks, z. B. bei echten Bonorum-Unterlagen mit vielen Objekten) könnte
sich das Verhältnis umkehren, da dort mehr semantisches Rauschen in den
Top-12-Treffern zu erwarten ist und Reranking dort eher einen
Präzisionsgewinn bringen könnte. Beim aktuellen Testcorpus überwiegt
der Nachteil (verlorene Breite bei Vergleichsfragen) klar den
möglichen Vorteil. Die Funktion bleibt daher als optionale, per Env-Var
zuschaltbare Möglichkeit im Code, ist aber standardmäßig deaktiviert —
eine ehrlich gemessene Entscheidung statt einer ungeprüften Annahme,
dass mehr Verarbeitungsschritte automatisch bessere Ergebnisse liefern.

**Hinweis zur Vollständigkeit dieses Eintrags:** Die Baseline
("ohne Reranking") verhält sich unverändert zum letzten dokumentierten
Lauf (13/13, siehe Eintrag vom 2026-08-06 02:28 weiter unten) — deren
vollständige Frage/Antwort-Paare werden hier nicht erneut abgedruckt,
um reine Redundanz zu vermeiden. Der Lauf **mit** Reranking (die neue,
bisher nicht dokumentierte Systemkonfiguration) ist unten vollständig
mit allen 13 Fragen, Antworten, Quellen und Richter-Begründungen
dokumentiert.

### Testfälle mit aktiviertem Reranking (`AKTIVIERE_RERANKING=true`)

**1/13 PASS — Widerspruch zwischen Quellen**
Frage: Wie groß ist die Wohnfläche der Wohnung Sonnenblick?
Filter: objekt_name = sonnenblick
Erwartung: Sollte beide Werte nennen (78 m² im Exposé, 76 m² im Energieausweis) und den Widerspruch explizit benennen, statt sich stillschweigend für einen Wert zu entscheiden.
Antwort: Die Wohnfläche der Wohnung "Sonnenblick" wird in zwei verschiedenen Quellen unterschiedlich angegeben. Im Exposé wird die Wohnfläche mit 78 m² angegeben, während im Energieausweis eine Wohnfläche von 76 m² angegeben ist. Exposé: 78 m², Energieausweis: 76 m². Diese unterschiedlichen Angaben stellen einen Widerspruch dar.
Quellen: objekt1_sonnenblick_energieausweis.pdf, objekt1_sonnenblick_expose.pdf
Richter: PASS — Die Antwort nennt beide Werte der Wohnfläche und benennt den Widerspruch zwischen den Quellen klar und deutlich.

**2/13 PASS — Information nur in einem Dokument**
Frage: Welche Firma wartet den Fahrstuhl im Haus Sonnenblick und wann war die letzte Prüfung?
Filter: objekt_name = sonnenblick
Erwartung: Sollte 'Aufzugstechnik Reiner GmbH' und das Prüfdatum 09.11.2023 nennen. Diese Info steht nur im Protokoll — Prüfung anhand der Quellen-Liste, nicht anhand des Antworttexts.
Antwort: Die Firma, die den Fahrstuhl im Haus Sonnenblick wartet, ist die "Aufzugstechnik Reiner GmbH". Die letzte Prüfung durch den TÜV Süd fand am 09.11.2023 statt.
Quellen: objekt1_sonnenblick_protokoll.pdf
Richter: PASS — Die Antwort nennt korrekt die Firma und das Prüfdatum.

**3/13 PASS — Einfacher Fakt (Kontrollfrage)**
Frage: Wie hoch ist der Kaufpreis der Wohnung Gartenhof?
Filter: objekt_name = gartenhof
Erwartung: 229.000 EUR, Quelle objekt2_gartenhof_expose.pdf.
Antwort: Der Kaufpreis der Wohnung "Gartenhof" beträgt 229.000 EUR, wie im Exposé angegeben. Es gibt keine abweichenden Angaben zu diesem Sachverhalt in den bereitgestellten Kontextinformationen.
Quellen: objekt2_gartenhof_energieausweis.pdf, objekt2_gartenhof_expose.pdf
Richter: PASS — Korrekter Kaufpreis, korrekter Quellenverweis.

**4/13 PASS — Negativ-Fakt**
Frage: Gibt es einen Fahrstuhl in der Wohnung Gartenhof?
Filter: objekt_name = gartenhof
Erwartung: Nein — laut Exposé kein Fahrstuhl (Erdgeschoss, nicht erforderlich).
Antwort: In der Wohnung "Gartenhof" gibt es keinen Fahrstuhl, da dies im Exposé ausdrücklich erwähnt wird: "Kein Fahrstuhl (nicht erforderlich, da Erdgeschoss)." Es sind keine widersprüchlichen Angaben vorhanden.
Quellen: objekt2_gartenhof_expose.pdf, objekt2_gartenhof_teilungserklaerung.pdf
Richter: PASS

**5/13 PASS — Halluzinationstest**
Frage: Wann wurde die Sauna im Haus Ahornhöhe zuletzt gewartet?
Filter: objekt_name = ahornhöhe
Erwartung: Es gibt keine Sauna in den Unterlagen. Sollte klar sagen, dass die Information nicht vorhanden ist.
Antwort: Die Informationen im Kontext enthalten keine Angaben zur Wartung einer Sauna im Haus Ahornhöhe. Daher kann ich keine Antwort auf die Frage geben.
Quellen: objekt3_ahornhöhe_protokoll.pdf
Richter: PASS

**6/13 PASS — Cross-Objekt-Verwechslung**
Frage: Wurde bei der WEG Gartenhof eine Erhöhung der Instandhaltungsrücklage beschlossen?
Filter: objekt_name = gartenhof
Erwartung: Diesen Beschluss gibt es nur bei Sonnenblick, nicht bei Gartenhof. Korrekte Antwort ist 'nicht enthalten'.
Antwort: Im bereitgestellten Kontext sind keine Informationen über eine Erhöhung der Instandhaltungsrücklage bei der WEG Gartenhof enthalten. Daher kann ich nicht bestätigen, ob eine solche Erhöhung beschlossen wurde oder nicht.
Quellen: objekt2_gartenhof_protokoll.pdf, objekt2_gartenhof_teilungserklaerung.pdf
Richter: PASS

**7/13 FAIL — Vergleich über mehrere Objekte (der durch Reranking beeinträchtigte Fall)**
Frage: Welches Objekt hat die beste Energieeffizienzklasse? Nenne alle Objekte mit ihrer jeweiligen Klasse.
Erwartung: Seeblick (A+) ist am besten, vor Rosenhügel/Ahornhöhe (A), Birkenallee/Lindenpark (B), Gartenhof (C), Sonnenblick (D) und Kastanienhof (F). Testet, ob genug Kontext aus allen 8 Energieausweisen zusammenkommt.
Antwort: Die Energieeffizienzklassen der Objekte sind wie folgt: 1. Seeblick: A+, 2. Ahornhöhe: A, 3. Rosenhügel: A, 4. Birkenallee: B. Das Objekt mit der besten Energieeffizienzklasse ist Seeblick mit der Klasse A+.
Quellen: objekt3_ahornhöhe_energieausweis.pdf, objekt3_ahornhöhe_protokoll.pdf, objekt5_seeblick_energieausweis.pdf, objekt7_rosenhügel_energieausweis.pdf, objekt8_birkenallee_energieausweis.pdf
Richter: FAIL — Die Antwort listet nicht alle Objekte mit ihren jeweiligen Energieeffizienzklassen auf und enthält somit unvollständige Informationen.
**Analyse:** Nur 5 statt 8 Objekte in den Quellen (genau `RERANK_TOP_N=5`) — das Reranking hat die 4 anderen Energieausweise, die für eine vollständige Antwort nötig gewesen wären, aus dem Kontext entfernt.

**8/13 PASS — Information nur in der Teilungserklärung**
Frage: Wer hat laut den Unterlagen ein Sondernutzungsrecht am Garten bei der Wohnung Lindenpark?
Filter: objekt_name = lindenpark
Erwartung: Der Eigentümer der Einheit Nr. 2 (Hochparterre) hat das exklusive Sondernutzungsrecht am Garten.
Antwort: Laut den Unterlagen hat der Eigentümer der Einheit Nr. 2 (Hochparterre) das ausschließliche Sondernutzungsrecht an der Gartenfläche im Hinterhof. Alle übrigen Einheiten haben kein Nutzungsrecht an dieser Fläche.
Quellen: objekt4_lindenpark_teilungserklaerung.pdf
Richter: PASS

**9/13 PASS — Widerspruch zwischen Quellen (unterschiedlich benannte Felder)**
Frage: In welchem Jahr wurde das Gebäude Kastanienhof gebaut?
Filter: objekt_name = kastanienhof
Erwartung: Exposé nennt Baujahr 1975, Energieausweis nennt Baujahr Gebäude 1974. Sollte beide Werte mit Quelle nennen.
Antwort: Das Gebäude Kastanienhof wurde laut dem Exposé im Jahr 1975 gebaut. Im Energieausweis wird jedoch das Baujahr des Gebäudes mit 1974 angegeben. Dies stellt einen Widerspruch dar. Exposé: 1975, Energieausweis: 1974.
Quellen: objekt6_kastanienhof_energieausweis.pdf, objekt6_kastanienhof_expose.pdf, objekt6_kastanienhof_teilungserklaerung.pdf
Richter: PASS

**10/13 PASS — Cross-Objekt-Verwechslung (ähnliche Neubauten)**
Frage: Wurde bei der WEG Birkenallee eine Photovoltaikanlage beschlossen?
Filter: objekt_name = birkenallee
Erwartung: Nein — diesen Beschluss gibt es nur bei Rosenhügel. Bei Birkenallee nur als möglicher künftiger Tagesordnungspunkt erwähnt.
Antwort: Nein, bei der WEG Birkenallee wurde keine Photovoltaikanlage beschlossen. In der Eigentümerversammlung am 08.06.2024 wurde lediglich angeregt, die Möglichkeit einer Photovoltaikanlage bei Bedarf für eine der nächsten Versammlungen vorzubereiten. Ein konkreter Beschluss wurde jedoch nicht gefasst.
Quellen: objekt8_birkenallee_protokoll.pdf, objekt8_birkenallee_teilungserklaerung.pdf
Richter: PASS

**11/13 PASS — Halluzinationstest**
Frage: Gibt es einen Concierge-Service im Haus Seeblick?
Filter: objekt_name = seeblick
Erwartung: Es gibt keinen Concierge-Service in den Unterlagen.
Antwort: Die Informationen im Kontext enthalten keine Angaben zu einem Concierge-Service im Haus Seeblick. Daher kann ich nicht bestätigen, ob ein solcher Service vorhanden ist oder nicht.
Quellen: objekt5_seeblick_expose.pdf, objekt5_seeblick_protokoll.pdf, objekt5_seeblick_teilungserklaerung.pdf
Richter: PASS

**12/13 PASS — Mehrseitiges Dokument (Chunking über Seitengrenze)**
Frage: Welche Hausverwaltung ist laut Teilungserklärung als Verwalter der WEG Lindenpark bestellt?
Filter: objekt_name = lindenpark
Erwartung: Hausverwaltung Baumann, Verwalter-Abschnitt steht auf Seite 2 der Teilungserklärung.
Antwort: Laut Teilungserklärung ist die Hausverwaltung Baumann als Verwalter der Wohnungseigentümergemeinschaft (WEG) Lindenpark bestellt. Dies ist in der Quelle auf Seite 2 angegeben.
Quellen: objekt4_lindenpark_teilungserklaerung.pdf
Richter: PASS

**13/13 PASS — Echtes Chunking mit Token-Overlap**
Frage: Wer wurde bei der WEG Ahornhöhe zum Vorsitzenden des Verwaltungsbeirats gewählt?
Filter: objekt_name = ahornhöhe
Erwartung: Antwort sollte 'Herr T. Nowak' nennen (Hintergrund zum Testdesign, kein Bewertungskriterium: absichtlich per Overlap-Chunking erreichbarer Fakt).
Antwort: Herr T. Nowak wurde zum Vorsitzenden des Verwaltungsbeirats der WEG Ahornhöhe gewählt. Dies wird in den Kontextinformationen unter TOP 8 der Eigentümerversammlung erwähnt.
Quellen: objekt3_ahornhöhe_protokoll.pdf
Richter: PASS

**Entscheidung:** `AKTIVIERE_RERANKING` bleibt standardmäßig `false`.
Reranking ist als Option im Code erhalten (z. B. für einen künftig
deutlich größeren Corpus mit echten Bonorum-Unterlagen erneut zu
prüfen), aber ohne messbaren Nutzen beim aktuellen Testcorpus nicht
aktiv.

---

## Lauf vom 2026-08-06 02:28 (CEST) — Echtes Token-Chunking mit Overlap

**Git-Commit:** `ef6c9055d9f5e69fe2883f18411caa8b85e85ed9` ("Echtes
Token-Chunking mit Overlap getestet (Ahornhöhe-Protokoll verlängert)")

**Was sich seit dem letzten Lauf geändert hat:**

- **Letzter offener technischer Punkt behoben**: Bisher wurde
  "Chunking" nur über PDF-Seitengrenzen getestet (PDFReader erzeugt
  1 Document/Seite), nie über den eigentlichen
  `SentenceSplitter`-Mechanismus mit 200-Token-Overlap. Das Protokoll
  von **Ahornhöhe** wurde bewusst um 6 zusätzliche, realistische
  Tagesordnungspunkte verlängert (Instandsetzung Tiefgaragenzufahrt,
  Hausmeisterservice-Neuvergabe, Ladeinfrastruktur E-Autos, Wahl
  Verwaltungsbeirat, Paketstation, Sonstiges) und auf einer einzelnen,
  extra vergrößerten PDF-Seite gerendert (Seitenhöhe verdreifacht,
  nur für Protokolle), damit der Text (~1140 Tokens) NICHT über
  PDFReader auf Seite 2 umbricht, sondern in einem einzigen
  PDFReader-Document bleibt. Dadurch musste der `SentenceSplitter`
  selbst chunken.
- **Verifiziert:** Das Dokument wird jetzt tatsächlich in 2 Chunks
  aufgeteilt (Corpus-Chunkzahl 40 → 41), mit ~590 Zeichen echtem
  Overlap zwischen den beiden Chunks (Gesamtlänge beider Chunks
  5153 Zeichen vs. 4562 Zeichen Originaltext) — entspricht ungefähr
  der erwarteten 200-Token-Vorgabe.
- Testkatalog um 1 auf 13 Fragen erweitert (Testfall 13: Fakt aus dem
  hinteren, nur per Overlap-Chunk erreichbaren Teil des verlängerten
  Protokolls).
- **Erneuter Richter-Kalibrierungsfall**: Testfall 13 schlug im ersten
  Durchlauf fehl, weil die Test-Erwartung mehrdeutig formuliert war
  ("Info liegt in der zweiten Hälfte des Dokuments" wurde vom Richter
  fälschlich als Kritik an der Quellenangabe "Seite 1" gelesen, obwohl
  das Dokument bewusst nur 1 Seite hat und "Seite 1" daher korrekt
  ist). Nach Klarstellung der Erwartung (Korrektheitskriterium explizit
  von Testdesign-Hintergrund getrennt): PASS.

**Ergebnis: 13 von 13 Testfällen bestanden** (automatisiert per
LLM-Richter bewertet).

| # | Kategorie | Ergebnis |
|---|---|---|
| 1 | Widerspruch zwischen Quellen (Sonnenblick, Wohnfläche) | ✅ PASS |
| 2 | Info nur in einem Dokument (Sonnenblick, Fahrstuhl) | ✅ PASS |
| 3 | Einfacher Fakt (Gartenhof, Kaufpreis) | ✅ PASS |
| 4 | Negativ-Fakt (Gartenhof, kein Fahrstuhl) | ✅ PASS |
| 5 | Halluzinationstest (Ahornhöhe, Sauna) | ✅ PASS |
| 6 | Cross-Objekt-Verwechslung (Gartenhof, Rücklage) | ✅ PASS |
| 7 | Vergleich über 8 Objekte (Energieeffizienzklasse) | ✅ PASS |
| 8 | Info nur in Teilungserklärung (Lindenpark, Garten-Sondernutzungsrecht) | ✅ PASS |
| 9 | Widerspruch, unterschiedlich benannte Felder (Kastanienhof, Baujahr) | ✅ PASS |
| 10 | Cross-Objekt-Verwechslung, ähnliche Neubauten (Birkenallee vs. Rosenhügel) | ✅ PASS |
| 11 | Halluzinationstest (Seeblick, Concierge-Service) | ✅ PASS |
| 12 | Mehrseitiges Dokument, seitenbasiertes Chunking (Lindenpark, Verwalter) | ✅ PASS |
| 13 | **Neu:** Echtes Token-Chunking mit Overlap (Ahornhöhe, Verwaltungsbeirat) | ✅ PASS |

### Alle Testfälle im Detail

#### 1. Widerspruch zwischen Quellen (Sonnenblick, Wohnfläche)

**Frage:** Wie groß ist die Wohnfläche der Wohnung Sonnenblick?

**Erwartung:** Sollte beide Werte nennen (78 m² im Exposé, 76 m² im
Energieausweis) und den Widerspruch explizit benennen, statt sich
stillschweigend für einen Wert zu entscheiden.

**Filter:** objekt_name = sonnenblick

**Antwort:**
> Die Wohnfläche der Wohnung "Sonnenblick" wird in zwei verschiedenen
> Quellen unterschiedlich angegeben. Im Exposé wird die Wohnfläche mit
> 78 m² angegeben, während im Energieausweis eine Wohnfläche von 76 m²
> angegeben ist.
>
> - Exposé: 78 m²
> - Energieausweis: 76 m²
>
> Diese unterschiedlichen Angaben stellen einen Widerspruch dar.

**Quellen:** objekt1_sonnenblick_energieausweis.pdf, objekt1_sonnenblick_expose.pdf, objekt1_sonnenblick_protokoll.pdf, objekt1_sonnenblick_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort nennt beide Werte der Wohnfläche und
benennt den Widerspruch zwischen den Quellen klar und deutlich.

---

#### 2. Information nur in einem Dokument (Sonnenblick, Fahrstuhl)

**Frage:** Welche Firma wartet den Fahrstuhl im Haus Sonnenblick und
wann war die letzte Prüfung?

**Erwartung:** Sollte "Aufzugstechnik Reiner GmbH" und das Prüfdatum
09.11.2023 nennen. Diese Info steht nur im Protokoll
(objekt1_sonnenblick_protokoll.pdf) — wird anhand der Quellen-Liste
geprüft, nicht anhand des Antworttexts.

**Filter:** objekt_name = sonnenblick

**Antwort:**
> Die Firma, die den Fahrstuhl im Haus Sonnenblick wartet, ist die
> "Aufzugstechnik Reiner GmbH". Die letzte Hauptprüfung durch den TÜV
> Süd fand am 09.11.2023 statt. Diese Informationen stammen aus dem
> Protokoll der ordentlichen Eigentümerversammlung.

**Quellen:** objekt1_sonnenblick_energieausweis.pdf, objekt1_sonnenblick_expose.pdf, objekt1_sonnenblick_protokoll.pdf, objekt1_sonnenblick_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort nennt korrekt die Firma "Aufzugstechnik
Reiner GmbH" und das Prüfdatum 09.11.2023, und verweist auf das
Protokoll der Eigentümerversammlung.

---

#### 3. Einfacher Fakt (Gartenhof, Kaufpreis)

**Frage:** Wie hoch ist der Kaufpreis der Wohnung Gartenhof?

**Erwartung:** 229.000 EUR, Quelle objekt2_gartenhof_expose.pdf.

**Filter:** objekt_name = gartenhof

**Antwort:**
> Der Kaufpreis der Wohnung "Gartenhof" beträgt 229.000 EUR. Diese
> Information stammt aus dem Exposé (page_label: 1). Es gibt keine
> abweichenden Angaben zu diesem Sachverhalt in den anderen
> Dokumenten.

**Quellen:** objekt2_gartenhof_energieausweis.pdf, objekt2_gartenhof_expose.pdf, objekt2_gartenhof_protokoll.pdf, objekt2_gartenhof_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort gibt den korrekten Kaufpreis der
Wohnung "Gartenhof" an und verweist auf das entsprechende Dokument.

---

#### 4. Negativ-Fakt (Gartenhof, kein Fahrstuhl)

**Frage:** Gibt es einen Fahrstuhl in der Wohnung Gartenhof?

**Erwartung:** Nein — laut Exposé kein Fahrstuhl (Erdgeschoss, nicht
erforderlich). Testet, ob explizit verneinte Fakten korrekt
wiedergegeben werden.

**Filter:** objekt_name = gartenhof

**Antwort:**
> In der Wohnung "Gartenhof" gibt es keinen Fahrstuhl, da dies im
> Exposé ausdrücklich erwähnt wird: "Kein Fahrstuhl (nicht
> erforderlich, da Erdgeschoss)." Es sind keine widersprüchlichen
> Angaben zu diesem Sachverhalt in den anderen Dokumenten vorhanden.

**Quellen:** objekt2_gartenhof_energieausweis.pdf, objekt2_gartenhof_expose.pdf, objekt2_gartenhof_protokoll.pdf, objekt2_gartenhof_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort gibt korrekt an, dass es keinen
Fahrstuhl gibt, und verweist auf die entsprechende Information im
Exposé.

---

#### 5. Halluzinationstest (Ahornhöhe, Sauna)

**Frage:** Wann wurde die Sauna im Haus Ahornhöhe zuletzt gewartet?

**Erwartung:** Es gibt keine Sauna in den Unterlagen. Sollte klar
sagen, dass die Information nicht vorhanden ist, statt etwas zu
erfinden.

**Filter:** objekt_name = ahornhöhe

**Antwort:**
> Im Kontext sind keine Informationen über eine Sauna im Haus
> Ahornhöhe enthalten. Daher kann ich keine Angaben zur letzten
> Wartung einer Sauna machen.

**Quellen:** objekt3_ahornhöhe_energieausweis.pdf, objekt3_ahornhöhe_expose.pdf, objekt3_ahornhöhe_protokoll.pdf, objekt3_ahornhöhe_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort stellt klar, dass keine Informationen
über eine Sauna vorhanden sind.

---

#### 6. Cross-Objekt-Verwechslung (Gartenhof, Rücklage)

**Frage:** Wurde bei der WEG Gartenhof eine Erhöhung der
Instandhaltungsrücklage beschlossen?

**Erwartung:** Diesen Beschluss gibt es nur bei Sonnenblick, nicht bei
Gartenhof. Sollte nicht fälschlich vermischt werden — korrekte Antwort
ist "nicht enthalten".

**Filter:** objekt_name = gartenhof

**Antwort:**
> Im bereitgestellten Kontext sind keine Informationen über eine
> Erhöhung der Instandhaltungsrücklage bei der WEG Gartenhof
> enthalten. Daher kann ich diese Frage nicht beantworten.

**Quellen:** objekt2_gartenhof_energieausweis.pdf, objekt2_gartenhof_expose.pdf, objekt2_gartenhof_protokoll.pdf, objekt2_gartenhof_teilungserklaerung.pdf

**Richter:** PASS — Die Antwort stellt klar, dass keine Informationen
über eine Erhöhung bei Gartenhof enthalten sind.

---

#### 7. Vergleich über 8 Objekte (Energieeffizienzklasse)

**Frage:** Welches Objekt hat die beste Energieeffizienzklasse? Nenne
alle Objekte mit ihrer jeweiligen Klasse.

**Erwartung:** Seeblick (A+) ist am besten, vor Rosenhügel/Ahornhöhe
(A), Birkenallee/Lindenpark (B), Gartenhof (C), Sonnenblick (D) und
Kastanienhof (F). Testet, ob similarity_top_k=12 zuverlässig Kontext
aus allen 8 Energieausweisen liefert.

**Filter:** keiner (objektübergreifende Frage, ungefiltert)

**Antwort:**
> Das Objekt mit der besten Energieeffizienzklasse ist "Seeblick" mit
> der Energieeffizienzklasse A+.
>
> 1. Kastanienhof: F
> 2. Gartenhof: C
> 3. Sonnenblick: D
> 4. Lindenpark: B
> 5. Birkenallee: B
> 6. Ahornhöhe: A
> 7. Rosenhügel: A
> 8. Seeblick: A+

**Quellen:** alle 8 Energieausweise plus vereinzelte weitere Dokumente
(insgesamt 12 Chunks, top_k=12)

**Richter:** PASS — Nennt korrekt das beste Objekt und listet alle 8
Objekte mit ihrer jeweiligen Klasse auf.

---

#### 8. Information nur in der Teilungserklärung (Lindenpark, Garten)

**Frage:** Wer hat laut den Unterlagen ein Sondernutzungsrecht am
Garten bei der Wohnung Lindenpark?

**Erwartung:** Der Eigentümer der Einheit Nr. 2 (Hochparterre) hat das
exklusive Sondernutzungsrecht am Garten — diese Info steht nur in
objekt4_lindenpark_teilungserklaerung.pdf.

**Filter:** objekt_name = lindenpark

**Antwort:**
> Laut den Unterlagen hat der Eigentümer der Einheit Nr. 2
> (Hochparterre) das ausschließliche Sondernutzungsrecht an der
> Gartenfläche im Hinterhof. Dies ist in der Teilungserklärung gemäß
> § 8 WEG festgelegt. Alle übrigen Einheiten haben kein Nutzungsrecht
> an dieser Fläche.

**Quellen:** objekt4_lindenpark_energieausweis.pdf, objekt4_lindenpark_expose.pdf, objekt4_lindenpark_protokoll.pdf, objekt4_lindenpark_teilungserklaerung.pdf

**Richter:** PASS — Nennt korrekt Einheit Nr. 2 und verweist auf die
Teilungserklärung.

---

#### 9. Widerspruch, unterschiedlich benannte Felder (Kastanienhof, Baujahr)

**Frage:** In welchem Jahr wurde das Gebäude Kastanienhof gebaut?

**Erwartung:** Leichte Abweichung: Exposé nennt Baujahr 1975,
Energieausweis nennt Baujahr Gebäude 1974. Sollte beide Werte mit
Quelle nennen. (Dies war ursprünglich ein bekannter Fehler — siehe
Lauf vom 2026-08-05 20:40 — behoben durch QA_PROMPT-Fix im Lauf vom
2026-08-06 01:43.)

**Filter:** objekt_name = kastanienhof

**Antwort:**
> Das Gebäude Kastanienhof wurde im Jahr 1975 erbaut, laut den
> Angaben im Exposé. Allerdings gibt es eine abweichende Angabe im
> Energieausweis, der das Baujahr des Gebäudes mit 1974 angibt.
> Somit gibt es einen Widerspruch zwischen den Quellen:
> - Exposé: Baujahr 1975
> - Energieausweis: Baujahr 1974

**Quellen:** objekt6_kastanienhof_energieausweis.pdf, objekt6_kastanienhof_expose.pdf, objekt6_kastanienhof_protokoll.pdf, objekt6_kastanienhof_teilungserklaerung.pdf

**Richter:** PASS — Nennt beide Quellen und weist korrekt auf den
Widerspruch hin.

---

#### 10. Cross-Objekt-Verwechslung, ähnliche Neubauten (Birkenallee, PV-Anlage)

**Frage:** Wurde bei der WEG Birkenallee eine Photovoltaikanlage
beschlossen?

**Erwartung:** Nein — diesen Beschluss gibt es nur bei Rosenhügel
(einem ähnlichen Neubau). Bei Birkenallee nur als möglicher künftiger
Tagesordnungspunkt erwähnt, kein Beschluss.

**Filter:** objekt_name = birkenallee

**Antwort:**
> Bei der WEG Birkenallee wurde keine Photovoltaikanlage beschlossen.
> In der Eigentümerversammlung am 08.06.2024 wurde lediglich
> angeregt, das Thema für eine der nächsten Versammlungen als eigenen
> Tagesordnungspunkt vorzubereiten, jedoch wurde kein konkreter
> Beschluss gefasst.

**Quellen:** objekt8_birkenallee_energieausweis.pdf, objekt8_birkenallee_expose.pdf, objekt8_birkenallee_protokoll.pdf, objekt8_birkenallee_teilungserklaerung.pdf

**Richter:** PASS — Stellt klar, dass kein Beschluss gefasst wurde,
keine Verwechslung mit Rosenhügel.

---

#### 11. Halluzinationstest (Seeblick, Concierge-Service)

**Frage:** Gibt es einen Concierge-Service im Haus Seeblick?

**Erwartung:** Es gibt keinen Concierge-Service in den Unterlagen.
Sollte klar sagen, dass die Information nicht vorhanden ist.

**Filter:** objekt_name = seeblick

**Antwort:**
> Die Informationen im Kontext enthalten keine Angaben zu einem
> Concierge-Service im Haus "Seeblick". Daher kann ich nicht
> bestätigen, ob ein solcher Service vorhanden ist oder nicht.

**Quellen:** objekt5_seeblick_energieausweis.pdf, objekt5_seeblick_expose.pdf, objekt5_seeblick_protokoll.pdf, objekt5_seeblick_teilungserklaerung.pdf

**Richter:** PASS — Stellt klar, dass keine Informationen vorhanden
sind.

---

#### 12. Mehrseitiges Dokument, seitenbasiertes Chunking (Lindenpark, Verwalter)

**Frage:** Welche Hausverwaltung ist laut Teilungserklärung als
Verwalter der WEG Lindenpark bestellt?

**Erwartung:** Hausverwaltung Baumann. Die Teilungserklärung umfasst
2 PDF-Seiten (PDFReader-Aufteilung); der Verwalter-Abschnitt steht auf
Seite 2. Testet, ob Retrieval den richtigen Teil eines mehrseitigen
Dokuments findet.

**Filter:** objekt_name = lindenpark

**Antwort:**
> Laut Teilungserklärung ist die Hausverwaltung Baumann als Verwalter
> der Wohnungseigentümergemeinschaft (WEG) Lindenpark bestellt.

**Quellen:** objekt4_lindenpark_energieausweis.pdf, objekt4_lindenpark_expose.pdf, objekt4_lindenpark_protokoll.pdf, objekt4_lindenpark_teilungserklaerung.pdf

**Richter:** PASS — Nennt korrekt Hausverwaltung Baumann.

---

#### 13. Echtes Token-Chunking mit Overlap (Ahornhöhe, Verwaltungsbeirat) — NEU

**Frage:** Wer wurde bei der WEG Ahornhöhe zum Vorsitzenden des
Verwaltungsbeirats gewählt?

**Erwartung:** "Herr T. Nowak" — einziges Korrektheitskriterium für
den Antworttext. Testdesign-Hintergrund (kein Bewertungskriterium):
Protokoll ist absichtlich eine einzelne, sehr lange PDF-Seite
(>1024 Tokens), damit der `SentenceSplitter` selbst mit echtem
200-Token-Overlap chunkt statt PDFReader seitenbasiert zu splitten.

**Filter:** objekt_name = ahornhöhe

**Antwort:**
> Herr T. Nowak wurde zum Vorsitzenden des Verwaltungsbeirats gewählt.
> Dies wird im Protokoll der ordentlichen Eigentümerversammlung
> (Seite 1) erwähnt.

**Quellen:** objekt3_ahornhöhe_energieausweis.pdf, objekt3_ahornhöhe_expose.pdf, objekt3_ahornhöhe_protokoll.pdf, objekt3_ahornhöhe_teilungserklaerung.pdf

**Richter:** PASS — Nennt korrekt Herr T. Nowak und verweist auf das
Protokoll.

---

## Lauf vom 2026-08-06 02:13 (CEST) — Metadaten-Filterung + automatisierte Bewertung (LLM-Richter)

**Git-Commit:** `214fc92` ("Metadaten-Filterung (Retrieval) und
LLM-als-Richter (automatisierte Tests)")

**Was sich seit dem letzten Lauf geändert hat:**

- **Metadaten-Filterung eingeführt**: Jedes Dokument bekommt beim
  Einlesen ein `objekt_name`-Metadatenfeld (aus dem Dateinamen
  extrahiert). Erkennt `main.py` in der Frage genau einen bekannten
  Objektnamen, filtert die Vektorsuche gezielt auf dessen Dokumente,
  statt über den ganzen 40-Chunk-Corpus zu suchen. Bei Fragen ohne
  eindeutigen Objektnamen (z. B. Vergleichsfragen) bleibt die Suche
  ungefiltert. Behebt strukturell die Ursache der letzten Regression
  (semantisches Rauschen zwischen ähnlichen Objekten).
- **LLM-als-Richter eingeführt**: `tests/testfragen.py` bewertet jede
  Antwort jetzt automatisiert per zweitem LLM-Call (PASS/FAIL +
  Begründung) gegen die hinterlegte Erwartung, statt dass die Antworten
  von Hand gelesen werden.
- **Wichtiger Kalibrierungs-Fund beim Richter selbst**: Der erste
  Richter-Prompt-Entwurf war zu wörtlich/streng — er verlangte u. a.,
  dass exakte Dateinamen im Antworttext wörtlich vorkommen, und
  bewertete korrekt-zurückhaltende Formulierungen ("kann ich nicht
  bestätigen") fälschlich als FAIL. Erster automatisierter Lauf: 8/12.
  Nach Analyse jedes einzelnen FAILs (3 von 4 waren Richterfehler, nicht
  Systemfehler) wurde der Richter-Prompt nachgeschärft (Fokus auf
  fachlichen Inhalt statt Zitierformat, explizite Regel für korrekt
  zurückhaltende "nicht vorhanden"-Antworten) — danach 11/12. Der
  letzte verbleibende Fall war ebenfalls ein reines
  Formulierungs-Problem der Testerwartung selbst (verlangte wörtliche
  Dateinamensnennung im Fließtext), nicht des Systems — nach Anpassung
  der Erwartung: 12/12.
- **Eine echte, kleine Verhaltensänderung durch die Filterung
  beobachtet**: Bei der Gartenhof-Rücklage-Frage antwortet das System
  jetzt vorsichtiger ("kann ich nicht bestätigen" statt zuvor "Nein, es
  wurde nicht beschlossen") — weil ohne Kontext zu anderen Objekten
  weniger Kontrastinformation für eine bestimmte Verneinung vorliegt.
  Sachlich weiterhin korrekt (keine Verwechslung, keine Erfindung),
  aber ein nachvollziehbarer Kompromiss der Filterung: weniger
  Cross-Objekt-Risiko, dafür etwas vorsichtigere Formulierung bei
  Abwesenheits-Fragen.

**Ergebnis: 12 von 12 Testfällen bestehen, automatisiert per
LLM-Richter bewertet** (nicht mehr manuell gelesen).

**Wichtige methodische Erkenntnis für dieses Projekt:** Ein
LLM-als-Richter ist kein Selbstläufer — er muss selbst kalibriert und
gegen bekannte, manuell verifizierte Fälle geprüft werden, sonst
produziert er eigene falsche Positive/Negative. Das haben wir hier
direkt erlebt (8/12 → 12/12 durch reine Prompt-Kalibrierung, ohne dass
sich das eigentliche System verändert hat).

---

## Lauf vom 2026-08-06 01:43 (CEST) — Längere Teilungserklärungen, Prompt-Fix

**Git-Commit:** `8f15fb46620aa4f083f7e9f770da0d2d180ac4fe` ("Teilungserklärungen verlängert, Widerspruchs-Prompt
nachgeschärft")

**Was sich seit dem letzten Lauf geändert hat:**

- **Teilungserklärungen verlängert**: Für alle 8 Objekte um 5 Abschnitte
  ergänzt (Bauliche Veränderungen, Instandhaltung, Verwalter,
  Tierhaltung, Schlussbestimmungen). Damit erstmals Dokumente, die über
  die 1024-Token-Chunking-Grenze kommen.
- **Wichtige technische Erkenntnis beim Chunking-Test**: Die
  Teilungserklärungen sind jetzt 2 PDF-Seiten lang. `PDFReader` (aus
  `llama-index-readers-file`) erzeugt dabei **ein Document pro
  PDF-Seite**, nicht eines pro Datei — die "Aufteilung" erfolgt also
  über Seitengrenzen, nicht über den `SentenceSplitter` mit
  Token-Overlap (der hätte nur bei einer einzelnen, über 1024 Tokens
  langen Seite gegriffen). Praktisch bedeutet das: kein Overlap
  zwischen den beiden Teilen, aber echtes Multi-Node-Retrieval pro
  Dokument wird trotzdem erstmals getestet. Corpus jetzt 40 Chunks
  (24 einseitige Dokumente + 8 Teilungserklärungen × 2 Seiten).
- **QA_PROMPT nachgeschärft**: Neue Anweisung, bei Zahlen-/Datumsangaben
  gezielt auf abweichende Werte zu prüfen, auch wenn Quellen den
  Sachverhalt unterschiedlich benennen (z. B. "Baujahr" vs. "Baujahr
  Gebäude"). Behebt den in der vorherigen Doku beschriebenen
  Schwachpunkt bei Testfall Kastanienhof/Baujahr.
- **Regression gefunden und behoben, bevor sie dokumentiert wurde**:
  Die erste Version der längeren Teilungserklärungen enthielt in 7 von
  8 Objekten eine fast wortgleiche generische Formulierung
  ("...wird aus der Instandhaltungsrücklage finanziert"). Das
  verwässerte den Begriff "Instandhaltungsrücklage" über alle Objekte
  hinweg und führte in Kombination mit dem nachgeschärften Prompt dazu,
  dass Testfall #6 (Gartenhof-Rücklage) plötzlich fehlschlug — das
  Modell antwortete widersprüchlich ("Ja... jedoch keine spezifische
  Erhöhung erwähnt"), reproduzierbar in 3/3 Versuchen. Behoben durch
  Umformulierung der 6 nicht objektspezifisch betroffenen
  Teilungserklärungen (Sonnenblick und Kastanienhof behalten den Begriff,
  da dort tatsächlich objektspezifisch relevant). Nach der Korrektur
  läuft Testfall #6 wieder zuverlässig (3/3 verifiziert).
- Testkatalog um 1 auf 12 Fragen erweitert (neuer Testfall: Info auf
  Seite 2 einer mehrseitigen Teilungserklärung).

**Ergebnis: 12 von 12 Testfällen wie erwartet.**

| # | Kategorie | Ergebnis |
|---|---|---|
| 1 | Widerspruch zwischen Quellen (Sonnenblick, Wohnfläche) | ✅ |
| 2 | Info nur in einem Dokument (Sonnenblick, Fahrstuhl) | ✅ |
| 3 | Einfacher Fakt (Gartenhof, Kaufpreis) | ✅ |
| 4 | Negativ-Fakt (Gartenhof, kein Fahrstuhl) | ✅ |
| 5 | Halluzinationstest (Ahornhöhe, Sauna) | ✅ |
| 6 | Cross-Objekt-Verwechslung (Gartenhof, Rücklage) | ✅ (nach Regression + Fix, siehe oben) |
| 7 | Vergleich über 8 Objekte (Energieeffizienzklasse) | ✅ |
| 8 | Info nur in Teilungserklärung (Lindenpark, Garten-Sondernutzungsrecht) | ✅ |
| 9 | Widerspruch, unterschiedlich benannte Felder (Kastanienhof, Baujahr) | ✅ **jetzt erkannt** (vorher bekannter Fehler, siehe Prompt-Fix oben) |
| 10 | Cross-Objekt-Verwechslung, ähnliche Neubauten (Birkenallee vs. Rosenhügel, PV-Anlage) | ✅ |
| 11 | Halluzinationstest (Seeblick, Concierge-Service) | ✅ |
| 12 | Mehrseitiges Dokument / Chunking über Seitengrenze (Lindenpark, Verwalter auf Seite 2) | ✅ **neu** |

Vollständige Frage/Antwort-Details für die unveränderten Testfälle
(1-8, 10-11) siehe vorherige Einträge unten — die Antworten sind
inhaltlich gleichwertig. Details zu den veränderten/neuen Fällen
(9, 12) und dem behobenen Regressionsfall (6):

### Test #6 — Cross-Objekt-Verwechslung (nach Fix)

**Antwort:**
> Nein, bei der WEG Gartenhof wurde keine Erhöhung der
> Instandhaltungsrücklage beschlossen. In den Protokollen der
> Eigentümerversammlung wird lediglich die Genehmigung der
> Jahresabrechnung und die Pflege der Gartenanlage behandelt, jedoch
> keine Erhöhung der Instandhaltungsrücklage erwähnt.

### Test #9 — Widerspruch, unterschiedlich benannte Felder (nach Prompt-Fix)

**Antwort:**
> Das Gebäude "Kastanienhof" wurde im Jahr 1974 gebaut, wie im
> Energieausweis angegeben (Dateiname:
> objekt6_kastanienhof_energieausweis.pdf). Im Exposé wird jedoch das
> Baujahr als 1975 angegeben (Dateiname: objekt6_kastanienhof_expose.pdf).
> Dies stellt einen Widerspruch dar.

### Test #12 — Mehrseitiges Dokument (neu)

**Frage:** Welche Hausverwaltung ist laut Teilungserklärung als
Verwalter der WEG Lindenpark bestellt?

**Antwort:**
> Laut der Teilungserklärung für die WEG Lindenpark ist die
> Hausverwaltung Baumann als Verwalter bestellt (Quelle:
> objekt4_lindenpark_teilungserklaerung.pdf).

**Quellen (Auszug):** `objekt4_lindenpark_teilungserklaerung.pdf`
erscheint zweimal in der Quellenliste — einmal pro Seite (Seite 1 und
Seite 2) — der Verwalter-Abschnitt liegt auf Seite 2 und wurde korrekt
gefunden.

---

## Lauf vom 2026-08-06 00:56 (CEST) — Bestätigungslauf, erstmals mit Git-Commit verknüpft

**Git-Commit:** `dbe75ceb7ad792bf7ce15c4d832cc011e0693859` ("README hinzufügen",
gepusht nach `https://github.com/MH-Git1/immobilien-rag`)

**Code-Stand:** identisch zum Lauf vom 2026-08-05 20:40 — `main.py` und
`tests/testfragen.py` haben sich seitdem nicht verändert (per
`git diff` gegen Commit `9ba1b36` bestätigt). Dieser Lauf dient primär
dazu, (a) Reproduzierbarkeit zu bestätigen und (b) erstmals einen
Testlauf mit einem echten Git-Commit-Hash zu verknüpfen, statt nur mit
einem Zeitstempel — wie im vorherigen Eintrag als offener Punkt
vermerkt.

**Ergebnis: 10 von 11 Testfällen wie erwartet — identisch zum
vorherigen Lauf.** Test #9 (Kastanienhof, Baujahr-Widerspruch) schlägt
erneut fehl, exakt wie zuvor: Die Antwort nennt nur 1975 (Exposé),
der abweichende Wert 1974 aus dem Energieausweis wird trotz korrekt
gefundener Quelle nicht erwähnt. Das bestätigt, dass es sich um einen
stabilen, reproduzierbaren Schwachpunkt der Widerspruchserkennung
handelt (nicht um Zufallsrauschen des LLMs) — Ursache weiterhin
vermutlich die unterschiedliche Feldbezeichnung ("Baujahr" vs.
"Baujahr Gebäude"), siehe Analyse im vorherigen Eintrag.

Alle übrigen 10 Antworten sind inhaltlich gleichwertig zum vorherigen
Lauf (in der Formulierung leicht, aber nicht in der Substanz
abweichend). Details siehe Q&A-Auflistung im Eintrag vom
2026-08-05 20:40 unten — die Fragen, Erwartungen und Kategorien sind
unverändert; hier nur die neue Verknüpfung mit Zeitstempel und
Commit-Hash sowie das bestätigte Ergebnis.

---

## Lauf vom 2026-08-05 20:40 (CEST) — PDF-Corpus, 8 Objekte

**Was sich seit dem letzten Lauf geändert hat:**

- Datenbasis auf **32 PDF-Dokumente** erweitert (8 fiktive Objekte ×
  4 Dokumenttypen: Exposé, Energieausweis, Protokoll,
  **Teilungserklärung neu dazugekommen**). Objekte 1-3 (Sonnenblick,
  Gartenhof, Ahornhöhe) sind die bisherigen; Objekte 4-8 (Lindenpark,
  Seeblick, Kastanienhof, Rosenhügel, Birkenallee) sind neu.
- Dokumente sind jetzt **echte PDFs** (`data_pdf/`, per `reportlab`
  generiert im Stil real recherchierter Vorlagen — stawag-Energieausweis,
  WEG-Wissen-Protokoll), nicht mehr reiner `.txt`.
- **Wichtiger Bugfix unterwegs gefunden:** `SimpleDirectoryReader` hatte
  ohne das Paket `llama-index-readers-file` PDFs nicht als solche
  erkannt und stattdessen rohe PDF-Binärdaten in den Index geschrieben
  (kein extrahierter Text) — nach Installation des fehlenden Pakets und
  Neuaufbau des Index behoben.
- `similarity_top_k` von 6 auf **12** erhöht (8 statt 3 Objekte, eine
  Vergleichsfrage kann Kontext aus bis zu 8 Energieausweisen brauchen).
- Postgres-Tabelle umbenannt zu `immobilien_chunks_v2` (alte Tabelle mit
  dem `.txt`-Corpus bleibt unberührt bestehen, wird aber nicht mehr
  verwendet).
- Aktuell 32 Chunks (1 Dokument = 1 Chunk — die Dokumente sind trotz
  realistischerer Formatierung noch unter der 1024-Token-Grenze).

**Ergebnis: 10 von 11 Testfällen wie erwartet, 1 Teilerfolg mit echter
Erkenntnis.**

| # | Kategorie | Ergebnis |
|---|---|---|
| 1 | Widerspruch zwischen Quellen (Sonnenblick, Wohnfläche) | ✅ |
| 2 | Info nur in einem Dokument (Sonnenblick, Fahrstuhl) | ✅ |
| 3 | Einfacher Fakt (Gartenhof, Kaufpreis) | ✅ |
| 4 | Negativ-Fakt (Gartenhof, kein Fahrstuhl) | ✅ |
| 5 | Halluzinationstest (Ahornhöhe, Sauna) | ✅ |
| 6 | Cross-Objekt-Verwechslung (Gartenhof, Rücklage) | ✅ |
| 7 | Vergleich über 8 Objekte (Energieeffizienzklasse) | ✅ — alle 8 korrekt, Seeblick (A+) richtig als bestes Objekt |
| 8 | Info nur in Teilungserklärung (Lindenpark, Garten-Sondernutzungsrecht) | ✅ |
| 9 | Widerspruch zwischen Quellen (Kastanienhof, Baujahr) | ⚠️ **Teilerfolg** — siehe unten |
| 10 | Cross-Objekt-Verwechslung, ähnliche Neubauten (Birkenallee vs. Rosenhügel, PV-Anlage) | ✅ |
| 11 | Halluzinationstest (Seeblick, Concierge-Service) | ✅ |

### Befund zu Test #9 — Widerspruchserkennung ist nicht immer zuverlässig

**Frage:** In welchem Jahr wurde das Gebäude Kastanienhof gebaut?

**Erwartung:** Exposé nennt 1975, Energieausweis nennt (als "Baujahr
Gebäude") 1974 — beide Werte sollten mit Quelle genannt werden.

**Tatsächliche Antwort (3x reproduziert, konsistent):**
> Das Gebäude Kastanienhof wurde im Jahr 1975 gebaut. Diese Information
> stammt aus dem Exposé der Wohnung "Kastanienhof" (Dateiname:
> objekt6_kastanienhof_expose.pdf).

Der Energieausweis mit dem abweichenden Wert (`objekt6_kastanienhof_
energieausweis.pdf`) wurde vom Retrieval korrekt gefunden und war Teil
des Kontexts — das Modell hat den Widerspruch trotzdem nicht erkannt
und genannt.

**Vermutliche Ursache:** Bei Sonnenblick (Test #1, funktioniert)
heißt das Feld in beiden Quellen identisch "Wohnfläche". Bei
Kastanienhof heißt es im Exposé "Baujahr", im Energieausweis aber
"Baujahr Gebäude" — die leicht abweichende Formulierung scheint die
im Prompt verankerte Widerspruchserkennung ("gleicher Sachverhalt")
zu schwächen. Das ist ein reales, reproduzierbares Limit des aktuellen
Custom-Prompts, kein einmaliger Ausrutscher.

**Mögliche nächste Schritte dazu:** Prompt könnte explizit auf
"inhaltlich gleiche Angaben trotz unterschiedlicher Formulierung"
hinweisen, oder Feldnamen in den Dokumenten stärker vereinheitlichen.

---

## Lauf vom 2026-08-05 19:15 (CEST)

**Code-/Systemstand bei diesem Lauf:**

- Git-Commit: — *(noch kein Git-Repository im Projekt initialisiert;
  siehe Hinweis unten)*
- Vektorspeicher: Postgres + pgvector (Docker, `docker-compose.yml`),
  Tabelle `data_immobilien_chunks`
- Embedding-Modell: `text-embedding-3-small`
- LLM: `gpt-4o-mini`
- `similarity_top_k`: 6
- Custom-QA-Prompt aktiv (weist Modell an, Widersprüche zwischen
  Quellen explizit zu benennen und nichts zu erfinden)
- Datenbasis: 9 Dokumente / 9 Chunks (3 fiktive Objekte: Sonnenblick,
  Gartenhof, Ahornhöhe)

**Ergebnis: 7/7 Testfälle wie erwartet.**

| # | Kategorie | Ergebnis |
|---|---|---|
| 1 | Widerspruch zwischen Quellen | ✅ Beide Werte (78 m² / 76 m²) korrekt mit Quelle genannt |
| 2 | Info nur in einem Dokument | ✅ Firma + Datum korrekt, richtige Quelle |
| 3 | Einfacher Fakt (Kontrolle) | ✅ Korrekt |
| 4 | Negativ-Fakt | ✅ Korrekt verneint, mit Beleg |
| 5 | Halluzinationstest | ✅ Kein erfundener Inhalt, klare Absage |
| 6 | Cross-Objekt-Verwechslung | ✅ Keine Vermischung zwischen Objekten |
| 7 | Vergleich über mehrere Objekte | ✅ Alle drei Energieausweise korrekt einbezogen |

### Details

#### 1. Widerspruch zwischen Quellen

**Frage:** Wie groß ist die Wohnfläche der Wohnung Sonnenblick?

**Erwartung:** Sollte beide Werte nennen (78 m² im Exposé, 76 m² im
Energieausweis) und den Widerspruch explizit benennen, statt sich
stillschweigend für einen Wert zu entscheiden.

**Antwort:**
> Die Wohnfläche der Wohnung "Sonnenblick" wird in zwei verschiedenen
> Quellen unterschiedlich angegeben. Im Exposé wird die Wohnfläche mit
> 78 m² angegeben (Dateiname: objekt1_sonnenblick_expose.txt), während
> im Energieausweis eine Wohnfläche von 76 m² angegeben wird
> (Dateiname: objekt1_sonnenblick_energieausweis.txt).

**Quellen:** objekt1_sonnenblick_expose.txt, objekt1_sonnenblick_energieausweis.txt, objekt3_ahornhoehe_expose.txt, objekt1_sonnenblick_protokoll.txt, objekt2_gartenhof_expose.txt, objekt3_ahornhoehe_energieausweis.txt

---

#### 2. Information nur in einem Dokument

**Frage:** Welche Firma wartet den Fahrstuhl im Haus Sonnenblick und
wann war die letzte Prüfung?

**Erwartung:** Sollte "Aufzugstechnik Reiner GmbH" und das Prüfdatum
09.11.2023 nennen, mit Quelle objekt1_sonnenblick_protokoll.txt —
diese Info steht nirgendwo sonst.

**Antwort:**
> Die Firma, die den Fahrstuhl im Haus Sonnenblick wartet, ist die
> "Aufzugstechnik Reiner GmbH". Die letzte Hauptprüfung durch den TÜV
> Süd fand am 09.11.2023 statt. (Quelle: objekt1_sonnenblick_protokoll.txt)

**Quellen:** objekt1_sonnenblick_protokoll.txt, objekt1_sonnenblick_expose.txt, objekt1_sonnenblick_energieausweis.txt, objekt3_ahornhoehe_protokoll.txt, objekt3_ahornhoehe_expose.txt, objekt3_ahornhoehe_energieausweis.txt

---

#### 3. Einfacher Fakt (Kontrollfrage)

**Frage:** Wie hoch ist der Kaufpreis der Wohnung Gartenhof?

**Erwartung:** 229.000 EUR, Quelle objekt2_gartenhof_expose.txt.

**Antwort:**
> Der Kaufpreis der Wohnung "Gartenhof" beträgt 229.000 EUR (Quelle:
> objekt2_gartenhof_expose.txt).

**Quellen:** objekt2_gartenhof_expose.txt, objekt3_ahornhoehe_expose.txt, objekt1_sonnenblick_expose.txt, objekt2_gartenhof_energieausweis.txt, objekt2_gartenhof_protokoll.txt, objekt3_ahornhoehe_energieausweis.txt

---

#### 4. Negativ-Fakt

**Frage:** Gibt es einen Fahrstuhl in der Wohnung Gartenhof?

**Erwartung:** Nein — laut Exposé kein Fahrstuhl (Erdgeschoss, nicht
erforderlich). Testet, ob explizit verneinte Fakten korrekt
wiedergegeben werden.

**Antwort:**
> In der Wohnung "Gartenhof" gibt es keinen Fahrstuhl, da die Wohnung
> im Erdgeschoss liegt und dies als nicht erforderlich angesehen wird.
> Diese Information stammt aus dem Exposé der Wohnung "Gartenhof"
> (Dateiname: objekt2_gartenhof_expose.txt).

**Quellen:** objekt2_gartenhof_expose.txt, objekt3_ahornhoehe_expose.txt, objekt2_gartenhof_protokoll.txt, objekt3_ahornhoehe_protokoll.txt, objekt1_sonnenblick_protokoll.txt, objekt1_sonnenblick_expose.txt

---

#### 5. Halluzinationstest

**Frage:** Wann wurde die Sauna im Haus Ahornhöhe zuletzt gewartet?

**Erwartung:** Es gibt keine Sauna in den Unterlagen. Sollte klar
sagen, dass die Information nicht vorhanden ist, statt etwas zu
erfinden.

**Antwort:**
> Die Informationen im Kontext enthalten keine Angaben zur Wartung
> einer Sauna im Haus Ahornhöhe. Daher kann ich diese Frage nicht
> beantworten.

**Quellen:** objekt3_ahornhoehe_protokoll.txt, objekt3_ahornhoehe_energieausweis.txt, objekt3_ahornhoehe_expose.txt, objekt1_sonnenblick_energieausweis.txt, objekt1_sonnenblick_protokoll.txt, objekt2_gartenhof_energieausweis.txt

---

#### 6. Cross-Objekt-Verwechslung

**Frage:** Wurde bei der WEG Gartenhof eine Erhöhung der
Instandhaltungsrücklage beschlossen?

**Erwartung:** Diesen Beschluss gibt es nur bei Sonnenblick, nicht bei
Gartenhof. Sollte nicht fälschlich vermischt werden — korrekte Antwort
ist "nicht enthalten".

**Antwort:**
> Im Protokoll der Eigentümerversammlung der WEG Gartenhof (Dateiname:
> objekt2_gartenhof_protokoll.txt) wird keine Erhöhung der
> Instandhaltungsrücklage beschlossen. Daher ist die Antwort: Nein, es
> wurde keine Erhöhung der Instandhaltungsrücklage beschlossen.

**Quellen:** objekt2_gartenhof_protokoll.txt, objekt3_ahornhoehe_protokoll.txt, objekt1_sonnenblick_protokoll.txt, objekt2_gartenhof_expose.txt, objekt2_gartenhof_energieausweis.txt, objekt3_ahornhoehe_energieausweis.txt

---

#### 7. Vergleich über mehrere Objekte

**Frage:** Welches der drei Objekte hat die beste
Energieeffizienzklasse?

**Erwartung:** Ahornhöhe (Klasse A) ist am besten, vor Gartenhof (C)
und Sonnenblick (D). Testet, ob similarity_top_k genug Kontext aus
allen drei Objekten liefert, um einen vollständigen Vergleich zu
ermöglichen.

**Antwort:**
> Das Objekt mit der besten Energieeffizienzklasse ist das Penthouse
> "Ahornhöhe" mit der Energieeffizienzklasse A.
>
> Die anderen beiden Objekte haben folgende Energieeffizienzklassen:
> - Wohnung "Sonnenblick": Energieeffizienzklasse D (Quelle:
>   objekt1_sonnenblick_energieausweis.txt)
> - Wohnung "Gartenhof": Energieeffizienzklasse C (Quelle:
>   objekt2_gartenhof_energieausweis.txt)

**Quellen:** objekt3_ahornhoehe_energieausweis.txt, objekt1_sonnenblick_energieausweis.txt, objekt2_gartenhof_energieausweis.txt, objekt1_sonnenblick_expose.txt, objekt3_ahornhoehe_expose.txt, objekt2_gartenhof_expose.txt


