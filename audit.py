# -*- coding: utf-8 -*-
"""
AUDIT FONCTIONNEL FLOW ENGINE — teste les 31 actifs en conditions réelles.
Usage : placer ce fichier à côté de flow_engine.py, puis :  py audit.py
Durée : ~2-5 min (un fetch réseau par actif, pause de politesse entre chaque).
Verdicts : OK = conforme · WARN = tolérable (illiquidité, collecte en cours,
source externe muette) · FAIL = anomalie à corriger.
"""
import sys, time, traceback
import flow_engine as fe

TAPE_ASSETS = ("BTC", "ETH")          # tape + block trades + basis + coinbase
HL_COINS = ("BTC", "ETH", "SOL", "XRP", "AVAX", "TRX", "HYPE")

def is_num(x):
    return isinstance(x, (int, float)) and x == x

def check_asset(asset, cfg):
    okc, warns, fails = 0, [], []
    def ok():            # compteur de succès
        nonlocal okc; okc += 1
    def warn(msg): warns.append(msg)
    def fail(msg): fails.append(msg)

    crypto = cfg["source"] == "deribit"
    try:
        t0 = time.time()
        m = fe.analyse(asset)
        dur = time.time() - t0
    except Exception as e:
        return okc, warns, [f"analyse() PLANTE : {type(e).__name__}: {e}"], 0.0

    # --- socle commun (tous actifs) ---
    if is_num(m.get("spot")) and m["spot"] > 0: ok()
    else: fail(f"spot invalide: {m.get('spot')}")
    for k in ("gex_total_musd", "dex_total_musd", "vex_musd", "theta_musd"):
        if is_num(m.get(k)): ok()
        else: fail(f"{k} manquant/invalide")
    if m.get("max_pain"): ok()
    else: fail("max_pain absent")
    if m.get("gamma_flip") is not None: ok()
    else: warn("gamma_flip=None (peut arriver si profil sans zéro)")

    h = m.get("histories") or {}
    if all(k in h for k in ("court", "mensuel", "trimestriel", "total")): ok()
    else: fail(f"histories incomplet: {list(h.keys())}")

    bt = m.get("book_total")
    if bt and is_num(bt.get("gex_musd")) and is_num(bt.get("dex_musd")): ok()
    else: fail("book_total invalide")

    cal = m.get("expiry_calendar")
    if cal and cal.get("rows"):
        ok()
        s = sum(r["gamma_pct"] for r in cal["rows"])
        if cal["n_expiries"] <= len(cal["rows"]) and not (60 <= s <= 100.5):
            warn(f"calendrier: somme gamma {s:.0f}% (troncature à 8 lignes = normal si >8 échéances)")
    else:
        fail("expiry_calendar vide")

    ivr = m.get("iv_rank")
    if ivr is None:
        warn("iv_rank=None (iv30 indisponible ?)")
    elif ivr.get("status") in ("ok", "collecting"): ok()
    else: fail(f"iv_rank statut inconnu: {ivr}")

    oi = m.get("oi_change_24h")
    if oi and oi.get("status") in ("ok", "collecting", "flat"): ok()
    else: fail(f"oi_change_24h invalide: {oi}")

    c = m.get("convergence")
    if c and c.get("direction") and c.get("conviction"):
        ok()
        kinds = {v.get("kind") for v in (c.get("levels") or {}).values()}
        if "directional" in kinds and "stress" in kinds: ok()
        else: fail("convergence: kinds directionnel/stress absents")
    else:
        fail("convergence invalide")

    if m.get("macro_context"): ok()
    else: warn("macro_context=None (historiques pas encore lisibles ?)")

    ds = m.get("dealer_sign") or {}
    if crypto:
        if ds.get("mode") in ("fixed", "empirical"): ok()
        else: fail(f"dealer_sign.mode inattendu: {ds.get('mode')}")
    else:
        if ds.get("locked") and ds.get("mode") == "fixed": ok()
        else: fail(f"macro: signe non verrouillé fixe ({ds.get('mode')}, locked={ds.get('locked')})")

    if m.get("rr_monthly") is None and m.get("rr_weekly") is None:
        warn("RR 25Δ indisponible (peu de strikes — illiquidité, attendu sur certains alts/ETF)")
    else:
        ok()

    # --- attentes par classe ---
    if crypto:
        if m.get("is_crypto") is True: ok()
        else: fail("is_crypto devrait être True")
        if m.get("funding"): ok()
        else: warn("funding=None (perp indisponible ?)")
        if m.get("stablecoins"): ok()
        else: warn("stablecoins=None (CoinGecko+DefiLlama muets — transitoire ?)")
        if m.get("funding_multi"): ok()
        else: warn("funding_multi=None (Binance/Bybit sans ce symbole ou géo-bloqué)")
        if asset in TAPE_ASSETS:
            for k in ("block_trades", "futures_basis", "tape_signs"):
                if m.get(k): ok()
                else: warn(f"{k}=None (attendu renseigné sur {asset})")
            if m.get("coinbase_premium") is not None: ok()
            else: warn("coinbase_premium=None")
        else:
            for k in ("block_trades", "futures_basis", "tape_signs"):
                if m.get(k) is None: ok()
                else: warn(f"{k} renseigné sur un altcoin (inattendu mais pas grave)")
        if asset in HL_COINS:
            if m.get("liq_map"): ok()
            else: warn("liq_map=None (Hyperliquid muet ou coin absent)")
    else:
        if m.get("is_crypto") is False: ok()
        else: fail("is_crypto devrait être False")
        for k in ("funding", "funding_multi", "coinbase_premium", "stablecoins",
                  "block_trades", "futures_basis", "liq_map"):
            if m.get(k) is None: ok()
            else: fail(f"FUITE crypto sur macro: {k} renseigné")

    return okc, warns, fails, dur


def main():
    only = [a.upper() for a in sys.argv[1:]]
    assets = [(k, v) for k, v in fe.ASSETS.items() if not only or k in only]
    print(f"AUDIT FLOW ENGINE — {len(assets)} actifs — version {fe.VERSION}\n" + "=" * 74)
    tot_ok = tot_w = tot_f = 0
    failed_assets = []
    for k, v in assets:
        okc, warns, fails, dur = check_asset(k, v)
        tot_ok += okc; tot_w += len(warns); tot_f += len(fails)
        status = "FAIL" if fails else ("WARN" if warns else "OK  ")
        print(f"[{status}] {k:8} ({v['source']:7}) {okc:2} ok, {len(warns)} warn, {len(fails)} fail  ({dur:.1f}s)")
        for w in warns: print(f"         ⚠ {w}")
        for f in fails: print(f"         ✗ {f}")
        if fails: failed_assets.append(k)
        time.sleep(0.4)   # politesse APIs
    print("=" * 74)
    print(f"TOTAL : {tot_ok} vérifications OK · {tot_w} avertissements · {tot_f} échecs")
    print("Actifs en échec :", ", ".join(failed_assets) if failed_assets else "aucun 🎉")
    print("\nNote : les WARN de type 'collecte' ou 'illiquidité' sont normaux et se")
    print("résorbent seuls. Seuls les FAIL demandent une correction de code.")

if __name__ == "__main__":
    main()
