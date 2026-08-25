# -*- coding: utf-8 -*-
"""
RECONSTRUIRE LE TOTAL BOOK depuis les snapshots JSON.

Pourquoi : une premiere version du reparateur SUPPRIMAIT les points dont le signe
paraissait incoherent, au lieu de les remettre a l'endroit. ~170 points ont ainsi
disparu des series *_total_dexgex.csv.

Heureusement, daily.py archive chaque jour un snapshot complet dans snapshots/ :
il contient book_total (dex/gex du book entier) ET gex_fixed_musd (convention fixe,
toujours correcte). On peut donc :
  1. relire tous les snapshots,
  2. retablir le signe quand le book_total du jour avait ete calcule en convention
     inversee (comparaison au gex_fixed du meme jour),
  3. reinjecter les jours manquants dans le CSV, sans toucher aux jours presents.

Usage :  py reconstruire_total.py              (apercu)
         py reconstruire_total.py --appliquer
"""
import os, sys, json, glob, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(BASE, "iv_history")
SNAP = os.path.join(BASE, "snapshots")
APPLIQUER = "--appliquer" in sys.argv


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def lire_csv(path):
    """{date: ligne_complete}"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split(",")
                if len(p) >= 3:
                    out[p[0]] = p
    except OSError:
        pass
    return out


def snapshots_par_actif():
    """{asset: {date: (dex, gex, spot, gex_fixed)}}"""
    data = {}
    for path in sorted(glob.glob(os.path.join(SNAP, "*.json"))):
        nom = os.path.basename(path)[:-5]
        if "_" not in nom:
            continue
        asset, date = nom.rsplit("_", 1)
        if len(date) != 10:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            continue
        bt = m.get("book_total") or {}
        gex, dex = num(bt.get("gex_musd")), num(bt.get("dex_musd"))
        if gex is None:
            continue
        data.setdefault(asset, {})[date] = (
            dex, gex, num(m.get("spot")), num(m.get("gex_fixed_musd")))
    return data


def main():
    if not os.path.isdir(SNAP):
        print(f"Dossier snapshots introuvable : {SNAP}")
        return
    snaps = snapshots_par_actif()
    if not snaps:
        print("Aucun snapshot exploitable trouve.")
        return

    print(f"{'MODE APPLICATION' if APPLIQUER else 'APERCU (aucune modification)'} — "
          f"{len(snaps)} actifs dans snapshots/\n" + "=" * 70)
    tot_add = tot_flip = 0
    for asset in sorted(snaps):
        csv_path = os.path.join(HIST, f"{asset}_total_dexgex.csv")
        present = lire_csv(csv_path)
        # reference de signe : gex_fixed du trimestriel du meme jour
        ref = {}
        try:
            with open(os.path.join(HIST, f"{asset}_trimestriel_dexgex.csv"),
                      encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split(",")
                    if len(p) >= 13:
                        v = num(p[12])
                        if v is not None:
                            ref[p[0]] = v
        except OSError:
            pass

        ajouts, flips = [], 0
        for date, (dex, gex, spot, gfix) in sorted(snaps[asset].items()):
            if date in present:
                continue                       # jour deja la : on n'y touche pas
            r = ref.get(date)
            if r is None:
                r = gfix                       # a defaut, le gex_fixed du snapshot
            g, d_ = gex, dex
            if (r is not None and abs(r) > 0.05 and abs(g) > 0.05
                    and (g > 0) != (r > 0)):
                g = -g                          # convention inversee ce jour-la
                if d_ is not None:
                    d_ = -d_
                flips += 1
            ajouts.append(f"{date},{round(d_, 1) if d_ is not None else ''},"
                          f"{round(g, 1)},{round(spot, 2) if spot else ''}")
        if ajouts:
            tot_add += len(ajouts)
            tot_flip += flips
            print(f"  {asset:8} +{len(ajouts):3} jours restaures"
                  + (f" (dont {flips} signes retablis)" if flips else ""))
            if APPLIQUER:
                if os.path.exists(csv_path):
                    shutil.copy2(csv_path, csv_path + ".avant_reconstruction")
                lignes = [",".join(p) for p in present.values()] + ajouts
                lignes.sort(key=lambda l: l.split(",")[0])
                with open(csv_path, "w", encoding="utf-8") as f:
                    for l in lignes:
                        f.write(l + "\n")

    print("=" * 70)
    print(f"TOTAL : {tot_add} jours a restaurer ({tot_flip} signes a retablir)")
    if tot_add and not APPLIQUER:
        print("\nPour appliquer :  py reconstruire_total.py --appliquer")
        print("(sauvegarde .avant_reconstruction creee pour chaque fichier)")
    elif tot_add:
        print("\nReconstruction terminee. Relance py verifier_coherence.py pour controler.")
    else:
        print("Rien a restaurer : les CSV couvrent deja tous les snapshots.")


if __name__ == "__main__":
    main()
