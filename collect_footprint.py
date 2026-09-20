#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_footprint.py — Collecteur footprint (order flow), Étape B de
ADDENDUM_FOOTPRINT.md.

Agrège les trades des ~2h30 dernières minutes en footprint 1 minute (par palier de
prix, acheteurs/vendeurs séparés) et les archive sur disque, pour chaque actif
crypto suivi. IDEMPOTENT : une minute déjà archivée est REMPLACÉE, jamais
additionnée — peut tourner en se chevauchant sans jamais doubler un volume.

Source : Binance (référence, l'essentiel du volume réel). Deribit n'est PAS
collecté ici : il reste une source de comparaison ponctuelle à la demande, pas
un historique à accumuler (cf. flow_engine.footprint(), section 7 de l'addendum).

À mettre dans le Planificateur de tâches Windows, toutes les 15 minutes (resserré
le 2026-09-20 : un intervalle de 2h laissait l'archive du jour jusqu'à 2h en
retard, ce que footprint() doit alors rattraper en LIVE à chaque requête -- c'est
la cause du chargement lent du mode footprint, pas la résolution demandée. Un
intervalle plus court réduit mécaniquement ce trou, donc le temps de chargement
à froid. Un run manqué (PC éteint/en veille) laisse un trou qui ne se rattrape
qu'au prochain deep_backfill sur ce jour-là -- voir footprint_backfill_status()).
Silencieux par design (échec réseau = skip) : la fraîcheur du collecteur est
visible sur le dashboard via data_health() (carte « Santé des collecteurs »),
pas ici.
"""
import sys, os, datetime as dt
import flow_engine as fe

# 35 min = intervalle de la tâche (15 min) x2 + marge, pour absorber UN run manqué
# sans se faire distancer. L'idempotence stricte de record_footprint() rend le
# recouvrement gratuit (jamais de double comptage) -- pas besoin de plus.
FENETRE_MIN = 35


def main():
    horodatage = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[collect_footprint] {horodatage} — début")
    for asset in fe.FOOTPRINT_ASSETS:
        try:
            fe.record_footprint(asset, source="binance", minutes_fenetre=FENETRE_MIN)
            print(f"  {asset}: ok")
        except Exception as e:
            # Ne doit jamais faire planter les autres actifs : chacun est indépendant.
            print(f"  {asset}: échec — {e}")
    print("[collect_footprint] terminé")


if __name__ == "__main__":
    main()
