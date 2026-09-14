#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pilzindex — Pilz-Fruktifikationsprognose auf Grundlage einer
standortbezogenen Bodenwasserbilanz.

Kerngedanke
-----------
Nicht der Regen entscheidet, sondern was der Boden davon behaelt.
Auf Sander (nFK ~30 mm) bedeutet derselbe Niederschlag etwas voellig
anderes als auf Geschiebemergel (~65 mm) oder im Niedermoor (~110 mm).
Deshalb wird die Wasserbilanz je Standort auf dessen nutzbare
Feldkapazitaet normiert (nFK-Fuellgrad), und erst dieser relative
Wert geht in die Artbewertung ein.

Modellkette
-----------
1.  Wetter    : Open-Meteo (Niederschlag, ET0 nach FAO-56, Lufttemperatur,
                Bodenfeuchte 9-27 cm, Bodentemperatur 18 cm)
2.  Bilanz    : Einschicht-Speichermodell ("bucket") ueber die effektive
                Durchwurzelungstiefe, Speichergroesse = nFK des Standorts
3.  Zustand   : Fuellgrad des Speichers, kombiniert mit der gemessenen
                Modell-Bodenfeuchte (Gewichtung konfigurierbar)
4.  Art       : Feuchtebedarf, Temperaturfenster und Latenzzeit je Art;
                bewertet wird der Bodenzustand VOR <lag_tage> Tagen
5.  Ausgabe   : Tagesindex 0-100 + Ampelstufe, Rueckblick und Prognose,
                Ermittlung des naechsten guenstigen Zeitfensters

Datenquellen fuer die Bodenparametrisierung (Brandenburg)
---------------------------------------------------------
Die nFK kann auf drei Wegen gesetzt werden, in aufsteigender Guete:

  a) Bodenklasse aus der Tabelle BODENKLASSEN (KA5-Orientierungswerte)
  b) eigener nFK-Wert in mm, z.B. aus der Bodenuebersichtskarte
     BUEK 300 des LBGR Brandenburg
  c) Forstliche Standortserkundung Brandenburg (Standortsformengruppe
     mit Naehrkraft- und Feuchtestufe) - fachlich die beste Grundlage
     fuer Waldstandorte; ueber das Geoportal Brandenburg bzw. den
     Landesbetrieb Forst zu beziehen

Fuer (b)/(c): Layer in QGIS laden, Standortpunkte verschneiden, den
nFK-Wert bzw. die Feuchtestufe in die spots.yaml uebertragen.
Die Funktion nfk_aus_shapefile() unten automatisiert das mit GeoPandas.

Aufruf
------
    python3 pilzindex.py                     # alle Standorte, heute
    python3 pilzindex.py --spot sperenberg   # ein Standort
    python3 pilzindex.py --tage 16           # Prognosehorizont
    python3 pilzindex.py --csv out.csv       # Zeitreihe exportieren
    python3 pilzindex.py --selftest          # ohne Netz, synthetische Daten

Lizenz: frei verwendbar. Ohne Gewaehr - eine Indexstufe ist keine
Aussage ueber Essbarkeit. Unklare Funde gehoeren zur Pilzberatung.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:
    requests = None

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Bodenparametrisierung
# ---------------------------------------------------------------------------

# nFK in mm, bezogen auf die effektive Durchwurzelungstiefe des
# Pilzmyzels (hier 30 cm angesetzt - Mykorrhiza-Feinwurzelhorizont).
# Werte als Orientierung nach KA5-Bodenartengruppen.
BODENKLASSEN: dict[str, dict[str, Any]] = {
    "reinsand": {
        "nfk_mm": 28.0,
        "bezeichnung": "Rein-/Duenensand, Sander (Ss, Sander/Talsand)",
    },
    "schwachlehmiger_sand": {
        "nfk_mm": 38.0,
        "bezeichnung": "schwach lehmiger Sand (Su2, St2)",
    },
    "lehmiger_sand": {
        "nfk_mm": 48.0,
        "bezeichnung": "lehmiger Sand (Sl3, Slu)",
    },
    "sandiger_lehm": {
        "nfk_mm": 60.0,
        "bezeichnung": "sandiger Lehm, Geschiebedecksand ueber Mergel (Ls)",
    },
    "geschiebemergel": {
        "nfk_mm": 68.0,
        "bezeichnung": "Geschiebemergel/Lehm (Lt, Ut) - z.B. Flaeming",
    },
    "anmoor": {
        "nfk_mm": 95.0,
        "bezeichnung": "Anmoor, grundwassernah",
    },
    "niedermoor": {
        "nfk_mm": 115.0,
        "bezeichnung": "Niedermoor/Bruchwald - z.B. Spreewald",
    },
}

# Zuschlag/Abschlag fuer Grundwasseranschluss (kapillarer Aufstieg).
# Stufen nach forstlicher Feuchtestufe, grob uebersetzt.
GRUNDWASSER_ZUSCHLAG_MM: dict[str, float] = {
    "trocken": -4.0,
    "maessig_frisch": 0.0,
    "frisch": 6.0,
    "feucht": 18.0,
    "nass": 35.0,
}


@dataclass
class Standort:
    """Ein Sammelstandort mit Boden- und Bestandsparametern."""

    key: str
    name: str
    lat: float
    lon: float
    bodenklasse: str = "reinsand"
    nfk_mm: float | None = None          # ueberschreibt die Bodenklasse
    feuchtestufe: str = "maessig_frisch"
    bestand: str = "Kiefer"
    exposition_faktor: float = 1.00      # <1 sonnig/Kuppe, >1 schattig/Senke
    notiz: str = ""

    def effektive_nfk(self) -> float:
        basis = self.nfk_mm
        if basis is None:
            eintrag = BODENKLASSEN.get(self.bodenklasse)
            if eintrag is None:
                raise ValueError(
                    f"Unbekannte Bodenklasse '{self.bodenklasse}' "
                    f"bei Standort '{self.key}'. "
                    f"Bekannt: {', '.join(sorted(BODENKLASSEN))}"
                )
            basis = float(eintrag["nfk_mm"])
        zuschlag = GRUNDWASSER_ZUSCHLAG_MM.get(self.feuchtestufe, 0.0)
        return max(10.0, (basis + zuschlag) * self.exposition_faktor)

    def bodenbezeichnung(self) -> str:
        if self.nfk_mm is not None:
            return f"eigener nFK-Wert ({self.nfk_mm:.0f} mm)"
        return BODENKLASSEN[self.bodenklasse]["bezeichnung"]


# ---------------------------------------------------------------------------
# Artprofile
# ---------------------------------------------------------------------------

@dataclass
class Artprofil:
    """
    feuchte_min : Fuellgrad der nFK, ab dem ueberhaupt fruktifiziert wird
    feuchte_opt : Fuellgrad, ab dem der Feuchtefaktor voll zaehlt
    temp_min/max: Fenster der Tagesmitteltemperatur in Grad C
    lag_tage    : Latenz zwischen Bodenzustand und sichtbarem Fruchtkoerper
    monate      : Monate, in denen die Art ueberhaupt bewertet wird
    baeume      : Mykorrhizapartner (nur informativ / zur Filterung)
    """

    name: str
    feuchte_min: float
    feuchte_opt: float
    temp_min: float
    temp_max: float
    lag_tage: int
    monate: tuple[int, ...]
    baeume: tuple[str, ...] = ()
    typ: str = "mykorrhiza"          # oder "saprotroph"
    habitate: tuple[str, ...] = ()   # nur fuer Saprotrophe


ARTEN: tuple[Artprofil, ...] = (
    Artprofil(
        name="Pfifferling",
        feuchte_min=0.30, feuchte_opt=0.55,
        temp_min=11.0, temp_max=24.0,
        lag_tage=14,
        monate=(6, 7, 8, 9, 10),
        baeume=("Kiefer", "Fichte", "Buche", "Eiche"),
    ),
    Artprofil(
        name="Steinpilz",
        feuchte_min=0.42, feuchte_opt=0.70,
        temp_min=9.0, temp_max=21.0,
        lag_tage=12,
        monate=(6, 7, 8, 9, 10, 11),
        baeume=("Kiefer", "Fichte", "Buche", "Eiche", "Birke"),
    ),
    Artprofil(
        name="Marone",
        feuchte_min=0.28, feuchte_opt=0.52,
        temp_min=7.0, temp_max=20.0,
        lag_tage=14,
        monate=(7, 8, 9, 10, 11),
        baeume=("Kiefer", "Fichte"),
    ),
    Artprofil(
        name="Birkenpilz",
        feuchte_min=0.30, feuchte_opt=0.55,
        temp_min=9.0, temp_max=22.0,
        lag_tage=12,
        monate=(6, 7, 8, 9, 10),
        baeume=("Birke",),
    ),
    Artprofil(
        name="Rotfussroehrling",
        feuchte_min=0.24, feuchte_opt=0.46,
        temp_min=9.0, temp_max=23.0,
        lag_tage=11,
        monate=(6, 7, 8, 9, 10),
        baeume=("Kiefer", "Eiche", "Buche"),
    ),
    Artprofil(
        name="Krause Glucke",
        feuchte_min=0.32, feuchte_opt=0.58,
        temp_min=9.0, temp_max=22.0,
        lag_tage=16,
        monate=(8, 9, 10),
        baeume=("Kiefer",),
    ),
    # Streuzersetzer: nicht an einen Baumpartner gebunden, sondern ans
    # Habitat. Auf reinem Kiefernsander praktisch nicht zu erwarten.
    Artprofil(
        name="Parasol",
        feuchte_min=0.26, feuchte_opt=0.50,
        temp_min=12.0, temp_max=25.0,
        lag_tage=10,
        monate=(7, 8, 9, 10),
        typ="saprotroph",
        habitate=("Waldrand", "Grünland"),
    ),
    Artprofil(
        name="Nelkenschwindling",
        feuchte_min=0.24, feuchte_opt=0.48,
        temp_min=11.0, temp_max=24.0,
        lag_tage=8,
        monate=(5, 6, 7, 8, 9, 10),
        typ="saprotroph",
        habitate=("Grünland", "Waldrand"),
    ),
)

STUFEN = (
    (0, 20, "—", "noch nichts"),
    (20, 40, "○", "vielleicht was"),
    (40, 65, "◐", "gute Chancen"),
    (65, 101, "●", "voller Korb"),
)


def stufe(index: float) -> tuple[str, str]:
    for lo, hi, symbol, text in STUFEN:
        if lo <= index < hi:
            return symbol, text
    return "—", "noch nichts"


# ---------------------------------------------------------------------------
# Wetterbeschaffung
# ---------------------------------------------------------------------------

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

TAGESGROESSEN = (
    "precipitation_sum",
    "et0_fao_evapotranspiration",
    "temperature_2m_mean",
    "temperature_2m_max",
    "temperature_2m_min",
)

STUNDENGROESSEN = (
    "soil_moisture_9_to_27cm",
    "soil_temperature_18cm",
)


def hole_wetter(
    lat: float,
    lon: float,
    past_days: int = 60,
    forecast_days: int = 16,
    timeout: int = 30,
) -> dict[str, Any]:
    """
    Holt Tages- und Stundenwerte von Open-Meteo.

    Hinweis zur Datenqualitaet: past_days liefert archivierte
    Modelllaeufe, keine Stationsmesswerte. Fuer die Bilanz ist das
    ausreichend und konsistent mit der Prognose. Wer gepruefte
    Messwerte braucht, zieht zusaetzlich DWD-Stationsdaten
    (opendata.dwd.de, Station Lindenberg/Baruth fuer den Raum
    Sperenberg) und ersetzt die Niederschlagsspalte.
    """
    if requests is None:
        raise RuntimeError("Modul 'requests' fehlt: pip install requests")

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "daily": ",".join(TAGESGROESSEN),
        "hourly": ",".join(STUNDENGROESSEN),
        "past_days": str(min(past_days, 92)),
        "forecast_days": str(min(forecast_days, 16)),
        "timezone": "Europe/Berlin",
    }
    antwort = requests.get(OPEN_METEO, params=params, timeout=timeout)
    antwort.raise_for_status()
    return antwort.json()


def tagesmittel_aus_stunden(
    zeiten: list[str], werte: list[float | None]
) -> dict[str, float]:
    """Aggregiert Stundenwerte zu Tagesmitteln."""
    eimer: dict[str, list[float]] = {}
    for t, v in zip(zeiten, werte):
        if v is None:
            continue
        eimer.setdefault(t[:10], []).append(float(v))
    return {tag: sum(vs) / len(vs) for tag, vs in eimer.items() if vs}


# ---------------------------------------------------------------------------
# Bodenwasserbilanz
# ---------------------------------------------------------------------------

# Bestandskoeffizient: Kiefernaltbestand fangt Niederschlag in der Krone
# ab (Interzeption) und transpiriert kraeftig. Beides reduziert die dem
# Myzel verfuegbare Wassermenge gegenueber dem Freiflaechenwert.
INTERZEPTION = {
    "Kiefer": 0.72,     # ca. 28 % Kroneninterzeption im Nadelaltholz
    "Fichte": 0.68,
    "Mischwald": 0.80,
    "Buche": 0.82,      # laubfrei im Winter, sommers dichter
    "Eiche": 0.84,
    "Birke": 0.86,
    "Erle": 0.86,
}

KC_BESTAND = {
    "Kiefer": 1.00,
    "Fichte": 1.05,
    "Mischwald": 0.95,
    "Buche": 0.95,
    "Eiche": 0.92,
    "Birke": 0.90,
    "Erle": 1.00,
}


def bilanziere(
    tage: list[str],
    niederschlag: list[float],
    et0: list[float],
    nfk_mm: float,
    bestand: str = "Kiefer",
    startfuellung: float = 0.45,
) -> list[float]:
    """
    Einschicht-Speichermodell. Gibt den Fuellgrad (0..1) je Tag zurueck.

        S(t) = clamp( S(t-1) + P(t)*i - ET0(t)*kc*f(S) , 0 , nFK )

    f(S) daempft die Verdunstung bei trockenem Speicher (Reduktions-
    funktion): bei fast leerem Speicher kann der Boden kein Wasser mehr
    abgeben. Ohne diesen Term laeuft das Modell auf Sand zu schnell
    auf Null und erholt sich danach unrealistisch schnell.
    """
    i_faktor = INTERZEPTION.get(bestand, 0.80)
    kc = KC_BESTAND.get(bestand, 0.95)

    speicher = nfk_mm * startfuellung
    verlauf: list[float] = []

    for p, e in zip(niederschlag, et0):
        p = 0.0 if p is None else float(p)
        e = 0.0 if e is None else float(e)

        zufluss = p * i_faktor
        fuellgrad = speicher / nfk_mm if nfk_mm > 0 else 0.0
        # Reduktionsfunktion: linear unterhalb 40 % Fuellgrad
        reduktion = min(1.0, fuellgrad / 0.40) if fuellgrad < 0.40 else 1.0
        abfluss = e * kc * reduktion

        speicher = speicher + zufluss - abfluss
        speicher = max(0.0, min(nfk_mm, speicher))
        verlauf.append(speicher / nfk_mm if nfk_mm > 0 else 0.0)

    return verlauf


def gleitend(werte: list[float], fenster: int) -> list[float]:
    """Rueckwaerts gerichteter gleitender Mittelwert."""
    raus: list[float] = []
    for i in range(len(werte)):
        ab = max(0, i - fenster + 1)
        teil = [w for w in werte[ab : i + 1] if w is not None]
        raus.append(sum(teil) / len(teil) if teil else 0.0)
    return raus


# ---------------------------------------------------------------------------
# Indexbildung
# ---------------------------------------------------------------------------

def normiere_modellfeuchte(
    werte: list[float], nfk_mm: float
) -> list[float]:
    """
    Rechnet die volumetrische Modell-Bodenfeuchte (m3/m3) in einen
    groben Fuellgrad um. Die nFK-Spanne wird aus dem Bodentyp
    abgeleitet: Sand hat bei 0.10 m3/m3 schon fast nichts mehr,
    Moor erst bei 0.25.
    """
    # Welkepunkt und Feldkapazitaet als Funktion der nFK abgeschaetzt
    pwp = 0.03 + 0.0012 * nfk_mm
    fk = pwp + (nfk_mm / 300.0)   # nFK mm ueber 300 mm Profiltiefe
    spanne = max(0.02, fk - pwp)
    return [
        max(0.0, min(1.0, ((0.0 if v is None else v) - pwp) / spanne))
        for v in werte
    ]


def temperaturfaktor(t: float, art: Artprofil) -> float:
    """Trapezfunktion: 1.0 im Kernbereich, linearer Abfall zu den Raendern."""
    if t is None:
        return 0.0
    puffer = 3.0
    if t <= art.temp_min - puffer or t >= art.temp_max + puffer:
        return 0.0
    if t < art.temp_min:
        return (t - (art.temp_min - puffer)) / puffer
    if t > art.temp_max:
        return ((art.temp_max + puffer) - t) / puffer
    return 1.0


def feuchtefaktor(fuellgrad: float, art: Artprofil) -> float:
    """
    Saettigungskurve statt harter Kappung.

    Bewusst streng: am Optimalpunkt werden erst 0.80 erreicht, 1.0 wird
    asymptotisch nie ganz erreicht. Sonst plateaut der Index bei 100 und
    "voller Korb" verliert jede Aussagekraft. Ein Index von 100 soll ein
    Ausnahmezustand sein, nicht der Normalfall eines feuchten Septembers.
    """
    if fuellgrad <= art.feuchte_min:
        return 0.0
    spanne = max(1e-6, art.feuchte_opt - art.feuchte_min)
    u = (fuellgrad - art.feuchte_min) / spanne
    return 1.0 - math.exp(-1.61 * u)


def trendfaktor(bilanz_mm: float) -> float:
    """
    Richtung der Wasserbilanz (P - ET0) der letzten 14 Tage in mm.

    Der Fuellgrad beschreibt den Zustand, die Bilanz die Richtung:
    ein noch feuchter Boden bei hoher Verdunstung ist auf dem Weg nach
    unten, und genau dieser Fall hat im Juli die Fehlprognose erzeugt.
    Wirkt nur daempfend (0.80..1.10), nicht dominierend.
    """
    if bilanz_mm >= 15.0:
        return 1.10
    if bilanz_mm >= 0.0:
        return 1.00 + 0.10 * (bilanz_mm / 15.0)
    if bilanz_mm <= -30.0:
        return 0.80
    return 1.00 - 0.20 * (abs(bilanz_mm) / 30.0)


@dataclass
class Tagesergebnis:
    tag: date
    fuellgrad: float
    bilanz14_mm: float = 0.0
    fuellgrad_lag: dict[str, float] = field(default_factory=dict)
    temp_mittel_lag: dict[str, float] = field(default_factory=dict)
    index: dict[str, float] = field(default_factory=dict)
    prognose: bool = False

    @property
    def gesamt(self) -> float:
        return max(self.index.values()) if self.index else 0.0


def berechne(
    standort: Standort,
    rohdaten: dict[str, Any],
    gewicht_bilanz: float = 0.55,
) -> list[Tagesergebnis]:
    """
    Verrechnet Rohdaten zu Tagesergebnissen.

    gewicht_bilanz : Anteil des eigenen Speichermodells am Feuchtewert.
                     Der Rest kommt aus der Modell-Bodenfeuchte des
                     Wetterdienstes. Zwei Quellen, weil beide je eigene
                     Schwaechen haben: das Speichermodell kennt keinen
                     Grundwasseranschluss, die Modellfeuchte kennt den
                     konkreten Bodentyp nicht.
    """
    nfk = standort.effektive_nfk()

    tage_str: list[str] = rohdaten["daily"]["time"]
    p = rohdaten["daily"]["precipitation_sum"]
    e = rohdaten["daily"]["et0_fao_evapotranspiration"]
    t_mittel = rohdaten["daily"]["temperature_2m_mean"]

    bilanz = bilanziere(tage_str, p, e, nfk, standort.bestand)

    stunden = rohdaten.get("hourly", {})
    if stunden.get("soil_moisture_9_to_27cm"):
        sm_tag = tagesmittel_aus_stunden(
            stunden["time"], stunden["soil_moisture_9_to_27cm"]
        )
        sm_reihe = [sm_tag.get(t) for t in tage_str]
        modellfeuchte = normiere_modellfeuchte(sm_reihe, nfk)
    else:
        modellfeuchte = list(bilanz)

    # Kombinierter Fuellgrad
    kombi = [
        gewicht_bilanz * b + (1.0 - gewicht_bilanz) * m
        for b, m in zip(bilanz, modellfeuchte)
    ]

    # Klimatische Wasserbilanz der jeweils letzten 14 Tage
    bilanz14: list[float] = []
    for idx in range(len(tage_str)):
        ab = max(0, idx - 13)
        summe_p = sum(0.0 if x is None else x for x in p[ab : idx + 1])
        summe_e = sum(0.0 if x is None else x for x in e[ab : idx + 1])
        bilanz14.append(summe_p - summe_e)

    heute = date.today()
    ergebnisse: list[Tagesergebnis] = []

    for idx, tag_str in enumerate(tage_str):
        tag = date.fromisoformat(tag_str)
        erg = Tagesergebnis(
            tag=tag,
            fuellgrad=kombi[idx],
            bilanz14_mm=bilanz14[idx],
            prognose=tag > heute,
        )

        for art in ARTEN:
            if tag.month not in art.monate:
                erg.index[art.name] = 0.0
                continue
            if standort.bestand not in art.baeume and art.baeume:
                # Bestand passt nicht zum Mykorrhizapartner
                if standort.bestand != "Mischwald":
                    erg.index[art.name] = 0.0
                    continue

            j = max(0, idx - art.lag_tage)
            # Feuchtezustand im Zeitfenster vor der Fruchtkoerperbildung
            fenster = kombi[max(0, j - 6) : j + 1] or [kombi[j]]
            f_lag = sum(fenster) / len(fenster)

            t_fenster = [
                x for x in t_mittel[max(0, j - 6) : idx + 1] if x is not None
            ]
            t_lag = sum(t_fenster) / len(t_fenster) if t_fenster else 0.0

            ff = feuchtefaktor(f_lag, art)
            tf = temperaturfaktor(t_lag, art)
            rf = trendfaktor(bilanz14[idx])

            erg.fuellgrad_lag[art.name] = f_lag
            erg.temp_mittel_lag[art.name] = t_lag
            # Multiplikativ: fehlt eine Bedingung, waechst nichts.
            erg.index[art.name] = round(
                min(100.0, 100.0 * ff * tf * rf), 1
            )

        ergebnisse.append(erg)

    return ergebnisse


def finde_fenster(
    ergebnisse: list[Tagesergebnis], schwelle: float = 40.0
) -> list[tuple[date, date, float]]:
    """Ermittelt zusammenhaengende Zeitraeume oberhalb der Schwelle."""
    fenster: list[tuple[date, date, float]] = []
    start: date | None = None
    spitze = 0.0
    for erg in ergebnisse:
        if erg.gesamt >= schwelle:
            if start is None:
                start = erg.tag
                spitze = erg.gesamt
            spitze = max(spitze, erg.gesamt)
        else:
            if start is not None:
                fenster.append((start, erg.tag - timedelta(days=1), spitze))
                start, spitze = None, 0.0
    if start is not None:
        fenster.append((start, ergebnisse[-1].tag, spitze))
    return fenster


# ---------------------------------------------------------------------------
# GeoPandas-Anbindung (optional)
# ---------------------------------------------------------------------------

def nfk_aus_shapefile(
    pfad: str | Path,
    lat: float,
    lon: float,
    feld: str,
    crs_ziel: str = "EPSG:25833",
) -> Any:
    """
    Liest den nFK-Wert (oder die Feuchtestufe) fuer einen Punkt aus
    einem Boden- oder Standortslayer.

    Beispiel mit der forstlichen Standortserkundung:

        wert = nfk_aus_shapefile(
            "standorte_bb.gpkg", 52.1303, 13.4076, feld="FEUCHTESTUFE"
        )

    EPSG:25833 = ETRS89 / UTM 33N, das amtliche System in Brandenburg.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import Point
    except ImportError as exc:
        raise RuntimeError(
            "GeoPandas/Shapely fehlen: pip install geopandas shapely"
        ) from exc

    flaechen = gpd.read_file(pfad)
    punkt = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs_ziel)
    flaechen = flaechen.to_crs(crs_ziel)
    treffer = flaechen[flaechen.contains(punkt.iloc[0])]
    if treffer.empty:
        return None
    return treffer.iloc[0][feld]


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

STANDARD_SPOTS = """\
# Sammelstandorte. Koordinaten in WGS84 (Dezimalgrad).
#
# bodenklasse : reinsand | schwachlehmiger_sand | lehmiger_sand |
#               sandiger_lehm | geschiebemergel | anmoor | niedermoor
# nfk_mm      : optional, ueberschreibt die Bodenklasse (aus BUEK 300
#               oder forstlicher Standortserkundung)
# feuchtestufe: trocken | maessig_frisch | frisch | feucht | nass
# bestand     : Kiefer | Fichte | Mischwald | Buche | Eiche | Birke | Erle
# exposition_faktor: 0.90 sonnige Duenenkuppe ... 1.15 beschattete Senke

sperenberg:
  name: Sperenberg / Luckenwalder Heide
  lat: 52.1303
  lon: 13.4076
  bodenklasse: reinsand
  feuchtestufe: maessig_frisch
  bestand: Kiefer
  exposition_faktor: 1.05
  notiz: >
    Sander am Rand des Baruther Urstromtals. Liefert Steinpilz, Marone
    und Pfifferling - also mehr als reiner Kiefernsand erwarten laesst,
    daher Expositionsfaktor leicht erhoeht. 13.09.2026: 1 Steinpilz,
    ca. 6 kg Maronen/Filzroehrlinge zu zweit.

flaeming:
  name: Hoher Flaeming bei Bad Belzig
  lat: 52.1400
  lon: 12.5900
  bodenklasse: geschiebemergel
  feuchtestufe: frisch
  bestand: Mischwald
  exposition_faktor: 1.00
  notiz: Geschiebemergel unter Sandauflage, deutlich puffernder als Sander.

spreewald:
  name: Spreewaldrand bei Schlepzig
  lat: 51.9750
  lon: 13.8000
  bodenklasse: niedermoor
  feuchtestufe: feucht
  bestand: Erle
  exposition_faktor: 1.05
  notiz: Trockenheits-Rueckfalloption. Birkenpilz/Marone vor Pfifferling.

dahme_heideseen:
  name: Dubrow / Dahme-Heideseen
  lat: 52.1900
  lon: 13.7300
  bodenklasse: schwachlehmiger_sand
  feuchtestufe: maessig_frisch
  bestand: Mischwald
  exposition_faktor: 1.00
  notiz: Kiefern-Eichenwald, hoeherer Laubholzanteil als Sperenberg.
"""


def lade_spots(pfad: Path) -> dict[str, Standort]:
    if yaml is None:
        raise RuntimeError("Modul 'pyyaml' fehlt: pip install pyyaml")
    if not pfad.exists():
        pfad.write_text(STANDARD_SPOTS, encoding="utf-8")
        print(f"[i] {pfad} neu angelegt. Bitte pruefen und anpassen.\n")
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    spots: dict[str, Standort] = {}
    for key, werte in daten.items():
        spots[key] = Standort(key=key, **werte)
    return spots


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------

def zeige_standort(
    standort: Standort,
    ergebnisse: list[Tagesergebnis],
    prognosetage: int,
) -> None:
    nfk = standort.effektive_nfk()
    heute = date.today()

    heute_erg = next(
        (e for e in ergebnisse if e.tag == heute), ergebnisse[-1]
    )

    print("=" * 66)
    print(f"  {standort.name}   ({standort.lat:.4f}, {standort.lon:.4f})")
    print("=" * 66)
    print(f"  Boden      : {standort.bodenbezeichnung()}")
    print(f"  nFK effektiv: {nfk:.0f} mm  "
          f"(Feuchtestufe {standort.feuchtestufe}, "
          f"Expo {standort.exposition_faktor:.2f})")
    print(f"  Bestand    : {standort.bestand}")
    print()

    sym, txt = stufe(heute_erg.gesamt)
    print(f"  HEUTE {heute:%d.%m.%Y}   {sym}  {txt}   "
          f"(Index {heute_erg.gesamt:.0f}/100)")
    print(f"  Bodenspeicher jetzt : {heute_erg.fuellgrad * 100:.0f} % der nFK "
          f"= {heute_erg.fuellgrad * nfk:.0f} mm")
    richtung = ("steigend" if heute_erg.bilanz14_mm > 2
                else "fallend" if heute_erg.bilanz14_mm < -2 else "stabil")
    print(f"  Wasserbilanz 14 d   : {heute_erg.bilanz14_mm:+.0f} mm "
          f"({richtung})")
    print()
    print("  'Reizfenster' = Bodenfeuchte zur Zeit der Anlage der")
    print("  Fruchtkoerper, also vor der artspezifischen Latenzzeit.")
    print()

    print("  Art                Index  Stufe          "
          "Reizfenster  T-Mittel")
    print("  " + "-" * 62)
    for art in ARTEN:
        wert = heute_erg.index.get(art.name, 0.0)
        s, t = stufe(wert)
        fl = heute_erg.fuellgrad_lag.get(art.name)
        tm = heute_erg.temp_mittel_lag.get(art.name)
        if heute.month not in art.monate:
            print(f"  {art.name:<18} {'':>5}  ausserhalb der Saison")
            continue
        fl_s = f"{fl * 100:>5.0f} %" if fl is not None else "    -"
        tm_s = f"{tm:>6.1f} C" if tm is not None else "     -"
        print(f"  {art.name:<18} {wert:>5.0f}  {s} {t:<14} "
              f"{fl_s:>9}   {tm_s}")
    print()

    # Verlauf
    print(f"  Verlauf (14 Tage zurueck, {prognosetage} Tage Prognose)")
    print("  " + "-" * 62)
    fenster_erg = [
        e for e in ergebnisse
        if heute - timedelta(days=14) <= e.tag
        <= heute + timedelta(days=prognosetage)
    ]
    for e in fenster_erg:
        balken = "█" * int(round(e.gesamt / 4.0))
        marke = "P" if e.prognose else " "
        heute_marke = "<<< heute" if e.tag == heute else ""
        print(f"  {e.tag:%d.%m} {marke} {e.gesamt:>3.0f} "
              f"{balken:<25} {heute_marke}")
    print()

    zukunft = [e for e in ergebnisse if e.tag >= heute]
    fenster = finde_fenster(zukunft, schwelle=40.0)
    if fenster:
        print("  Guenstige Zeitfenster (Index >= 40):")
        for von, bis, spitze in fenster:
            print(f"    {von:%d.%m.} bis {bis:%d.%m.}  "
                  f"Spitzenindex {spitze:.0f}")
    else:
        beste = max(zukunft, key=lambda e: e.gesamt)
        print(f"  Kein Fenster im Prognosehorizont. "
              f"Bester Tag: {beste.tag:%d.%m.} mit Index {beste.gesamt:.0f}.")
    print()
    if standort.notiz:
        print(f"  Notiz: {standort.notiz.strip()}")
    print()


def schreibe_csv(
    pfad: Path, alle: dict[str, list[Tagesergebnis]]
) -> None:
    import csv

    spalten = ["standort", "datum", "prognose", "fuellgrad"] + [
        a.name for a in ARTEN
    ]
    with pfad.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(spalten)
        for key, ergebnisse in alle.items():
            for e in ergebnisse:
                w.writerow(
                    [key, e.tag.isoformat(), int(e.prognose),
                     f"{e.fuellgrad:.3f}"]
                    + [f"{e.index.get(a.name, 0.0):.1f}" for a in ARTEN]
                )
    print(f"[i] CSV geschrieben: {pfad}")


# ---------------------------------------------------------------------------
# Selbsttest mit synthetischen Daten (ohne Netz)
# ---------------------------------------------------------------------------

def synthetische_daten(
    past_days: int = 60, forecast_days: int = 16, szenario: str = "sept2026"
) -> dict[str, Any]:
    """
    Erzeugt einen plausiblen Wetterverlauf, um die Modelllogik ohne
    Netzzugriff zu pruefen. szenario 'sept2026' bildet die Lage nach:
    trockener August, Regen Ende August/Anfang September, danach mild.
    """
    heute = date.today()
    start = heute - timedelta(days=past_days)
    n = past_days + forecast_days

    tage, p, e, tm, tmax, tmin = [], [], [], [], [], []
    for i in range(n):
        tag = start + timedelta(days=i)
        tage.append(tag.isoformat())
        rel = (tag - heute).days  # negativ = Vergangenheit

        if szenario == "sept2026":
            if rel < -24:            # trockener August
                regen = 0.0 if i % 9 else 3.0
                temp = 23.0
            elif -24 <= rel < -16:   # Regenphase
                regen = 9.0 if i % 2 else 4.0
                temp = 17.0
            elif -16 <= rel < 0:     # mild, wechselhaft
                regen = 2.5 if i % 3 else 0.0
                temp = 16.0
            else:                    # Prognose
                regen = 4.0 if i % 4 else 0.5
                temp = 14.0
        else:                        # 'trocken'
            regen = 0.0
            temp = 24.0

        p.append(regen)
        tm.append(temp)
        tmax.append(temp + 5)
        tmin.append(temp - 6)
        # ET0 grob temperaturabhaengig
        e.append(round(max(0.6, 0.16 * temp - 0.4), 2))

    stunden_zeit, stunden_sm, stunden_st = [], [], []
    for i, tag in enumerate(tage):
        for h in range(0, 24, 3):
            stunden_zeit.append(f"{tag}T{h:02d}:00")
            # groebe Annaeherung: folgt dem Regen mit Daempfung
            basis = 0.07 + 0.010 * sum(p[max(0, i - 10): i + 1]) / 10.0
            stunden_sm.append(round(min(0.32, basis), 3))
            stunden_st.append(tm[i] - 1.0)

    return {
        "daily": {
            "time": tage,
            "precipitation_sum": p,
            "et0_fao_evapotranspiration": e,
            "temperature_2m_mean": tm,
            "temperature_2m_max": tmax,
            "temperature_2m_min": tmin,
        },
        "hourly": {
            "time": stunden_zeit,
            "soil_moisture_9_to_27cm": stunden_sm,
            "soil_temperature_18cm": stunden_st,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pilz-Fruktifikationsprognose ueber standortbezogene "
                    "Bodenwasserbilanz."
    )
    ap.add_argument("--spots", default="spots.yaml",
                    help="Pfad zur Standortkonfiguration")
    ap.add_argument("--spot", action="append",
                    help="nur diesen Standort (mehrfach moeglich)")
    ap.add_argument("--tage", type=int, default=10,
                    help="Prognosehorizont in Tagen (max. 16)")
    ap.add_argument("--rueckblick", type=int, default=60,
                    help="Tage Vorgeschichte fuer die Bilanz (max. 92)")
    ap.add_argument("--csv", help="Zeitreihe als CSV exportieren")
    ap.add_argument("--json", help="Rohergebnis als JSON exportieren")
    ap.add_argument("--gewicht-bilanz", type=float, default=0.55,
                    help="Anteil des Speichermodells (0..1)")
    ap.add_argument("--selftest", action="store_true",
                    help="mit synthetischen Daten rechnen, ohne Netz")
    args = ap.parse_args(list(argv) if argv is not None else None)

    spots = lade_spots(Path(args.spots))
    if args.spot:
        fehlend = [s for s in args.spot if s not in spots]
        if fehlend:
            print(f"[!] Unbekannt: {', '.join(fehlend)}", file=sys.stderr)
            print(f"    Vorhanden: {', '.join(spots)}", file=sys.stderr)
            return 2
        spots = {k: v for k, v in spots.items() if k in args.spot}

    alle: dict[str, list[Tagesergebnis]] = {}

    for key, standort in spots.items():
        try:
            if args.selftest:
                roh = synthetische_daten(
                    past_days=args.rueckblick, forecast_days=args.tage
                )
            else:
                roh = hole_wetter(
                    standort.lat, standort.lon,
                    past_days=args.rueckblick,
                    forecast_days=max(args.tage, 7),
                )
        except Exception as exc:
            print(f"[!] {standort.name}: Wetterabruf fehlgeschlagen "
                  f"({exc})", file=sys.stderr)
            continue

        ergebnisse = berechne(
            standort, roh, gewicht_bilanz=args.gewicht_bilanz
        )
        alle[key] = ergebnisse
        zeige_standort(standort, ergebnisse, args.tage)

    if not alle:
        return 1

    # Rangliste ueber alle Standorte
    if len(alle) > 1:
        heute = date.today()
        print("=" * 66)
        print("  RANGLISTE HEUTE")
        print("=" * 66)
        rang = []
        for key, ergebnisse in alle.items():
            e = next((x for x in ergebnisse if x.tag == heute), ergebnisse[-1])
            beste_art = max(e.index.items(), key=lambda kv: kv[1],
                            default=("-", 0.0))
            rang.append((e.gesamt, spots[key].name, beste_art))
        for wert, name, (art, aw) in sorted(rang, reverse=True):
            s, t = stufe(wert)
            zusatz = f"beste Art: {art} ({aw:.0f})" if aw > 0 else ""
            print(f"  {wert:>3.0f}  {s} {t:<15} {name:<34} {zusatz}")
        print()

    if args.csv:
        schreibe_csv(Path(args.csv), alle)

    if args.json:
        nutz = {
            key: [
                {
                    "datum": e.tag.isoformat(),
                    "prognose": e.prognose,
                    "fuellgrad": round(e.fuellgrad, 3),
                    "index": e.index,
                }
                for e in ergebnisse
            ]
            for key, ergebnisse in alle.items()
        }
        Path(args.json).write_text(
            json.dumps(nutz, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[i] JSON geschrieben: {args.json}")

    print("  Hinweis: Der Index bewertet Wachstumsbedingungen, nicht")
    print("  Essbarkeit. Unklare Funde gehoeren zur Pilzberatung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
