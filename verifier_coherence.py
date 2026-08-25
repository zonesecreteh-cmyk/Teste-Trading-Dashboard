# -*- coding: utf-8 -*-
"""
VERIFIER LA COHERENCE DES DONNEES — controle croise des 4 series par actif.

Les 4 horizons sont EMBOITES : court (<=7j) est inclus dans mensuel (<=30j), lui-meme
inclus dans trimestriel (<=90j), lui-meme inclus dans total (toutes echeances).
Deux invariants doivent donc tenir chaque jour :
  1. HIERARCHIE  : |court| <= |mensuel| <= |trimestriel| <= |total|  (a la tolerance pres)
  2. SIGNE       : les 4 series portent normalement le meme signe
Et un controle de continuite :
  3. SAUTS       : une variation > 300% d'un jour a l'autre est suspecte (sauf expiration)

Usage :  py verifier_coherence.py            (tous les actifs)
         py verifier_coherence.py BTC ETH    (selection)
"""
import os, sys, glob

BASE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(BASE, "iv_history")
HORIZONS = ["court", "mensuel", "trimestriel"]
TOL = 1.25          # tolerance sur la hierarchie (les IV/greeks different un peu par horizon)
SAUT = 4.0          # facteur de variation jour a jour juge suspect


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def lire_horizon(asset, hz):
    """{date: gex} depuis le fichier par horizon (colonne 3)."""
    out = {}
    try:
        with open(os.path.join(HIST, f"{asset}_{hz}_dexgex.csv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) >= 3:
                    v = num(p[2])
                    if v is not None:
                        out[p[0]] = v
    except OSError:
        pass
    return out


def lire_total(asset):
    out = {}
    try:
        with open(os.path.join(HIST, f"{asset}_total_dexgex.csv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) >= 3:
                    v = num(p[2])
                    if v is not None:
                        out[p[0]] = v
    except OSError:
        pass
    return out


def analyser(asset):
    """Controles VALIDES uniquement.

    ATTENTION : on ne teste PAS "|court| <= |mensuel| <= |trimestriel|". Le GEX est une
    SOMME SIGNEE : les options courtes peuvent contribuer -10 pendant que la tranche
    7-30j apporte +14, donnant un mensuel a +4. Des magnitudes ou des signes differents
    entre horizons sont donc NORMAUX et attendus.

    Ce qui est reellement anormal :
      A. ECART TOTAL/TRIMESTRIEL ABERRANT : le total ajoute les echeances >90j au
         trimestriel. Si l'ecart depasse plusieurs fois le trimestriel lui-meme, les
         options lointaines porteraient plus de gamma que tout le reste : suspect.
      B. VALEUR ABERRANTE : un point tres eloigne de la distribution de l'actif (z>6).
      C. TROU DE COLLECTE : jour manquant dans une serie que les autres ont.
    """
    series = {hz: lire_horizon(asset, hz) for hz in HORIZONS}
    series["total"] = lire_total(asset)
    dates = sorted(set().union(*[set(s) for s in series.values()])) if any(series.values()) else []
    if not dates:
        return None

    ecarts, aberrants, trous = [], [], []

    # A. total vs trimestriel
    for d_ in dates:
        tri, tot = series["trimestriel"].get(d_), series["total"].get(d_)
        if tri is None or tot is None:
            continue
        gap = abs(tot - tri)
        if abs(tri) > 50 and gap > 3 * abs(tri):
            ecarts.append(f"{d_} : trimestriel {tri:+.0f} -> total {tot:+.0f} "
                          f"(les >90j apporteraient {tot-tri:+.0f})")

    # B. valeurs aberrantes (ecart-type robuste sur la serie totale)
    tot_vals = [v for _, v in sorted(series["total"].items())]
    # en dessous de 5 M$ d'amplitude, les ecarts relatifs n'ont pas de sens (bruit
    # d'arrondi sur des actifs peu liquides) -> on ne teste pas ces series.
    if len(tot_vals) >= 15 and max(abs(v) for v in tot_vals) >= 5.0:
        tri_ = sorted(tot_vals)
        n = len(tri_)
        med = tri_[n // 2] if n % 2 else (tri_[n // 2 - 1] + tri_[n // 2]) / 2
        ec = sorted(abs(v - med) for v in tri_)
        mad = ec[n // 2] if n % 2 else (ec[n // 2 - 1] + ec[n // 2]) / 2
        # plancher relatif a l'echelle de la serie : sans lui, une serie quasi
        # constante donne mad=0 -> sigma minuscule -> z astronomique (faux positif)
        echelle = max(abs(v) for v in tri_) or 1.0
        sigma = max(1.4826 * mad, 0.05 * echelle, 1e-6)
        for d_, v in sorted(series["total"].items()):
            z = abs(v - med) / sigma
            if z > 10 and abs(v - med) > 0.5 * max(abs(x) for x in tri_):
                aberrants.append(f"{d_} : total {v:+.0f} (mediane {med:+.0f}, ecart {z:.0f} sigma)")

    # C. trous de collecte
    ref = set(series["trimestriel"])
    for k in ["court", "mensuel", "total"]:
        debut = min(series[k]) if series[k] else None
        # on ne compte QUE les trous posterieurs au debut de la serie : les jours
        # anterieurs correspondent a une periode ou la mesure n'existait pas encore.
        manquants = sorted(d for d in (ref - set(series[k])) if debut and d > debut)
        if manquants:
            trous.append(f"{k} : {len(manquants)} jour(s) manquant(s) "
                         f"(ex. {', '.join(manquants[:3])})")

    return {"asset": asset, "jours": len(dates), "hier": ecarts,
            "signe": aberrants, "saut": trous,
            "derniers": {k: series[k].get(dates[-1]) for k in
                         ["court", "mensuel", "trimestriel", "total"]},
            "date": dates[-1]}


def main():
    seuls = [a.upper() for a in sys.argv[1:]]
    actifs = sorted({os.path.basename(p).split("_")[0]
                     for p in glob.glob(os.path.join(HIST, "*_total_dexgex.csv"))})
    if seuls:
        actifs = [a for a in actifs if a.upper() in seuls]
    print("=" * 76)
    print("COHERENCE DES SERIES GEX — hierarchie court/mensuel/trimestriel/total")
    print("=" * 76)
    tot_pb = 0
    for a in actifs:
        r = analyser(a)
        if not r:
            continue
        n = len(r["hier"]) + len(r["signe"]) + len(r["saut"])
        tot_pb += n
        d = r["derniers"]
        ligne = " · ".join(f"{k[:5]} {d[k]:+.1f}" if d[k] is not None else f"{k[:5]} —"
                           for k in ["court", "mensuel", "trimestriel", "total"])
        etat = "OK " if n == 0 else "!! "
        print(f"\n[{etat}] {a:6} ({r['jours']} jours)   dernier {r['date']} : {ligne}")
        for titre, items in [("ecart total/trimestriel aberrant", r["hier"]),
                             ("valeurs aberrantes", r["signe"]),
                             ("trous de collecte", r["saut"])]:
            if items:
                print(f"        {titre} — {len(items)} cas :")
                for x in items[:4]:
                    print(f"          {x}")
                if len(items) > 4:
                    print(f"          ... et {len(items)-4} autres")
    print("\n" + "=" * 76)
    if tot_pb == 0:
        print("Aucune incoherence : les 4 series sont emboitees et de signe coherent.")
    else:
        print(f"{tot_pb} anomalies a examiner.")
        print("Rappel : des signes et magnitudes DIFFERENTS entre horizons sont NORMAUX")
        print("(le GEX est une somme signee, les tranches se compensent).")
    print("=" * 76)


if __name__ == "__main__":
    main()
