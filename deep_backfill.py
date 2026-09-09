#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_backfill.py — Rattrapage historique PROFOND (6 mois), BTCUSDT + ETHUSDT
uniquement (altcoins exclus : volume trop faible pour un footprint lisible).

RESUMABLE SANS ÉTAT SÉPARÉ : relancer ce script après une interruption (crash,
Ctrl+C, redémarrage du PC) reprend exactement où il en était -- un jour déjà
agrégé (fichier .json.gz marqué complete=True) est simplement sauté, jamais
retéléchargé. Voir flow_engine.run_deep_backfill().

Mesuré sur un jour réel : ~6-7s/jour/actif, ~255 Ko/jour/actif compressé.
Estimation 180 jours x 2 actifs : ~41 min, ~90 Mo -- sous les seuils fixés
(4h / 2 Go), lancé sans confirmation supplémentaire.

Conçu pour tourner détaché de la session qui l'a lancé (voir run_deep_backfill.bat).
"""
import time, datetime as dt
import flow_engine as fe

ASSETS = ["BTC", "ETH"]
JOURS = 180


def main():
    debut = time.time()

    def on_progress(asset, date_str, statut, i, total):
        if statut == "deja_fait":
            return   # ne pas polluer le journal avec ce qui saute instantanement (reprise)
        ecoule_min = round((time.time() - debut) / 60, 1)
        print(f"[{i}/{total}] {asset} {date_str} -> {statut}  (ecoule: {ecoule_min} min)", flush=True)

    print(f"[deep_backfill] debut {dt.datetime.now(dt.timezone.utc).isoformat()} "
          f"-- {JOURS} jours x {len(ASSETS)} actifs ({', '.join(ASSETS)})", flush=True)
    fe.run_deep_backfill(ASSETS, days=JOURS, source="binance", on_progress=on_progress)
    print(f"[deep_backfill] termine en {round((time.time() - debut) / 60, 1)} min", flush=True)
    # footprint_backfill_status() est scope sur la fenetre COURTE (depuis le debut
    # de collecte, pour data_health()) -- ici on veut le bilan sur les JOURS.
    # jours de CETTE passe (180j), donc un decompte dedie sur la meme plage.
    hier = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    jours = [(hier - dt.timedelta(days=k)).isoformat() for k in range(JOURS)]
    for a in ASSETS:
        compte = {"complete": 0, "provisional": 0, "missing": 0}
        for j in jours:
            compte[fe._footprint_day_status(a, "binance", j)] += 1
        print(f"  {a} (sur {JOURS}j) : {compte}", flush=True)


if __name__ == "__main__":
    main()
