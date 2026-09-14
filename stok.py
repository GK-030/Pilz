#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stok — Forstliche Standortskarte Brandenburg (STOK) als Bodenebene.

Zweck
-----
Ersetzt die Schaetztabelle in pilzindex.py durch die amtliche
Standortskartierung: nutzbare Feldkapazitaet und Grundwassernaehe je
Flaeche, abgeleitet aus der Stamm-Standortsgruppe (SEA 95).

Datenquelle
-----------
WFS des Landesbetriebs Forst Brandenburg:
    https://www.brandenburg-forst.de/geoserver/ISTOK/wfs
    typeNames = ISTOK:fs_kf
Landesweiter Download alternativ:
    https://www.brandenburg-forst.de/inspire/dls/stok/stok_25833.gml

Lizenz: Datenlizenz Deutschland - Namensnennung - Version 2.0
        (https://www.govdata.de/dl-de/by-2-0)
Pflichtangabe bei Nutzung: "© Landesbetrieb Forst Brandenburg"
Die Funktion namensnennung() gibt den Text zur Einbindung zurueck.

Schluessel der Stamm-Standortsgruppe
------------------------------------
Aufbau:  [Praefix][Naehrkraft][Feuchtestufe][Zusatz]

Praefix - Feuchteregime (SEA 95, Teil C):
    (keins)  terrestrisch; das T wird vereinfachend nicht mitgeschrieben
    Ü        periodische/episodische Ueberflutung und Ueberwaesserung (Auen)
    N        mineralischer Nassstandort, geringer Kontrast Fruehjahr/Herbst,
             auch staerker entwaesserte halb- und vollhydromorphe Boeden
    O        organischer Nassstandort - Moor und Gleymoor

Naehrkraft (Stamm-Naehrkraftstufe):
    R reich | K kraeftig | M maessig naehrstoffhaltig
    Z ziemlich arm | A arm

Feuchtestufe (Ziffer):
    WICHTIG - bei den Nassstandorten ist die KLEINERE Ziffer die
    NAESSERE. Belegt durch die SEA-Tabellen: O...1 bis O...4ue nach
    spaetsommerlichen Absinkstufen, und Ü...0 ueberflutungssumpfig,
    Ü...1 ueberflutungsnass, Ü...2 ueberflutungsfeucht.

Feldbedeutung (aus der Dienstbeschreibung von stok_fskf)
-------------------------------------------------------
    BFFG1..BFFG3   Feinbodenformen mit Zusatzmerkmalen wie
                   Grundwasserstufen aus dem Gelaendebefund
    NFGR1..NFGR3   Stamm-Standortsgruppen bzw. Naehrkraft-Feuchtegruppen;
                   NFGR4 nur bei Kleinarealen
    kf_gesamt      Gesamt-Klimafeuchte
    AZ1..AZ3       Flaechenanteile in ZEHNTELN (Anteilszehntel), NICHT
                   in Prozent
    stgr           Leitgruppe der Flaeche

Die Richtung der Feuchtestufen ist belegt: die Dienstbeschreibung nennt
fuer trockenere Verhaeltnisse ausdruecklich die Stamm-Feuchtestufe
(T)..3, Beispiel Z3. Hoehere Ziffer = trockener, in allen Praefixgruppen.

UNGEPRUEFT
----------
Der Zusatz "g" (A2g, Z2g, M2g, K2g, R2g) ist unklar. Er tritt in der
Legende nur zusammen mit der Ziffer 2 auf. Belegt sind in der SEA nur
"v" (verhagert) und "+" (reicherer Untergrund). Bis zur Klaerung wird
"g" ignoriert, also wie die Gruppe ohne Zusatz behandelt - markiert in
ZUSATZ_UNKLAR, damit es nicht heimlich in die nFK eingeht.

PRUEFUNG GEGEN AMTLICHE WERTE (14.09.2026)
------------------------------------------
Der WFS bowassverh (LBGR, FeatureType app:WaBi) fuehrt die nutzbare
Feldkapazitaet als amtliche Auswertung - in Volumenprozent, nicht in mm.
Abfrage fuer Sperenberg (391002, 5776726 in EPSG:25833):

    fk_1m       Klasse 11   < 13 Vol.%              sehr gering
    nfk_1m      Klasse 12   < 6 Vol.%, z.T. < 14    sehr gering, z.T. gering
    nfkwe       Klasse 12   < 6 Vol.%, z.T. < 14    sehr gering, z.T. gering
    nfkweauf    Klasse 21   gering, z.T. < 6 Vol.%  gering, z.T. sehr gering

Umgerechnet auf 30 cm Durchwurzelungstiefe: unter 18 mm im Boden selbst,
mit kapillarem Aufstieg bis etwa 42 mm. Mitte der Spanne rund 30 mm.

Meine Ableitung aus Z2 mit Klimafeuchte t ergibt 35 mm - also am oberen
Rand, tendenziell ZU HOCH. Die Tabelle NAEHRKRAFT_NFK ist damit nicht
widerlegt, aber auch nicht bestaetigt; sie liegt an diesem einen
Pruefpunkt etwas zu grosszuegig. Nicht nachjustiert, weil ein einzelner
Punkt keine Kalibrierung ist.

Einschraenkung der Pruefquelle: das zurueckgegebene Polygon spannt rund
4,3 x 3,6 km. Der Layer taugt als Mengenanker, nicht als raeumliche
Auflösung. Die Standortskarte bleibt die feinere Quelle.

Weiterer Layer, noch nicht ausgewertet
--------------------------------------
stok_memi (Meso- und Mikroklima) zeigt reliefbedingte Abweichungen zum
Wuchsbezirksklima als frischer (fr) oder trockener (tr). Das entspricht
dem handgesetzten Expositionsfaktor in pilzindex.py, nur amtlich
kartiert - ein Kandidat, diesen Faktor abzuloesen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NAMENSNENNUNG = "© Landesbetrieb Forst Brandenburg (dl-de/by-2.0)"

TERRESTRISCH_UNGEPRUEFT = False
ZUSATZ_UNKLAR = ("g",)

# ---------------------------------------------------------------------------
# Ableitung der nFK
# ---------------------------------------------------------------------------

# Basis-nFK in mm ueber 30 cm Durchwurzelungstiefe, aus der
# Naehrkraftstufe als Texturhinweis. Naehrkraft ist kein Wassermass,
# korreliert im nordostdeutschen Tiefland aber eng mit dem Lehmanteil:
# arme Standorte sind die reinen Sande, reiche die lehmigen.
# Werte als KA5-Orientierung, nicht als Messwert.
NAEHRKRAFT_NFK = {
    "A": 26.0,   # arm        - Rein- und Duenensand
    "Z": 34.0,   # ziemlich arm - schwach lehmiger Sand
    "M": 46.0,   # maessig    - lehmiger Sand
    "K": 58.0,   # kraeftig   - stark lehmiger Sand bis sandiger Lehm
    "R": 68.0,   # reich      - Lehm, Mergel
}

# Feuchtestufe: Zuschlag in mm. Kleinere Ziffer = naesser.
# Der Zuschlag bildet kapillaren Aufstieg ab, nicht die Textur —
# deshalb gedeckelt, sonst uebertrifft ein armer Sand mit hohem
# Grundwasser einen lehmigen Standort, was die Textur ignoriert.
FEUCHTE_ZUSCHLAG = {
    0: 26.0,
    1: 18.0,
    2: 6.0,
    3: -2.0,
    4: -8.0,
}

# Obergrenze des pflanzenverfuegbaren Wassers. Jenseits davon ist der
# Boden nicht besser versorgt, sondern vernaesst: Sauerstoffmangel im
# Wurzelraum. Das Speichermodell wuerde solche Standorte sonst dauerhaft
# als randvoll und damit als Bestlage ausgeben.
NFK_OBERGRENZE = 140.0

# Dauernaesse: fuer die betrachteten Mykorrhizapilze ungeeignet.
# Belegt ueber die verbalen Bezeichnungen der SEA: Ü...0
# ueberflutungssumpfig, Ü...1 ueberflutungsnass, O...1 nass.
def ist_zu_nass(praefix: str, feuchtestufe: int) -> bool:
    if praefix == "Ü" and feuchtestufe <= 1:
        return True
    if praefix == "O" and feuchtestufe <= 1:
        return True
    return False

# Praefix: Feuchteregime. Organische Nassstandorte halten ein Vielfaches,
# deshalb hier ein Faktor statt eines Zuschlags.
PRAEFIX = {
    "":  {"faktor": 1.00, "bez": "terrestrisch", "grundwasser": "fern"},
    "N": {"faktor": 1.35, "bez": "mineralischer Nassstandort",
          "grundwasser": "nah"},
    "Ü": {"faktor": 1.45, "bez": "Aue, periodisch ueberflutet",
          "grundwasser": "nah"},
    "O": {"faktor": 2.30, "bez": "organischer Nassstandort (Moor, Gleymoor)",
          "grundwasser": "sehr nah"},
}

# Bestandsvorschlag: die Standortsgruppe sagt nichts ueber den
# tatsaechlichen Bestand, aber etwas ueber den moeglichen.
# Nur als Rueckfall, wenn keine Biotopkartierung vorliegt.
PRAEFIX_BESTAND = {
    "":  None,        # aus Biotopkartierung oder OSM nehmen
    "N": "Erle",
    "Ü": "Erle",
    "O": "Erle",
}

# Gesamt-Klimafeuchte (Feld kf_gesamt, in der Karte als Buchstabe hinter
# der Standortsgruppe, z.B. "Z2 t"). Sie wird grossraeumig aus dem
# Wuchsbezirksklima zugewiesen und bei bewegtem Relief durch das Meso-
# und Mikroklima in Richtung frischer oder trockener modifiziert.
# Wirkt als Faktor auf das pflanzenverfuegbare Wasser.
# Buchstaben nach dem Schema der SEA-Feuchtestufen:
#   t trocken | m maessig frisch | i frisch | f feucht | n nass
KLIMAFEUCHTE = {
    "t": 0.88,
    "m": 1.00,
    "i": 1.08,
    "f": 1.16,
    "n": 1.22,
}
# Mehrbuchstabige Eintraege kommen in der Karte vor (z.B. "rmt"). Deren
# Bedeutung ist unklar; in solchen Faellen wird nicht geraten, sondern
# der neutrale Wert genommen und der Fall gemeldet.
KLIMAFEUCHTE_UNKLAR = True

MUSTER = re.compile(
    r"^(?P<praefix>[NÜO]?)(?P<naehr>[RKMZA])(?P<feuchte>\d)(?P<zusatz>[a-zäöü+]*)$"
)


@dataclass
class Standort:
    """Ausgewertete Stamm-Standortsgruppe."""

    kuerzel: str
    praefix: str
    naehrkraft: str
    feuchtestufe: int
    zusatz: str
    klimafeuchte: str | None
    nfk_mm: float
    grundwasser: str
    bestand_vorschlag: str | None
    zu_nass: bool
    ungeprueft: tuple[str, ...]

    def klartext(self) -> str:
        n = {"A": "arm", "Z": "ziemlich arm", "M": "mäßig nährstoffhaltig",
             "K": "kräftig", "R": "reich"}[self.naehrkraft]
        return (f"{self.kuerzel}: {n}, Feuchtestufe {self.feuchtestufe}, "
                f"{PRAEFIX[self.praefix]['bez']} — "
                f"nFK {self.nfk_mm:.0f} mm")


def deute_stgr(kuerzel: str, klimafeuchte: str | None = None) -> Standort | None:
    """
    Uebersetzt eine Stamm-Standortsgruppe in nFK und Feuchteregime.
    Gibt None zurueck, wenn das Kuerzel nicht dem Muster entspricht —
    dann besser die Schaetztabelle nutzen als falsch zu rechnen.
    """
    if not kuerzel:
        return None
    k = kuerzel.strip()
    m = MUSTER.match(k)
    if not m:
        return None

    praefix = m.group("praefix")
    naehr = m.group("naehr")
    feuchte = int(m.group("feuchte"))
    zusatz = m.group("zusatz")

    basis = NAEHRKRAFT_NFK[naehr]
    zuschlag = FEUCHTE_ZUSCHLAG.get(feuchte, 0.0)
    faktor = PRAEFIX[praefix]["faktor"]

    offen: list[str] = []
    kf = 1.00
    if klimafeuchte:
        kfz = str(klimafeuchte).strip().lower()
        if len(kfz) == 1 and kfz in KLIMAFEUCHTE:
            kf = KLIMAFEUCHTE[kfz]
        else:
            offen.append(f"Klimafeuchte '{klimafeuchte}' nicht deutbar, "
                         f"neutral gerechnet")

    nfk = max(12.0, min(NFK_OBERGRENZE, (basis + zuschlag) * faktor * kf))
    if praefix == "" and TERRESTRISCH_UNGEPRUEFT:
        offen.append("Benennung der terrestrischen Feuchtestufen ungeprüft")
    for z in ZUSATZ_UNKLAR:
        if z in zusatz:
            offen.append(f"Zusatz '{z}' unbekannt, wird ignoriert")

    return Standort(
        kuerzel=k, praefix=praefix, naehrkraft=naehr,
        feuchtestufe=feuchte, zusatz=zusatz,
        klimafeuchte=klimafeuchte,
        nfk_mm=round(nfk, 1),
        grundwasser=PRAEFIX[praefix]["grundwasser"],
        bestand_vorschlag=PRAEFIX_BESTAND[praefix],
        zu_nass=ist_zu_nass(praefix, feuchte),
        ungeprueft=tuple(offen),
    )


# Ab welchem Flaechenanteil ein feuchterer Teilanteil die Bewertung
# bestimmt. 0.20 heisst: zwei Zehntel Moor in einem Sanderumfeld machen
# die Zelle zur Moorzelle. Das ist eine Modellentscheidung, keine
# Messgroesse — fuer Pilze plausibel, weil dort gefruchtet wird, aber
# je nach Fragestellung anzupassen.
ANTEIL_SCHWELLE = 0.20


def deute_mosaik(komponenten: list[tuple[str, float]],
                 schwelle: float = ANTEIL_SCHWELLE) -> dict:
    """
    Wertet ein Standortsmosaik aus: Liste von (Kuerzel, Anteilszahl).

    Eine STOK-Flaeche ist kein einheitlicher Standort, sondern ein
    Mosaik mit Anteilszahlen (Felder nfgr1..4 mit az1..3). Fuer Pilze
    ist der FEUCHTERE Teilanteil interessanter als das Mittel: auf einer
    Flaeche aus 60 Prozent Duenensand und 40 Prozent anlehmigem Sand
    fruchtet der feuchtere Anteil, waehrend der trockene ausfaellt.

    Deshalb zwei Werte:
      nfk_mittel : flaechengewichtetes Mittel - fuer Flaechenbilanzen
      nfk_feucht : der feuchteste Anteil ab 20 Prozent Flaechenanteil -
                   fuer die Pilzbewertung
    """
    ausgewertet = []
    for kuerzel, anteil in komponenten:
        s = deute_stgr(kuerzel)
        if s is not None and anteil and anteil > 0:
            ausgewertet.append((s, float(anteil)))

    if not ausgewertet:
        return {"erkannt": False}

    summe = sum(a for _, a in ausgewertet)
    mittel = sum(s.nfk_mm * a for s, a in ausgewertet) / summe

    relevant = [(s, a) for s, a in ausgewertet if a / summe >= schwelle]
    if not relevant:
        relevant = ausgewertet
    # Dauernasse Anteile scheiden aus: sie sind nicht die beste, sondern
    # eine ungeeignete Teilflaeche.
    nutzbar = [(s, a) for s, a in relevant if not s.zu_nass]
    nur_nass = not nutzbar
    if nur_nass:
        nutzbar = relevant
    feuchtester = max(nutzbar, key=lambda p: p[0].nfk_mm)[0]

    offen = sorted({h for s, _ in ausgewertet for h in s.ungeprueft})

    return {
        "erkannt": True,
        "nfk_mittel": round(mittel, 1),
        "nfk_feucht": feuchtester.nfk_mm,
        "leitgruppe": max(ausgewertet, key=lambda p: p[1])[0].kuerzel,
        "feuchteste_gruppe": feuchtester.kuerzel,
        "grundwasser": feuchtester.grundwasser,
        "bestand_vorschlag": feuchtester.bestand_vorschlag,
        "zu_nass": nur_nass,
        "komponenten": [(s.kuerzel, a) for s, a in ausgewertet],
        "ungeprueft": tuple(offen),
    }


def namensnennung() -> str:
    return NAMENSNENNUNG


# ---------------------------------------------------------------------------
# WFS-Abruf
# ---------------------------------------------------------------------------

WFS = "https://www.brandenburg-forst.de/geoserver/ISTOK/wfs"
TYPENAME = "ISTOK:fs_kf"


def hole_stok(bbox_25833: tuple[float, float, float, float],
              count: int = 20000):
    """
    Laedt die Standortskarte fuer einen Ausschnitt als GeoDataFrame.

    bbox in EPSG:25833 (amtliches System in Brandenburg).
    Der Dienst liefert GML; GDAL liest das ueber GeoPandas direkt.
    GeoJSON ist auf diesem Server NICHT freigeschaltet — eine Anfrage
    mit outputFormat=application/json wird mit
    "Invalid Output Format Parameter" abgewiesen.
    """
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise RuntimeError("pip install geopandas") from exc

    x0, y0, x1, y1 = bbox_25833
    url = (f"{WFS}?service=WFS&version=2.0.0&request=GetFeature"
           f"&typeNames={TYPENAME}"
           f"&bbox={x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},EPSG:25833"
           f"&count={count}")
    g = gpd.read_file(url)
    if g.crs is None:
        g = g.set_crs("EPSG:25833")
    return g


# Anteilszehntel: AZ1..AZ3 zaehlen in 1/10 der Flaeche, nicht in
# Prozent. Die Summe betraegt also 10, nicht 100.
ANTEIL_SUMME = 10.0


def spalten_mosaik(zeile) -> list[tuple[str, float]]:
    """
    Liest die Mosaikkomponenten aus einer STOK-Zeile.

    NFGR1..NFGR3 mit AZ1..AZ3 in Zehnteln. NFGR4 kommt laut
    Dienstbeschreibung nur bei Kleinarealen vor und hat keine eigene
    Anteilszahl - es erhaelt den Rest bis 10 Zehntel.
    """
    raus: list[tuple[str, float]] = []
    anteile = []
    for i in (1, 2, 3):
        a = zeile.get(f"az{i}")
        try:
            anteile.append(float(a) if a not in (None, "") else 0.0)
        except (TypeError, ValueError):
            anteile.append(0.0)

    for i, a in zip((1, 2, 3), anteile):
        k = zeile.get(f"nfgr{i}")
        if k and a > 0:
            raus.append((str(k).strip(), a))

    rest = max(0.0, ANTEIL_SUMME - sum(anteile))
    k4 = zeile.get("nfgr4")
    if k4 and rest > 0:
        raus.append((str(k4).strip(), rest))

    # Wenn keine Anteile angegeben sind, aber Gruppen: gleichmaessig
    if not raus:
        gruppen = [str(zeile.get(f"nfgr{i}")).strip()
                   for i in (1, 2, 3, 4)
                   if zeile.get(f"nfgr{i}")]
        if gruppen:
            raus = [(k, ANTEIL_SUMME / len(gruppen)) for k in gruppen]
        elif zeile.get("stgr"):
            raus = [(str(zeile["stgr"]).strip(), ANTEIL_SUMME)]
    return raus


if __name__ == "__main__":
    # Alle Kuerzel aus der Legende des Dienstes durchrechnen
    legende = """A A1 A2 A3 A2g Z Z1 Z2 Z3 Z2g M M1 M2 M3 M2g
                 K1 K2 K3 K2g R R1 R2 R3 R2g
                 NR2 NK2 NM2 NZ2 NA2 ÜR2 ÜK2 ÜK1 ÜM2
                 OR4 OR3 OK4 OK3 OK2 OK1 OM4 OM3 OM2 OM1 OZ4 OZ3""".split()
    print(f"{'Kürzel':<7}{'nFK mm':>8}  {'Grundwasser':<11} Regime")
    print("-" * 68)
    for k in legende:
        s = deute_stgr(k)
        if s is None:
            print(f"{k:<7}{'—':>8}  nicht deutbar (ohne Feuchtestufe)")
            continue
        marke = "  ZU NASS" if s.zu_nass else ""
        print(f"{k:<7}{s.nfk_mm:>8.0f}  {s.grundwasser:<11} "
              f"{PRAEFIX[s.praefix]['bez']}{marke}")
    print()
    print("Mosaikbeispiel 60 % A3 / 40 % M2:")
    m = deute_mosaik([("A3", 60), ("M2", 40)])
    print(f"  Mittel  {m['nfk_mittel']} mm")
    print(f"  feucht  {m['nfk_feucht']} mm  ({m['feuchteste_gruppe']})")
    print()
    print(namensnennung())
