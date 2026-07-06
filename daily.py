#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily.py — Run quotidien automatique + RAPPORT DU MATIN.

1) Analyse TOUS les actifs et enregistre, pour CHAQUE horizon (court / mensuel /
   trimestriel), un point d'historique DEX & GEX calculé avec un DTE FIGÉ.
   SEUL ce script écrit l'historique par horizon.
2) Écrit un snapshot JSON complet par actif (archive du jour).
3) NOUVEAU : génère un rapport texte lisible dans rapports/rapport_YYYY-MM-DD.txt
   (verdicts, alertes concrètes, santé des collecteurs). Le dernier rapport est
   aussi copié dans rapports/dernier_rapport.txt pour un accès rapide.

À mettre dans le Planificateur de tâches Windows, une fois par jour.
"""

import os, json, datetime as dt
import flow_engine as fe

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "snapshots")
RAP = os.path.join(BASE, "rapports")

# Actifs mis en avant dans le rapport (les autres n'y figurent que s'ils ont
# une conviction non neutre).
FOCUS = ["BTC", "ETH", "SPY", "QQQ", "GC"]


def _fmt_pct(v, plus=True):
    if v is None:
        return "—"
    s = f"{v:+.2f}%" if plus else f"{v:.2f}%"
    return s


def build_alerts(asset, d):
    """Alertes concrètes et actionnables pour un actif donné."""
    alerts = []
    # 1) Grosse échéance imminente (leçon du 27 juin)
    cal = d.get("expiry_calendar") or {}
    for r in cal.get("rows", []):
        if r["days"] <= 7 and r["gamma_pct"] >= 25:
            alerts.append(f"{asset}: {r['gamma_pct']}% du gamma expire dans {r['days']}j "
                          f"({r['date']}) -> régime instable après cette date")
    # 2) Spot proche du gamma flip
    fv = d.get("flip_vs_spot_pct")
    if fv is not None and abs(fv) <= 1.0:
        alerts.append(f"{asset}: spot à {abs(fv):.1f}% du gamma flip -> zone de bascule de régime")
    # 3) Poche de liquidations proche (crypto)
    lm = d.get("liq_map")
    if lm:
        for b in (lm.get("above") or []) + (lm.get("below") or []):
            dist = abs(b["price"] / lm["spot"] - 1) * 100
            if dist <= 3 and b["musd"] >= 30:
                side = "shorts" if b["price"] > lm["spot"] else "longs"
                alerts.append(f"{asset}: poche de liquidations {side} ~{b['musd']}M$ "
                              f"à ${b['price']:,} ({dist:.1f}% du spot)")
                break
    # 4) Funding extrême (crypto)
    f = d.get("funding")
    if f and abs(f.get("annualized_pct") or 0) >= 25:
        alerts.append(f"{asset}: funding {f['annualized_pct']:+.0f}% annualisé -> "
                      f"levier déséquilibré, reversal possible")
    # 5) Gros mouvement d'OI (si mesuré)
    oi = d.get("oi_change_24h")
    if oi and oi.get("status") == "ok":
        nc, np_ = oi.get("net_call_doi") or 0, oi.get("net_put_doi") or 0
        if abs(nc) + abs(np_) > 0 and (abs(nc) > 3000 or abs(np_) > 3000):
            alerts.append(f"{asset}: OI {'calls' if abs(nc) > abs(np_) else 'puts'} "
                          f"en fort mouvement ({nc:+.0f} / {np_:+.0f} contrats vs {oi['asof']})")
    return alerts


def build_report(date, results):
    lines = []
    add = lines.append
    add(f"RAPPORT FLOW ENGINE — {date}  [moteur {fe.VERSION}]")
    add("=" * 68)

    # --- contexte macro (une seule fois, identique pour tous) ---
    mc = None
    for d in results.values():
        mc = d.get("macro_context")
        if mc:
            break
    if mc:
        add(f"\nCONTEXTE : {mc['regime']}"
            + (f"  (vote {mc['votes']:+} sur {mc['n_signals']})" if mc.get("n_signals") else ""))
        add(f"  SPX 5j {_fmt_pct(mc.get('spy_5d'))} · BTC 5j {_fmt_pct(mc.get('btc_5d'))}"
            f" · $ proxy 5j {_fmt_pct(mc.get('dxy_5d'))} · Or 5j {_fmt_pct(mc.get('gld_5d'))}")
        if mc.get("corr_btc_spx") is not None:
            add(f"  Corrélation BTC/SPX : {mc['corr_btc_spx']} sur {mc['corr_n']}j"
                + ("  (fiabilité limitée <15j)" if mc["corr_n"] < 15 else ""))

    # --- verdicts ---
    add("\nVERDICTS (horizon mensuel) :")
    shown = set()
    for asset in FOCUS:
        d = results.get(asset)
        if not d:
            continue
        shown.add(asset)
        c = d["convergence"]
        btm = d.get("book_total") or {}
        add(f"  {asset:5} {c['direction']:9} {c['conviction']:8} score {c['score']:+5} "
            f"| GEX total {btm.get('gex_musd', '—')}M$ | spot {d['spot']:,} "
            f"| max pain {d.get('max_pain', '—')}")
    extra = [(a, d) for a, d in results.items()
             if a not in shown and d["convergence"]["conviction"] in ("FORTE", "MODÉRÉE")]
    if extra:
        add("  — autres actifs avec signal :")
        for a, d in extra:
            c = d["convergence"]
            add(f"  {a:5} {c['direction']:9} {c['conviction']:8} score {c['score']:+5}")

    # --- alertes ---
    add("\nALERTES :")
    n_alerts = 0
    for asset, d in results.items():
        for al in build_alerts(asset, d):
            add(f"  ⚠ {al}")
            n_alerts += 1
    if n_alerts == 0:
        add("  (aucune)")

    # --- signe empirique ---
    for a in ("BTC", "ETH"):
        d = results.get(a)
        if d and (d.get("dealer_sign") or {}).get("mode") == "empirical":
            add(f"\nSIGNE DEALER {a} : empirique — {d['dealer_sign'].get('reason', '')}")

    # --- santé des collecteurs ---
    add("\nSANTÉ DES COLLECTEURS :")
    try:
        h = fe.data_health()
        icons = {"ok": "OK ", "warn": "!! ", "late": "XX ", "never": "-- "}
        for r in h["rows"]:
            age = ("aujourd'hui" if r["age_days"] == 0 else f"{r['age_days']}j"
                   ) if r["age_days"] is not None else "jamais"
            add(f"  [{icons.get(r['status'], '?')}] {r['flux']:38} {r['last'] or '—'} ({age})")
        if h["worst"] in ("late", "never"):
            add("  -> Au moins un flux en retard/absent : vérifier la tâche Windows « Flow daily ».")
    except Exception as e:
        add(f"  (santé indisponible : {e})")

    add("\n" + "=" * 68)
    add("Rappel : outil d'aide à la lecture du positionnement, pas un signal de trading.")
    return "\n".join(lines)


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(RAP, exist_ok=True)
    date = dt.date.today().isoformat()
    print(f"\n=== Run quotidien {date}  [flow_engine {fe.VERSION}] ===")
    print(f"    Horizons : {', '.join(f'{h}({int(d)}j)' for h, d in fe.HORIZONS)}\n")

    results = {}                                   # asset -> metrics (horizon mensuel)
    for asset in fe.ASSETS:                        # tous les actifs configurés
        try:
            S, book = fe.fetch_chain(asset)        # fetch réseau UNE seule fois
        except Exception as e:
            print(f"  ERR {asset:<4} -> fetch impossible : {e}")
            continue

        last = None
        for horizon, dte in fe.HORIZONS:
            try:
                data = fe.analyse(asset, dte_days=dte, store_history=True,
                                  prefetched=(S, book))   # écrit l'historique de CET horizon
                last = data
                if horizon == "mensuel":
                    results[asset] = data          # base du rapport
                c = data["convergence"]
                print(f"  OK  {asset:<4} [{horizon:<5}] DEX {data['dex_total_musd']:>10.1f}M  "
                      f"GEX {data['gex_total_musd']:>8.1f}M  score {c['score']:+}")
            except Exception as e:
                print(f"  ERR {asset:<4} [{horizon}] -> {e}")

        # snapshot complet du jour pour archive
        if last is not None:
            with open(os.path.join(OUT, f"{asset}_{date}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(last, f, indent=2, default=str, ensure_ascii=False)

    # === rapport du matin ===
    if results:
        try:
            report = build_report(date, results)
            path = os.path.join(RAP, f"rapport_{date}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            with open(os.path.join(RAP, "dernier_rapport.txt"), "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n--- RAPPORT ---\n{report}\n")
            print(f"=== Rapport écrit : {path} ===")
        except Exception as e:
            print(f"  ERR rapport -> {e}")

    print(f"\n=== Terminé. Historiques mis à jour, snapshots dans {OUT} ===\n")


if __name__ == "__main__":
    main()
