# -*- coding: utf-8 -*-
"""
REPARER L'HISTORIQUE — réaligne les séries GEX/DEX sur la convention FIXE.

Pourquoi : pendant quelques jours, le mode "empirique" a piloté les chiffres et
inversé le signe du GEX. La colonne principale (col 3) porte donc la convention
active du jour = série incohérente, avec des sauts qui ne viennent pas du marché.

Heureusement, le GEX en convention FIXE a TOUJOURS été écrit à part (col 13), et
le DEX-dealer fixe aussi (col 15). Ce script recopie ces colonnes fiables dans les
colonnes principales, pour toutes les lignes où elles existent.

Sécurité : une sauvegarde .bak est créée avant toute modification.
Usage :  py reparer_historique.py           (aperçu, ne modifie rien)
         py reparer_historique.py --appliquer
"""
import os, sys, glob, shutil, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(BASE, "iv_history")
APPLIQUER = "--appliquer" in sys.argv


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def traiter(path):
    """Renvoie (lignes_corrigees, total, nouvelles_lignes)."""
    out, fixed, total = [], 0, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < 3:
                out.append(line)
                continue
            total += 1
            gex_main = num(p[2])
            gex_fixe = num(p[12]) if len(p) >= 13 else None
            dex_main = num(p[1])
            dex_fixe = num(p[14]) if len(p) >= 15 else None
            changed = False
            # GEX : la colonne fixe fait foi
            if gex_fixe is not None and gex_main is not None and abs(gex_fixe - gex_main) > 0.05:
                p[2] = str(gex_fixe)
                changed = True
            # DEX dealer : idem (le DEX "positionnement" col 1 n'est pas signé dealer,
            # on ne le touche que s'il diverge franchement de sa version fixe)
            if dex_fixe is not None and dex_main is not None and abs(dex_fixe - dex_main) > 0.05:
                p[1] = str(dex_fixe)
                changed = True
            if changed:
                fixed += 1
            out.append(",".join(p) + "\n")
    return out, fixed, total


def _ref_gex_fixe(asset):
    """{date: gex_fixed} depuis le fichier trimestriel — colonne TOUJOURS ecrite en
    convention fixe, quel que soit le mode actif du jour. C'est notre reference."""
    ref = {}
    path = os.path.join(HIST, f"{asset}_trimestriel_dexgex.csv")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                p = line.rstrip("\n").split(",")
                if len(p) >= 13:
                    v = num(p[12])
                    if v is not None:
                        ref[p[0]] = v
    except OSError:
        pass
    return ref


def reparer_total():
    """Repare les series *_total_dexgex.csv (4 colonnes, sans colonne de secours).

    Methode par PREUVE et non par heuristique : pour chaque date, on compare le signe
    du GEX total a celui du gex_fixed trimestriel du meme jour (colonne toujours
    correcte). Le total (toutes echeances) et le trimestriel (<=90j) portent l'essentiel
    du meme gamma : un signe oppose signifie que le total a ete ecrit avec la convention
    inversee. Comme la pollution est une pure inversion de signe, on RETABLIT la valeur
    en l'inversant, au lieu de supprimer le point.
    """
    fichiers = sorted(glob.glob(os.path.join(HIST, "*_total_dexgex.csv")))
    tot_fix = tot_lignes = 0
    for path in fichiers:
        asset = os.path.basename(path).replace("_total_dexgex.csv", "")
        ref = _ref_gex_fixe(asset)
        if not ref:
            continue
        lignes, nfix = [], 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                p = line.rstrip("\n").split(",")
                g = num(p[2]) if len(p) >= 3 else None
                r = ref.get(p[0]) if p else None
                tot_lignes += 1
                if (g is not None and r is not None and abs(g) > 0.05 and abs(r) > 0.05
                        and (g > 0) != (r > 0)):
                    p[2] = str(round(-g, 1))          # inversion : on retablit
                    d_ = num(p[1])
                    if d_ is not None:
                        p[1] = str(round(-d_, 1))
                    nfix += 1
                lignes.append(",".join(p) + "\n")
        if nfix:
            tot_fix += nfix
            print(f"  {os.path.basename(path):42} {nfix:3} points reinverses "
                  f"(compares au trimestriel)")
            if APPLIQUER:
                shutil.copy2(path, path + ".bak")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lignes)
    return tot_fix, tot_lignes


def main():
    if not os.path.isdir(HIST):
        print(f"Dossier introuvable : {HIST}")
        return
    fichiers = sorted(glob.glob(os.path.join(HIST, "*_dexgex.csv")))
    if not fichiers:
        print("Aucun fichier d'historique trouvé.")
        return

    print(f"{'MODE APPLICATION' if APPLIQUER else 'APERÇU (aucune modification)'} — "
          f"{len(fichiers)} fichiers\n" + "=" * 66)
    tot_fix = tot_lignes = 0
    touches = []
    for path in fichiers:
        lignes, fixed, total = traiter(path)
        tot_fix += fixed
        tot_lignes += total
        if fixed:
            touches.append((os.path.basename(path), fixed, total))
            if APPLIQUER:
                shutil.copy2(path, path + ".bak")
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lignes)

    for nom, fixed, total in touches:
        print(f"  {nom:42} {fixed:3}/{total:3} lignes réalignées")
    if not touches:
        print("  Aucune incohérence détectée — historique déjà propre.")

    print("\n--- Total book (series sans colonne de secours) ---")
    sup, tot_t = reparer_total()
    if not sup:
        print("  Aucun point incoherent detecte.")

    print("=" * 66)
    print(f"TOTAL : {tot_fix} lignes a corriger sur {tot_lignes}"
          + (f" | total book : {sup} points a retirer sur {tot_t}" if tot_t else ""))
    if (touches or sup) and not APPLIQUER:
        print("\nRien n'a été modifié. Pour appliquer :")
        print("   py reparer_historique.py --appliquer")
        print("(une sauvegarde .bak sera créée pour chaque fichier)")
    elif touches or sup:
        print("\nCorrection appliquée. Sauvegardes : *.csv.bak")
        print("Les graphiques d'historique sont maintenant homogènes (convention fixe).")


if __name__ == "__main__":
    main()
