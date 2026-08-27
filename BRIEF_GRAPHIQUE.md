# BRIEF — Refonte du graphique Flow Engine (page /chart)

Point de depart pour Claude Code (Antigravity). Dossier du projet :
`C:\Users\Hugob\Desktop\Test Trading Dasbord`

## 1. Contexte du projet

Flow Engine est un outil local d'analyse de flux d'options (GEX/DEX, gamma flip, max pain,
murs, liquidations) sur 31 actifs : 7 cryptos via Deribit (temps reel) et 24 indices/ETF via
CBOE (differe 15 min).

Fichiers principaux :

| Fichier | Role |
|---|---|
| `flow_engine.py` | ~2700 lignes. Tout le calcul : greeks Black-Scholes, GEX/DEX, niveaux, historiques, sources externes. |
| `flow_dashboard.py` | Serveur HTTP + interfaces. Le HTML du dashboard est stocke en base64 dans la constante `HTML_B64`. |
| `daily.py` | Run quotidien (22h) : ecrit les historiques et le rapport. |
| `audit.py`, `verifier_calculs.py`, `verifier_coherence.py`, `backtest.py` | Outils de controle qualite. |
| `iv_history/` | CSV d'historiques quotidiens. |
| `snapshots/` | Un JSON complet par actif et par jour. |

Lancer : `py flow_dashboard.py` puis http://localhost:8000
Arreter : `.\Arreter_flow.bat` (tue python.exe ET pythonw.exe)

Piege connu : si un ancien processus Python tourne encore, il occupe le port 8000 et sert du
code perime. Toujours arreter proprement avant de relancer.

## 2. La page a refondre : /chart

Generee par `_chart_html()` dans `flow_dashboard.py`. Page HTML complete avec du JS inline qui
dessine des bougies directement sur un canvas (pas de librairie de graphique).

Pourquoi le canvas et pas Chart.js : Chart.js placait les series de barres cote a cote, ce qui
desalignait la meche du corps de la bougie. Le rendu manuel a resolu ca et il est plus rapide.
Garder le canvas.

### Architecture actuelle du JS

```
TOUT[]        toutes les bougies chargees {t,o,h,l,c,v}
i0, i1        fenetre visible (indices) ; i1-i0 = largeur, ne change QUE par zoom
yMin, yMax    echelle verticale ; null = ajustement automatique
NIV[]         niveaux du jour {v, c (couleur), t (libelle), fin (bool)}
HIST[]        historique quotidien des niveaux (max_pain, flip, call_wall, put_wall)
IND{}         etat des indicateurs actives (memorise en localStorage)
STYLE{}       couleurs personnalisees (localStorage)

charger()     appels reseau -> remplit TOUT, NIV, HIST -> appelle dessiner()
dessiner()    tout le rendu canvas (appele a chaque interaction, sans reseau)
```

### API disponibles

| Route | Contenu |
|---|---|
| `GET /api/candles/{actif}?res=1h` | `{res, temps_reel, source, bougies:[{t,o,h,l,c,v}]}` — resolutions : 1m 5m 15m 30m 1h 2h 4h 12h 1j |
| `GET /api/{actif}` | Tout le payload d'analyse : gamma_flip, max_pain, gamma_walls{call_wall,put_wall}, expected_move{pct,low,high}, liq_map{spot,above[],below[]}, gex_by_strike[{strike,gex_musd}], oi_change_24h{top[{cp,strike,doi}]}, expiry_calendar{rows[{date,days,gamma_pct,notional_musd,max_pain}]} |
| `GET /api/niveaux/{actif}` | Historique quotidien : `[{date, max_pain, flip, call_wall, put_wall}]` |

## 3. Bugs corriges (2026-08-27)

1. **Niveaux qui disparaissaient hors echelle** — desormais epingles en haut/bas du graphe
   avec une fleche (▲/▼) et leur valeur, au lieu de disparaitre completement.
2. **Interrupteur Grille** — gouverne maintenant a la fois les lignes horizontales et les
   separateurs verticaux de changement de jour/mois.
3. **Mur put qui disparaissait** — quand la valeur du jour est absente, on reprend la derniere
   valeur connue via `/api/niveaux/` et on la marque avec `≈` (estimee) ; sans aucun historique,
   l'indicateur reste simplement masque.
4. **Echelle Y instable au zoom** — `bornesY()` n'elargit plus l'echelle avec les niveaux
   proches (les poches de liquidation en particulier faisaient sauter l'echelle) ; elle suit
   desormais uniquement les bougies visibles. Les niveaux hors echelle sont epingles (cf. #1)
   plutot que d'influencer la projection prix→pixel.
5. **Volume de donnees** — passe de 2000 a 5000 bougies (`price_candles(..., bougies=5000)`
   dans `flow_engine.py`), la limite acceptee par Deribit. Comme seules les bougies visibles
   sont dessinees, le cout est negligeable.

## 4. Ce que fait TradingView et qu'on n'a pas encore

### Navigation
- Zoom molette centre sur le curseur : fait
- Glisser pour se deplacer : fait (largeur constante)
- Glisser sur l'axe des prix = compresser/etirer l'echelle : fait
- Glisser sur l'axe des dates = zoom horizontal : absent
- Auto-scale : bouton qui reajuste + reverrouillage automatique en revenant a droite : absent
- Inertie au relachement du glisser : absent
- Zoom par selection rectangulaire : absent
- Raccourcis clavier (fleches, +/-) : absent

### Presentation
- Axe des prix : densite adaptee a la hauteur, pas de chevauchement : partiel
- Axe des dates : hierarchie de graduations (heure -> jour -> mois -> annee) : partiel
- Etiquette de prix courant qui clignote a la mise a jour : absent
- Ligne horizontale du dernier prix prolongee jusqu'au bord droit : absent
- Marge droite ajustable par l'utilisateur : fixe a 12 colonnes
- Curseur : etiquette de date manquante sur l'axe (prix present)
- Panneau OHLC en haut a gauche avec variation %/valeur : partiel

### Outils de dessin (absents, demandes)
Ligne horizontale, ligne de tendance, rectangle, texte ; selection/deplacement/suppression ;
persistance par actif (localStorage) ; barre d'outils verticale a gauche.

### Autres
Types de graphe (bougies/barres/ligne/Heikin-Ashi), echelle log, comparaison de deux actifs,
plein ecran, capture PNG.

## 5. Indicateurs — etat actuel

Tous activables via la barre du haut, etat memorise en localStorage : Max pain, Gamma flip,
Mur call, Mur put, Liquidations, Profil GEX par strike, Echeances, Moyennes 20/50, Volume,
Expected move, Variation OI, Grille.

Idees supplementaires (donnees deja disponibles) :
- Profil de gamma cumule en fond (`gamma_profile`) en heatmap verticale
- VWAP de session (calculable depuis bougies + volume)
- Zones de forte concentration d'OI en bandes semi-transparentes plutot qu'en lignes
- Historique du gamma flip sur 90 jours une fois la collecte murie

## 6. Contraintes a respecter

- Aucune dependance externe (pas de CDN). Chart.js est deja embarque en dur mais le graphe
  n'en a plus besoin.
- Le HTML du dashboard principal est en base64 (`HTML_B64`) ; la page `/chart` est du texte
  Python normal (`_chart_html()`) — beaucoup plus simple a editer.
- Valider le JS apres modification (`node --check` sur les blocs script).
- Ne pas casser `/`, `/rapport`, `/backtest`, `/api/*`.
- Francais dans l'interface et les commentaires.
- Sobriete visuelle : fond `#0b0d12`, texte `#e8e6e3`, vert `#3fd07f`, rouge `#e0524f`,
  accents `#00d2ff` / `#e8a93c` / `#b48cff`.

## 7. Ordre de travail suggere

1. Corriger les 5 bugs de la section 3 — fait.
2. Restructurer `dessiner()` en fonctions distinctes (axes, bougies, niveaux, indicateurs,
   curseur) — a faire, le monolithe actuel est la cause des regressions.
3. Completer la navigation : glisser sur l'axe des dates, auto-scale, raccourcis clavier.
4. Structurer les axes proprement (densite adaptative, pas de chevauchement).
5. Outils de dessin (le plus gros morceau — a traiter en dernier).
