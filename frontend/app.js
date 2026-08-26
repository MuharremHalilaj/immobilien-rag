const verlauf = document.getElementById("verlauf");
const leerzustand = document.getElementById("leerzustand");
const formular = document.getElementById("frage-formular");
const eingabe = document.getElementById("frage-eingabe");
const sendenButton = document.getElementById("senden-button");

function nachrichtHinzufuegen(html, klasse) {
  const el = document.createElement("div");
  el.className = `nachricht ${klasse}`;
  el.innerHTML = html;
  verlauf.appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
  return el;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

async function frageSenden(frage) {
  leerzustand.style.display = "none";
  nachrichtHinzufuegen(`<div class="blase">${escapeHtml(frage)}</div>`, "frage");

  const ladeBlase = nachrichtHinzufuegen(
    `<div class="blase ladeanzeige"><span></span><span></span><span></span></div>`,
    "antwort"
  );

  eingabe.disabled = true;
  sendenButton.disabled = true;

  try {
    const response = await fetch("/api/frage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frage }),
    });

    if (!response.ok) {
      throw new Error(`Server-Fehler (${response.status})`);
    }

    const daten = await response.json();

    let inhalt = "";
    if (daten.objekt) {
      inhalt += `<div class="objekt-badge">Gefiltert auf Objekt: ${escapeHtml(daten.objekt)}</div>`;
    }
    inhalt += `<div class="blase">${escapeHtml(daten.antwort)}</div>`;
    if (daten.quellen && daten.quellen.length > 0) {
      inhalt += `<div class="quellen">${daten.quellen
        .map(
          (q) =>
            `<span class="quelle-chip">${escapeHtml(q.dateiname)} · ${(q.score * 100).toFixed(0)}%</span>`
        )
        .join("")}</div>`;
    }
    inhalt += `<button class="kopieren-button" data-antwort="${escapeHtml(daten.antwort)}">Kopieren</button>`;
    ladeBlase.innerHTML = inhalt;
  } catch (fehler) {
    ladeBlase.innerHTML = `<div class="blase">Entschuldigung, es ist ein Fehler aufgetreten: ${escapeHtml(fehler.message)}</div>`;
  } finally {
    eingabe.disabled = false;
    sendenButton.disabled = false;
    eingabe.focus();
  }
}

formular.addEventListener("submit", (ereignis) => {
  ereignis.preventDefault();
  const frage = eingabe.value.trim();
  if (!frage) return;
  eingabe.value = "";
  frageSenden(frage);
});

document.querySelectorAll(".beispiel-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    frageSenden(chip.textContent);
  });
});

// Event-Delegation statt Listener pro Nachricht, da Antwort-Elemente
// erst zur Laufzeit über innerHTML entstehen.
verlauf.addEventListener("click", async (ereignis) => {
  const button = ereignis.target.closest(".kopieren-button");
  if (!button) return;

  try {
    await navigator.clipboard.writeText(button.dataset.antwort);
    button.textContent = "Kopiert ✓";
    button.disabled = true;
    setTimeout(() => {
      button.textContent = "Kopieren";
      button.disabled = false;
    }, 1500);
  } catch (fehler) {
    button.textContent = "Kopieren fehlgeschlagen";
  }
});

// --- Tabs ---

document.querySelectorAll(".tab-button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab-button").forEach((b) => b.classList.remove("aktiv"));
    document.querySelectorAll(".tab-inhalt").forEach((t) => t.classList.remove("aktiv"));
    button.classList.add("aktiv");
    document.getElementById(`${button.dataset.tab}-tab`).classList.add("aktiv");
  });
});

// --- Upload ---

const objektEingabe = document.getElementById("objekt-eingabe");
const objektListe = document.getElementById("objekt-liste");
const dropZone = document.getElementById("drop-zone");
const dateiEingabe = document.getElementById("datei-eingabe");
const dateiListeEl = document.getElementById("datei-liste");
const uploadButton = document.getElementById("upload-button");

let ausgewaehlteDateien = [];

async function bekannteObjekteLaden() {
  try {
    const response = await fetch("/api/objekte");
    const objekte = await response.json();
    objektListe.innerHTML = objekte
      .map((name) => `<option value="${escapeHtml(name)}"></option>`)
      .join("");
  } catch (fehler) {
    // Datalist ist nur eine Komfortfunktion — bei Fehler einfach leer lassen.
  }
}

function uploadButtonAktualisieren() {
  uploadButton.disabled =
    ausgewaehlteDateien.length === 0 || objektEingabe.value.trim() === "";
}

function dateiListeRendern() {
  dateiListeEl.innerHTML = ausgewaehlteDateien
    .map(
      (eintrag, index) => `
      <li class="datei-eintrag" data-index="${index}">
        <span class="name">${escapeHtml(eintrag.datei.name)}</span>
        <span class="status ${eintrag.statusKlasse || ""}">${eintrag.status}</span>
        ${eintrag.entfernbar ? '<button class="entfernen" aria-label="Entfernen">×</button>' : ""}
      </li>`
    )
    .join("");
  uploadButtonAktualisieren();
}

function dateienHinzufuegen(fileList) {
  Array.from(fileList).forEach((datei) => {
    if (datei.type !== "application/pdf") return;
    const bereitsDrin = ausgewaehlteDateien.some(
      (e) => e.datei.name === datei.name && e.datei.size === datei.size
    );
    if (bereitsDrin) return;
    ausgewaehlteDateien.push({ datei, status: "wartet", statusKlasse: "", entfernbar: true });
  });
  dateiListeRendern();
}

dropZone.addEventListener("click", () => dateiEingabe.click());

dateiEingabe.addEventListener("change", () => {
  dateienHinzufuegen(dateiEingabe.files);
  dateiEingabe.value = "";
});

["dragover", "dragenter"].forEach((ereignis) => {
  dropZone.addEventListener(ereignis, (e) => {
    e.preventDefault();
    dropZone.classList.add("ueber-ziel");
  });
});

["dragleave", "dragend"].forEach((ereignis) => {
  dropZone.addEventListener(ereignis, () => dropZone.classList.remove("ueber-ziel"));
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("ueber-ziel");
  dateienHinzufuegen(e.dataTransfer.files);
});

dateiListeEl.addEventListener("click", (e) => {
  const button = e.target.closest(".entfernen");
  if (!button) return;
  const index = Number(button.closest(".datei-eintrag").dataset.index);
  ausgewaehlteDateien.splice(index, 1);
  dateiListeRendern();
});

objektEingabe.addEventListener("input", uploadButtonAktualisieren);

uploadButton.addEventListener("click", async () => {
  const objektName = objektEingabe.value.trim();
  if (!objektName || ausgewaehlteDateien.length === 0) return;

  uploadButton.disabled = true;
  ausgewaehlteDateien.forEach((e) => {
    e.status = "wird hochgeladen …";
    e.entfernbar = false;
  });
  dateiListeRendern();

  const formData = new FormData();
  formData.append("objekt_name", objektName);
  ausgewaehlteDateien.forEach((e) => formData.append("dateien", e.datei));

  try {
    const response = await fetch("/api/upload", { method: "POST", body: formData });
    if (!response.ok) {
      throw new Error(`Server-Fehler (${response.status})`);
    }
    const daten = await response.json();
    // Pro Datei einzeln auswerten statt pauschal "erledigt": der Server
    // meldet einzelne fehlerhafte Dateien (z.B. beschädigtes PDF) jetzt
    // mit HTTP 200 und einem fehler-Feld je Datei, statt den ganzen
    // Batch mit 500 abzubrechen. Reihenfolge entspricht der Upload-
    // Reihenfolge.
    daten.hochgeladen.forEach((ergebnis, index) => {
      const eintrag = ausgewaehlteDateien[index];
      if (!eintrag) return;
      if (ergebnis.fehler) {
        eintrag.status = ergebnis.fehler;
        eintrag.statusKlasse = "fehler";
      } else {
        eintrag.status = "erledigt";
        eintrag.statusKlasse = "erledigt";
      }
    });
    await bekannteObjekteLaden();
    await objekteUebersichtLaden();
  } catch (fehler) {
    ausgewaehlteDateien.forEach((e) => {
      e.status = "Fehler";
      e.statusKlasse = "fehler";
    });
  }

  dateiListeRendern();
});

bekannteObjekteLaden();

// --- Objekt-Übersicht ---

const objekteListeBereich = document.getElementById("objekte-liste-bereich");

// Reihenfolge der Dokumenttypen, aus denen ein Feld bevorzugt entnommen
// wird, wenn mehrere Quellen einen Wert dafür haben (z.B. Baujahr eher
// aus dem Energieausweis als aus dem Exposé). Nennen zwei Quellen
// unterschiedliche Werte, wird das trotzdem als Widerspruch markiert —
// die Priorität entscheidet nur, welcher Wert prominent angezeigt wird.
const FELD_PRIORITAET = {
  kaufpreis_eur: ["expose"],
  wohnflaeche_qm: ["expose", "energieausweis"],
  zimmer: ["expose"],
  etage: ["expose", "teilungserklaerung"],
  hausgeld_eur_monatlich: ["expose"],
  baujahr: ["energieausweis", "expose"],
  energieeffizienzklasse: ["energieausweis"],
};

const FELDER = [
  { key: "kaufpreis_eur", label: "Kaufpreis", format: (v) => `${Number(v).toLocaleString("de-DE")} €` },
  { key: "wohnflaeche_qm", label: "Wohnfläche", format: (v) => `${v} m²` },
  { key: "zimmer", label: "Zimmer", format: (v) => `${v}` },
  { key: "baujahr", label: "Baujahr", format: (v) => `${v}` },
  { key: "energieeffizienzklasse", label: "Energieeffizienz", format: (v) => v },
  { key: "hausgeld_eur_monatlich", label: "Hausgeld", format: (v) => `${v} €/Monat` },
  { key: "etage", label: "Etage", format: (v) => v },
];

function dokumenttypAusDateiname(dateiname) {
  for (const typ of ["expose", "energieausweis", "protokoll", "teilungserklaerung"]) {
    if (dateiname.includes(typ)) return typ;
  }
  return "unbekannt";
}

function feldZusammenfassen(zeilen, feldKey) {
  const werte = zeilen
    .map((z) => ({ wert: z[feldKey], typ: dokumenttypAusDateiname(z.dateiname) }))
    .filter((w) => w.wert !== null && w.wert !== undefined && w.wert !== "");
  if (werte.length === 0) return null;

  const eindeutigeWerte = new Set(werte.map((w) => String(w.wert)));
  const widerspruch = eindeutigeWerte.size > 1;

  let ausgewaehlt = werte[0];
  for (const typ of FELD_PRIORITAET[feldKey] || []) {
    const treffer = werte.find((w) => w.typ === typ);
    if (treffer) {
      ausgewaehlt = treffer;
      break;
    }
  }

  return { wert: ausgewaehlt.wert, widerspruch, alleWerte: werte };
}

function kennzahlenFelderHtml(zeilen) {
  return FELDER.map((feld) => {
    const ergebnis = feldZusammenfassen(zeilen, feld.key);
    if (!ergebnis) return "";
    const titel = ergebnis.widerspruch
      ? `Abweichende Angaben: ${ergebnis.alleWerte.map((w) => `${w.typ} = ${w.wert}`).join(" · ")}`
      : "";
    return `
      <div class="kennzahl-zeile">
        <span class="kennzahl-label">${escapeHtml(feld.label)}</span>
        <span class="kennzahl-wert"${titel ? ` title="${escapeHtml(titel)}"` : ""}>
          ${escapeHtml(feld.format(ergebnis.wert))}${ergebnis.widerspruch ? ' <span class="widerspruch-marker">⚠</span>' : ""}
        </span>
      </div>`;
  }).join("");
}

function objektKarteRendern(objektName, zeilen) {
  const felderHtml = kennzahlenFelderHtml(zeilen);

  return `
    <button class="objekt-karte" data-objekt="${escapeHtml(objektName)}" type="button">
      <h3>${escapeHtml(objektName)}</h3>
      <div class="kennzahlen-liste">
        ${felderHtml || '<p class="keine-daten">Keine Kennzahlen extrahiert.</p>'}
      </div>
    </button>`;
}

async function objekteUebersichtLaden() {
  try {
    const response = await fetch("/api/kennzahlen");
    const zeilen = await response.json();

    const jeObjekt = {};
    for (const zeile of zeilen) {
      (jeObjekt[zeile.objekt_name] ??= []).push(zeile);
    }

    const objektNamen = Object.keys(jeObjekt).sort();
    if (objektNamen.length === 0) {
      objekteListeBereich.innerHTML = '<p class="objekte-lade-hinweis">Noch keine Objekte vorhanden.</p>';
      return;
    }

    objekteListeBereich.innerHTML = objektNamen
      .map((name) => objektKarteRendern(name, jeObjekt[name]))
      .join("");
  } catch (fehler) {
    objekteListeBereich.innerHTML = '<p class="objekte-lade-hinweis">Objekte konnten nicht geladen werden.</p>';
  }
}

objekteUebersichtLaden();

// --- Objekt-Detailansicht ---

const objektDetailBereich = document.getElementById("objekt-detail-bereich");

function listeAbschnitt(titel, punkte, klasse = "") {
  if (!punkte || punkte.length === 0) return "";
  return `
    <div class="detail-abschnitt ${klasse}">
      <h4>${escapeHtml(titel)}</h4>
      <ul class="detail-liste">
        ${punkte.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}
      </ul>
    </div>`;
}

async function objektDetailAnzeigen(objektName) {
  objekteListeBereich.classList.add("verborgen");
  objektDetailBereich.classList.remove("verborgen");
  objektDetailBereich.innerHTML = `
    <button class="detail-zurueck" type="button">← Zurück zur Übersicht</button>
    <p class="detail-lade-hinweis">Lade Zusammenfassung …</p>`;

  let zusammenfassungHtml = "";
  try {
    const response = await fetch(`/api/zusammenfassung/${encodeURIComponent(objektName)}`);
    if (response.status === 404) {
      zusammenfassungHtml = `
        <div class="detail-abschnitt">
          <p>Für dieses Objekt liegt noch keine Zusammenfassung vor — sie wird
          nach dem nächsten Dokumenten-Upload für dieses Objekt automatisch
          erstellt.</p>
        </div>`;
    } else if (!response.ok) {
      throw new Error(`Server-Fehler (${response.status})`);
    } else {
      const z = await response.json();
      const gekuerztBadge = z.text_gekuerzt
        ? `<span class="gekuerzt-badge" title="Das Objekt hat sehr viele Dokumente — die Zusammenfassung basiert nur auf einem Teil davon.">Teilweise erfasst</span>`
        : "";
      zusammenfassungHtml = `
        <div class="detail-kopf">
          <h2>${escapeHtml(objektName)}</h2>
          ${gekuerztBadge}
        </div>
        <div class="detail-abschnitt">
          <h4>Überblick</h4>
          <p>${escapeHtml(z.kurzueberblick)}</p>
        </div>
        ${listeAbschnitt("Eckdaten", z.eckdaten)}
        ${listeAbschnitt("Besonderheiten", z.besonderheiten)}
        ${listeAbschnitt("Offene Punkte & Widersprüche", z.offene_punkte, "offene-punkte")}`;
    }
  } catch (fehler) {
    zusammenfassungHtml = `<div class="detail-abschnitt"><p>Zusammenfassung konnte nicht geladen werden.</p></div>`;
  }

  let kennzahlenHtml = "";
  try {
    const response = await fetch(`/api/kennzahlen/${encodeURIComponent(objektName)}`);
    const zeilen = await response.json();
    const felderHtml = kennzahlenFelderHtml(zeilen);
    if (felderHtml) {
      kennzahlenHtml = `
        <div class="detail-abschnitt">
          <h4>Kennzahlen (je Dokument geprüft)</h4>
          <div class="kennzahlen-liste">${felderHtml}</div>
        </div>`;
    }
  } catch (fehler) {
    // Kennzahlen sind eine Zusatzinfo unterhalb der Zusammenfassung -- bei
    // einem Fehler hier einfach weglassen, die Zusammenfassung bleibt sichtbar.
  }

  objektDetailBereich.innerHTML = `
    <button class="detail-zurueck" type="button">← Zurück zur Übersicht</button>
    ${zusammenfassungHtml}
    ${kennzahlenHtml}`;
}

objekteListeBereich.addEventListener("click", (ereignis) => {
  const karte = ereignis.target.closest(".objekt-karte");
  if (!karte) return;
  objektDetailAnzeigen(karte.dataset.objekt);
});

objektDetailBereich.addEventListener("click", (ereignis) => {
  if (!ereignis.target.closest(".detail-zurueck")) return;
  objektDetailBereich.classList.add("verborgen");
  objektDetailBereich.innerHTML = "";
  objekteListeBereich.classList.remove("verborgen");
});
