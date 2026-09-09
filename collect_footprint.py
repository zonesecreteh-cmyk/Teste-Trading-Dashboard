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

À mettre dans le Planificateur de tâches Windows, toutes les 2 heures.
Silencieux par design (échec réseau = skip) : la fraîcheur du collecteur est
visible sur le dashboard via data_health() (carte « Santé des collecteurs »),
pas ici.
"""
import sys, os, datetime as dt
import flow_engine as fe

# 150 min = 2h30 : l'intervalle de la tâche (2h) + 30 min de marge contre un run
# en retard ou manqué. L'idempotence stricte de record_footprint() rend ce
# recouvrement gratuit (jamais de double comptage).
FENETRE_MIN = 150


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
