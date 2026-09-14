#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screening — flaechenhafte Pilz-Potenzialkarte fuer eine Region.

Unterschied zu pilzindex.py
---------------------------
pilzindex bewertet Standorte, die man ihm vorgibt. screening sucht
Standorte: es legt ein Raster ueber eine Region, bewertet jede Zelle
und gibt eine Karte aus.

Aufloesungsarchitektur — der entscheidende Punkt
------------------------------------------------
Wetter und Boden variieren auf voellig verschiedenen Skalen:

  Wetter : glatt ueber ~10 km. Ein grobes Raster genuegt, und mehr
           waere reine Rechenlast ohne Informationsgewinn.
  Boden  : variiert ueber Meter. Hier braucht es die echten Polygone.

Deshalb zwei Raster: ein grobes Wetterraster (Vorgabe 0,1 Grad, rund
7 x 11 km in Brandenburg) und ein feines Bewertungsraster, das seine
nFK aus der Bodenkarte zieht und das Wetter der naechsten
Wetterrasterzelle uebernimmt. Open-Meteo nimmt Mehrfachkoordinaten
kommagetrennt an, ein Landkreis kostet damit wenige Abfragen.

Datengrundlagen
---------------
Boden (eine der drei Moeglichkeiten, in aufsteigender Guete):
  a) --boden nicht gesetzt: einheitliche nFK. Dann ist das Ergebnis
     eine WETTERKARTE, keine Standortbewertung. Das Skript warnt.
  b) --boden buek300.gpkg --boden-feld NFK_MM
     Bodenuebersichtskarte BUEK 300, LBGR Brandenburg,
     umweltdaten.brandenburg.de/open-data/boden
  c) --boden standorte.gpkg --boden-feld FEUCHTESTUFE --feld-typ feuchtestufe
     Forstliche Standortserkundung. Fachlich die beste Grundlage fuer
     Waldstandorte. umweltdaten.brandenburg.de/open-data/forst

Wald/Biotop (optional, empfohlen):
  --wald bbk.gpkg --wald-feld BIOTOPTYP
  Biotop- und FFH-Lebensraumtypenkartierung. Ohne diesen Layer werden
  auch Acker und Siedlung bewertet, was die Karte unbrauchbar macht.

Schutzgebiete (optional, empfohlen):
  --schutz schutzgebiete.gpkg
  Natur-, Landschafts- und Grossschutzgebiete. NSG-Flaechen werden
  ausmaskiert, weil dort das Sammeln untersagt ist.

Ausgabe
-------
  <name>.gpkg  Rasterzellen mit allen Kennwerten, fuer QGIS
  <name>.html  eigenstaendige Leaflet-Karte, laeuft auch auf dem Handy
  <name>.csv   Tabelle, nach Index sortiert

Aufruf
------
    # Teltow-Flaeming und Dahme-Spreewald, Marone und Steinpilz
    python3 screening.py \\
        --bbox 13.0,51.85,14.05,52.45 \\
        --aufloesung 0.02 \\
        --arten Marone,Steinpilz \\
        --boden buek300.gpkg --boden-feld NFK_MM \\
        --wald bbk.gpkg --wald-feld BIOTOPTYP \\
        --schutz schutzgebiete.gpkg \\
        --out screening_tf

    # Ohne Netz und ohne Fachdaten, nur um die Mechanik zu sehen
    python3 screening.py --bbox 13.2,52.0,13.6,52.3 --selftest --out probe

Ohne Gewaehr. Der Index bewertet Wachstumsbedingungen, nicht
Essbarkeit, und keine Betretungsrechte.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

# Modell aus pilzindex nachnutzen, damit es nur eine Wahrheit gibt.
try:
    from pilzindex import (ARTEN, BODEN_KLASSEN_FALLBACK)  # type: ignore
except Exception:
    ARTEN = None  # wird unten lokal definiert

if ARTEN is None:
    try:
        import pilzindex as _pi
        ARTEN = _pi.ARTEN
        BODENKLASSEN = _pi.BODENKLASSEN
        GRUNDWASSER = _pi.GRUNDWASSER_ZUSCHLAG_MM
        INTERZEPTION = _pi.INTERZEPTION
        KC = _pi.KC_BESTAND
        bilanziere = _pi.bilanziere
        feuchtefaktor = _pi.feuchtefaktor
        temperaturfaktor = _pi.temperaturfaktor
        trendfaktor = _pi.trendfaktor
        normiere_modellfeuchte = _pi.normiere_modellfeuchte
    except Exception as exc:
        print("[!] pilzindex.py muss im selben Ordner liegen "
              f"({exc})", file=sys.stderr)
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Biotoptyp-Bewertung
# ---------------------------------------------------------------------------

# Flaechen, die gar nicht bewertet werden. Ohne diesen Filter landen
# Acker, Siedlung und Gewaesser in der Karte und verwaessern sie.
AUSSCHLUSS = (
    "acker", "siedlung", "bebau", "verkehr", "strasse", "straße",
    "gewaesser", "gewässer", "see", "fluss", "abbau", "halde",
    "gruenland intensiv", "grünland intensiv", "garten", "park",
    "sportanlage", "deponie", "industrie",
)

WALD_KENNUNG = ("wald", "forst", "kiefer", "fichte", "buche", "eiche",
                "birke", "erle", "gehoelz", "gehölz", "bruch")


def biotop_bewertung(text: str) -> dict[str, object] | None:
    """
    Ordnet einem kartierten Biotoptyp Bestand und Habitat zu.
    Gibt None zurueck, wenn die Flaeche nicht bewertet werden soll.
    """
    t = (text or "").lower()
    if not t:
        return None
    if any(a in t for a in AUSSCHLUSS):
        return None

    # Mischbestaende zuerst, sonst greift die Reinbestandsregel
    if any(s in t for s in ("kiefern-eichen", "eichen-kiefern",
                            "laubmischwald", "nadelmischwald",
                            "mischwald", "kiefern-birken")):
        bestand = "Mischwald"
    elif any(s in t for s in ("erlen", "erlenbruch", "esche")):
        bestand = "Erle"
    elif any(s in t for s in ("moor", "torf", "bruchwald")):
        bestand = "Erle"
    elif any(s in t for s in ("kiefer", "nadelforst")):
        bestand = "Kiefer"
    elif "fichte" in t:
        bestand = "Fichte"
    elif any(s in t for s in ("buche", "hainsimsen", "waldmeister")):
        bestand = "Buche"
    elif "eiche" in t:
        bestand = "Eiche"
    elif "birke" in t:
        bestand = "Birke"
    else:
        bestand = "Mischwald"

    if any(s in t for s in WALD_KENNUNG):
        habitat = "Wald"
    elif any(s in t for s in ("saum", "rand", "vorwald", "hecke",
                              "gebuesch", "gebüsch")):
        habitat = "Waldrand"
    elif any(s in t for s in ("gruenland", "grünland", "wiese", "weide",
                              "rasen", "heide", "trockenrasen")):
        habitat = "Grünland"
    else:
        return None

    # nFK-Hinweis, falls keine Bodenkarte vorliegt
    boden = None
    if any(s in t for s in ("moor", "torf", "bruch")):
        boden = "niedermoor"
    elif any(s in t for s in ("feucht", "nass", "quell")):
        boden = "anmoor"
    elif any(s in t for s in ("duene", "düne", "sandtrocken",
                              "flechten", "silbergras")):
        boden = "reinsand"

    return {"bestand": bestand, "habitat": habitat,
            "bodenklasse_hinweis": boden}


# ---------------------------------------------------------------------------
# Raster
# ---------------------------------------------------------------------------

def raster(bbox: tuple[float, float, float, float],
           schritt: float) -> list[tuple[float, float]]:
    """Zellmittelpunkte in WGS84. bbox = (lon_min, lat_min, lon_max, lat_max)."""
    lon0, lat0, lon1, lat1 = bbox
    punkte: list[tuple[float, float]] = []
    lat = lat0 + schritt / 2
    while lat < lat1:
        lon = lon0 + schritt / 2
        while lon < lon1:
            punkte.append((round(lat, 6), round(lon, 6)))
            lon += schritt
        lat += schritt
    return punkte


def naechste(punkt: tuple[float, float],
             kandidaten: list[tuple[float, float]]) -> int:
    """Index des naechstgelegenen Wetterrasterpunkts (ebene Näherung)."""
    lat, lon = punkt
    kf = math.cos(math.radians(lat))
    best, bi = float("inf"), 0
    for i, (a, b) in enumerate(kandidaten):
        d = (a - lat) ** 2 + ((b - lon) * kf) ** 2
        if d < best:
            best, bi = d, i
    return bi


# ---------------------------------------------------------------------------
# Wetter, gebuendelt
# ---------------------------------------------------------------------------

TAGESGROESSEN = ("precipitation_sum", "et0_fao_evapotranspiration",
                 "temperature_2m_mean")


def hole_wetter_buendel(punkte: list[tuple[float, float]],
                        past_days: int = 60,
                        forecast_days: int = 16,
                        pro_anfrage: int = 25) -> list[dict]:
    """
    Holt Tageswerte fuer viele Punkte. Open-Meteo nimmt Koordinaten
    kommagetrennt an und liefert dann eine Liste von Strukturen.
    Bodenfeuchte wird hier NICHT abgefragt: die Stundenwerte vieler
    Punkte sprengen die Antwortgroesse, und das Speichermodell traegt
    die Bilanz ohnehin.
    """
    if requests is None:
        raise RuntimeError("Modul 'requests' fehlt: pip install requests")

    raus: list[dict] = []
    for i in range(0, len(punkte), pro_anfrage):
        block = punkte[i:i + pro_anfrage]
        params = {
            "latitude": ",".join(f"{a:.4f}" for a, _ in block),
            "longitude": ",".join(f"{b:.4f}" for _, b in block),
            "daily": ",".join(TAGESGROESSEN),
            "past_days": str(min(past_days, 92)),
            "forecast_days": str(min(forecast_days, 16)),
            "timezone": "Europe/Berlin",
        }
        antwort = requests.get("https://api.open-meteo.com/v1/forecast",
                               params=params, timeout=90)
        antwort.raise_for_status()
        j = antwort.json()
        teil = j if isinstance(j, list) else [j]
        if len(teil) != len(block):
            raise RuntimeError(
                f"Antwort passt nicht: {len(teil)} Strukturen "
                f"fuer {len(block)} Punkte"
            )
        raus.extend(teil)
        print(f"    Wetter {min(i + pro_anfrage, len(punkte))}"
              f"/{len(punkte)} Rasterpunkte", file=sys.stderr)
    return raus


def synth_wetter(n: int, past_days: int, forecast_days: int) -> list[dict]:
    """Synthetische Wetterdaten fuer --selftest, leicht ortsvariabel."""
    heute = date.today()
    start = heute - timedelta(days=past_days)
    tage = [(start + timedelta(days=k)).isoformat()
            for k in range(past_days + forecast_days)]
    raus = []
    for idx in range(n):
        p, e, t = [], [], []
        for k, tag in enumerate(tage):
            rel = (date.fromisoformat(tag) - heute).days
            # Ortsversatz, damit die Karte Struktur zeigt
            phase = (idx % 7) - 3
            regen = 0.0
            if rel < -24:
                regen = 3.0 if k % 9 == 0 else 0.0
            elif rel < -16 + phase:
                regen = 8.0 if k % 2 else 4.0
            elif rel < 0:
                regen = 2.5 if k % 3 else 0.0
            else:
                regen = 4.0 if k % 4 else 0.5
            temp = 16.0 + (idx % 5) * 0.4
            p.append(regen)
            t.append(temp)
            e.append(round(max(0.6, 0.16 * temp - 0.4), 2))
        raus.append({"daily": {
            "time": tage, "precipitation_sum": p,
            "et0_fao_evapotranspiration": e, "temperature_2m_mean": t,
        }})
    return raus


# ---------------------------------------------------------------------------
# Bewertung einer Zelle
# ---------------------------------------------------------------------------

def passt(art, bestand: str, habitat: str) -> bool:
    typ = getattr(art, "typ", "mykorrhiza")
    if typ == "saprotroph":
        return habitat in getattr(art, "habitate", ())
    if habitat == "Grünland":
        return False
    baeume = getattr(art, "baeume", ())
    if not baeume:
        return True
    return bestand in baeume or bestand == "Mischwald"


def bewerte_zelle(wetter: dict, nfk: float, bestand: str, habitat: str,
                  arten_namen: set[str]) -> dict[str, object]:
    d = wetter["daily"]
    tage = d["time"]
    p = d["precipitation_sum"]
    e = d["et0_fao_evapotranspiration"]
    tm = d["temperature_2m_mean"]

    fuell = bilanziere(tage, p, e, nfk, bestand)

    bil14 = []
    for i in range(len(tage)):
        a = max(0, i - 13)
        bil14.append(sum(x or 0 for x in p[a:i + 1])
                     - sum(x or 0 for x in e[a:i + 1]))

    heute = date.today().isoformat()
    try:
        idx = tage.index(heute)
    except ValueError:
        idx = len(tage) - 1

    beste, bester_wert = None, 0.0
    je_art: dict[str, float | None] = {}
    for art in ARTEN:
        if art.name not in arten_namen:
            continue
        monat = int(tage[idx][5:7])
        if monat not in art.monate or not passt(art, bestand, habitat):
            je_art[art.name] = None
            continue
        j = max(0, idx - art.lag_tage)
        fenster = fuell[max(0, j - 6): j + 1] or [fuell[j]]
        f_lag = sum(fenster) / len(fenster)
        t_f = [x for x in tm[max(0, j - 6): idx + 1] if x is not None]
        t_lag = sum(t_f) / len(t_f) if t_f else 0.0
        wert = min(100.0, 100.0 * feuchtefaktor(f_lag, art)
                   * temperaturfaktor(t_lag, art)
                   * trendfaktor(bil14[idx]))
        je_art[art.name] = round(wert, 1)
        if wert > bester_wert:
            beste, bester_wert = art.name, wert

    # Bestes Fenster in den naechsten 16 Tagen
    fenster_ab = None
    for k in range(idx, len(tage)):
        monat = int(tage[k][5:7])
        tages_max = 0.0
        for art in ARTEN:
            if art.name not in arten_namen:
                continue
            if monat not in art.monate or not passt(art, bestand, habitat):
                continue
            j = max(0, k - art.lag_tage)
            fe = fuell[max(0, j - 6): j + 1] or [fuell[j]]
            f_lag = sum(fe) / len(fe)
            t_f = [x for x in tm[max(0, j - 6): k + 1] if x is not None]
            t_lag = sum(t_f) / len(t_f) if t_f else 0.0
            tages_max = max(tages_max, 100.0 * feuchtefaktor(f_lag, art)
                            * temperaturfaktor(t_lag, art)
                            * trendfaktor(bil14[k]))
        if tages_max >= 40.0:
            fenster_ab = tages_max and tage[k]
            break

    return {
        "index": round(bester_wert, 1),
        "beste_art": beste,
        "fuellgrad": round(fuell[idx], 3),
        "bilanz14_mm": round(bil14[idx], 1),
        "fenster_ab": fenster_ab,
        "je_art": je_art,
    }


# ---------------------------------------------------------------------------
# Fachdaten
# ---------------------------------------------------------------------------

def lade_layer(pfad: str):
    import geopandas as gpd
    g = gpd.read_file(pfad)
    if g.crs is None:
        raise RuntimeError(f"{pfad} hat kein CRS. In QGIS setzen.")
    return g.to_crs("EPSG:4326")


def verschneide(punkte, layer, feld):
    """Ordnet jedem Punkt den Attributwert der ueberlagernden Flaeche zu."""
    import geopandas as gpd
    from shapely.geometry import Point

    if feld not in layer.columns:
        raise RuntimeError(
            f"Feld '{feld}' fehlt. Vorhanden: "
            f"{', '.join(c for c in layer.columns if c != 'geometry')}"
        )
    pg = gpd.GeoDataFrame(
        {"i": range(len(punkte))},
        geometry=[Point(lon, lat) for lat, lon in punkte],
        crs="EPSG:4326",
    )
    treffer = gpd.sjoin(pg, layer[[feld, "geometry"]],
                        how="left", predicate="within")
    treffer = treffer[~treffer.index.duplicated(keep="first")]
    return treffer.sort_values("i")[feld].tolist()


# ---------------------------------------------------------------------------
# Leaflet-Karte
# ---------------------------------------------------------------------------

KARTE = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITEL__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
 html,body{margin:0;height:100%;font:15px/1.45 Archivo,system-ui,sans-serif}
 #karte{position:absolute;inset:0}
 .steuer{position:absolute;z-index:1000;top:10px;left:10px;
   background:rgba(253,252,249,.95);padding:.7rem .9rem;border-radius:3px;
   box-shadow:0 1px 8px rgba(0,0,0,.3);max-width:20rem}
 .steuer h1{font-size:.95rem;margin:0 0 .35rem;font-weight:800}
 .steuer p{margin:0;font-size:.7rem;color:#6b5a42;line-height:1.5}
 .rampe{display:flex;height:10px;margin:.5rem 0 .2rem;border-radius:2px;
   overflow:hidden}
 .rampe i{flex:1}
 .marken{display:flex;justify-content:space-between;font-size:.62rem;
   color:#6b5a42}
 .regler{margin-top:.5rem;font-size:.72rem}
 .regler input{width:100%}
 .leaflet-popup-content{font-size:.8rem;line-height:1.5}
</style></head><body>
<div id="karte"></div>
<div class="steuer">
  <h1>__TITEL__</h1>
  <div class="rampe" id="rampe"></div>
  <div class="marken"><span>0</span><span>20</span><span>40</span>
    <span>65</span><span>100</span></div>
  <div class="regler">
    Glättung <input type="range" id="glatt" min="1" max="30" value="12">
  </div>
  <p>__UNTERTITEL__</p>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATEN = __DATEN__;

/* Farbrampe: Sand (nichts) über Ocker und Grün bis Dunkelgrün.
   Wird als Gradient zwischen Stützstellen interpoliert, damit die
   Fläche wirklich glatt wirkt und keine Stufen zeigt. */
const STUETZ = [
  [  0, [207,200,182]],
  [ 20, [192,138, 30]],
  [ 40, [140,160, 80]],
  [ 65, [110,138, 85]],
  [100, [ 32, 66, 24]],
];
function rampe(v){
  v = Math.max(0, Math.min(100, v));
  for(let i=1;i<STUETZ.length;i++){
    if(v <= STUETZ[i][0]){
      const [a,ca]=STUETZ[i-1], [b,cb]=STUETZ[i];
      const t=(v-a)/(b-a);
      return ca.map((c,k)=>Math.round(c+(cb[k]-c)*t));
    }
  }
  return STUETZ[STUETZ.length-1][1];
}
document.getElementById("rampe").innerHTML =
  Array.from({length:40},(_,i)=>{
    const c=rampe(i/39*100);
    return `<i style="background:rgb(${c[0]},${c[1]},${c[2]})"></i>`;
  }).join("");

const karte = L.map("karte");
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:17, opacity:.85, attribution:"&copy; OpenStreetMap"}).addTo(karte);

/* Glatte Fläche per inverser Distanzwichtung auf einem Canvas.
   Im Unterschied zu Kacheln entsteht so eine durchgehende
   Dichteverteilung — genau die Optik einer Verbreitungskarte.
   Zellen ohne Bewertung (Acker, Siedlung, NSG) fehlen in DATEN und
   ziehen die Fläche daher nicht nach unten: sie bleiben transparent,
   weil dort kein Stützpunkt liegt. */
const FlaechenLayer = L.Layer.extend({
  onAdd(k){
    this._karte = k;
    this._leinwand = L.DomUtil.create("canvas", "leaflet-zoom-animated");
    this._leinwand.style.pointerEvents = "none";
    k.getPanes().overlayPane.appendChild(this._leinwand);
    k.on("moveend zoomend resize", this._zeichnen, this);
    this._zeichnen();
  },
  onRemove(k){
    L.DomUtil.remove(this._leinwand);
    k.off("moveend zoomend resize", this._zeichnen, this);
  },
  setGlaettung(g){ this._g = g; this._zeichnen(); },
  _zeichnen(){
    const k = this._karte, c = this._leinwand;
    const gr = k.getSize(), ob = k.containerPointToLayerPoint([0,0]);
    L.DomUtil.setPosition(c, ob);
    c.width = gr.x; c.height = gr.y;
    const ctx = c.getContext("2d");
    ctx.clearRect(0,0,gr.x,gr.y);

    // Stützpunkte in Bildschirmkoordinaten
    const pkt = DATEN.zellen.map(z=>{
      const p = k.latLngToContainerPoint([z.lat, z.lon]);
      return {x:p.x, y:p.y, v:z.index};
    }).filter(p=>p.x>-260 && p.y>-260 && p.x<gr.x+260 && p.y<gr.y+260);
    if(!pkt.length) return;

    // Suchradius aus dem Zellabstand ableiten, mal Glättungsfaktor
    const a = k.latLngToContainerPoint([DATEN.lat0, DATEN.lon0]);
    const b = k.latLngToContainerPoint([DATEN.lat1, DATEN.lon1]);
    const abstand = Math.max(6, Math.hypot(b.x-a.x, b.y-a.y));
    const radius = abstand * ((this._g || 12) / 6);
    const schritt = 4;                     // Rasterweite der Ausgabe

    const bild = ctx.createImageData(gr.x, gr.y);
    const d = bild.data;
    for(let y=0; y<gr.y; y+=schritt){
      for(let x=0; x<gr.x; x+=schritt){
        let summe=0, gewicht=0, naechste=Infinity;
        for(const p of pkt){
          const dq = (p.x-x)*(p.x-x) + (p.y-y)*(p.y-y);
          if(dq > radius*radius) continue;
          naechste = Math.min(naechste, dq);
          const w = 1/(dq + 25);           // inverse Distanz, gedämpft
          summe += p.v*w; gewicht += w;
        }
        if(!gewicht) continue;
        const v = summe/gewicht;
        const c3 = rampe(v);
        // Randabfall: außerhalb des Stützpunktnetzes ausblenden
        const rand = Math.sqrt(naechste)/radius;
        const alpha = Math.round(205 * Math.max(0, 1 - rand*rand));
        for(let dy=0; dy<schritt && y+dy<gr.y; dy++){
          for(let dx=0; dx<schritt && x+dx<gr.x; dx++){
            const i = ((y+dy)*gr.x + (x+dx))*4;
            d[i]=c3[0]; d[i+1]=c3[1]; d[i+2]=c3[2]; d[i+3]=alpha;
          }
        }
      }
    }
    ctx.putImageData(bild, 0, 0);
  },
});

const flaeche = new FlaechenLayer();
const grenzen = L.latLngBounds(
  [DATEN.bbox[1], DATEN.bbox[0]], [DATEN.bbox[3], DATEN.bbox[2]]);
karte.fitBounds(grenzen);
flaeche.addTo(karte);

document.getElementById("glatt").addEventListener("input", ev=>{
  flaeche.setGlaettung(+ev.target.value);
});

/* Unsichtbare Klickpunkte für die Detailabfrage — die Fläche selbst
   nimmt keine Klicks an, sonst wäre sie nicht durchschaubar. */
const treffer = L.featureGroup().addTo(karte);
for(const z of DATEN.zellen){
  const arten = Object.entries(z.je_art||{})
    .filter(([,v])=>v!=null).sort((a,b)=>b[1]-a[1])
    .map(([n,v])=>`${n} ${Math.round(v)}`).join("<br>") || "keine Art möglich";
  treffer.addLayer(L.circleMarker([z.lat,z.lon],
    {radius:7, stroke:false, fillOpacity:0})
    .bindPopup(
      `<b>Index ${Math.round(z.index)}</b>`+
      (z.beste_art?` — ${z.beste_art}`:"")+"<br>"+
      `${z.bestand} · ${z.habitat}<br>`+
      `Boden ${Math.round(z.nfk)} mm nFK, `+
      `${Math.round(z.fuellgrad*100)} % gefüllt<br>`+
      `Bilanz 14 d ${z.bilanz14_mm>0?"+":""}${z.bilanz14_mm} mm<br>`+
      (z.fenster_ab?`Fenster ab ${z.fenster_ab}<br>`:"")+
      `<br>${arten}<br>`+
      `<span style="font-size:.7rem;color:#6b5a42">`+
      `${z.lat.toFixed(4)}, ${z.lon.toFixed(4)}</span>`));
}
</script></body></html>
"""


def schreibe_karte(pfad: Path, zellen: list[dict], titel: str,
                   untertitel: str, bbox: tuple, schritt: float) -> None:
    """
    Schreibt die glatte Flaechenkarte. Neben den Zellen braucht die
    Darstellung den Zellabstand (fuer den Interpolationsradius) und die
    bbox (fuer den Kartenausschnitt).
    """
    mitte = [(z["lat0"] + z["lat1"]) / 2 for z in zellen]
    daten = {
        "zellen": [
            {**{k: v for k, v in z.items()
                if k not in ("lat0", "lat1", "lon0", "lon1")},
             "lat": round((z["lat0"] + z["lat1"]) / 2, 6),
             "lon": round((z["lon0"] + z["lon1"]) / 2, 6)}
            for z in zellen
        ],
        "bbox": list(bbox),
        # zwei benachbarte Punkte, damit die Darstellung den
        # Zellabstand in Bildschirmpixeln messen kann
        "lat0": zellen[0]["lat0"], "lon0": zellen[0]["lon0"],
        "lat1": zellen[0]["lat0"] + schritt, "lon1": zellen[0]["lon0"],
    }
    html = (KARTE
            .replace("__TITEL__", titel)
            .replace("__UNTERTITEL__", untertitel)
            .replace("__DATEN__", json.dumps(daten)))
    pfad.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Flaechenhafte Pilz-Potenzialkarte fuer eine Region."
    )
    ap.add_argument("--bbox", required=True,
                    help="lon_min,lat_min,lon_max,lat_max in WGS84")
    ap.add_argument("--aufloesung", type=float, default=0.02,
                    help="Bewertungsraster in Grad (0.02 ≈ 1,4 x 2,2 km)")
    ap.add_argument("--wetterraster", type=float, default=0.10,
                    help="Wetterraster in Grad (Vorgabe 0.10 ≈ 7 x 11 km)")
    ap.add_argument("--arten", default="Marone,Steinpilz,Pfifferling")
    ap.add_argument("--boden"); ap.add_argument("--boden-feld")
    ap.add_argument("--feld-typ", choices=["nfk_mm", "feuchtestufe",
                                           "bodenklasse"],
                    default="nfk_mm")
    ap.add_argument("--wald"); ap.add_argument("--wald-feld")
    ap.add_argument("--waldmaske",
                    help="Waldflaechen OHNE Attribute (Forstgrundkarte). "
                         "Zellen ausserhalb fallen heraus. Kombinierbar "
                         "mit --wald: die Maske filtert, --wald liefert "
                         "die Bestandsangabe.")
    ap.add_argument("--waldtoleranz", type=float, default=25.0,
                    help="Suchabstand in Metern zur Waldkante. Wege sind "
                         "aus den Polygonen ausgestanzt, ein Punkt am "
                         "Wegrand liegt formal ausserhalb.")
    ap.add_argument("--schutz")
    ap.add_argument("--nfk-standard", type=float, default=35.0,
                    help="nFK in mm, wenn keine Bodenkarte vorliegt")
    ap.add_argument("--bestand-standard", default="Kiefer")
    ap.add_argument("--habitat-standard", default="Wald")
    ap.add_argument("--out", default="screening")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    try:
        bbox = tuple(float(x) for x in args.bbox.split(","))
        assert len(bbox) == 4
    except Exception:
        print("[!] --bbox braucht vier Zahlen", file=sys.stderr)
        return 2

    arten_namen = {a.strip() for a in args.arten.split(",") if a.strip()}
    bekannt = {a.name for a in ARTEN}
    unbekannt = arten_namen - bekannt
    if unbekannt:
        print(f"[!] Unbekannte Arten: {', '.join(unbekannt)}",
              file=sys.stderr)
        print(f"    Bekannt: {', '.join(sorted(bekannt))}", file=sys.stderr)
        return 2

    fein = raster(bbox, args.aufloesung)
    grob = raster(bbox, args.wetterraster)
    print(f"[i] {len(fein)} Bewertungszellen, {len(grob)} Wetterpunkte")
    if not fein or not grob:
        print("[!] Raster leer. bbox oder Aufloesung pruefen.",
              file=sys.stderr)
        return 2
    if len(fein) > 20000:
        print("[!] Sehr viele Zellen. --aufloesung vergroessern.",
              file=sys.stderr)
        return 2

    # --- Boden ---
    nfk_je_zelle = [args.nfk_standard] * len(fein)
    if args.boden and args.boden_feld:
        print(f"[i] Boden aus {args.boden} / {args.boden_feld}")
        layer = lade_layer(args.boden)
        werte = verschneide(fein, layer, args.boden_feld)
        for i, w in enumerate(werte):
            if w is None or (isinstance(w, float) and math.isnan(w)):
                continue
            if args.feld_typ == "nfk_mm":
                try:
                    nfk_je_zelle[i] = float(w)
                except (TypeError, ValueError):
                    pass
            elif args.feld_typ == "bodenklasse":
                eintrag = BODENKLASSEN.get(str(w).strip().lower())
                if eintrag:
                    nfk_je_zelle[i] = float(eintrag["nfk_mm"])
            else:  # feuchtestufe
                z = GRUNDWASSER.get(str(w).strip().lower())
                if z is not None:
                    nfk_je_zelle[i] = args.nfk_standard + z
    else:
        print("[!] Keine Bodenkarte uebergeben. Das Ergebnis ist eine")
        print("    WETTERKARTE, keine Standortbewertung — die nFK ist")
        print("    ueberall gleich. Mit --boden wird es erst sinnvoll.")

    # --- Wald/Biotop ---
    bestand_je = [args.bestand_standard] * len(fein)
    habitat_je = [args.habitat_standard] * len(fein)
    behalten = [True] * len(fein)
    if args.wald and args.wald_feld:
        print(f"[i] Biotope aus {args.wald} / {args.wald_feld}")
        layer = lade_layer(args.wald)
        werte = verschneide(fein, layer, args.wald_feld)
        for i, w in enumerate(werte):
            b = biotop_bewertung(str(w) if w is not None else "")
            if b is None:
                behalten[i] = False
                continue
            bestand_je[i] = b["bestand"]
            habitat_je[i] = b["habitat"]
            if (not args.boden) and b["bodenklasse_hinweis"]:
                nfk_je_zelle[i] = float(
                    BODENKLASSEN[b["bodenklasse_hinweis"]]["nfk_mm"])
        print(f"    {sum(behalten)} von {len(fein)} Zellen bewertbar")
    elif not args.waldmaske:
        print("[!] Kein Biotoplayer und keine Waldmaske. Acker und")
        print("    Siedlung werden mitbewertet — die Karte ist dann")
        print("    grob irrefuehrend.")

    # --- Waldmaske (nur Geometrie, keine Attribute) ---
    if args.waldmaske:
        print(f"[i] Waldmaske aus {args.waldmaske}")
        try:
            import geopandas as gpd
            from shapely.geometry import Point
            wald = lade_layer(args.waldmaske).to_crs("EPSG:25833")
            punkte_utm = gpd.GeoSeries(
                [Point(lon, lat) for lat, lon in fein],
                crs="EPSG:4326").to_crs("EPSG:25833")
            # Toleranz: Waldwege sind ausgestanzt, ein Punkt am Wegrand
            # liegt formal ausserhalb. 25 m Puffer faengt das ab.
            gepuffert = punkte_utm.buffer(args.waldtoleranz)
            treffer = gpd.GeoDataFrame(
                {"i": range(len(fein))}, geometry=gepuffert,
                crs="EPSG:25833")
            verschnitt = gpd.sjoin(treffer, wald[["geometry"]],
                                   how="inner", predicate="intersects")
            im_wald = set(verschnitt["i"].tolist())
            raus = 0
            for i in range(len(fein)):
                if i not in im_wald and behalten[i]:
                    behalten[i] = False
                    raus += 1
            print(f"    {raus} Zellen ausserhalb des Waldes entfernt, "
                  f"{sum(behalten)} verbleiben")
        except Exception as exc:
            print(f"    [!] Waldmaske nicht anwendbar: {exc}",
                  file=sys.stderr)

    # --- Schutzgebiete ausmaskieren ---
    if args.schutz:
        print(f"[i] Schutzgebiete aus {args.schutz}")
        layer = lade_layer(args.schutz)
        feld = next((c for c in layer.columns
                     if c.lower() in ("kategorie", "schutzkategorie",
                                      "typ", "art")), None)
        if feld is None:
            print("    [!] Kategorie-Feld nicht erkannt, keine Maskierung")
        else:
            werte = verschneide(fein, layer, feld)
            raus = 0
            for i, w in enumerate(werte):
                if w and any(s in str(w).lower()
                             for s in ("nsg", "naturschutzgebiet",
                                       "kernzone", "totalreservat")):
                    behalten[i] = False
                    raus += 1
            print(f"    {raus} Zellen als NSG ausmaskiert")

    # --- Wetter ---
    print("[i] Wetterdaten …")
    if args.selftest:
        wetter = synth_wetter(len(grob), 60, 16)
    else:
        try:
            wetter = hole_wetter_buendel(grob)
        except Exception as exc:
            print(f"[!] Wetterabruf fehlgeschlagen: {exc}", file=sys.stderr)
            return 1

    zuordnung = [naechste(pt, grob) for pt in fein]

    # --- Bewertung ---
    print("[i] Bewerte Zellen …")
    h = args.aufloesung
    zellen: list[dict] = []
    for i, (lat, lon) in enumerate(fein):
        if not behalten[i]:
            continue
        erg = bewerte_zelle(wetter[zuordnung[i]], nfk_je_zelle[i],
                            bestand_je[i], habitat_je[i], arten_namen)
        zellen.append({
            "lat0": round(lat - h / 2, 6), "lat1": round(lat + h / 2, 6),
            "lon0": round(lon - h / 2, 6), "lon1": round(lon + h / 2, 6),
            "nfk": round(nfk_je_zelle[i], 1),
            "bestand": bestand_je[i], "habitat": habitat_je[i],
            **erg,
        })

    if not zellen:
        print("[!] Keine bewertbare Zelle uebrig.", file=sys.stderr)
        return 1

    zellen.sort(key=lambda z: z["index"], reverse=True)
    aus = Path(args.out)

    # CSV
    with aus.with_suffix(".csv").open("w", newline="",
                                      encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["lat", "lon", "index", "beste_art", "nfk_mm",
                    "fuellgrad", "bilanz14_mm", "bestand", "habitat",
                    "fenster_ab"])
        for z in zellen:
            w.writerow([
                round((z["lat0"] + z["lat1"]) / 2, 6),
                round((z["lon0"] + z["lon1"]) / 2, 6),
                z["index"], z["beste_art"] or "", z["nfk"],
                z["fuellgrad"], z["bilanz14_mm"], z["bestand"],
                z["habitat"], z["fenster_ab"] or "",
            ])

    # GeoPackage
    try:
        import geopandas as gpd
        from shapely.geometry import box
        g = gpd.GeoDataFrame(
            [{k: v for k, v in z.items()
              if k not in ("je_art", "lat0", "lat1", "lon0", "lon1")}
             for z in zellen],
            geometry=[box(z["lon0"], z["lat0"], z["lon1"], z["lat1"])
                      for z in zellen],
            crs="EPSG:4326",
        )
        g.to_file(aus.with_suffix(".gpkg"), driver="GPKG",
                  layer="potenzial")
        print(f"[i] {aus.with_suffix('.gpkg')} geschrieben "
              f"({len(g)} Zellen)")
    except Exception as exc:
        print(f"[!] GeoPackage nicht geschrieben ({exc}). "
              f"CSV und Karte liegen vor.", file=sys.stderr)

    # Karte
    grundlage = ("Boden aus Karte" if args.boden
                 else "einheitliche nFK — Wetterkarte, keine Bodenbewertung")
    schreibe_karte(
        aus.with_suffix(".html"), zellen,
        "Pilzpotenzial " + ", ".join(sorted(arten_namen)),
        f"Stand {date.today():%d.%m.%Y} · {len(zellen)} bewertete Zellen · "
        f"{grundlage}. Flaeche interpoliert (inverse Distanzwichtung), "
        f"nicht gemessen. Bewertet Wachstumsbedingungen, nicht "
        f"Essbarkeit und nicht Betretungsrechte.",
        bbox, args.aufloesung)
    print(f"[i] {aus.with_suffix('.html')} geschrieben")
    print(f"[i] {aus.with_suffix('.csv')} geschrieben")

    print()
    print("  Beste Zellen:")
    for z in zellen[:10]:
        print(f"    {(z['lat0'] + z['lat1']) / 2:.4f}, "
              f"{(z['lon0'] + z['lon1']) / 2:.4f}  "
              f"Index {z['index']:>5.1f}  {z['beste_art'] or '-':<16} "
              f"nFK {z['nfk']:>5.1f} mm  {z['bestand']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
