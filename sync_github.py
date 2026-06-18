#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_github.py — Pousse automatiquement le dossier vers GitHub.

A quoi ca sert :
  - garde ton depot a jour (fini la version perimee sur GitHub)
  - sauvegarde ton historique (snapshots/, iv_history/) jour apres jour
  - synchronise tes DEUX machines via GitHub

Pre-requis (UNE SEULE FOIS, c'est toi qui le fais, pas moi) :
  1. Ouvre PowerShell dans ton dossier du dashboard.
  2. git init                                  (si pas deja fait)
     git branch -M main
     git remote add origin https://github.com/zonesecreteh-cmyk/Teste-Trading-Dashboard.git
     (si "remote origin already exists", ignore)
  3. Fais UN push manuel : git add -A && git commit -m "init" && git push -u origin main
     -> une fenetre te demande de te connecter a GitHub. Connecte-toi.
     Windows (Git Credential Manager) garde le jeton en memoire.
     A partir de la, ce script pousse tout seul, sans mot de passe.

Ensuite, ce script se lance sans rien te demander.
"""

import subprocess, sys, os, datetime as dt

REPO = os.path.dirname(os.path.abspath(__file__))


def run(args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def main():
    # Verifie qu'on est bien dans un depot git
    if not os.path.isdir(os.path.join(REPO, ".git")):
        sys.exit("Pas un depot git ici. Fais d'abord la config UNE FOIS (voir en haut du fichier).")

    run(["git", "add", "-A"])

    # Y a-t-il quelque chose a committer ?
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if not status:
        print("Rien de neuf a pousser — GitHub est deja a jour.")
        return

    msg = "auto: maj " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    c = run(["git", "commit", "-m", msg])
    if c.returncode != 0:
        print("Echec commit :", c.stderr.strip() or c.stdout.strip())
        return

    p = run(["git", "push"])
    if p.returncode != 0:
        print("Echec push :", p.stderr.strip() or p.stdout.strip())
        print("-> Si c'est un probleme d'authentification, refais UN push manuel "
              "(git push) pour reconnecter ton compte GitHub.")
        return

    print("OK pousse vers GitHub :", msg)


if __name__ == "__main__":
    main()
