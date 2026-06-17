#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily.py — Run quotidien automatique.
Analyse TOUS les actifs et enregistre un snapshot du jour. C'est ce qui
construit, jour après jour, l'historique DEX & GEX affiché sur le dashboard.

À mettre dans le Planificateur de tâches Windows (voir plus bas).
Lance-le une fois par jour, idéalement en fin de séance US.
"""

import os, json, datetime as dt
import flow_engine as fe

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def main():
    os.makedirs(OUT, exist_ok=True)
    date = dt.date.today().isoformat()
    print(f"\n=== Run quotidien {date}  [flow_engine {fe.VERSION}] ===")
    for asset in fe.ASSETS:                       # les 10 actifs configurés
        try:
            data = fe.analyse(asset)              # <- enregistre aussi le point d'historique
            with open(os.path.join(OUT, f"{asset}_{date}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
            c = data["convergence"]
            print(f"  OK  {asset:<4} -> {c['direction']:<9} "
                  f"conviction {c['conviction']:<8} score {c['score']:+}  "
                  f"(point d'historique enregistré)")
        except Exception as e:
            print(f"  ERR {asset:<4} -> {e}")
    print(f"=== Terminé. Historique mis à jour, snapshots dans {OUT} ===\n")


if __name__ == "__main__":
    main()
