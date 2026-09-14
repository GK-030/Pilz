# Pilzwetter

Zwei Werkzeuge für eine Frage: **lohnt sich der Weg in den Wald, und
wenn nicht, wann?**

Der Kern ist eine standortbezogene Bodenwasserbilanz. Flächengemittelte
Feuchteindizes rechnen auf Kilometerraster und sind auf mittlere Böden
geeicht — auf einem Sander mit 35 mm nutzbarer Feldkapazität bedeutet
„für die Jahreszeit übliche Bodenfeuchte" etwas anderes als auf
Geschiebemergel mit 68 mm. Genau diese Verwechslung produziert
Fehlprognosen.

---

## Was hier drin ist

| Datei | Was sie tut | Läuft wo |
|---|---|---|
| `index.html` | Web-App: Urteil, Karte, Fundbuch | Browser, Handy |
| `screening.py` | Potenzialkarte für eine ganze Region | Rechner |
| `stok.py` | Übersetzt die amtliche Standortskarte in nFK | Rechner |
| `pilzindex.py` | Kommandozeilen-Variante des Modells | Rechner |
| `geo_info.py` | Liest Struktur großer Geodaten, ohne sie zu laden | Rechner |
| `bb_geodaten.py` | Schutzgebiets- und Biotopabfrage (**ungetestet**) | Rechner |
| `daten/wald_brandenburg.gpkg` | Waldmaske Brandenburg, 62.609 Flächen | — |
| `beispielkarte-glatt.html` | Wie die Flächenkarte aussieht (Testdaten) | Browser |
| `beispielkarte-waldmaske.html` | Dasselbe mit echter Waldmaske | Browser |
| `MODELL.md` | Technische Beschreibung des Rechenmodells | — |

---

## Die Frage nach der Heatmap — ehrliche Antwort

Es gibt **zwei** Karten, und sie können nicht dasselbe.

### Die Karte in der App

Läuft live im Browser: rechnet 121 Rasterzellen aus dem Wetter, blendet
Naturschutzgebiete als WMS ein, und bewertet jeden angetippten Punkt mit
der Landnutzung aus OpenStreetMap.

**Aber:** flächige Bodendaten sind im Browser nicht zu bekommen.
SoilGrids erlaubt rund fünf Abfragen pro Minute, die Landesdienste
liefern per WMS Bilder statt Werte, WFS scheitert an CORS. Das Raster
rechnet deshalb mit **einer** Bodenannahme für die ganze Fläche. Es ist
eine Niederschlagskarte — nützlich, um zu sehen, wo der Regen angekommen
ist, aber keine Standortbewertung.

### Die Karte aus `screening.py`

Verschneidet Wetter, **amtliche Bodendaten**, Waldmaske und
Schutzgebiete, interpoliert das Ergebnis zu einer glatten Dichtefläche
und schreibt eine eigenständige HTML-Datei. **Das ist die Karte, die du
willst** — sie braucht aber die Bodendaten als Datei und läuft daher auf
dem Rechner.

### Und damit zur Antwort

Ja, du bekommst eine Internetseite mit dieser Karte. `screening.py`
schreibt eine fertige HTML-Datei; die legst du ins Repository und sie ist
unter deiner Adresse erreichbar wie jede andere Seite.

**Der Haken:** Sie ist eine **Momentaufnahme** vom Tag des Durchlaufs.
Morgen stimmt sie nicht mehr. Entweder du erzeugst sie neu, wenn du sie
brauchst — oder du gehst den Weg über GitHub Actions unten.

---

## Auf GitHub einrichten

### 1. Die App

```
Repository → Add file → Upload files → index.html
Settings → Pages → Deploy from a branch → main → / (root)
```

Nach einer Minute läuft sie unter `https://DEINNAME.github.io/REPO/`.
Im Safari öffnen, Teilen → „Zum Home-Bildschirm", und du hast ein Icon.

Unter „Feinjustierung" steht unten, welche Fassung läuft und welcher
Speicher greift. Zeigt der Browser eine alte Version: privates Tab —
GitHub Pages wird von Safari hartnäckig gecacht.

### 2. Die Potenzialkarte

```bash
pip install requests pyyaml geopandas pyogrio pyproj

python3 screening.py \
    --bbox 13.0,51.85,14.05,52.45 \
    --arten Marone,Steinpilz \
    --waldmaske daten/wald_brandenburg.gpkg \
    --schutz schutzgebiete.gpkg \
    --out karte
```

Ergebnis: `karte.html` für die Seite, `karte.gpkg` für QGIS,
`karte.csv` nach Index sortiert. Die HTML ins Repository, und sie liegt
unter `https://DEINNAME.github.io/REPO/karte.html`.

Ohne `--boden` ist das Ergebnis eine Wetterkarte — das Skript warnt beim
Start. Für die Bodenebene fehlt noch die Standortskarte als Datei; siehe
„Nächster Schritt".

### 3. Wenn die Karte sich selbst aktualisieren soll

Eine GitHub-Action kann das Skript täglich ausführen und das Ergebnis
einchecken. Datei `.github/workflows/karte.yml`:

```yaml
name: Potenzialkarte aktualisieren
on:
  schedule:
    - cron: "30 4 * * *"
  workflow_dispatch:

jobs:
  karte:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests pyyaml geopandas pyogrio pyproj
      - run: |
          python3 screening.py \
            --bbox 13.0,51.85,14.05,52.45 \
            --arten Marone,Steinpilz \
            --waldmaske daten/wald_brandenburg.gpkg \
            --out karte
      - run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add karte.html karte.csv
          git diff --quiet --staged || git commit -m "Karte aktualisiert"
          git push
```

**Ungetestet** — ich konnte das hier nicht laufen lassen. Absehbare
Stolpersteine: GeoPandas braucht ein paar Minuten Installationszeit, und
die Bodendatei muss im Repository liegen (unter 100 MB, sonst Git LFS).
Die Waldmaske mit 52 MB passt.

---

## Nächster Schritt

Der letzte fehlende Baustein ist die **Standortskarte als Datei**. Der
Dienst liefert sie unter
`brandenburg-forst.de/ogc/stok`, Layer `stok_fskf`; in QGIS als WMS
aufnehmbar, der Download liegt als GML bereit. Feldbedeutung ist geklärt:

| Feld | Inhalt |
|---|---|
| `stgr` | Leitgruppe der Fläche |
| `nfgr1..4` | Stamm-Standortsgruppen (Nährkraft-Feuchtegruppen) |
| `az1..3` | Flächenanteile in **Zehnteln**, nicht Prozent |
| `bffg1..3` | Feinbodenformen mit Grundwasserstufen |
| `kf_gesamt` | Gesamt-Klimafeuchte (`t` trocken … `n` nass) |

`stok.py` übersetzt das in nFK. Geprüft an allen Kürzeln der
Dienstlegende. Für Sperenberg: `Z2` mit Klimafeuchte `t` ergibt **35 mm**.

---

## Wo die Daten herkommen

| Ebene | Quelle | Lizenz |
|---|---|---|
| Wetter, Verdunstung | Open-Meteo | frei |
| Bodenfeuchte 9–27 cm | Open-Meteo (ICON-Modellwert, **keine Messung**) | frei |
| Waldflächen | Forstgrundkarte, Landesbetrieb Forst Brandenburg | dl-de/by-2.0 |
| Standortskarte | `brandenburg-forst.de/ogc/stok` | dl-de/by-2.0 |
| Schutzgebiete, Biotope | LfU Brandenburg, INSPIRE | siehe Dienst |
| Bewuchs am Punkt | OpenStreetMap / Overpass | ODbL |

Bei Nutzung der Forstdaten ist **„© Landesbetrieb Forst Brandenburg"**
als Bereitsteller anzugeben — das fordern die Zugangsbedingungen des
Dienstes ausdrücklich.

---

## Was am Modell noch wackelt

**Die Kalibrierung ist unbelegt.** Feuchtebedarf, Temperaturfenster und
besonders die Latenzzeit sind Literatur- und Erfahrungswerte, keine
gemessenen Parameter. Am 13.09. zeigte das Modell für Sperenberg Index 0,
während dort 6 kg Maronen im Korb landeten. Drei Hypothesen, unaufgelöst:

1. ~~Bodenannahme zu trocken~~ — **geprüft und verworfen.** Der WFS
   `bowassverh` des LBGR stuft den Standort als „sehr gering" ein
   (< 6 Vol.%, mit kapillarem Aufstieg bis „gering"), umgerechnet rund
   30 mm. Die App rechnete mit 29 mm — das war richtig. Meine Ableitung
   aus `Z2` mit 35 mm liegt eher zu hoch.
2. **Das Wettermodell verfehlt lokale Gewitterzellen.**
3. **Die Latenzzeit ist zu lang angesetzt.**

Nach der Prüfung tragen 2 und 3 das Gewicht. Beide lassen sich nur über
das Fundbuch trennen.

Das Fundbuch schreibt Regensummen, Bodenzustand und Latenzeinstellung
mit. Nach einer Saison lässt sich daran trennen, welche zutrifft — das
ist der Punkt, an dem das Werkzeug den generischen Tickern überlegen
wird, weil es dann auf deine Standorte geeicht ist.

**Die Fläche ist interpoliert, nicht gemessen.** Zwischen zwei
Stützpunkten steht eine Rechenannahme.

**Der Index ist eine Momentaufnahme.** Weil er den *relativen* Füllgrad
bewertet, punkten Sander nach Regen höher als Lehm — sie fruchten dann
auch wirklich, fallen aber als erste wieder aus. Für „wo lohnt ein neuer
Stammplatz" braucht es mehrere Läufe über eine Saison, nicht einen.

---

## Haftungsausschluss

Die Vorhersage bewertet **Wachstumsbedingungen, nicht Essbarkeit**. Was
du findest, musst du selbst sicher bestimmen; unklare Funde gehören zur
Pilzberatung. Der Landesverband der Pilzsachverständigen Brandenburg
vermittelt Berater.

Sammeln ist nur in geringer Menge für den Eigenbedarf erlaubt
(§ 15 LWaldG Bbg); eine Kilogrammgrenze nennt das Gesetz nicht. In
Naturschutzgebieten ist es untersagt. Auf ehemaligen Militärflächen ist
mit Munitionsbelastung zu rechnen. Die eingeblendeten Schutzgebiete sind
eine Übersichtsdarstellung ohne Rechtsverbindlichkeit — maßgeblich ist
die jeweilige Schutzgebietsverordnung.
