# -*- coding: utf-8 -*-
"""
DIAGNOSTIC DU TOTAL BOOK — d'ou vient le gamma, tranche d'echeance par tranche.

Repond a la question : "pourquoi le total (toutes echeances) s'ecarte-t-il autant du
trimestriel (<=90j) ?" Les echeances lointaines portent-elles vraiment autant de gamma,
ou y a-t-il des donnees douteuses (IV aberrante, OI nul, prix stale) ?

Le script recupere le book EN DIRECT et decompose :
  - le GEX par tranche (<=7j, 8-30j, 31-90j, >90j)
  - l'open interest et le nombre d'options de chaque tranche
  - un controle qualite : options a OI nul, IV aberrante (<1% ou >300%)
Puis il recalcule le total EN EXCLUANT les options douteuses, pour mesurer leur poids.

Usage :  py diagnostic_total.py SPX
         py diagnostic_total.py BTC NDX
"""
import sys, datetime as dt
import flow_engine as fe

ACTIFS = [a.upper() for a in sys.argv[1:]] or ["SPX"]
TRANCHES = [("<=7j", 0, 7), ("8-30j", 7, 30), ("31-90j", 30, 90), (">90j", 90, 99999)]


def diag(asset):
    if asset not in fe.ASSETS:
        print(f"{asset} : actif inconnu")
        return
    cfg = fe.ASSETS[asset]
    csize = cfg["contract"]
    print("=" * 74)
    print(f"DIAGNOSTIC TOTAL BOOK — {asset}")
    print("=" * 74)
    try:
        S, book = fe.fetch_chain(asset)
    except Exception as e:
        print(f"  fetch impossible : {e}")
        return
    print(f"  spot {S:,.2f} · {len(book)} options dans le book · contrat {csize}")

    now = dt.datetime.now(dt.timezone.utc)
    sc, sp = fe.SIGN_CALL, fe.SIGN_PUT

    def gex_of(o):
        sign = sc if o["type"] == "C" else sp
        return sign * (o.get("gamma") or 0) * o["oi"] * csize * (S ** 2) * 0.01

    # --- repartition par tranche ---
    print(f"\n  {'tranche':>8} {'GEX (M$)':>12} {'part':>7} {'options':>8} "
          f"{'OI total':>12} {'IV moy':>7}")
    total = 0.0
    lignes = []
    for nom, lo, hi in TRANCHES:
        sel = []
        for o in book:
            exp = o.get("expiry")
            if not exp:
                continue
            j = (exp - now).total_seconds() / 86400
            if lo < j <= hi:
                sel.append(o)
        g = sum(gex_of(o) for o in sel)
        total += g
        oi = sum(o["oi"] for o in sel)
        ivs = [o["iv"] for o in sel if o.get("iv")]
        lignes.append((nom, g, len(sel), oi, (sum(ivs) / len(ivs) * 100) if ivs else 0))
    for nom, g, n, oi, iv in lignes:
        part = (100 * abs(g) / sum(abs(x[1]) for x in lignes)) if any(x[1] for x in lignes) else 0
        print(f"  {nom:>8} {g/1e6:>12,.1f} {part:>6.1f}% {n:>8} {oi:>12,.0f} {iv:>6.1f}%")
    print(f"  {'TOTAL':>8} {total/1e6:>12,.1f}")

    # --- controle qualite ---
    oi_nul = [o for o in book if not o["oi"]]
    iv_abs = [o for o in book if not o.get("iv") or o["iv"] < 0.01 or o["iv"] > 3.0]
    loin = [o for o in book if o.get("expiry")
            and (o["expiry"] - now).total_seconds() / 86400 > 365]
    print(f"\n  QUALITE DES DONNEES")
    print(f"    options a OI nul          : {len(oi_nul):>6}")
    print(f"    IV aberrante (<1% ou >300%) : {len(iv_abs):>6}")
    print(f"    echeances au-dela d'1 an  : {len(loin):>6}"
          + (f" (GEX {sum(gex_of(o) for o in loin)/1e6:+,.1f} M$)" if loin else ""))

    # --- total en excluant les douteuses ---
    douteux = {id(o) for o in iv_abs}
    propre = sum(gex_of(o) for o in book if id(o) not in douteux)
    print(f"\n  TOTAL brut              : {total/1e6:>12,.1f} M$")
    print(f"  TOTAL sans IV aberrantes: {propre/1e6:>12,.1f} M$")
    ecart = total - propre
    if abs(total) > 1e-9:
        print(f"  -> les options a IV douteuse pesent {ecart/1e6:+,.1f} M$ "
              f"({100*abs(ecart)/max(abs(total), 1e-9):.1f}% du total)")

    # --- verdict ---
    tri = sum(g for nom, g, _, _, _ in lignes if nom != ">90j")
    lointain = next((g for nom, g, _, _, _ in lignes if nom == ">90j"), 0.0)
    print(f"\n  LECTURE")
    print(f"    <=90j (trimestriel) : {tri/1e6:>12,.1f} M$")
    print(f"    >90j                : {lointain/1e6:>12,.1f} M$")
    if abs(tri) > 1e-9 and abs(lointain) > 2 * abs(tri):
        print("    -> les echeances lointaines DOMINENT le total. A verifier :")
        print("       OI reel et IV moyenne de la tranche >90j ci-dessus.")
    else:
        print("    -> repartition plausible, pas de domination anormale du lointain.")


for a in ACTIFS:
    diag(a)
    print()
