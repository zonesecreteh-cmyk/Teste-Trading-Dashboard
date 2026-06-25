#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily.py — Run quotidien automatique.
Analyse TOUS les actifs et enregistre, pour CHAQUE horizon (court / swing / long),
un point d'historique DEX & GEX calculé avec un DTE FIGÉ.

Pourquoi par horizon : un scalp (DTE court) et un swing (DTE long) ne voient pas le
même open interest, donc pas les mêmes chiffres. Les mélanger sur une seule courbe
rend l'historique faux. Ici, chaque horizon a sa propre série cohérente — et SEUL ce
script écrit l'historique (les vues interactives du dashboard ne l'écrivent jamais).

À mettre dans le Planificateur de tâches Windows. Lance-le une fois par jour,
idéalement en fin de séance US.
"""

import os, json, datetime as dt
import flow_engine as fe

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def main():
    os.makedirs(OUT, exist_ok=True)
    date = dt.date.today().isoformat()
    print(f"\n=== Run quotidien {date}  [flow_engine {fe.VERSION}] ===")
    print(f"    Horizons : {', '.join(f'{h}({int(d)}j)' for h, d in fe.HORIZONS)}\n")

    for asset in fe.ASSETS:                       # tous les actifs configurés
        try:
            S, book = fe.fetch_chain(asset)       # fetch réseau UNE seule fois
        except Exception as e:
            print(f"  ERR {asset:<4} -> fetch impossible : {e}")
            continue

        last = None
        for horizon, dte in fe.HORIZONS:
            try:
                data = fe.analyse(asset, dte_days=dte, store_history=True,
                                  prefetched=(S, book))   # écrit l'historique de CET horizon
                last = data
                c = data["convergence"]
                print(f"  OK  {asset:<4} [{horizon:<5}] DEX {data['dex_total_musd']:>10.1f}M  "
                      f"GEX {data['gex_total_musd']:>8.1f}M  score {c['score']:+}")
            except Exception as e:
                print(f"  ERR {asset:<4} [{horizon}] -> {e}")

        # snapshot complet du jour (vue 'long' par défaut) pour archive
        if last is not None:
            with open(os.path.join(OUT, f"{asset}_{date}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(last, f, indent=2, default=str, ensure_ascii=False)

    print(f"\n=== Terminé. Historiques par horizon mis à jour, snapshots dans {OUT} ===\n")


if __name__ == "__main__":
    main()
