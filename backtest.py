# -*- coding: utf-8 -*-
"""
BACKTEST — est-ce que mes indicateurs anticipent vraiment le prix ?

Teste, sur l'historique reellement collecte, si les signaux du jour J ont un
lien avec le mouvement du prix entre J et J+1. Approche volontairement severe :
- on compare a un "hasard" (taux de hausse de base sur la periode)
- on affiche le nombre d'observations : sous 30, AUCUNE conclusion n'est fiable
- on teste la correlation ET le taux de reussite directionnel

Usage :  py backtest.py            (BTC par defaut, horizon mensuel)
         py backtest.py ETH court
"""
import os, sys, math, statistics as st
import flow_engine as fe

ASSET = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
HZ = sys.argv[2] if len(sys.argv) > 2 else "mensuel"


def charger():
    rows = fe._read_history_file(ASSET, HZ)
    return [r for r in rows if r.get("spot")]


def rendements(rows):
    """[(ligne_J, rendement J->J+1 en %)]"""
    out = []
    for i in range(len(rows) - 1):
        s0, s1 = rows[i]["spot"], rows[i + 1]["spot"]
        if s0 and s1:
            out.append((rows[i], (s1 / s0 - 1) * 100))
    return out


def correl(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else None


def tester(nom, paires, predicat, base_hausse):
    """predicat(ligne) -> True/False/None. Mesure le taux de hausse quand True."""
    sel = [(r, ret) for r, ret in paires if predicat(r) is True]
    if len(sel) < 5:
        print(f"  {nom:38} — seulement {len(sel)} cas (insuffisant)")
        return
    hausses = sum(1 for _, ret in sel if ret > 0)
    taux = 100 * hausses / len(sel)
    moy = sum(ret for _, ret in sel) / len(sel)
    ecart = taux - base_hausse
    verdict = ("aucun edge" if abs(ecart) < 8 else
               "piste faible" if abs(ecart) < 15 else "signal notable")
    if len(sel) < 30:
        verdict += " (n<30 : NON FIABLE)"
    print(f"  {nom:38} {len(sel):3} cas · {taux:5.1f}% hausse "
          f"({ecart:+5.1f} pt vs base) · rdt moy {moy:+.2f}% → {verdict}")


def rescore(rows, asset, crypto=True):
    """Recalcule le score de convergence de chaque jour passe avec la LOGIQUE ACTUELLE.

    Les scores stockes dans l'historique ont ete calcules jour apres jour, parfois
    avec des regles depuis corrigees (skew lu dans l'absolu au lieu d'etre compare
    au normal de l'actif). On rejoue donc L1+L2 a partir des colonnes brutes
    (gex, spot, max_pain, rr) pour tester la logique d'AUJOURD'HUI sur le passe.
    """
    # baseline du skew calculee sur l'historique lui-meme (comme le moteur)
    rrs = sorted(r["rr"] for r in rows if isinstance(r.get("rr"), (int, float)))
    if len(rrs) >= 20:
        n = len(rrs)
        med = rrs[n // 2] if n % 2 else (rrs[n // 2 - 1] + rrs[n // 2]) / 2
        ec = sorted(abs(v - med) for v in rrs)
        mad = ec[n // 2] if n % 2 else (ec[n // 2 - 1] + ec[n // 2]) / 2
        base, spread = med, max(0.8, 1.5 * mad)
    else:
        base, spread = (-3.5, 2.0) if crypto else (-1.5, 1.2)

    out = []
    for r in rows:
        gex, spot, mp, rr = r.get("gex"), r.get("spot"), r.get("max_pain"), r.get("rr")
        v1 = v2 = 0.0
        # L1 : aimantation max pain, uniquement si gamma positif (regime de range)
        if isinstance(gex, (int, float)) and isinstance(spot, (int, float)) \
                and isinstance(mp, (int, float)) and spot:
            if gex > 0:
                pct = 100 * (mp - spot) / spot
                if abs(pct) > 0.3:
                    v1 = (1 if pct > 0 else -1) * min(1.0, abs(pct) / 2)
        # L2 : skew compare au normal de l'actif
        if isinstance(rr, (int, float)):
            ecart = rr - base
            if abs(ecart) >= spread:
                v2 = (1 if ecart > 0 else -1) * min(1.0, abs(ecart) / (2 * spread))
        s = max(-10, min(10, (v1 + v2) / 2.5 * 10))
        r2 = dict(r)
        r2["score"] = round(s, 1)
        out.append(r2)
    print(f"  (skew normal retenu : {base:+.2f}, zone neutre +/-{spread:.2f})")
    return out


def main():
    rows = charger()
    if "--rescore" in sys.argv:
        print("\n*** MODE RESCORE : scores recalcules avec la logique ACTUELLE ***")
        rows = rescore(rows, ASSET, crypto=(fe.ASSETS.get(ASSET, {}).get("source") == "deribit"))
    print("=" * 74)
    print(f"BACKTEST {ASSET} — horizon {HZ} — {len(rows)} points d'historique")
    print("=" * 74)
    if len(rows) < 10:
        print("\nPas assez d'historique pour un backtest (minimum ~10 points).")
        print("Continue la collecte quotidienne et relance dans quelques semaines.")
        return

    paires = rendements(rows)
    n = len(paires)
    rets = [r for _, r in paires]
    hausses = sum(1 for r in rets if r > 0)
    base = 100 * hausses / n
    print(f"\nPeriode : {rows[0]['date']} → {rows[-1]['date']}")
    print(f"Base de reference : {base:.1f}% de jours en hausse "
          f"({hausses}/{n}) · rendement moyen {sum(rets)/n:+.2f}%")
    print(f"Volatilite quotidienne : {st.pstdev(rets):.2f}%")

    print("\n1. CORRELATIONS (indicateur du jour J vs rendement J→J+1)")
    print("   Une correlation |r| < 0.2 = pas de lien exploitable.")
    for nom, cle in [("GEX", "gex"), ("DEX", "dex"), ("Score convergence", "score"),
                     ("Risk reversal", "rr")]:
        xs, ys = [], []
        for r, ret in paires:
            v = r.get(cle)
            if isinstance(v, (int, float)):
                xs.append(v)
                ys.append(ret)
        if len(xs) < 5:
            print(f"  {nom:38} — {len(xs)} valeurs (insuffisant)")
            continue
        c = correl(xs, ys)
        if c is None:
            print(f"  {nom:38} — serie constante, correlation impossible")
            continue
        force = ("aucun lien" if abs(c) < 0.2 else
                 "lien faible" if abs(c) < 0.4 else "lien notable")
        fiab = "" if len(xs) >= 30 else "  (n<30 : NON FIABLE)"
        print(f"  {nom:38} r = {c:+.3f} sur {len(xs)} pts → {force}{fiab}")

    print("\n2. SIGNAUX DIRECTIONNELS (taux de hausse quand le signal est actif)")
    def spot_sous_maxpain(r):
        return (r.get("spot") and r.get("max_pain")
                and r["spot"] < r["max_pain"] * 0.99)
    def spot_sur_maxpain(r):
        return (r.get("spot") and r.get("max_pain")
                and r["spot"] > r["max_pain"] * 1.01)
    tester("GEX positif (regime range)", paires,
           lambda r: isinstance(r.get("gex"), (int, float)) and r["gex"] > 0, base)
    tester("GEX negatif (amplification)", paires,
           lambda r: isinstance(r.get("gex"), (int, float)) and r["gex"] < 0, base)
    tester("Score convergence > +2", paires,
           lambda r: isinstance(r.get("score"), (int, float)) and r["score"] > 2, base)
    tester("Score convergence < -2", paires,
           lambda r: isinstance(r.get("score"), (int, float)) and r["score"] < -2, base)
    tester("Spot sous le max pain (>1%)", paires, spot_sous_maxpain, base)
    tester("Spot au-dessus du max pain (>1%)", paires, spot_sur_maxpain, base)

    print("\n3. EFFET D'AIMANT DU MAX PAIN")
    rappr = tot = 0
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if not (a.get("spot") and a.get("max_pain") and b.get("spot")):
            continue
        d0 = abs(a["spot"] - a["max_pain"]) / a["spot"]
        d1 = abs(b["spot"] - a["max_pain"]) / b["spot"]
        if d0 > 0.005:
            tot += 1
            if d1 < d0:
                rappr += 1
    if tot >= 5:
        pct = 100 * rappr / tot
        v = ("aucun effet" if abs(pct - 50) < 8 else
             "effet leger" if abs(pct - 50) < 15 else "effet net")
        fiab = "" if tot >= 30 else "  (n<30 : NON FIABLE)"
        print(f"  Le prix se rapproche du max pain : {pct:.1f}% des cas "
              f"({rappr}/{tot}) → {v}{fiab}")
    else:
        print(f"  Pas assez de cas ({tot}).")

    print("\n" + "=" * 74)
    print("LECTURE HONNETE")
    print("=" * 74)
    if n < 30:
        print(f"Avec {n} observations, AUCUN resultat ci-dessus n'est statistiquement")
        print("fiable. Un ecart de 15 points sur 15 cas releve du hasard pur.")
        print("Il faut viser 60-100 jours de collecte propre pour commencer a conclure.")
    else:
        print(f"{n} observations : les tendances commencent a etre lisibles, mais")
        print("restent fragiles. Un vrai edge se confirme sur plusieurs centaines")
        print("d'observations ET sur des regimes de marche differents.")
    print("\nRappel : ces indicateurs decrivent une STRUCTURE (ou est le risque, ou")
    print("les dealers doivent hedger), pas une prediction de prix. Un GEX negatif")
    print("dit 'les mouvements seront amplifies', pas 'le prix va baisser'.")


if __name__ == "__main__":
    main()
