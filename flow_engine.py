#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flow_engine.py — Moteur complet d'analyse de positionnement des dealers d'options.
Remplace et étend crypto_flow.py.

  - Ingestion crypto  (BTC, ETH)  -> API publique Deribit       (gratuit)
  - Ingestion indices (SPX, NDX)  -> CBOE delayed quotes JSON    (gratuit, J-1/différé)
  - Métriques : GEX, DEX, Risk Reversal 25d, Term structure, Max Pain, IV percentile
  - MOTEUR DE CONVERGENCE : 5 niveaux L1->L5 -> score [-10,+10] -> conviction -> sizing

Usage:
    python flow_engine.py BTC
    python flow_engine.py SPX
    python flow_engine.py NDX --json

Dépendances: requests, numpy, scipy
"""

import sys, json, math, os, re, datetime as dt
import requests
import numpy as np
from scipy.stats import norm

HIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iv_history")

VERSION = "2026-06-20-a"   # affiché à chaque lancement pour vérifier qu'on a la bonne version

# ---- Convention de signe dealer (SpotGamma : long calls / short puts) -------
SIGN_CALL, SIGN_PUT = +1.0, -1.0

# ---- Garde-fous données ------------------------------------------------------
# Plancher de maturité : sous ce seuil, le gamma Black-Scholes explose
# (options 0DTE) et fausse tout le GEX. On plafonne à ~1 jour.
T_FLOOR = 1.0 / 365.25
# On exclut carrément les options qui expirent dans moins de MIN_DTE_DAYS jour(s) :
# leur gamma est mécaniquement instable et fausse le GEX (surtout SPX/NDX 0DTE).
MIN_DTE_DAYS = 1.0
MIN_DTE = MIN_DTE_DAYS / 365.25
# Fenêtre d'échéances pour les métriques de POSITIONNEMENT (GEX, DEX, charm, vanna,
# matrice, max pain, gamma flip). Outil orienté COURT TERME (1 à 7 jours) : on ne garde
# que les échéances proches, qui pilotent le hedging immédiat des dealers. Les hebdo
# imminentes + la mensuelle la plus proche tiennent dans ~30 jours. Baisse à 14 pour
# coller encore plus court ; monte si tu veux réintégrer le book lointain.
# (La structure des échéances et le risk reversal gardent TOUT le book, eux.)
# ============================================================================
# SCOPE DU POSITIONNEMENT — réglable ici, par TYPE d'actif.
# C'est ce qui détermine l'AMPLEUR des GEX/DEX (combien d'échéances/strikes on somme).
# Plus c'est large, plus les chiffres sont gros. Tune ces 6 valeurs pour caler
# l'échelle sur ce que tu veux (ex. coller à une référence externe).
#   DTE_DAYS     = nb de jours d'échéances inclus (court terme = petit)
#   POS_BAND     = bande de strikes pour le DEX (±x% autour du spot)
#   DISPLAY_BAND = zoom du graphe GEX (±x% autour du spot)
# (La structure des échéances et le risk reversal gardent TOUT le book, eux.)
# ----------------------------------------------------------------------------
CRYPTO_DTE_DAYS, CRYPTO_POS_BAND, CRYPTO_DISPLAY_BAND = 10.0, 0.25, 0.20
INDEX_DTE_DAYS,  INDEX_POS_BAND,  INDEX_DISPLAY_BAND  = 14.0, 0.13, 0.06
# On ignore les strikes trop loin du spot (instruments parasites / illiquides).
STRIKE_MIN_RATIO, STRIKE_MAX_RATIO = 0.5, 1.5

# ---- Catalogue des actifs ---------------------------------------------------
ASSETS = {
    "BTC": {"source": "deribit", "contract": 1.0,   "label": "BTC"},
    "ETH": {"source": "deribit", "contract": 1.0,   "label": "ETH"},
    "SPX": {"source": "cboe",    "contract": 100.0, "cboe": "_SPX", "label": "SPX"},
    "NDX": {"source": "cboe",    "contract": 100.0, "cboe": "_NDX", "label": "NDX"},
    "RUT": {"source": "cboe",    "contract": 100.0, "cboe": "_RUT", "label": "RUT"},
    "VIX": {"source": "cboe",    "contract": 100.0, "cboe": "_VIX", "label": "VIX"},
    "DJX": {"source": "cboe",    "contract": 100.0, "cboe": "_DJX", "label": "DJX"},
    "UUP": {"source": "cboe",    "contract": 100.0, "cboe": "UUP",  "label": "UUP"},
    "EWG": {"source": "cboe",    "contract": 100.0, "cboe": "EWG",  "label": "DE EWG"},
    "EWQ": {"source": "cboe",    "contract": 100.0, "cboe": "EWQ",  "label": "FR EWQ"},
    "CL":  {"source": "cboe",    "contract": 100.0, "cboe": "USO",  "label": "CL"},
    "GC":  {"source": "cboe",    "contract": 100.0, "cboe": "GLD",  "label": "GC"},
    "EU 6E": {"source": "cboe",  "contract": 100.0, "cboe": "FXE",  "label": "EU 6E"},
    "JP 6J": {"source": "cboe",  "contract": 100.0, "cboe": "FXY",  "label": "JP 6J"},
    # Fallback si _NDX indisponible : décommente la ligne ci-dessous
    # "NDX": {"source": "cboe", "contract": 100.0, "cboe": "QQQ", "label": "Nasdaq (QQQ)"},
}


# =============================================================================
#  GREEKS — Black-Scholes (utilisé seulement quand la source ne les fournit pas)
# =============================================================================
def _d1(S, K, T, sigma, r=0.0):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

def bs_gamma(S, K, T, sigma):
    T = max(T, T_FLOOR)                     # évite l'explosion gamma des 0DTE
    d1 = _d1(S, K, T, sigma)
    return 0.0 if d1 is None else norm.pdf(d1) / (S * sigma * math.sqrt(T))

def bs_delta(S, K, T, sigma, is_call):
    d1 = _d1(S, K, T, sigma)
    if d1 is None:
        return 0.0
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0


# =============================================================================
#  INGESTION 1 — Deribit (crypto)
# =============================================================================
DERIBIT = "https://www.deribit.com/api/v2"

def _deribit_get(endpoint, **params):
    r = requests.get(f"{DERIBIT}/{endpoint}", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]

def ingest_deribit(currency):
    idx = {"BTC": "btc_usd", "ETH": "eth_usd"}[currency]
    S = _deribit_get("public/get_index_price", index_name=idx)["index_price"]
    rows = _deribit_get("public/get_book_summary_by_currency",
                        currency=currency, kind="option")
    now = dt.datetime.now(dt.timezone.utc)
    book = []
    for x in rows:
        parts = x.get("instrument_name", "").split("-")     # BTC-27JUN25-60000-C
        if len(parts) != 4:
            continue
        _, exp_str, strike_str, cp = parts
        try:
            strike = float(strike_str)
            expiry = dt.datetime.strptime(exp_str, "%d%b%y").replace(
                hour=8, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        oi, iv = x.get("open_interest") or 0.0, x.get("mark_iv")
        if not oi or iv is None or iv <= 0:
            continue
        if strike < STRIKE_MIN_RATIO * S or strike > STRIKE_MAX_RATIO * S:
            continue                                          # strike parasite
        iv = float(iv) / 100.0
        T = max((expiry - now).total_seconds() / (365.25 * 86400), 1e-6)
        if T < MIN_DTE:
            continue                                          # 0DTE exclu (gamma instable)
        is_call = (cp == "C")
        book.append({
            "type": "C" if is_call else "P", "strike": strike, "expiry": expiry,
            "oi": float(oi), "iv": iv, "T": T,
            "gamma": bs_gamma(S, strike, T, iv),                 # calculés (Deribit ne les donne pas ici)
            "delta": bs_delta(S, strike, T, iv, is_call),
        })
    return S, book


# =============================================================================
#  INGESTION 2 — CBOE delayed quotes (indices)
# =============================================================================
OCC = re.compile(r'([A-Z]+)(\d{6})([CP])(\d{8})$')

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.cboe.com/",
    "Origin": "https://www.cboe.com",
}

def _fetch_cboe_json(url):
    """
    CBOE est derrière Cloudflare qui bloque l'empreinte TLS de Python.
    On tente requests, puis on bascule sur curl_cffi (imite Chrome au niveau TLS).
    """
    try:
        r = requests.get(url, timeout=25, headers=_BROWSER_HEADERS)
        r.raise_for_status()
        return r.json()
    except Exception as e1:
        try:
            from curl_cffi import requests as creq
        except ImportError:
            raise RuntimeError(
                "CBOE a bloqué la connexion TLS (Cloudflare). "
                "Installe la parade : py -m pip install curl_cffi"
            ) from e1
        try:
            r = creq.get(url, impersonate="chrome", timeout=25, headers=_BROWSER_HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as e2:
            raise RuntimeError(
                "CBOE injoignable même avec imitation navigateur. "
                "Ton réseau (wifi entreprise / public) bloque peut-être cdn.cboe.com — "
                f"teste sur un autre réseau. Détail : {e2}"
            ) from e2


def ingest_cboe(symbol_cfg):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol_cfg['cboe']}.json"
    d = _fetch_cboe_json(url)["data"]
    S = d["current_price"]
    book = []
    now = dt.datetime.now(dt.timezone.utc)
    for o in d["options"]:
        m = OCC.search(o["option"])
        if not m:
            continue
        _, yymmdd, cp, strike8 = m.groups()
        strike = int(strike8) / 1000.0
        if strike < STRIKE_MIN_RATIO * S or strike > STRIKE_MAX_RATIO * S:
            continue                                          # strike parasite (ex: 200 vs spot 7554)
        try:
            expiry = dt.datetime.strptime(yymmdd, "%y%m%d").replace(
                hour=21, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        oi = o.get("open_interest") or 0.0
        iv = o.get("iv")
        if not oi or iv is None or iv <= 0:
            continue
        iv = float(iv)
        if iv > 3:                       # garde-fou si CBOE renvoie en %
            iv /= 100.0
        T = max((expiry - now).total_seconds() / (365.25 * 86400), 1e-6)
        if T < MIN_DTE:
            continue                                          # 0DTE exclu (gamma instable, fausse le GEX SPX)
        is_call = (cp == "C")
        # CBOE fournit delta/gamma -> on les prend, sinon BS en secours
        gamma = o.get("gamma")
        delta = o.get("delta")
        gamma = bs_gamma(S, strike, T, iv) if gamma in (None, 0) else float(gamma)
        delta = bs_delta(S, strike, T, iv, is_call) if delta is None else float(delta)
        book.append({"type": "C" if is_call else "P", "strike": strike,
                     "expiry": expiry, "oi": float(oi), "iv": iv, "T": T,
                     "gamma": gamma, "delta": delta})
    return S, book


# =============================================================================
#  MÉTRIQUES (contract_size paramétré : 1 crypto, 100 indices)
# =============================================================================
def gamma_exposure(book, S, csize):
    by_strike, total = {}, 0.0
    for o in book:
        sign = SIGN_CALL if o["type"] == "C" else SIGN_PUT
        gex = sign * o["gamma"] * o["oi"] * csize * (S ** 2) * 0.01
        by_strike[o["strike"]] = by_strike.get(o["strike"], 0.0) + gex
        total += gex
    return total, dict(sorted(by_strike.items()))

def gamma_flip(book, S, csize, span=0.20, steps=41):
    """
    Niveau de prix où le GEX TOTAL bascule de positif à négatif (le 'gamma flip').
    On recalcule le gamma de chaque option à des prix hypothétiques autour du spot
    et on cherche le prix où l'exposition gamma agrégée s'annule.
    Au-dessus : dealers long gamma (range). En dessous : short gamma (cassures).
    Renvoie None si aucun basculement dans la fenêtre ±span.
    """
    if not book:
        return None
    K  = np.array([o["strike"] for o in book], dtype=float)
    T  = np.array([o["T"]      for o in book], dtype=float)
    iv = np.array([o["iv"]     for o in book], dtype=float)
    oi = np.array([o["oi"]     for o in book], dtype=float)
    sg = np.array([SIGN_CALL if o["type"] == "C" else SIGN_PUT for o in book], dtype=float)
    keep = (T > 0) & (iv > 0)
    K, T, iv, oi, sg = K[keep], T[keep], iv[keep], oi[keep], sg[keep]
    if K.size == 0:
        return None
    sqrtT = np.sqrt(T)
    INV_SQRT_2PI = 0.3989422804014327
    levels = np.linspace(S * (1 - span), S * (1 + span), steps)
    prev_g = prev_L = None
    for Sp in levels:
        d1 = (np.log(Sp / K) + 0.5 * iv * iv * T) / (iv * sqrtT)
        pdf = INV_SQRT_2PI * np.exp(-0.5 * d1 * d1)
        gamma = pdf / (Sp * iv * sqrtT)
        g = float(np.sum(sg * gamma * oi * csize * (Sp ** 2) * 0.01))
        if prev_g is not None and (g >= 0) != (prev_g >= 0):
            t = prev_g / (prev_g - g) if (prev_g - g) != 0 else 0.5
            return round(prev_L + t * (Sp - prev_L), 2)   # interpolation du zéro
        prev_g, prev_L = g, Sp
    return None


def bs_charm(S, K, T, sigma, is_call):
    """dDelta/dT (variation du delta avec le temps, par an). r=q=0."""
    d1 = _d1(S, K, T, sigma)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(T)
    return -norm.pdf(d1) * d2 / (2 * T)


def bs_vanna(S, K, T, sigma):
    """dDelta/dVol (variation du delta avec la vol, pour 1.00 de vol). r=q=0."""
    d1 = _d1(S, K, T, sigma)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(T)
    return -norm.pdf(d1) * d2 / sigma


def charm_vanna_flow(book, S, csize):
    """
    Flux spot (M$) que les dealers doivent faire mécaniquement pour rester delta-neutres :
    - charm : à cause du passage du temps (sur 24h).
    - vanna : si l'IV bouge de +1 point.
    Modèle basé sur la convention de signe dealer (comme GEX/DEX), pas une vérité.
    """
    charm_daily = 0.0
    vanna_1pt = 0.0
    for o in book:
        sign = SIGN_CALL if o["type"] == "C" else SIGN_PUT
        ch = bs_charm(S, o["strike"], o["T"], o["iv"], o["type"] == "C")
        vn = bs_vanna(S, o["strike"], o["T"], o["iv"])
        charm_daily += sign * (ch / 365.0) * o["oi"] * csize * S
        vanna_1pt += sign * (vn * 0.01) * o["oi"] * csize * S
    return charm_daily, vanna_1pt


def scenario_matrix(book, S, csize, moves=(-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10)):
    """
    Pour chaque mouvement du spot, combien les dealers doivent acheter/vendre en spot
    pour rester couverts (flux de hedge mécanique). Déduit du profil delta du book.
    flow > 0 = dealers ACHÈTENT ; flow < 0 = dealers VENDENT.
    """
    def net_delta(Sp):
        tot = 0.0
        for o in book:
            sign = SIGN_CALL if o["type"] == "C" else SIGN_PUT
            tot += sign * bs_delta(Sp, o["strike"], o["T"], o["iv"], o["type"] == "C") * o["oi"] * csize
        return tot
    d0 = net_delta(S)
    out = []
    for m in moves:
        Sp = S * (1 + m)
        flow = -(net_delta(Sp) - d0) * Sp   # ce que les dealers tradent pour rester neutres
        out.append({"move_pct": round(m * 100, 1), "target": round(Sp, 2),
                    "flow_musd": round(flow / 1e6, 1)})
    return out


def delta_exposure(book, S, csize):
    # Delta net agrégé du book : le delta porte déjà son signe (call >0, put <0).
    # Pas de double signe -> la valeur peut être positive OU négative selon le
    # positionnement réel, au lieu de toujours ressortir positive.
    return sum(o["delta"] * o["oi"] * csize * S for o in book)

def max_pain(book):
    strikes = sorted({o["strike"] for o in book})
    best_k, best = None, float("inf")
    for s in strikes:
        loss = sum(o["oi"] * (max(0.0, s - o["strike"]) if o["type"] == "C"
                              else max(0.0, o["strike"] - s)) for o in book)
        if loss < best:
            best, best_k = loss, s
    return best_k

def _nearest_expiry(book, target_days):
    now = dt.datetime.now(dt.timezone.utc)
    return min({o["expiry"] for o in book}, key=lambda e: abs((e - now).days - target_days))

def risk_reversal(book, target_days):
    exp = _nearest_expiry(book, target_days)
    leg = [o for o in book if o["expiry"] == exp]
    calls = [o for o in leg if o["type"] == "C"]
    puts = [o for o in leg if o["type"] == "P"]
    if not calls or not puts:
        return None
    c25 = min(calls, key=lambda o: abs(o["delta"] - 0.25))
    p25 = min(puts, key=lambda o: abs(o["delta"] + 0.25))
    return round((c25["iv"] - p25["iv"]) * 100, 2)

def term_structure(book, S):
    now = dt.datetime.now(dt.timezone.utc)
    by_exp = {}
    for o in book:
        by_exp.setdefault(o["expiry"], []).append(o)
    curve = []
    for exp, opts in sorted(by_exp.items()):
        atm = min(opts, key=lambda o: abs(o["strike"] - S))
        curve.append({"days": (exp - now).days, "atm_iv": round(atm["iv"] * 100, 1)})
    return [c for c in curve if c["days"] >= 0]

def put_call_ratio(book):
    """Ratio Put/Call sur l'open interest. >1 = plus de puts (couverture/peur),
    <1 = plus de calls (appétit haussier). Jauge de sentiment classique."""
    call_oi = sum(o["oi"] for o in book if o["type"] == "C")
    put_oi = sum(o["oi"] for o in book if o["type"] == "P")
    if call_oi <= 0:
        return None
    return {"pcr_oi": round(put_oi / call_oi, 2),
            "call_oi": round(call_oi, 0), "put_oi": round(put_oi, 0)}

def vol_smile(book, S, band=0.15):
    """Smile de volatilité de l'échéance la plus proche : IV par strike (convention
    OTM — puts sous le spot, calls au-dessus). Montre toute la structure de peur/euphorie,
    là où le risk reversal ne donne que 2 points."""
    fronts = [o for o in book if o["T"] > 0]
    if not fronts:
        return None
    exp0 = min(o["expiry"] for o in fronts)
    lo, hi = S * (1 - band), S * (1 + band)
    by_strike = {}
    for o in fronts:
        if o["expiry"] != exp0 or not (lo <= o["strike"] <= hi):
            continue
        otm = (o["type"] == "P" and o["strike"] <= S) or (o["type"] == "C" and o["strike"] >= S)
        # privilégie l'option OTM à ce strike ; sinon prend ce qu'on a
        if o["strike"] not in by_strike or otm:
            by_strike[o["strike"]] = round(o["iv"] * 100, 1)
    pts = [{"strike": k, "iv": v} for k, v in sorted(by_strike.items())]
    return pts if len(pts) >= 3 else None

def expected_move(curve, S):
    """Amplitude attendue jusqu'à la prochaine échéance, à partir de l'IV ATM front.
    EM% = IV_atm × √(T). C'est LA donnée que les pros regardent pour cadrer un trade :
    'le marché price ±X% d'ici l'échéance'."""
    fronts = [c for c in curve if c["days"] >= 1]
    if not fronts:
        return None
    f = fronts[0]
    iv = f["atm_iv"] / 100.0
    T = f["days"] / 365.25
    pct = iv * (T ** 0.5) * 100
    return {"days": f["days"], "pct": round(pct, 2),
            "usd": round(S * pct / 100, 0),
            "low": round(S * (1 - pct / 100), 0),
            "high": round(S * (1 + pct / 100), 0)}

def gamma_walls(gex_strikes):
    """Strikes à plus fort GEX = murs (aimants de support/résistance).
    Mur call = plus gros GEX positif ; mur put = plus gros GEX négatif (abs)."""
    if not gex_strikes:
        return {"call_wall": None, "put_wall": None}
    items = list(gex_strikes.items())
    pos = [(k, v) for k, v in items if v > 0]
    neg = [(k, v) for k, v in items if v < 0]
    call_wall = max(pos, key=lambda kv: kv[1])[0] if pos else None
    put_wall = min(neg, key=lambda kv: kv[1])[0] if neg else None
    return {"call_wall": call_wall, "put_wall": put_wall}

def deribit_funding(currency):
    """Funding 8h du perpétuel (crypto seulement). Renvoie None si indisponible :
    le dashboard affichera alors 'DONNÉE MANQUANTE' au lieu de planter."""
    try:
        t = _deribit_get("public/ticker", instrument_name=f"{currency}-PERPETUAL")
        f8 = t.get("funding_8h")
        if f8 is None:
            return None
        return {"rate_8h_pct": round(f8 * 100, 4),
                "annualized_pct": round(f8 * 3 * 365 * 100, 1)}   # 3 fenêtres de 8h/jour
    except Exception:
        return None

def detect_term_regime(curve):
    pts = curve[:6]
    if len(pts) < 2:
        return "INDÉTERMINÉ"
    front, back = pts[0]["atm_iv"], pts[-1]["atm_iv"]
    if front > back + 0.5:
        return "BACKWARDATION — stress détecté"
    if back > front + 0.5:
        return "CONTANGO — calme, pas de stress immédiat"
    return "PLAT"

def iv_percentile(asset, atm_iv_30d):
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, f"{asset}.csv")
    today = dt.date.today().isoformat()
    hist = []
    if os.path.exists(path):
        for line in open(path):
            try:
                d, v = line.strip().split(",")
                hist.append((d, float(v)))
            except ValueError:
                pass
    if not hist or hist[-1][0] != today:
        with open(path, "a") as f:
            f.write(f"{today},{atm_iv_30d}\n")
        hist.append((today, atm_iv_30d))
    vals = [v for _, v in hist]
    if len(vals) < 2:
        return None, len(vals)
    return round(100 * sum(v <= atm_iv_30d for v in vals) / len(vals)), len(vals)


def dex_gex_history(asset, dex_musd, gex_musd, score=None, spot=None, max_pain=None):
    """Stocke (date, DEX, GEX, score, spot, max_pain) une fois par jour et renvoie l'historique.
    spot + max_pain servent de fondation pour la vol réalisée (VRP), la migration du max pain
    et le backtest des signaux. Rétro-compatible avec les anciens fichiers 3/4 colonnes."""
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, f"{asset}_dexgex.csv")
    today = dt.date.today().isoformat()

    def _f(x):
        return float(x) if x not in ("", "None", None) else None

    def _parse(line):
        p = line.strip().split(",")
        if len(p) < 3:
            return None
        d = p[0]
        dx = _f(p[1]); gx = _f(p[2])
        sc = _f(p[3]) if len(p) >= 4 else None
        sp = _f(p[4]) if len(p) >= 5 else None
        mp = _f(p[5]) if len(p) >= 6 else None
        return {"date": d, "dex": dx, "gex": gx, "score": sc, "spot": sp, "max_pain": mp}

    def _row(h):
        def s(v): return "" if v is None else v
        return f"{h['date']},{s(h['dex'])},{s(h['gex'])},{s(h['score'])},{s(h['spot'])},{s(h['max_pain'])}\n"

    hist = []
    if os.path.exists(path):
        for line in open(path):
            row = _parse(line)
            if row:
                hist.append(row)

    point = {"date": today, "dex": dex_musd, "gex": gex_musd, "score": score,
             "spot": spot, "max_pain": max_pain}
    if not hist or hist[-1]["date"] != today:
        with open(path, "a") as f:
            f.write(_row(point))
        hist.append(point)
    else:                                          # met à jour la valeur du jour
        hist[-1] = point
        with open(path, "w") as f:
            for h in hist:
                f.write(_row(h))
    return hist[-90:]                              # 90 derniers jours max


# =============================================================================
#  MOTEUR DE CONVERGENCE — 5 niveaux -> score -> conviction -> sizing
# =============================================================================
# NB : c'est une reconstruction COHÉRENTE et PARAMÉTRABLE de la logique du
# dashboard d'origine (dont je n'ai pas le code). Chaque niveau vote une
# direction ; le score n'est PAS une moyenne : il exige une convergence.
# Ajuste librement les seuils ci-dessous.

TH = {
    "regime_maxpain_pct": 0.3,   # écart max_pain/spot (%) pour trancher une direction
    "rr_bear": -2.0,             # risk reversal sous ce seuil = peur installée
    "rr_bull":  1.0,             # risk reversal au-dessus = appétit haussier
    "ivp_calme": 30,             # IV percentile bas = liquidité confortable
    "ivp_stress": 70,            # IV percentile haut = stress de liquidité
}

def _vote(direction, strength, reason):
    return {"verdict": direction, "strength": strength, "reason": reason}

def level_regime(m):
    """L1 — range vs amplification + aimantation max pain."""
    gex = m["gex_total_musd"]
    mp_pct = m["max_pain_vs_spot_pct"]
    if gex < 0:
        return _vote("NEUTRAL", 0.3, "Gamma négatif : amplification, pas d'ancrage directionnel")
    # gamma positif -> prix aimanté vers le max pain
    if mp_pct > TH["regime_maxpain_pct"]:
        return _vote("BULL", min(1.0, abs(mp_pct) / 2), f"Max pain {mp_pct:+.1f}% au-dessus du spot, aimantation haussière")
    if mp_pct < -TH["regime_maxpain_pct"]:
        return _vote("BEAR", min(1.0, abs(mp_pct) / 2), f"Max pain {mp_pct:+.1f}% sous le spot, aimantation baissière")
    return _vote("NEUTRAL", 0.4, "Spot collé au max pain, pas de cible nette")

def level_positioning(m):
    """L2 — skew (risk reversal) + flux delta."""
    rr = m["rr_monthly"] if m["rr_monthly"] is not None else m["rr_weekly"]
    if rr is None:
        return _vote("NEUTRAL", 0.0, "Skew indisponible")
    if rr <= TH["rr_bear"]:
        return _vote("BEAR", min(1.0, abs(rr) / 5), f"Risk reversal {rr} : puts biddés, couverture institutionnelle")
    if rr >= TH["rr_bull"]:
        return _vote("BULL", min(1.0, rr / 5), f"Risk reversal {rr} : appétit call, optimisme")
    return _vote("NEUTRAL", 0.3, f"Risk reversal {rr} : skew neutre")

def level_structure(m):
    """L3 — term structure."""
    reg = m["term_regime"]
    if reg.startswith("BACKWARDATION"):
        return _vote("BEAR", 0.7, "Backwardation : stress immédiat anticipé")
    if reg.startswith("CONTANGO"):
        return _vote("BULL", 0.4, "Contango : pas de stress, conditions calmes")
    return _vote("NEUTRAL", 0.2, "Term structure plate")

def level_liquidity(m):
    """L4 — IV percentile."""
    ivp = m["iv_percentile"]
    if ivp is None:
        return _vote("NEUTRAL", 0.0, "Historique IV insuffisant (percentile en construction)")
    if ivp <= TH["ivp_calme"]:
        return _vote("BULL", 0.4, f"IV percentile {ivp} : vol basse, liquidité confortable")
    if ivp >= TH["ivp_stress"]:
        return _vote("BEAR", 0.6, f"IV percentile {ivp} : vol haute, stress de liquidité")
    return _vote("NEUTRAL", 0.2, f"IV percentile {ivp} : régime médian")

def level_catalyst(m, catalyst_bias=None):
    """L5 — événements / news. Hook : branche ici un flux news ou un override IA."""
    if catalyst_bias in ("BULL", "BEAR"):
        return _vote(catalyst_bias, 0.6, "Catalyseur externe fourni")
    return _vote("NEUTRAL", 0.0, "Aucun catalyseur connu (à brancher)")

def converge(metrics, catalyst_bias=None):
    levels = {
        "L1_REGIME":       level_regime(metrics),
        "L2_POSITIONING":  level_positioning(metrics),
        "L3_STRUCTURE":    level_structure(metrics),
        "L4_LIQUIDITE":    level_liquidity(metrics),
        "L5_CATALYST":     level_catalyst(metrics, catalyst_bias),
    }
    sign = {"BULL": +1, "BEAR": -1, "NEUTRAL": 0}
    # score brut = somme (direction × force), normalisé sur [-10, +10]
    raw = sum(sign[v["verdict"]] * v["strength"] for v in levels.values())
    score = round(max(-10, min(10, raw / 5 * 10)), 1)

    bulls = sum(1 for v in levels.values() if v["verdict"] == "BULL")
    bears = sum(1 for v in levels.values() if v["verdict"] == "BEAR")
    aligned = max(bulls, bears)
    direction = "HAUSSIER" if bulls > bears else "BAISSIER" if bears > bulls else "NEUTRE"

    # --- règle de convergence (pas de moyenne molle) ---
    # >=3 niveaux alignés -> conviction MODÉRÉE minimum imposée, NEUTRAL interdit
    # <=2 alignés -> NEUTRAL acceptable sauf signal isolé très fort
    if aligned >= 4:
        conviction = "FORTE"
    elif aligned >= 3:
        conviction = "MODÉRÉE"
    elif aligned == 2 and abs(score) >= 3:        # 2 alignés + signal pas isolé
        conviction = "FAIBLE"
    else:
        conviction, direction = "NEUTRE", "NEUTRE"

    # --- stress marché (0-10) : IV percentile + backwardation ---
    ivp = metrics["iv_percentile"] or 50
    stress = ivp / 10.0
    if metrics["term_regime"].startswith("BACKWARDATION"):
        stress = min(10.0, stress + 2.5)
    stress = round(stress, 1)
    stress_label = "CALM" if stress < 3 else "NORMAL" if stress < 6 else "ÉLEVÉ"

    # --- sizing : conviction × pénalité de stress ---
    base = {"FORTE": 1.0, "MODÉRÉE": 0.84, "FAIBLE": 0.5, "NEUTRE": 0.0}[conviction]
    stress_penalty = 1.0 - min(0.5, max(0.0, (stress - 5) / 10))
    sizing = round(base * stress_penalty, 2)

    return {
        "score": score, "direction": direction, "conviction": conviction,
        "aligned": aligned, "bulls": bulls, "bears": bears,
        "stress": stress, "stress_label": stress_label,
        "sizing": sizing, "levels": levels,
    }


# =============================================================================
#  ASSEMBLAGE
# =============================================================================
def analyse(asset, catalyst_bias=None, dte_days=None):
    asset = asset.upper()
    if asset not in ASSETS:
        raise ValueError(f"Actif inconnu : {asset}. Dispo : {list(ASSETS)}")
    cfg = ASSETS[asset]
    S, book = (ingest_deribit(asset) if cfg["source"] == "deribit"
               else ingest_cboe(cfg))
    if not book:
        raise RuntimeError("Aucune option exploitable récupérée.")
    csize = cfg["contract"]
    # COURT TERME : on ne garde que les échéances proches (<= MAX_DTE_DAYS).
    #  - book_dte  : échéances proches, TOUS les strikes -> GEX/charm/vanna/matrice/flip/max pain.
    #    Le gamma est naturellement concentré près du spot, pas besoin de couper les strikes
    #    (couper enlevait des strikes à GEX positif et rendait le GEX trop négatif).
    #  - book_pos  : en plus, strikes ±25% -> SEULEMENT le DEX, que les options profondément
    #    ITM (delta ≈ ±1) gonflaient artificiellement.
    # La structure des échéances et le risk reversal gardent le book complet.
    crypto = (cfg["source"] == "deribit")
    default_dte = CRYPTO_DTE_DAYS if crypto else INDEX_DTE_DAYS
    eff_dte = float(dte_days) if dte_days else default_dte
    max_dte = eff_dte / 365.25
    pos_band = CRYPTO_POS_BAND if crypto else INDEX_POS_BAND
    display_band = CRYPTO_DISPLAY_BAND if crypto else INDEX_DISPLAY_BAND
    pos_lo, pos_hi = 1 - pos_band, 1 + pos_band
    book_dte = [o for o in book if o["T"] <= max_dte] or book
    book_pos = [o for o in book_dte
                if pos_lo <= o["strike"] / S <= pos_hi] or book_dte

    gex_total, gex_strikes = gamma_exposure(book_dte, S, csize)
    dex_total = delta_exposure(book_pos, S, csize)
    flip = gamma_flip(book_dte, S, csize)
    curve = term_structure(book, S)
    atm30 = min(curve, key=lambda c: abs(c["days"] - 30))["atm_iv"] if curve else None
    ivp, n = iv_percentile(asset, atm30) if atm30 else (None, 0)
    mp = max_pain(book_dte)

    metrics = {
        "asset": asset, "label": cfg["label"], "spot": round(S, 2),
        "version": VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "gex_total_musd": round(gex_total / 1e6, 1),
        "gex_regime": "ANCRAGE (range)" if gex_total > 0 else "AMPLIFICATION (cassure)",
        "dex_total_musd": round(dex_total / 1e6, 1),
        "dex_flux": "haussier" if dex_total > 0 else "baissier",
        "max_pain": mp, "max_pain_vs_spot_pct": round(100 * (mp - S) / S, 2),
        "gamma_flip": flip,
        "flip_vs_spot_pct": (round(100 * (flip - S) / S, 2) if flip else None),
        "expected_move": expected_move(curve, S),
        "gamma_walls": gamma_walls(gex_strikes),
        "put_call": put_call_ratio(book_dte),
        "vol_smile": vol_smile(book_dte, S),
        "rr_weekly": risk_reversal(book, 7), "rr_monthly": risk_reversal(book, 30),
        "term_regime": detect_term_regime(curve),
        "iv_percentile": ivp, "iv_history_points": n,
        "term_curve": curve[:9],
        "gex_by_strike": [{"strike": k, "gex_musd": round(v / 1e6, 2)}
                          for k, v in gex_strikes.items()],
        "n_options": len(book_dte), "n_options_total": len(book),
        "display_band": display_band,
        "dte_days": eff_dte, "dte_default": default_dte,
    }
    metrics["convergence"] = converge(metrics, catalyst_bias)
    metrics["history"] = dex_gex_history(asset, metrics["dex_total_musd"],
                                         metrics["gex_total_musd"],
                                         metrics["convergence"]["score"],
                                         spot=S, max_pain=metrics.get("max_pain"))

    # --- Forecast dealer (charm/vanna) + matrice de scénarios : calculés sur le book ---
    charm_d, vanna_1 = charm_vanna_flow(book_dte, S, csize)
    metrics["charm_musd"] = round(charm_d / 1e6, 1)
    metrics["vanna_musd"] = round(vanna_1 / 1e6, 1)
    metrics["scenario_matrix"] = scenario_matrix(book_dte, S, csize)
    metrics["iv30"] = atm30

    # --- Champs nécessitant une source externe non branchée : explicitement None ---
    # Le dashboard affiche "DONNÉE MANQUANTE" pour chacun.
    metrics["rv30"] = None                # vol réalisée 30j -> besoin historique de prix
    metrics["funding"] = (deribit_funding(asset) if cfg["source"] == "deribit" else None)
    metrics["coinbase_premium"] = None    # Coinbase premium -> autre source
    metrics["oi_by_exchange"] = None      # OI Binance/Bybit/OKX -> autre source
    metrics["block_trades"] = None        # block trades >1M$ -> flux de trades Deribit
    metrics["oi_change_24h"] = None       # variation OI 24h -> 2+ jours de snapshots
    metrics["stablecoin_supply"] = None   # supply USDT/USDC -> source on-chain
    return metrics


def render(m):
    c = m["convergence"]
    print(f"\n{'='*64}")
    print(f"  [flow_engine {VERSION}]")
    print(f"  {m['label']} ({m['asset']})   spot {m['spot']:,.0f}   {m['timestamp']}")
    print(f"{'='*64}")
    print(f"  BIAIS  {c['direction']}  ·  conviction {c['conviction']}  ·  score {c['score']:+}/10")
    print(f"  Stress marché : {c['stress_label']} {c['stress']}/10   ·   Sizing ×{c['sizing']}")
    print(f"  {c['aligned']}/5 niveaux alignés ({c['bulls']} bull / {c['bears']} bear)")
    print(f"{'-'*64}")
    for name, v in c["levels"].items():
        print(f"  {name:<16} {v['verdict']:<8} {v['reason']}")
    print(f"{'-'*64}")
    print(f"  GEX  {m['gex_total_musd']:+,.1f} M$  ({m['gex_regime']})")
    print(f"  DEX  {m['dex_total_musd']:+,.1f} M$  (flux {m['dex_flux']})")
    print(f"  Max pain {m['max_pain']:,.0f} ({m['max_pain_vs_spot_pct']:+.2f}%)  ·  "
          f"RR {m['rr_weekly']}/{m['rr_monthly']}  ·  {m['term_regime']}")
    print(f"  IV percentile {m['iv_percentile']} ({m['iv_history_points']}j d'historique)\n")


def debug_gex(asset):
    """Imprime les options qui pèsent le plus dans le GEX, pour diagnostiquer une barre aberrante."""
    asset = asset.upper()
    cfg = ASSETS[asset]
    S, book = (ingest_deribit(asset) if cfg["source"] == "deribit" else ingest_cboe(cfg))
    csize = cfg["contract"]
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for o in book:
        sign = SIGN_CALL if o["type"] == "C" else SIGN_PUT
        gex = sign * o["gamma"] * o["oi"] * csize * (S ** 2) * 0.01
        days = (o["expiry"] - now).days
        rows.append((abs(gex), o["strike"], o["type"], days, o["oi"], o["iv"], o["gamma"], gex))
    rows.sort(reverse=True)
    print(f"\n=== DEBUG {asset}  [flow_engine {VERSION}] ===")
    print(f"Config : MIN_DTE={round(MIN_DTE*365.25,2)}j  strike_filter=[{STRIKE_MIN_RATIO}-{STRIKE_MAX_RATIO}]x spot")
    print(f"Spot = {S:,.2f}   options retenues = {len(book)}")
    print(f"{'strike':>9} {'type':>4} {'jours':>6} {'OI':>12} {'IV':>6} {'gamma':>9} {'GEX (M$)':>12}")
    for _, k, t, d, oi, iv, g, gex in rows[:10]:
        print(f"{k:>9,.0f} {t:>4} {d:>6} {oi:>12,.0f} {iv*100:>5.1f}% {g:>9.5f} {gex/1e6:>+12,.1f}")
    print("=> si la 1re ligne écrase les autres : c'est elle la barre. Regarde 'jours' et 'OI'.\n")


if __name__ == "__main__":
    asset = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
    if "--debug" in sys.argv:
        debug_gex(asset)
    else:
        res = analyse(asset)
        if "--json" in sys.argv:
            print(json.dumps(res, indent=2, default=str))
        else:
            render(res)
