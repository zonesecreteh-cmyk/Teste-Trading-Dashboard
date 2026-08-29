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

VERSION = "2026-08-26-k"   # affiché à chaque lancement pour vérifier qu'on a la bonne version

# ---- Convention de signe dealer ---------------------------------------------
# IMPORTANT : le signe dealer (long/short par type d'option) n'est PAS observable
# depuis l'OI et l'IV publics — on ne sait pas qui a vendu/acheté. C'est donc une
# HEURISTIQUE, pas une vérité. Deux modes :
#   "fixed"    : convention SqueezeMetrics/SpotGamma -> dealers LONGS calls / COURTS puts.
#                Raisonnable sur indices (call-overwriting + achat de puts), discutable ailleurs.
#   "adaptive" : déduit l'orientation du SKEW (risk reversal 25d). Le côté dont l'IV est la
#                plus chère = côté le plus demandé par les clients = côté où le DEALER est COURT.
#                  RR < -bande (skew put)  -> dealers courts puts  -> (+call, -put)  [= fixed]
#                  RR > +bande (skew call) -> dealers courts calls -> (-call, +put)  [FLIP]
#                  |RR| <= bande           -> bande morte : on GARDE la convention fixed.
# La bande morte (hystérésis) évite que le GEX change de signe à chaque micro-variation du
# skew autour de 0 — ce qui rendrait la SÉRIE D'HISTORIQUE incohérente (l'inverse du but).
# Tant que le backtest (point 5) n'a pas tranché, on reste en "fixed" : l'orientation
# adaptative est calculée et AFFICHÉE comme diagnostic, mais PAS appliquée à l'historique.
DEALER_SIGN_MODE = "fixed"        # "fixed" | "adaptive" -- bascule quand le backtest aura parlé
SKEW_FLIP_BAND   = 1.5            # |RR| (pts d'IV) au-delà duquel l'orientation peut basculer
SIGN_CALL, SIGN_PUT = +1.0, -1.0  # défaut = mode fixed


def dealer_signs(rr, mode=None):
    """(sign_call, sign_put, info) selon le mode et le skew. rr = risk reversal 25d (pts d'IV).
    Heuristique — voir la note ci-dessus. info = {mode, rr, flipped, reason} pour l'afficher."""
    mode = (mode or DEALER_SIGN_MODE)
    if mode != "adaptive" or rr is None:
        return SIGN_CALL, SIGN_PUT, {"mode": "fixed", "rr": rr, "flipped": False,
                                     "reason": "convention fixe : dealers longs calls / courts puts"}
    if rr > SKEW_FLIP_BAND:                       # skew call -> dealers courts calls
        return -1.0, +1.0, {"mode": "adaptive", "rr": rr, "flipped": True,
                            "reason": f"skew call (RR {rr:+.1f}) : dealers courts calls -> (-call,+put)"}
    flipped = False
    reason = (f"skew put (RR {rr:+.1f}) : dealers courts puts -> (+call,-put)" if rr < -SKEW_FLIP_BAND
              else f"skew neutre (RR {rr:+.1f}, |RR|<={SKEW_FLIP_BAND}) : convention standard maintenue")
    return +1.0, -1.0, {"mode": "adaptive", "rr": rr, "flipped": flipped, "reason": reason}

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
    # ---------- CRYPTO (Deribit) ----------
    "BTC": {"source": "deribit", "contract": 1.0, "label": "BTC", "cat": "crypto", "group": "Majeures"},
    "ETH": {"source": "deribit", "contract": 1.0, "label": "ETH", "cat": "crypto", "group": "Majeures"},
    "SOL":  {"source": "deribit", "contract": 10.0,    "label": "SOL",  "cat": "crypto", "group": "Altcoins"},
    "XRP":  {"source": "deribit", "contract": 1000.0,  "label": "XRP",  "cat": "crypto", "group": "Altcoins"},
    "AVAX": {"source": "deribit", "contract": 100.0,   "label": "AVAX", "cat": "crypto", "group": "Altcoins"},
    "TRX":  {"source": "deribit", "contract": 10000.0, "label": "TRX",  "cat": "crypto", "group": "Altcoins"},
    "HYPE": {"source": "deribit", "contract": 10.0,    "label": "HYPE", "cat": "crypto", "group": "Altcoins"},
    # ---------- MACRO (CBOE) ----------
    # Indices US
    "SPX": {"source": "cboe", "contract": 100.0, "cboe": "_SPX", "label": "SPX", "cat": "macro", "group": "Indices US"},
    "NDX": {"source": "cboe", "contract": 100.0, "cboe": "_NDX", "label": "NDX", "cat": "macro", "group": "Indices US"},
    "RUT": {"source": "cboe", "contract": 100.0, "cboe": "_RUT", "label": "RUT", "cat": "macro", "group": "Indices US"},
    "DJX": {"source": "cboe", "contract": 100.0, "cboe": "_DJX", "label": "DJX", "cat": "macro", "group": "Indices US"},
    "VIX": {"source": "cboe", "contract": 100.0, "cboe": "_VIX", "label": "VIX", "cat": "macro", "group": "Indices US"},
    # ETF larges
    "SPY": {"source": "cboe", "contract": 100.0, "cboe": "SPY", "label": "SPY", "cat": "macro", "group": "ETF larges"},
    "QQQ": {"source": "cboe", "contract": 100.0, "cboe": "QQQ", "label": "QQQ", "cat": "macro", "group": "ETF larges"},
    "IWM": {"source": "cboe", "contract": 100.0, "cboe": "IWM", "label": "IWM", "cat": "macro", "group": "ETF larges"},
    "DIA": {"source": "cboe", "contract": 100.0, "cboe": "DIA", "label": "DIA", "cat": "macro", "group": "ETF larges"},
    # Secteurs
    "XLF": {"source": "cboe", "contract": 100.0, "cboe": "XLF", "label": "XLF banques", "cat": "macro", "group": "Secteurs"},
    "XLE": {"source": "cboe", "contract": 100.0, "cboe": "XLE", "label": "XLE énergie", "cat": "macro", "group": "Secteurs"},
    "XLK": {"source": "cboe", "contract": 100.0, "cboe": "XLK", "label": "XLK tech", "cat": "macro", "group": "Secteurs"},
    "SMH": {"source": "cboe", "contract": 100.0, "cboe": "SMH", "label": "SMH semis", "cat": "macro", "group": "Secteurs"},
    # International (pays)
    "EWG": {"source": "cboe", "contract": 100.0, "cboe": "EWG", "label": "Allemagne (EWG)", "cat": "macro", "group": "International"},
    "EWQ": {"source": "cboe", "contract": 100.0, "cboe": "EWQ", "label": "France (EWQ)", "cat": "macro", "group": "International"},
    # Matières premières
    "GC":  {"source": "cboe", "contract": 100.0, "cboe": "GLD", "label": "Or (GLD)", "cat": "macro", "group": "Matières premières"},
    "SLV": {"source": "cboe", "contract": 100.0, "cboe": "SLV", "label": "Argent (SLV)", "cat": "macro", "group": "Matières premières"},
    "CL":  {"source": "cboe", "contract": 100.0, "cboe": "USO", "label": "Pétrole (USO)", "cat": "macro", "group": "Matières premières"},
    "UNG": {"source": "cboe", "contract": 100.0, "cboe": "UNG", "label": "Gaz (UNG)", "cat": "macro", "group": "Matières premières"},
    # Devises
    "UUP": {"source": "cboe", "contract": 100.0, "cboe": "UUP", "label": "Dollar (UUP)", "cat": "macro", "group": "Devises"},
    "EU 6E": {"source": "cboe", "contract": 100.0, "cboe": "FXE", "label": "Euro (FXE)", "cat": "macro", "group": "Devises"},
    "JP 6J": {"source": "cboe", "contract": 100.0, "cboe": "FXY", "label": "Yen (FXY)", "cat": "macro", "group": "Devises"},
    # Taux / obligations
    "TLT": {"source": "cboe", "contract": 100.0, "cboe": "TLT", "label": "Treasuries 20a (TLT)", "cat": "macro", "group": "Taux / oblig"},
    "HYG": {"source": "cboe", "contract": 100.0, "cboe": "HYG", "label": "High yield (HYG)", "cat": "macro", "group": "Taux / oblig"},
}


# =============================================================================
#  GREEKS — Black-Scholes (utilisé seulement quand la source ne les fournit pas)
# =============================================================================
# PORTAGE (carry) : par défaut le moteur tourne SANS portage -> r=0, q=0.
#   - Crypto (Deribit) : q=0 (pas de dividende) et r~0 est la convention standard ;
#     les greeks crypto sont calculés ici par Black-Scholes.
#   - Indices/ETF (CBOE) : les greeks PRIMAIRES viennent de CBOE (qui intègrent déjà
#     le vrai taux et le dividende via les prix de marché) ; BS n'est qu'un secours.
# Pour les horizons courts que vise l'outil, l'effet de r/q est faible. Si tu veux
# activer le portage globalement, mets RISK_FREE à ta valeur (ex. 0.045) ; q reste
# par option (défaut 0) car il ne compte vraiment que sur indices, déjà couverts par CBOE.
# C'est documenté et explicite : pas de fausse précision cachée.
RISK_FREE = 0.0            # taux sans risque annualisé appliqué aux greeks BS (0 = carry-free)
# Taux appliqué aux greeks SECONDAIRES (charm/vanna/vega/theta) des actifs MACRO (CBOE).
# Les delta/gamma macro viennent de CBOE (carry déjà inclus) ; ce taux aligne nos greeks
# internes dessus. Crypto reste à r=0 (convention standard options inverses Deribit).
MACRO_RISK_FREE = 0.04


def _d1(S, K, T, sigma, r=None, q=0.0):
    r = RISK_FREE if r is None else r
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    return (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

def bs_gamma(S, K, T, sigma, r=None, q=0.0):
    T = max(T, T_FLOOR)                     # évite l'explosion gamma des 0DTE
    d1 = _d1(S, K, T, sigma, r, q)
    return 0.0 if d1 is None else math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))

def bs_delta(S, K, T, sigma, is_call, r=None, q=0.0):
    d1 = _d1(S, K, T, sigma, r, q)
    if d1 is None:
        return 0.0
    disc = math.exp(-q * T)
    return disc * norm.cdf(d1) if is_call else disc * (norm.cdf(d1) - 1.0)

def bs_vega(S, K, T, sigma, r=None, q=0.0):
    T = max(T, T_FLOOR)
    d1 = _d1(S, K, T, sigma, r, q)
    return 0.0 if d1 is None else S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)

def bs_theta(S, K, T, sigma, is_call, r=None, q=0.0):
    r = RISK_FREE if r is None else r
    T = max(T, T_FLOOR)
    d1 = _d1(S, K, T, sigma, r, q)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(T)
    term1 = -S * math.exp(-q * T) * norm.pdf(d1) * sigma / (2 * math.sqrt(T))
    if is_call:
        return term1 - r * K * math.exp(-r * T) * norm.cdf(d2) + q * S * math.exp(-q * T) * norm.cdf(d1)
    return term1 + r * K * math.exp(-r * T) * norm.cdf(-d2) - q * S * math.exp(-q * T) * norm.cdf(-d1)


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

# Indices spot Deribit par coin. Les altcoins (DERIBIT_LINEAR) sont des options
# USDC linéaires : leur book est rangé sous currency="USDC" (pas sous le nom du coin),
# et le point décimal des strikes est encodé en 'd' (ex. 0d5 = 0.5).
DERIBIT_INDEX = {
    "BTC": "btc_usd", "ETH": "eth_usd",
    "SOL": "sol_usdc", "XRP": "xrp_usdc", "AVAX": "avax_usdc",
    "TRX": "trx_usdc", "HYPE": "hype_usdc",
}
DERIBIT_LINEAR = {"SOL", "XRP", "AVAX", "TRX", "HYPE"}
_USDC_BOOK = {"ts": 0.0, "rows": None}

def _usdc_book():
    """Book complet des options USDC (toutes bases), en cache 60 s pour ne pas
    re-télécharger les ~3000 lignes à chaque altcoin pendant daily.py."""
    import time as _t
    now = _t.time()
    if _USDC_BOOK["rows"] is not None and now - _USDC_BOOK["ts"] < 60:
        return _USDC_BOOK["rows"]
    rows = _deribit_get("public/get_book_summary_by_currency", currency="USDC", kind="option")
    _USDC_BOOK["ts"], _USDC_BOOK["rows"] = now, rows
    return rows

def ingest_deribit(currency):
    idx = DERIBIT_INDEX[currency]
    S = _deribit_get("public/get_index_price", index_name=idx)["index_price"]
    if currency in DERIBIT_LINEAR:
        prefix = f"{currency}_USDC-"
        rows = [x for x in _usdc_book() if x.get("instrument_name", "").startswith(prefix)]
    else:
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
            strike = float(strike_str.replace("d", "."))   # Deribit encode le point décimal en 'd' (0d5 = 0.5)
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
            "volume": float(x.get("volume") or 0.0),             # volume 24h (flux du jour)
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
                     "volume": float(o.get("volume") or 0.0),
                     "gamma": gamma, "delta": delta})
    return S, book


# =============================================================================
#  MÉTRIQUES (contract_size paramétré : 1 crypto, 100 indices)
# =============================================================================
def gamma_exposure(book, S, csize, signs=None):
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    by_strike, total = {}, 0.0
    for o in book:
        sign = sc if o["type"] == "C" else sp
        gex = sign * o["gamma"] * o["oi"] * csize * (S ** 2) * 0.01
        by_strike[o["strike"]] = by_strike.get(o["strike"], 0.0) + gex
        total += gex
    return total, dict(sorted(by_strike.items()))

def gamma_flip(book, S, csize, span=0.20, steps=41, signs=None):
    """
    Niveau de prix où le GEX TOTAL bascule de positif à négatif (le 'gamma flip').
    On recalcule le gamma de chaque option à des prix hypothétiques autour du spot
    et on cherche le prix où l'exposition gamma agrégée s'annule.
    Au-dessus : dealers long gamma (range). En dessous : short gamma (cassures).
    Renvoie None si aucun basculement dans la fenêtre ±span.
    """
    if not book:
        return None
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    K  = np.array([o["strike"] for o in book], dtype=float)
    T  = np.array([o["T"]      for o in book], dtype=float)
    iv = np.array([o["iv"]     for o in book], dtype=float)
    oi = np.array([o["oi"]     for o in book], dtype=float)
    sg = np.array([sc if o["type"] == "C" else sp for o in book], dtype=float)
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


def bs_charm(S, K, T, sigma, is_call, r=0.0):
    """dDelta/dT (variation du delta avec le temps, par an). r=0 pour crypto (options
    inverses) ; MACRO_RISK_FREE pour les actifs CBOE (aligné sur leurs greeks à carry)."""
    d1 = _d1(S, K, T, sigma, r=r, q=0.0)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(T)
    return -norm.pdf(d1) * d2 / (2 * T)


def bs_vanna(S, K, T, sigma, r=0.0):
    """dDelta/dVol (variation du delta avec la vol, pour 1.00 de vol). r : voir bs_charm."""
    d1 = _d1(S, K, T, sigma, r=r, q=0.0)
    if d1 is None:
        return 0.0
    d2 = d1 - sigma * math.sqrt(T)
    return -norm.pdf(d1) * d2 / sigma


def charm_vanna_flow(book, S, csize, signs=None, r=0.0):
    """
    Flux spot (M$) que les dealers doivent faire mécaniquement pour rester delta-neutres :
    - charm : à cause du passage du temps (sur 24h).
    - vanna : si l'IV bouge de +1 point.
    Modèle basé sur la convention de signe dealer (comme GEX/DEX), pas une vérité.
    """
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    charm_daily = 0.0
    vanna_1pt = 0.0
    for o in book:
        sign = sc if o["type"] == "C" else sp
        ch = bs_charm(S, o["strike"], o["T"], o["iv"], o["type"] == "C", r=r)
        vn = bs_vanna(S, o["strike"], o["T"], o["iv"], r=r)
        charm_daily += sign * (ch / 365.0) * o["oi"] * csize * S
        vanna_1pt += sign * (vn * 0.01) * o["oi"] * csize * S
    return charm_daily, vanna_1pt

def vega_theta_exposure(book, S, csize, signs=None, r=0.0):
    """VEX (M$ de P&L dealers par +1pt d'IV) et theta agrégé (M$/jour), dealer-signés,
    même convention de signe que GEX/charm/vanna."""
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    vex = 0.0
    theta_d = 0.0
    for o in book:
        sign = sc if o["type"] == "C" else sp
        vex += sign * bs_vega(S, o["strike"], o["T"], o["iv"], r=r) * 0.01 * o["oi"] * csize
        theta_d += sign * (bs_theta(S, o["strike"], o["T"], o["iv"], o["type"] == "C", r=r) / 365.0) * o["oi"] * csize
    return vex, theta_d

def expiry_calendar(book, S, csize, max_rows=8):
    """Par échéance : jours restants, notionnel OI (M$), part du gamma total (%).
    Sert à anticiper les 'marches d'escalier' quand une grosse échéance expire."""
    now = dt.datetime.now(dt.timezone.utc)
    agg = {}
    tot_gamma = 0.0
    for o in book:
        g = abs((o.get("gamma") or 0.0) * o["oi"] * csize * (S ** 2) * 0.01)
        n = o["oi"] * csize * S
        a = agg.setdefault(o["expiry"].date(), {"notional": 0.0, "gamma": 0.0, "legs": []})
        a["notional"] += n
        a["gamma"] += g
        a["legs"].append(o)
        tot_gamma += g
    rows = []
    for e in sorted(agg):
        days = max(0, (e - now.date()).days)
        mp = max_pain(agg[e]["legs"])
        rows.append({"date": e.isoformat(), "days": days,
                     "max_pain": round(mp) if mp else None,
                     "mp_dist_pct": round((mp / S - 1) * 100, 1) if mp else None,
                     "notional_musd": round(agg[e]["notional"] / 1e6),
                     "gamma_pct": round(100 * agg[e]["gamma"] / tot_gamma, 1) if tot_gamma > 0 else 0.0})
    return {"rows": rows[:max_rows], "n_expiries": len(agg)}


def scenario_matrix(book, S, csize, moves=(-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10), signs=None):
    """
    Pour chaque mouvement du spot, combien les dealers doivent acheter/vendre en spot
    pour rester couverts (flux de hedge mécanique). Déduit du profil delta du book.
    flow > 0 = dealers ACHÈTENT ; flow < 0 = dealers VENDENT.
    """
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    def net_delta(Sp):
        tot = 0.0
        for o in book:
            sign = sc if o["type"] == "C" else sp
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


# Historique quotidien de la matrice de scénarios (flow de hedge par mouvement de spot),
# par horizon. Mêmes points que le défaut de scenario_matrix() ; 1 ligne/jour, alignée par
# INDICE (pas par valeur de move_pct) avec scenario_matrix() côté dashboard, pour tracer
# l'évolution jour après jour de chaque niveau (-10 %, -5 %, ... +10 %).
SCENARIO_MOVES = (-10.0, -5.0, -2.0, 0.0, 2.0, 5.0, 10.0)

def _scenario_hist_path(asset, horizon):
    return os.path.join(HIST_DIR, f"{asset}_{horizon}_scenario.csv")

def scenario_history(asset, horizon, matrix=None, store=False):
    """[{date, moves:[{move_pct, flow_musd}, ...]}] pour tracer chaque niveau de la
    matrice de scénarios dans le temps. store=False = lecture seule. Seul daily.py écrit.
    Écriture atomique (fichier temporaire + remplacement) pour éviter qu'une lecture
    concurrente (dashboard ouvert pendant le run quotidien) ne tombe sur un fichier
    à moitié réécrit."""
    path = _scenario_hist_path(asset, horizon)
    today = dt.date.today().isoformat()
    hist = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            p = line.strip().split(",")
            if len(p) < 1 + len(SCENARIO_MOVES):
                continue
            moves = [{"move_pct": mv, "flow_musd": (float(p[1 + i]) if p[1 + i] not in ("", "None") else None)}
                      for i, mv in enumerate(SCENARIO_MOVES)]
            hist.append({"date": p[0], "moves": moves})
    if not store or matrix is None:
        return hist[-90:]
    by_move = {r["move_pct"]: r["flow_musd"] for r in matrix}
    point_moves = [{"move_pct": mv, "flow_musd": by_move.get(mv)} for mv in SCENARIO_MOVES]
    def _line(date, moves):
        vals = ",".join("" if m["flow_musd"] is None else str(m["flow_musd"]) for m in moves)
        return f"{date},{vals}\n"
    os.makedirs(HIST_DIR, exist_ok=True)
    if not hist or hist[-1]["date"] != today:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_line(today, point_moves))
        hist.append({"date": today, "moves": point_moves})
    else:
        hist[-1] = {"date": today, "moves": point_moves}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for h in hist:
                f.write(_line(h["date"], h["moves"]))
        os.replace(tmp, path)
    return hist[-90:]


def delta_exposure(book, S, csize):
    # DEX "POSITIONNEMENT" : delta net agrégé du book, le delta porte déjà son signe
    # (call >0, put <0). PAS de signe dealer ici -> lecture directionnelle du marché.
    return sum(o["delta"] * o["oi"] * csize * S for o in book)

def delta_exposure_dealer(book, S, csize, signs=None):
    # DEX "FLUX DE COUVERTURE" : applique le signe dealer (comme le GEX). C'est le delta
    # net que portent les DEALERS. dex_dealer > 0 = dealers longs delta -> ils VENDENT du
    # spot pour rester neutres ; dex_dealer < 0 = courts -> ils ACHÈTENT. Heuristique
    # (même hypothèse de signe que le GEX), pas une vérité.
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    return sum((sc if o["type"] == "C" else sp) * o["delta"] * o["oi"] * csize * S for o in book)

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

def skew_term_at(book, dte):
    return risk_reversal(book, max(1, round(dte)))

def charm_vanna_opex(book, S, csize, signs=None, r=0.0):
    """Flux charm/vanna concentré sur la PROCHAINE GROSSE ÉCHÉANCE (OPEX mensuelle) — c'est là
    que les dealers ont le plus de delta à re-hedger mécaniquement. charm = delta à ajuster sur
    24h (passage du temps) ; vanna = delta à ajuster si l'IV bouge de +1 point. Moteur des
    'vanna rallies' et du pin d'OPEX."""
    dtes = horizon_dtes(book)
    exp = _nearest_expiry(book, dtes["mensuel"])
    leg = [o for o in book if o["expiry"] == exp]
    if not leg:
        return None
    ch, vn = charm_vanna_flow(leg, S, csize, signs, r=r)
    now = dt.datetime.now(dt.timezone.utc)
    days = (exp - now).days if exp else None
    return {"charm_musd": round(ch / 1e6, 2), "vanna_musd": round(vn / 1e6, 2), "days": days}

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

def data_quality(book, source):
    """Indicateur de confiance : plus il y a de strikes/échéances et plus la source est
    fraîche, plus le GEX/DEX est fiable. Donne un score 0-100 + un libellé, pour ne pas
    prendre un chiffre brut pour argent comptant."""
    strikes = len({o["strike"] for o in book})
    expiries = len({o["expiry"] for o in book})
    delayed = (source != "deribit")          # CBOE = différé ~15 min
    # score : strikes (60 pts max), échéances (20 pts), fraîcheur (20 pts)
    s_strikes = min(60, strikes * 2)          # 30 strikes -> plein pot
    s_exp = min(20, expiries * 4)             # 5 échéances -> plein pot
    s_fresh = 8 if delayed else 20
    score = int(s_strikes + s_exp + s_fresh)
    label = "ÉLEVÉE" if score >= 75 else "MOYENNE" if score >= 50 else "FAIBLE"
    return {"score": score, "label": label, "strikes": strikes,
            "expiries": expiries, "delayed": delayed,
            "source": "Deribit (temps réel)" if not delayed else "CBOE (différé ~15 min)"}

def realized_vol(spots, window=10, ann=365):
    """Vol réalisée annualisée à partir de la série de prix spot quotidiens (déjà stockés).
    ann = jours de cotation par an : 365 pour le crypto (24/7), ~252 pour les indices (bourse).
    Renvoie {ready, have, need, rv} : tant qu'il n'y a pas assez de jours, ready=False."""
    import math
    clean = [s for s in spots if s]
    need = window + 1
    if len(clean) < need:
        return {"ready": False, "have": len(clean), "need": need, "rv": None}
    seg = clean[-need:]
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = (var ** 0.5) * (ann ** 0.5) * 100        # annualisée
    return {"ready": True, "have": len(clean), "need": need, "rv": round(rv, 1)}

def put_call_ratio(book):
    """Ratio Put/Call sur l'open interest (positions ouvertes) ET sur le volume (flux du jour).
    OI = stock de positions ; volume = ce qui s'est traité aujourd'hui (sentiment plus frais)."""
    call_oi = sum(o["oi"] for o in book if o["type"] == "C")
    put_oi = sum(o["oi"] for o in book if o["type"] == "P")
    call_v = sum(o.get("volume", 0) for o in book if o["type"] == "C")
    put_v = sum(o.get("volume", 0) for o in book if o["type"] == "P")
    if call_oi <= 0:
        return None
    out = {"pcr_oi": round(put_oi / call_oi, 2),
           "call_oi": round(call_oi, 0), "put_oi": round(put_oi, 0)}
    out["pcr_vol"] = round(float(put_v) / float(call_v), 2) if call_v > 0 else None
    return out

def gex_by_expiry(book, S, csize, signs=None):
    """Répartit le GEX par horizon d'échéance : court (≤2j), hebdo (≤9j), mensuel (≤35j),
    long (>35j). Montre d'où vient le gamma — le 0DTE/court pèse sur l'intraday, le mensuel
    sur la tendance de fond."""
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    buckets = [("court", 2), ("hebdo", 9), ("mensuel", 35), ("long", 10 ** 9)]
    now = dt.datetime.now(dt.timezone.utc)
    agg = {k: 0.0 for k, _ in buckets}
    for o in book:
        sign = sc if o["type"] == "C" else sp
        gex = sign * o["gamma"] * o["oi"] * csize * (S ** 2) * 0.01
        days = (o["expiry"] - now).total_seconds() / 86400
        for name, lim in buckets:
            if days <= lim:
                agg[name] += gex
                break
    out = [{"bucket": k, "gex_musd": round(float(agg[k]) / 1e6, 1)} for k, _ in buckets if abs(agg[k]) > 1e3]
    return out or None

def gamma_profile(book, S, csize, span=0.18, steps=37, signs=None):
    """Profil de gamma cumulé : le GEX total des dealers SI le spot était à chaque niveau de prix.
    La courbe traverse zéro au gamma flip — au-dessus le marché est 'amorti' (gamma+),
    en-dessous il est 'amplifié' (gamma−). Montre toute la structure, pas juste un point."""
    sc, sp = signs if signs else (SIGN_CALL, SIGN_PUT)
    lo, hi = S * (1 - span), S * (1 + span)
    pts = []
    for i in range(steps):
        p = lo + (hi - lo) * i / (steps - 1)
        tot = 0.0
        for o in book:
            sign = sc if o["type"] == "C" else sp
            g = bs_gamma(p, o["strike"], o["T"], o["iv"])
            tot += sign * g * o["oi"] * csize * (p ** 2) * 0.01
        pts.append({"price": round(float(p), 2), "gex_musd": round(float(tot) / 1e6, 2)})
    return pts

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

def coinbase_premium(currency, ref_price):
    """Premium Coinbase vs l'indice de référence Deribit (déjà fetché) en points de base.
    >0 = Coinbase au-dessus -> demande institutionnelle US ; <0 = US vendent.
    API Coinbase publique sans clé, BTC/ETH uniquement. None si indispo (la carte montre alors
    DONNÉE MANQUANTE au lieu de planter)."""
    if currency not in ("BTC", "ETH") or not ref_price:
        return None
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{currency}-USD/spot",
                         timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        cb = float(r.json()["data"]["amount"])
        bps = round((cb - ref_price) / ref_price * 10000, 1)
        return {"bps": bps, "coinbase": round(cb, 2), "ref": round(ref_price, 2)}
    except Exception:
        return None

_STABLE_CACHE = {"ts": 0.0, "data": None}

def stablecoins():
    """Supply USDT + USDC (dry powder dispo pour acheter du crypto) via CoinGecko
    (API publique sans clé). Cache 5 min. None si indispo (carte = DONNÉE MANQUANTE)."""
    import time as _t
    now = _t.time()
    if _STABLE_CACHE["data"] is not None and now - _STABLE_CACHE["ts"] < 300:
        return _STABLE_CACHE["data"]
    try:
        out = []
        try:  # source primaire : CoinGecko
            r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                             params={"vs_currency": "usd", "ids": "tether,usd-coin"},
                             timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                j = r.json()
                for sym, cid in [("USDT", "tether"), ("USDC", "usd-coin")]:
                    row = next((x for x in j if x.get("id") == cid), None)
                    if row:
                        out.append({"coin": sym,
                                    "supply_b": round((row.get("market_cap") or 0) / 1e9, 1),
                                    "chg24h": round(row.get("market_cap_change_percentage_24h") or 0, 2)})
        except Exception:
            out = []
        if not out:  # fallback : DefiLlama (pas de rate-limit agressif)
            r = requests.get("https://stablecoins.llama.fi/stablecoins",
                             params={"includePrices": "false"},
                             timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            for a in (r.json().get("peggedAssets") or []):
                if a.get("symbol") in ("USDT", "USDC"):
                    cur = ((a.get("circulating") or {}).get("peggedUSD")) or 0
                    prev = ((a.get("circulatingPrevDay") or {}).get("peggedUSD")) or 0
                    chg = round((cur / prev - 1) * 100, 2) if prev else 0.0
                    out.append({"coin": a["symbol"], "supply_b": round(cur / 1e9, 1),
                                "chg24h": chg})
            out.sort(key=lambda x: x["coin"], reverse=True)  # USDT d'abord
        if not out:
            return None
        _STABLE_CACHE["ts"], _STABLE_CACHE["data"] = now, out
        return out
    except Exception:
        return None

def block_trades(currency, ref_price, min_notional=1_000_000):
    """Gros trades d'options (≥ 1M$ notionnel) sur 24h via Deribit get_last_trades_by_currency.
    Agrège flux net call/put + top strikes ciblés. BTC/ETH seulement. None si indispo."""
    if currency not in ("BTC", "ETH") or not ref_price:
        return None
    try:
        import time as _t
        cutoff = int(_t.time() * 1000) - 24 * 3600 * 1000
        blocks = []
        for t in _recent_option_trades(currency):
            if (t.get("timestamp") or 0) < cutoff:
                continue
            parts = t.get("instrument_name", "").split("-")
            if len(parts) != 4:
                continue
            cp = parts[3].upper()
            try:
                strike = float(parts[2].replace("d", "."))
            except ValueError:
                continue
            ipx = t.get("index_price") or ref_price
            notional = (t.get("amount") or 0) * ipx
            if notional < min_notional:
                continue
            signed = notional if t.get("direction") == "buy" else -notional
            blocks.append((cp, strike, signed, notional))
        if not blocks:
            return {"count": 0, "total_musd": 0.0, "net_call_musd": 0.0,
                    "net_put_musd": 0.0, "top": [], "bias": "AUCUN"}
        net_call = sum(s for cp, _, s, _ in blocks if cp == "C")
        net_put = sum(s for cp, _, s, _ in blocks if cp == "P")
        total = sum(n for _, _, _, n in blocks)
        agg = {}
        for cp, strike, signed, _ in blocks:
            agg[(cp, strike)] = agg.get((cp, strike), 0) + signed
        top = sorted(agg.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
        top_list = [{"cp": k[0], "strike": k[1], "flux_musd": round(v / 1e6, 1)} for k, v in top]
        bias = "MIXTE"
        if net_call > 0 and net_put <= 0:
            bias = "HAUSSIER"
        elif net_call < 0 and net_put >= 0:
            bias = "BAISSIER"
        return {"count": len(blocks), "total_musd": round(total / 1e6, 1),
                "net_call_musd": round(net_call / 1e6, 1), "net_put_musd": round(net_put / 1e6, 1),
                "top": top_list, "bias": bias}
    except Exception:
        return None

def deribit_funding(currency):
    """Funding 8h du perpétuel (crypto seulement). Renvoie None si indisponible :
    le dashboard affichera alors 'DONNÉE MANQUANTE' au lieu de planter."""
    try:
        perp = f"{currency}_USDC-PERPETUAL" if currency in DERIBIT_LINEAR else f"{currency}-PERPETUAL"
        t = _deribit_get("public/ticker", instrument_name=perp)
        f8 = t.get("funding_8h")
        if f8 is None:
            return None
        return {"rate_8h_pct": round(f8 * 100, 4),
                "annualized_pct": round(f8 * 3 * 365 * 100, 1)}   # 3 fenêtres de 8h/jour
    except Exception:
        return None

def binance_funding(coin):
    """Funding du perpétuel USDT Binance (premiumIndex). None si indispo / coin absent."""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": f"{coin}USDT"}, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        f8 = r.json().get("lastFundingRate")
        if f8 in (None, ""):
            return None
        f8 = float(f8)
        return {"rate_8h_pct": round(f8 * 100, 4),
                "annualized_pct": round(f8 * 3 * 365 * 100, 1)}
    except Exception:
        return None

def bybit_funding(coin):
    """Funding du perpétuel USDT Bybit (tickers v5). None si indispo / coin absent."""
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "linear", "symbol": f"{coin}USDT"}, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        lst = (r.json().get("result") or {}).get("list") or []
        if not lst:
            return None
        fr = lst[0].get("fundingRate")
        if fr in (None, ""):
            return None
        f8 = float(fr)
        return {"rate_8h_pct": round(f8 * 100, 4),
                "annualized_pct": round(f8 * 3 * 365 * 100, 1)}
    except Exception:
        return None

def funding_multi(coin, deribit_res):
    """Compare le funding Deribit / Binance / Bybit pour repérer une divergence
    (= déséquilibre de levier entre plateformes, parfois arbitrable). Renvoie None
    s'il n'y a pas au moins 2 plateformes pour comparer."""
    rows = []
    if deribit_res:
        rows.append({"ex": "Deribit", "rate": deribit_res["rate_8h_pct"], "ann": deribit_res["annualized_pct"]})
    bi = binance_funding(coin)
    if bi:
        rows.append({"ex": "Binance", "rate": bi["rate_8h_pct"], "ann": bi["annualized_pct"]})
    by = bybit_funding(coin)
    if by:
        rows.append({"ex": "Bybit", "rate": by["rate_8h_pct"], "ann": by["annualized_pct"]})
    if len(rows) < 2:
        return None
    rates = [r["rate"] for r in rows]
    spread_bps = round((max(rates) - min(rates)) * 100, 2)   # rate en % -> *100 = bps du 8h
    return {"rows": rows, "spread_bps": spread_bps, "diverge": spread_bps >= 1.0}

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
    # Profondeur : on privilegie le DVOL (indice officiel Deribit, plusieurs annees)
    # plutot que nos quelques semaines de collecte. Sans ca, le percentile qui alimente
    # L4 ET le score de stress reposerait sur ~50 jours alors que l'IV Rank affiche
    # 3 ans -> deux chiffres contradictoires pour la meme notion.
    try:
        hl, _src = long_vol_history(asset)
    except Exception:
        hl = []
    if hl:
        dvals = [v for _, v in hl]
        cur = dvals[-1]
        return round(100 * sum(v <= cur for v in dvals) / len(dvals)), len(dvals)
    vals = [v for _, v in hist]
    if len(vals) < 2:
        return None, len(vals)
    return round(100 * sum(v <= atm_iv_30d for v in vals) / len(vals)), len(vals)


def dex_gex_history(asset, dex_musd, gex_musd, score=None, spot=None, max_pain=None, rr=None,
                    store=True, horizon="long", levels=None, gex_fixed=None, gex_adaptive=None,
                    dex_dealer_fixed=None, dex_dealer_adaptive=None,
                    flip=None, call_wall=None, put_wall=None):
    """Historique PAR HORIZON. store=False = lecture seule. Seul daily.py écrit.
    levels = liste ordonnée [L1,L2,L3,L4,L5] de verdicts (BULL/BEAR/NEUTRAL) — colonnes 8-12.
    gex_fixed / gex_adaptive (col 13-14) ET dex_dealer_fixed / dex_dealer_adaptive (col 15-16) :
    le GEX et le DEX-dealer dans CHAQUE convention de signe, écrits TOUS chaque jour quel que soit
    le mode actif. Le mode ne change que ce qu'on REGARDE, jamais ce qu'on ENREGISTRE.
    Compat ascendante : les vieux fichiers (7, 12 ou 14 colonnes) restent lus tels quels."""
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, f"{asset}_{horizon}_dexgex.csv")
    today = dt.date.today().isoformat()
    LKEYS = ["L1_REGIME", "L2_POSITIONING", "L3_STRUCTURE", "L4_LIQUIDITE", "L5_CATALYST"]

    def _f(x):
        return float(x) if x not in ("", "None", None) else None

    def _parse(line):
        p = line.strip().split(",")
        if len(p) < 3:
            return None
        lv = {}
        if len(p) >= 12:
            for i, k in enumerate(LKEYS):
                lv[k] = p[7 + i] or None
        return {"date": p[0], "dex": _f(p[1]), "gex": _f(p[2]),
                "score": _f(p[3]) if len(p) >= 4 else None,
                "spot": _f(p[4]) if len(p) >= 5 else None,
                "max_pain": _f(p[5]) if len(p) >= 6 else None,
                "rr": _f(p[6]) if len(p) >= 7 else None,
                "gex_fixed": _f(p[12]) if len(p) >= 13 else None,
                "gex_adaptive": _f(p[13]) if len(p) >= 14 else None,
                "dex_dealer_fixed": _f(p[14]) if len(p) >= 15 else None,
                "dex_dealer_adaptive": _f(p[15]) if len(p) >= 16 else None,
                "flip": _f(p[16]) if len(p) >= 17 else None,
                "call_wall": _f(p[17]) if len(p) >= 18 else None,
                "put_wall": _f(p[18]) if len(p) >= 19 else None,
                "levels": lv}

    def _row(h):
        def s(v): return "" if v is None else v
        lv = h.get("levels") or {}
        lcols = ",".join(str(lv.get(k, "") or "") for k in LKEYS)
        return (f"{h['date']},{s(h['dex'])},{s(h['gex'])},{s(h['score'])},"
                f"{s(h['spot'])},{s(h['max_pain'])},{s(h.get('rr'))},{lcols},"
                f"{s(h.get('gex_fixed'))},{s(h.get('gex_adaptive'))},"
                f"{s(h.get('dex_dealer_fixed'))},{s(h.get('dex_dealer_adaptive'))},"
                f"{s(h.get('flip'))},{s(h.get('call_wall'))},{s(h.get('put_wall'))}\n")

    hist = []
    if os.path.exists(path):
        for line in open(path):
            row = _parse(line)
            if row:
                hist.append(row)

    if not store:                                  # vue interactive : on lit seulement
        return hist[-90:]

    lv = {}
    if levels:
        for i, k in enumerate(LKEYS):
            lv[k] = levels[i] if i < len(levels) else None
    point = {"date": today, "dex": dex_musd, "gex": gex_musd, "score": score,
             "spot": spot, "max_pain": max_pain, "rr": rr, "levels": lv,
             "gex_fixed": gex_fixed, "gex_adaptive": gex_adaptive,
             "dex_dealer_fixed": dex_dealer_fixed, "dex_dealer_adaptive": dex_dealer_adaptive,
             "flip": flip, "call_wall": call_wall, "put_wall": put_wall}
    if not hist or hist[-1]["date"] != today:
        with open(path, "a") as f:
            f.write(_row(point))
        hist.append(point)
    else:
        hist[-1] = point
        with open(path, "w") as f:
            for h in hist:
                f.write(_row(h))
    return hist[-90:]


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
    "catalyst_kink": 2.5,        # |RR court - RR mensuel| (pts IV) au-delà = catalyseur imminent
}

def _vote(direction, strength, reason):
    return {"verdict": direction, "strength": strength, "reason": reason}

def _pin_weight(m):
    """Poids de l'aimantation max pain selon la proximite de l'echeance dominante.

    Le "pin risk" est un phenomene de FIN DE VIE des options : il se manifeste dans
    les derniers jours avant expiration, quand le gamma explose et que les dealers
    doivent hedger de plus en plus fort autour des gros strikes. A 30 jours de
    l'echeance, il est negligeable.

    Sans cette ponderation, un max pain structurellement eloigne du spot (cas du BTC
    en tendance : max pain 68k pendant que le spot est a 77k) fait voter L1 dans la
    meme direction tous les jours pendant des semaines — un biais, pas un signal.
    """
    cal = (m.get("expiry_calendar") or {}).get("rows") or []
    if not cal:
        return 0.35                                  # calendrier indisponible : poids prudent
    # echeance qui porte le plus de gamma parmi les proches, sinon la plus proche
    grosses = [r for r in cal if (r.get("gamma_pct") or 0) >= 15]
    ref = min(grosses, key=lambda r: r["days"]) if grosses else min(cal, key=lambda r: r["days"])
    d = max(0, ref.get("days", 30))
    return round(math.exp(-d / 6.0), 3)              # 0j->1.0  3j->0.61  7j->0.31  21j->0.03

def level_regime(m):
    """L1 — regime (range vs amplification) + aimantation max pain ponderee."""
    gex = m["gex_total_musd"]
    mp_pct = m["max_pain_vs_spot_pct"]
    if gex < 0:
        return _vote("NEUTRAL", 0.3, "Gamma négatif : amplification, pas d'ancrage directionnel")
    w = _pin_weight(m)
    if w < 0.12:
        return _vote("NEUTRAL", 0.3,
                     f"Gamma positif mais échéance dominante trop lointaine : "
                     f"l'aimantation max pain ({mp_pct:+.1f}%) n'agit pas encore")
    force = min(1.0, abs(mp_pct) / 2) * w
    if mp_pct > TH["regime_maxpain_pct"]:
        return _vote("BULL", force,
                     f"Max pain {mp_pct:+.1f}% au-dessus du spot, aimantation haussière "
                     f"(poids échéance {w:.2f})")
    if mp_pct < -TH["regime_maxpain_pct"]:
        return _vote("BEAR", force,
                     f"Max pain {mp_pct:+.1f}% sous le spot, aimantation baissière "
                     f"(poids échéance {w:.2f})")
    return _vote("NEUTRAL", 0.4, "Spot collé au max pain, pas de cible nette")

def rr_baseline(asset, crypto):
    """Niveau NORMAL du risk reversal pour cet actif.

    Le skew est structurellement négatif (puts chers) : sur crypto il tourne
    couramment entre -3 et -7, sur indices autour de -2. Comparer ces valeurs à 0
    fait voter BEAR en permanence — c'est le biais qu'on corrige ici, exactement
    comme on l'a fait pour le Put/Call ratio.

    On utilise la MÉDIANE de l'historique réel de l'actif dès 20 points ; sinon on
    retombe sur une référence de classe. Le verdict devient donc "anormalement
    peureux / optimiste PAR RAPPORT À SON PROPRE NORMAL", et non dans l'absolu.
    """
    vals = []
    try:
        for r in _read_history_file(asset, "mensuel"):
            v = r.get("rr")
            if isinstance(v, (int, float)):
                vals.append(v)
    except Exception:
        vals = []
    vals = vals[-90:]
    if len(vals) >= 20:
        vals_tri = sorted(vals)
        n = len(vals_tri)
        med = (vals_tri[n // 2] if n % 2 else (vals_tri[n // 2 - 1] + vals_tri[n // 2]) / 2)
        # dispersion robuste (écart absolu médian) -> largeur de la zone "normale"
        ecarts = sorted(abs(v - med) for v in vals_tri)
        mad = (ecarts[n // 2] if n % 2 else (ecarts[n // 2 - 1] + ecarts[n // 2]) / 2)
        return {"base": round(med, 2), "spread": round(max(0.8, 1.5 * mad), 2),
                "src": f"médiane {n}j"}
    return ({"base": -3.5, "spread": 2.0, "src": "référence crypto"} if crypto
            else {"base": -1.5, "spread": 1.2, "src": "référence indices"})

def level_positioning(m):
    """L2 — skew (risk reversal) lu PAR RAPPORT au normal de l'actif."""
    rr = m["rr_monthly"] if m["rr_monthly"] is not None else m["rr_weekly"]
    if rr is None:
        return _vote("NEUTRAL", 0.0, "Skew indisponible")
    b = m.get("rr_baseline") or {"base": 0.0, "spread": 1.5, "src": "brut"}
    ecart = rr - b["base"]                       # <0 = plus peureux que d'habitude
    force = min(1.0, abs(ecart) / (2 * b["spread"]))
    if ecart <= -b["spread"]:
        return _vote("BEAR", force,
                     f"Risk reversal {rr} vs normal {b['base']} ({b['src']}) : "
                     f"peur inhabituelle, puts nettement plus chers que d'ordinaire")
    if ecart >= b["spread"]:
        return _vote("BULL", force,
                     f"Risk reversal {rr} vs normal {b['base']} ({b['src']}) : "
                     f"appétit call inhabituel")
    return _vote("NEUTRAL", 0.3,
                 f"Risk reversal {rr} conforme au normal de l'actif ({b['base']}, {b['src']})")

def level_structure(m):
    """L3 — term structure. Mesure le STRESS, pas la direction : la backwardation
    (vol court terme qui flambe) = stress immédiat, légèrement baissier ; le contango
    = simplement calme, donc NEUTRE (un marché calme n'est pas haussier en soi)."""
    reg = m["term_regime"]
    if reg.startswith("BACKWARDATION"):
        return _vote("BEAR", 0.7, "Backwardation : stress immédiat anticipé")
    if reg.startswith("CONTANGO"):
        return _vote("NEUTRAL", 0.25, "Contango : conditions calmes, pas de signal directionnel")
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
    """L5 — catalyseur. Deux entrées :
      1) override manuel directionnel (catalyst_bias) si tu sais qu'un événement va dans un sens ;
      2) sinon, détection AUTO d'un catalyseur IMMINENT via le 'kink' de skew : quand la peur
         est concentrée sur le court terme (RR court << RR mensuel), le marché price un événement
         proche. Sa DIRECTION est inconnue -> on reste NEUTRE (on ne devine pas le sens), mais on
         lève un drapeau 'catalyst_imminent' qui RÉDUIRA la conviction (on trim avant l'événement).
    Plus de niveau inerte : L5 lit maintenant une vraie donnée."""
    if catalyst_bias in ("BULL", "BEAR"):
        return _vote(catalyst_bias, 0.6, "Catalyseur externe fourni (override manuel)")
    rr_c, rr_m = m.get("rr_weekly"), m.get("rr_monthly")
    if rr_c is not None and rr_m is not None:
        kink = rr_c - rr_m                       # court - mensuel ; très négatif = peur front-loaded
        if kink <= -TH["catalyst_kink"]:
            v = _vote("NEUTRAL", 0.0,
                      f"Catalyseur imminent détecté (peur court terme, kink {kink:+.1f}) — direction inconnue")
            v["catalyst_imminent"] = True
            return v
    return _vote("NEUTRAL", 0.0, "Aucun catalyseur imminent détecté")

def converge(metrics, catalyst_bias=None):
    levels = {
        "L1_REGIME":       level_regime(metrics),
        "L2_POSITIONING":  level_positioning(metrics),
        "L3_STRUCTURE":    level_structure(metrics),
        "L4_LIQUIDITE":    level_liquidity(metrics),
        "L5_CATALYST":     level_catalyst(metrics, catalyst_bias),
    }
    # L3 (term structure) et L4 (IV) mesurent le STRESS / la vol, PAS la direction.
    # Leur info est DÉJÀ dans le score de stress (iv_percentile + backwardation). On les
    # exclut donc du vote directionnel pour ne pas fabriquer de fausse convergence
    # (ex. "IV basse" comptée comme signal haussier). Direction = L1, L2, L5(override).
    DIRECTIONAL = ("L1_REGIME", "L2_POSITIONING", "L5_CATALYST")
    for k, v in levels.items():
        v["kind"] = "directional" if k in DIRECTIONAL else "stress"

    sign = {"BULL": +1, "BEAR": -1, "NEUTRAL": 0}
    dir_votes = [levels[k] for k in DIRECTIONAL]
    # score directionnel normalisé sur [-10,+10] (2.5 ≈ |raw| réaliste quand L1+L2 alignés forts)
    raw = sum(sign[v["verdict"]] * v["strength"] for v in dir_votes)
    score = round(max(-10, min(10, raw / 2.5 * 10)), 1)

    bulls = sum(1 for v in dir_votes if v["verdict"] == "BULL")
    bears = sum(1 for v in dir_votes if v["verdict"] == "BEAR")
    aligned = max(bulls, bears)
    direction = "HAUSSIER" if bulls > bears else "BAISSIER" if bears > bulls else "NEUTRE"

    # --- règle de convergence sur le noyau directionnel (L1, L2, +L5 si override) ---
    if aligned >= 3:                              # L1+L2+L5 tous alignés = setup rare et fort
        conviction = "FORTE"
    elif aligned == 2:                            # L1 et L2 d'accord
        conviction = "MODÉRÉE"
    elif aligned == 1 and abs(score) >= 4:        # un seul signal mais marqué
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

    # --- L5 : catalyseur imminent (direction inconnue) -> on trim la taille avant l'événement ---
    catalyst_imminent = bool(levels["L5_CATALYST"].get("catalyst_imminent"))
    if catalyst_imminent:
        sizing = round(sizing * 0.85, 2)

    return {
        "score": score, "direction": direction, "conviction": conviction,
        "aligned": aligned, "bulls": bulls, "bears": bears,
        "stress": stress, "stress_label": stress_label,
        "sizing": sizing, "levels": levels,
        "catalyst_imminent": catalyst_imminent,
    }


# =============================================================================
#  ASSEMBLAGE
# =============================================================================
# Horizons canoniques alignés sur les VRAIS tenors d'open interest (là où le gamma vit) :
#   court ≤7j (weeklies), mensuel ≤30j, trimestriel ≤90j (l'OI dominant sur Deribit).
# Cumulatif : chaque horizon inclut tout le gamma jusqu'à son échéance. daily.py écrit les 3.
HORIZONS = [("court", 7.0), ("mensuel", 30.0), ("trimestriel", 90.0)]

def horizon_dtes(book):
    """PRO : snappe chaque horizon sur la VRAIE échéance dominante (par open interest) de l'actif,
    au lieu d'un seuil en jours arbitraire. court = échéance la plus proche ; mensuel = la plus
    chargée en OI dans ~14-45j (l'OPEX/fin de mois) ; trimestriel = la plus chargée au-delà de 45j
    (le quarterly Deribit). Renvoie {court, mensuel, trimestriel} en jours, propres à l'actif."""
    now = dt.datetime.now(dt.timezone.utc)
    oi_by_exp = {}
    for o in book:
        d = (o["expiry"] - now).total_seconds() / 86400
        if d <= 0:
            continue
        e = oi_by_exp.setdefault(o["expiry"], [0.0, d])
        e[0] += o.get("oi", 0)
    if not oi_by_exp:
        return {"court": 7.0, "mensuel": 30.0, "trimestriel": 90.0}
    exps = sorted(oi_by_exp.values(), key=lambda v: v[1])          # [oi, days] triés par jours
    court = exps[0][1]                                             # échéance la plus proche
    band_m = [e for e in exps if 14 <= e[1] <= 45]
    mensuel = (max(band_m, key=lambda e: e[0])[1] if band_m
               else next((e[1] for e in exps if e[1] > court + 10), exps[-1][1]))
    band_q = [e for e in exps if e[1] > 45]
    trimestriel = (max(band_q, key=lambda e: e[0])[1] if band_q else exps[-1][1])
    return {"court": round(court, 1), "mensuel": round(mensuel, 1),
            "trimestriel": round(trimestriel, 1)}

def _horizon_for(dte):
    return min(HORIZONS, key=lambda h: abs(h[1] - float(dte)))[0]

def _read_history_file(asset, horizon):
    path = os.path.join(HIST_DIR, f"{asset}_{horizon}_dexgex.csv")
    out = []
    if os.path.exists(path):
        for line in open(path):
            p = line.strip().split(",")
            if len(p) < 3:
                continue
            def f(x): return float(x) if x not in ("", "None") else None
            LK = ["L1_REGIME", "L2_POSITIONING", "L3_STRUCTURE", "L4_LIQUIDITE", "L5_CATALYST"]
            lv = {}
            if len(p) >= 12:
                for i, k in enumerate(LK):
                    lv[k] = p[7 + i] or None
            out.append({"date": p[0], "dex": f(p[1]), "gex": f(p[2]),
                        "score": f(p[3]) if len(p) >= 4 else None,
                        "spot": f(p[4]) if len(p) >= 5 else None,
                        "max_pain": f(p[5]) if len(p) >= 6 else None,
                        "rr": f(p[6]) if len(p) >= 7 else None,
                        "gex_fixed": f(p[12]) if len(p) >= 13 else None,
                        "gex_adaptive": f(p[13]) if len(p) >= 14 else None,
                        "dex_dealer_fixed": f(p[14]) if len(p) >= 15 else None,
                        "dex_dealer_adaptive": f(p[15]) if len(p) >= 16 else None,
                        "flip": f(p[16]) if len(p) >= 17 else None,
                        "call_wall": f(p[17]) if len(p) >= 18 else None,
                        "put_wall": f(p[18]) if len(p) >= 19 else None,
                        "levels": lv})
    return out[-90:]

def all_histories(asset):
    """Renvoie les 3 séries d'historique (une par horizon) pour basculer côté dashboard."""
    return {h: _read_history_file(asset, h) for h, _ in HORIZONS}

# === Historique DEX/GEX book TOTAL (toutes échéances agrégées) ====================
# C'est la métrique "standard" des sites publics (Laevitas, CryptoGamma) et de la
# plupart des vidéos : une photo du book ENTIER, pas d'une tranche DTE. 1 ligne/jour,
# idempotent. Permet la comparaison jour-à-jour avec les modèles externes.
def _total_hist_path(asset):
    return os.path.join(HIST_DIR, f"{asset}_total_dexgex.csv")

def record_total_history(asset, dex_musd, gex_musd, spot):
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        date = dt.date.today().isoformat()
        path = _total_hist_path(asset)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if any(line.startswith(date + ",") for line in f):
                    return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{date},{dex_musd},{gex_musd},{spot}\n")
    except Exception:
        pass

def read_total_history(asset):
    try:
        out = []
        with open(_total_hist_path(asset), encoding="utf-8") as f:
            for line in f:
                p = line.strip().split(",")
                if len(p) >= 3:
                    out.append({"date": p[0], "dex": float(p[1]), "gex": float(p[2]),
                                "spot": float(p[3]) if len(p) > 3 and p[3] else None})
        return out[-90:]
    except OSError:
        return []

# === IV Rank ======================================================================
# Réutilise l'historique DÉJÀ collecté par iv_percentile ({asset}.csv) — pas de
# fichier séparé. Place l'IV du jour dans sa distribution : 80 = plus haute que 80%
# des jours enregistrés (optionalité chère). status='collecting' sous 5 jours.
# --- DVOL : l'indice de volatilite implicite officiel de Deribit (le "VIX du BTC") ---
# Deribit n'archive PAS les chaines d'options passees (donc pas de GEX historique),
# mais il publie le DVOL en bougies journalieres sur PLUSIEURS ANNEES. On s'en sert
# pour situer la volatilite actuelle dans son histoire longue au lieu de 50 jours.
# Cache disque : on ne refait le reseau qu'une fois par jour.
def _dvol_path(currency):
    return os.path.join(HIST_DIR, f"{currency}_dvol.csv")

def dvol_history(currency, years=3):
    """[(date, close)] du DVOL. Cache local rafraichi une fois par jour."""
    if currency not in ("BTC", "ETH"):
        return []
    path = _dvol_path(currency)
    today = dt.date.today().isoformat()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.strip().split(",")
                if len(p) >= 2:
                    rows.append((p[0], float(p[1])))
    except (OSError, ValueError):
        rows = []
    if rows and rows[-1][0] >= today:
        return rows                                   # deja a jour aujourd'hui
    try:
        import time as _t
        end = int(_t.time() * 1000)
        got = {}
        # on decoupe en tranches d'un an : l'API limite l'amplitude par requete
        for k in range(years):
            hi = end - k * 365 * 86400000
            lo = hi - 365 * 86400000
            r = requests.get(f"{DERIBIT}/public/get_volatility_index_data",
                             params={"currency": currency, "start_timestamp": lo,
                                     "end_timestamp": hi, "resolution": "1D"},
                             timeout=15)
            if r.status_code != 200:
                break
            data = (r.json().get("result") or {}).get("data") or []
            if not data:
                break
            for c in data:
                # format : [timestamp, open, high, low, close]
                d = dt.datetime.fromtimestamp(c[0] / 1000, dt.timezone.utc).date().isoformat()
                got[d] = float(c[4])
        if got:
            rows = sorted(got.items())
            os.makedirs(HIST_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for d, v in rows:
                    f.write(f"{d},{round(v, 2)}\n")
    except Exception:
        pass
    return rows

# --- Indices de volatilite CBOE : l'equivalent du DVOL pour les actifs macro ------
# CBOE publie l'historique quotidien de ses indices de vol depuis des decennies.
# On associe chaque actif macro a son indice de reference pour donner a l'IV
# percentile la meme profondeur que le DVOL cote crypto.
CBOE_VOL_INDEX = {
    "SPX": "VIX", "SPY": "VIX", "DJX": "VXD", "DIA": "VXD",
    "NDX": "VXN", "QQQ": "VXN", "RUT": "RVX", "IWM": "RVX",
    "GC": "GVZ", "SLV": "GVZ", "CL": "OVX",
}

def cboe_vol_history(asset):
    """[(date, close)] de l'indice de vol CBOE associe. Cache local, 1 fetch/jour."""
    idx = CBOE_VOL_INDEX.get(asset)
    if not idx:
        return []
    path = os.path.join(HIST_DIR, f"_cboe_{idx}.csv")
    today = dt.date.today().isoformat()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.strip().split(",")
                if len(p) >= 2:
                    rows.append((p[0], float(p[1])))
    except (OSError, ValueError):
        rows = []
    if rows and rows[-1][0] >= today:
        return rows
    try:
        r = requests.get(
            f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{idx}_History.csv",
            timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return rows
        out = []
        for line in r.text.strip().splitlines()[1:]:
            p = line.split(",")
            if len(p) >= 5:
                try:
                    d = p[0].strip()
                    if "/" in d:                       # format MM/DD/YYYY
                        mo, da, yr = d.split("/")
                        d = f"{yr}-{int(mo):02d}-{int(da):02d}"
                    out.append((d, float(p[4])))
                except ValueError:
                    continue
        if out:
            out = out[-2600:]                          # ~10 ans suffisent largement
            os.makedirs(HIST_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for d, v in out:
                    f.write(f"{d},{round(v, 2)}\n")
            rows = out
    except Exception:
        pass
    return rows

def long_vol_history(asset):
    """Historique de vol le plus profond disponible : DVOL (crypto) ou CBOE (macro)."""
    dv = dvol_history(asset)
    if len(dv) >= 60:
        return dv, "DVOL Deribit"
    cv = cboe_vol_history(asset)
    if len(cv) >= 60:
        return cv, f"{CBOE_VOL_INDEX.get(asset)} CBOE"
    return [], None

def iv_rank(asset, iv_now):
    """Place la volatilite du jour dans sa distribution historique.

    Priorite au DVOL (annees d'historique, indice officiel Deribit) ; sinon on
    retombe sur l'IV que l'on collecte nous-memes (quelques semaines)."""
    hist, src = long_vol_history(asset)
    if hist:
        vals = [v for _, v in hist]
        cur = vals[-1]
        rank = round(100 * sum(1 for v in vals if v <= cur) / len(vals))
        return {"status": "ok", "iv": round(cur, 1), "rank": rank, "days": len(vals),
                "lo": round(min(vals), 1), "hi": round(max(vals), 1),
                "src": src, "years": round(len(vals) / 252, 1)}
    if iv_now is None:
        return None
    vals = []
    try:
        with open(os.path.join(HIST_DIR, f"{asset}.csv"), encoding="utf-8") as f:
            for line in f:
                p = line.strip().split(",")
                if len(p) >= 2:
                    try:
                        vals.append(float(p[1]))
                    except ValueError:
                        pass
        vals = vals[-90:]
    except OSError:
        vals = []
    if len(vals) < 5:
        return {"status": "collecting", "have": len(vals), "iv": iv_now}
    rank = round(100 * sum(1 for v in vals if v <= iv_now) / len(vals))
    return {"status": "ok", "iv": iv_now, "rank": rank, "days": len(vals),
            "lo": round(min(vals), 1), "hi": round(max(vals), 1), "src": "IV collectee"}

def futures_basis(currency):
    """Basis annualisé des futures datés Deribit vs index (contango/backwardation).
    BTC/ETH seulement. None si indispo (carte masquée)."""
    if currency not in ("BTC", "ETH"):
        return None
    try:
        res = _deribit_get("public/get_book_summary_by_currency",
                           currency=currency, kind="future")
        idx = _deribit_get("public/get_index_price",
                           index_name=f"{currency.lower()}_usd")["index_price"]
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for f in res:
            name = f.get("instrument_name", "")
            mark = f.get("mark_price")
            if "PERPETUAL" in name or not mark:
                continue
            try:
                exp = dt.datetime.strptime(name.split("-")[1], "%d%b%y").replace(
                    hour=8, tzinfo=dt.timezone.utc)
            except (ValueError, IndexError):
                continue
            days = (exp - now).total_seconds() / 86400
            if days <= 0.5:
                continue
            ann = (mark / idx - 1) * 365 / days * 100
            rows.append({"label": name.split("-")[1], "days": round(days),
                         "ann_pct": round(ann, 2)})
        if not rows:
            return None
        rows.sort(key=lambda r: r["days"])
        rows = rows[:4]
        avg = sum(r["ann_pct"] for r in rows) / len(rows)
        regime = "contango" if avg > 0.3 else ("backwardation" if avg < -0.3 else "flat")
        return {"rows": rows, "avg_ann_pct": round(avg, 2), "regime": regime}
    except Exception:
        return None

# === Heatmap liquidations (Hyperliquid, ESTIMATION) ================================
# Méthode standard des heatmaps publiques (Coinglass-like) : le volume de chaque bougie
# 1h (7 jours) est supposé ouvrir des positions à paliers de levier typiques ; on en
# déduit les niveaux de liquidation, et on RETIRE ceux que le prix a déjà balayés.
# C'est une ESTIMATION de la densité de levier, pas une mesure de positions réelles.
_HL_CACHE = {}
_HL_LEVERAGE = {5: 0.15, 10: 0.30, 25: 0.30, 50: 0.20, 100: 0.05}   # distribution supposée

def hyperliquid_liq_map(coin, band=0.25, bucket_pct=0.01):
    """Zones estimées de liquidations longs (sous le spot) et shorts (au-dessus).
    Cache 5 min. None si Hyperliquid indispo ou coin absent (carte masquée)."""
    import time as _t
    now = _t.time()
    c = _HL_CACHE.get(coin)
    if c and now - c[0] < 300:
        return c[1]
    try:
        import time as _t2
        end = int(_t2.time() * 1000)
        start = end - 7 * 24 * 3600 * 1000
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "candleSnapshot",
                                "req": {"coin": coin, "interval": "1h",
                                        "startTime": start, "endTime": end}},
                          timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        candles = r.json()
        if not isinstance(candles, list) or len(candles) < 24:
            return None
        n = len(candles)
        closes = [float(cd["c"]) for cd in candles]
        lows = [float(cd["l"]) for cd in candles]
        highs = [float(cd["h"]) for cd in candles]
        vols = [float(cd["v"]) for cd in candles]
        spot = closes[-1]
        # suffixes : extrêmes atteints APRÈS chaque bougie (pour retirer les niveaux balayés)
        smin = [0.0] * n
        smax = [0.0] * n
        cur_min, cur_max = float("inf"), 0.0
        for i in range(n - 1, -1, -1):
            smin[i], smax[i] = cur_min, cur_max
            cur_min = min(cur_min, lows[i])
            cur_max = max(cur_max, highs[i])
        lo_b, hi_b = spot * (1 - band), spot * (1 + band)
        step = spot * bucket_pct
        buckets = {}
        for i in range(n):
            notional = vols[i] * closes[i]
            if notional <= 0:
                continue
            age_w = 0.5 + 0.5 * (i + 1) / n           # les bougies récentes pèsent plus
            for lev, w in _HL_LEVERAGE.items():
                amt = notional * w * age_w * 0.5       # moitié longs, moitié shorts
                liq_l = closes[i] * (1 - 1.0 / lev)    # liquidation des longs (dessous)
                if lo_b <= liq_l < spot and not (smin[i] <= liq_l):
                    k = round(liq_l / step)
                    b = buckets.setdefault(k, [0.0, 0.0])
                    b[0] += amt
                liq_s = closes[i] * (1 + 1.0 / lev)    # liquidation des shorts (dessus)
                if spot < liq_s <= hi_b and not (smax[i] >= liq_s):
                    k = round(liq_s / step)
                    b = buckets.setdefault(k, [0.0, 0.0])
                    b[1] += amt
        below, above = [], []
        for k, (l_amt, s_amt) in buckets.items():
            price = k * step
            if l_amt > 0 and price < spot:
                below.append({"price": round(price), "musd": round(l_amt / 1e6, 1)})
            if s_amt > 0 and price > spot:
                above.append({"price": round(price), "musd": round(s_amt / 1e6, 1)})
        below.sort(key=lambda b: b["musd"], reverse=True)
        above.sort(key=lambda b: b["musd"], reverse=True)
        below, above = below[:6], above[:6]
        below.sort(key=lambda b: b["price"], reverse=True)
        above.sort(key=lambda b: b["price"], reverse=True)
        out = {"spot": round(spot, 2),
               "above": above, "below": below,
               "total_above_musd": round(sum(b["musd"] for b in above), 1),
               "total_below_musd": round(sum(b["musd"] for b in below), 1)}
        _HL_CACHE[coin] = (now, out)
        return out
    except Exception:
        return None

_TRADES_CACHE = {}

def _recent_option_trades(currency, count=1000):
    """Derniers trades d'options (cache 5 min, partagé par les analyses de flux)."""
    import time as _t
    now = _t.time()
    c = _TRADES_CACHE.get(currency)
    if c and now - c[0] < 300:
        return c[1]
    res = _deribit_get("public/get_last_trades_by_currency",
                       currency=currency, kind="option", count=count)
    trades = res.get("trades", [])
    _TRADES_CACHE[currency] = (now, trades)
    return trades

def flow_summary(asset, ref_price):
    """QUI achète QUOI, à quel prix, et sens de la volatilité — sur 24h de tape.
    Le côté 'direction' de Deribit est celui du TAKER (l'agresseur) = le client.
    Le dealer prend systématiquement l'autre côté. BTC/ETH seulement."""
    if asset not in ("BTC", "ETH") or not ref_price:
        return None
    try:
        import time as _t
        cutoff = int(_t.time() * 1000) - 24 * 3600 * 1000
        net_c = net_p = 0.0
        buy_prem = sell_prem = 0.0
        n = 0
        by_strike = {}
        for t in _recent_option_trades(asset):
            if (t.get("timestamp") or 0) < cutoff:
                continue
            parts = t.get("instrument_name", "").split("-")
            if len(parts) != 4:
                continue
            cp = parts[3].upper()
            if cp not in ("C", "P"):
                continue
            try:
                strike = float(parts[2].replace("d", "."))
            except ValueError:
                continue
            ipx = t.get("index_price") or ref_price
            amount = t.get("amount") or 0
            notional = amount * ipx
            is_buy = t.get("direction") == "buy"
            signed = notional if is_buy else -notional
            # prime payée/reçue (proxy du vega échangé : acheter une option = acheter de la vol)
            prem = (t.get("price") or 0) * amount * ipx
            if is_buy:
                buy_prem += prem
            else:
                sell_prem += prem
            if cp == "C":
                net_c += signed
            else:
                net_p += signed
            k = (cp, round(strike))
            by_strike[k] = by_strike.get(k, 0.0) + signed
            n += 1
        if n == 0:
            return None
        net_tot = net_c + net_p
        net_vol = buy_prem - sell_prem          # >0 : clients paient de la prime = long vol
        tot_prem = buy_prem + sell_prem
        vol_pct = round(100 * net_vol / tot_prem, 1) if tot_prem > 0 else 0.0
        top = sorted(by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
        thr = 0.02 * max(abs(net_c) + abs(net_p), 1)
        def stance(v):
            return "acheteurs" if v > thr else ("vendeurs" if v < -thr else "équilibrés")
        return {
            "n_trades": n,
            "net_musd": round(net_tot / 1e6, 1),
            "net_call_musd": round(net_c / 1e6, 1),
            "net_put_musd": round(net_p / 1e6, 1),
            "clients_options": stance(net_tot),
            "clients_calls": stance(net_c),
            "clients_puts": stance(net_p),
            "dealers_options": ("vendeurs" if net_tot > thr else
                                "acheteurs" if net_tot < -thr else "équilibrés"),
            "vol_net_musd": round(net_vol / 1e6, 2),
            "vol_pct": vol_pct,
            "vol_stance": ("acheteurs de volatilité" if vol_pct > 5 else
                           "vendeurs de volatilité" if vol_pct < -5 else
                           "neutres sur la volatilité"),
            "top": [{"cp": k[0], "strike": k[1], "net_musd": round(v / 1e6, 1)} for k, v in top],
        }
    except Exception:
        return None

# === Détection des changements notables (rapport intelligent) =====================
# Compare l'état du jour à celui de la veille et ne retient QUE ce qui a vraiment
# changé, avec un niveau de gravité. C'est ce qui transforme une liste de chiffres
# en "voilà ce qui s'est passé depuis hier et pourquoi ça compte".
def _daily_state_path(asset):
    return os.path.join(HIST_DIR, f"{asset}_state.json")

def _capture_state(m):
    """Photo compacte des indicateurs qu'on veut surveiller d'un jour à l'autre."""
    c = m.get("convergence") or {}
    bt = m.get("book_total") or {}
    fs = m.get("flow_summary") or {}
    f = m.get("funding") or {}
    ivr = m.get("iv_rank") or {}
    return {
        "date": dt.date.today().isoformat(),
        "spot": m.get("spot"),
        "gex": bt.get("gex_musd", m.get("gex_total_musd")),
        "dex": bt.get("dex_musd", m.get("dex_total_musd")),
        "flip": m.get("gamma_flip"),
        "max_pain": m.get("max_pain"),
        "regime": m.get("regime_label") or c.get("direction"),
        "conviction": c.get("conviction"),
        "score": c.get("score"),
        "iv30": m.get("iv30"),
        "iv_rank": ivr.get("rank"),
        "rr": m.get("rr_monthly") if m.get("rr_monthly") is not None else m.get("rr_weekly"),
        "funding_ann": f.get("annualized_pct"),
        "flow_net": fs.get("net_musd"),
        "vol_pct": fs.get("vol_pct"),
        "sign_mode": (m.get("dealer_sign") or {}).get("mode"),
    }

def _read_prev_state(asset, today):
    try:
        with open(_daily_state_path(asset), encoding="utf-8") as f:
            st = json.load(f)
        return st if st.get("date") != today else st.get("_prev")
    except Exception:
        return None

def save_state(asset, m):
    """Écrit l'état du jour en gardant celui de la veille sous _prev."""
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        cur = _capture_state(m)
        old = None
        try:
            with open(_daily_state_path(asset), encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            pass
        if old and old.get("date") != cur["date"]:
            old.pop("_prev", None)
            cur["_prev"] = old
        elif old and old.get("_prev"):
            cur["_prev"] = old["_prev"]          # même jour : on garde la vraie veille
        with open(_daily_state_path(asset), "w", encoding="utf-8") as f:
            json.dump(cur, f)
    except Exception:
        pass

def priorities(m, asset):
    """QUOI REGARDER AUJOURD'HUI — note chaque situation (0-100) et ne remonte que ce
    qui sort de l'ordinaire, avec une lecture detaillee : ce que ca signifie, ce que
    ca implique concretement, et ce qui invaliderait la lecture."""
    out = []
    gex = m.get("gex_total_musd")
    spot = m.get("spot")
    em = (m.get("expected_move") or {})
    walls = (m.get("gamma_walls") or {})
    cw, pw = walls.get("call_wall"), walls.get("put_wall")
    mp = m.get("max_pain")

    def euro(x):
        return f"${x:,.0f}" if isinstance(x, (int, float)) else "?"

    def add(score, titre, detail, faire, surveiller=None):
        out.append({"score": int(max(0, min(100, score))), "titre": titre,
                    "detail": detail, "quoi_faire": faire,
                    "surveiller": surveiller})

    # 1) REGIME
    if isinstance(gex, (int, float)):
        borne = ""
        if cw and pw:
            borne = f" Les bornes du jour : mur put {euro(pw)} en bas, mur call {euro(cw)} en haut."
        if gex < 0:
            add(85, "Regime d'AMPLIFICATION (gamma negatif)",
                f"GEX {gex:+,.0f}M$. Les dealers sont du mauvais cote du gamma : quand le prix "
                f"monte ils doivent acheter, quand il baisse ils doivent vendre. Leur couverture "
                f"POUSSE le mouvement au lieu de le freiner." + borne,
                "Concretement : les cassures ont plus de chances d'aller au bout, les retours a la "
                "moyenne echouent plus souvent. Privilegier le suivi de tendance, entrer sur cassure "
                "plutot que sur repli. Elargir les stops (la volatilite realisee sera superieure a "
                "l'ordinaire) et reduire la taille pour compenser.",
                "Un retour du GEX en territoire positif : le regime redeviendrait amorti.")
        else:
            add(70, "Regime de RANGE (gamma positif)",
                f"GEX {gex:+,.0f}M$. Les dealers absorbent : ils vendent quand ca monte et achetent "
                f"quand ca baisse. Leur couverture FREINE le mouvement." + borne
                + (f" Aimant : max pain {euro(mp)}." if mp else ""),
                "Concretement : les extremes ont tendance a etre rachetes, les cassures echouent plus "
                "souvent. Privilegier les strategies de range : vendre pres du mur call, acheter pres "
                "du mur put, viser le centre. Stops plus serres acceptables, la volatilite realisee "
                "sera inferieure a l'ordinaire.",
                "Une cassure nette d'un mur avec volume : le gamma se reduirait et le regime changerait.")

    # 2) PROXIMITE DU FLIP
    fv = m.get("flip_vs_spot_pct")
    flip = m.get("gamma_flip")
    if isinstance(fv, (int, float)) and abs(fv) <= 1.5:
        au_dessus = fv < 0
        add(95, "Spot COLLE au gamma flip",
            f"Le flip est a {euro(flip)}, soit {abs(fv):.1f}% du spot. Le spot est "
            f"{'AU-DESSUS' if au_dessus else 'SOUS'} ce niveau. C'est la frontiere exacte entre les "
            f"deux regimes : au-dessus le marche est amorti, en dessous il est amplifie.",
            "Concretement : la nature meme du marche peut changer dans la journee. Une position prise "
            "maintenant peut se retrouver dans un regime oppose en quelques heures. Le plus sage est "
            "d'attendre la cassure et de trader DANS le nouveau regime : si ca casse par le bas, "
            "passer en mode tendance (le mouvement s'auto-entretient) ; si ca tient au-dessus, "
            "revenir en mode range.",
            f"Le franchissement franc de {euro(flip)}, confirme par du volume.")

    # 3) EXPIRATION MAJEURE
    for r in (m.get("expiry_calendar") or {}).get("rows", []):
        if r["days"] <= 7 and r["gamma_pct"] >= 25:
            mp_e = r.get("max_pain")
            ecart = ((mp_e / spot - 1) * 100) if (mp_e and spot) else None
            add(90 - r["days"] * 3, f"Expiration majeure dans {r['days']}j",
                f"{r['gamma_pct']}% du gamma total disparait le {r['date']} "
                f"({r['notional_musd']:,}M$ de notionnel)"
                + (f", max pain de cette echeance {euro(mp_e)}"
                   f" ({ecart:+.1f}% du spot)" if ecart is not None else "")
                + ". Ce gamma est ce qui tient actuellement le prix.",
                "Concretement, deux phases. AVANT : plus on approche, plus les dealers hedgent fort "
                "autour des gros strikes — le prix a tendance a etre 'epingle' pres du max pain de "
                "cette echeance, et la volatilite realisee reste contenue. APRES : ce gamma sort du "
                "book d'un coup, les dealers n'ont plus a hedger ces positions, et le marche est "
                "libere. Les mouvements post-expiration sont souvent plus amples. Ne pas construire "
                "de position longue duree sur les niveaux actuels : ils sont perimes apres cette date.",
                f"Recalculer tous les niveaux le {r['date']} au soir : flip, murs et max pain "
                f"seront differents.")
            break

    # 4) EXTREMES DE VOLATILITE
    ivr = m.get("iv_rank") or {}
    if ivr.get("status") == "ok":
        if ivr["rank"] >= 85:
            add(75, f"Volatilite TRES CHERE ({ivr['rank']}/100)",
                f"L'IV actuelle ({ivr['iv']}%) est plus haute que {ivr['rank']}% des "
                f"{ivr.get('years', '?')} dernieres annees ({ivr.get('src')}). "
                f"Bornes historiques : {ivr.get('lo')}% a {ivr.get('hi')}%.",
                "Concretement : acheter des options coute cher, et le temps joue contre l'acheteur. "
                "Les strategies vendeuses de prime (spreads vendeurs, iron condors) sont statistiquement "
                "favorisees. Mais attention : une IV elevee signale souvent un risque reel — vendre de "
                "la vol dans une tension qui s'aggrave est le meilleur moyen de perdre gros. Toujours "
                "borner le risque.",
                "Un repli rapide de l'IV : c'est le signal que la tension retombe.")
        elif ivr["rank"] <= 15:
            add(72, f"Volatilite TRES BON MARCHE ({ivr['rank']}/100)",
                f"L'IV actuelle ({ivr['iv']}%) est plus basse que {100 - ivr['rank']}% des "
                f"{ivr.get('years', '?')} dernieres annees ({ivr.get('src')}). "
                f"Bornes historiques : {ivr.get('lo')}% a {ivr.get('hi')}%.",
                "Concretement : l'optionalite est peu chere. Se couvrir coute peu, et prendre du levier "
                "via des options est plus interessant que d'habitude. C'est le moment de payer pour de "
                "la protection, pas d'en vendre. Rappel : une vol basse n'annonce pas le calme, elle "
                "signale surtout que le marche ne price aucun risque — ce qui rend les surprises plus "
                "violentes.",
                "Un reveil de l'IV : les positions acheteuses de vol deviendraient rapidement gagnantes.")

    # 5) LEVIER EXTREME
    f = m.get("funding") or {}
    ann = f.get("annualized_pct")
    if isinstance(ann, (int, float)) and abs(ann) >= 40:
        sens = "longs" if ann > 0 else "shorts"
        oppose = "shorts" if ann > 0 else "longs"
        add(80, f"Funding EXTREME ({ann:+.0f}% annualise)",
            f"Les {sens} paient {abs(ann):.0f}% par an aux {oppose} pour tenir leur position. "
            f"C'est le signe d'un desequilibre marque du levier : le marche est massivement "
            f"positionne du meme cote.",
            f"Concretement : porter une position {sens} coute cher chaque jour, ce qui fragilise les "
            f"mains faibles. Historiquement, ces exces se resorbent soit par une consolidation qui "
            f"decourage les {sens}, soit par un mouvement brutal qui les liquide. Ce n'est PAS un "
            f"signal d'entree contrarien immediat (un funding extreme peut durer des semaines en "
            f"tendance forte), mais c'est une raison de reduire le levier et de se mefier des entrees "
            f"tardives dans le sens du consensus.",
            "Une normalisation du funding : le desequilibre se serait purge.")

    # 6) POCHE DE LIQUIDATIONS
    lm = m.get("liq_map")
    if lm:
        for b in (lm.get("above") or []) + (lm.get("below") or []):
            dist = abs(b["price"] / lm["spot"] - 1) * 100
            if dist <= 3 and b["musd"] >= 50:
                dessus = b["price"] > lm["spot"]
                qui = "shorts" if dessus else "longs"
                sens = "hausse" if dessus else "baisse"
                add(78, f"Poche de liquidations a {dist:.1f}% du spot",
                    f"Environ {b['musd']:,.0f}M$ de positions {qui} seraient liquidees vers "
                    f"{euro(b['price'])} ({'au-dessus' if dessus else 'en dessous'} du spot). "
                    f"Estimation basee sur la distribution de levier, pas sur des positions reelles.",
                    f"Concretement : si le prix atteint cette zone, les liquidations forcees "
                    f"alimentent le mouvement dans le sens de la {sens} — c'est un accelerateur, pas "
                    f"un support. Deux implications : cette zone agit comme un aimant (le marche va "
                    f"souvent chercher la liquidite ou elle se trouve), et c'est le pire endroit pour "
                    f"placer un stop, car il sera balaye par la cascade. Placer les stops AU-DELA de "
                    f"la poche, pas dedans.",
                    "Le passage du prix dans la zone : si elle est traversee sans acceleration, "
                    "l'estimation etait fausse ou la poche deja purgee.")
                break

    # 7) FLUX CLIENT MARQUE
    fs = m.get("flow_summary")
    if fs and abs(fs.get("net_musd", 0)) >= 20:
        cote_dealer = fs.get("dealers_options", "?")
        add(60, f"Flux client net {fs['clients_options']} d'options",
            f"{fs['net_musd']:+,.1f}M$ de flux client net sur 24h "
            f"(calls {fs.get('net_call_musd', 0):+,.1f}M$, puts {fs.get('net_put_musd', 0):+,.1f}M$). "
            f"Les dealers sont donc {cote_dealer} nets. {fs.get('vol_stance', '').capitalize()}.",
            "Concretement : les dealers prennent toujours le cote oppose au client, puis se couvrent "
            "sur le spot. Savoir de quel cote ils sont permet d'anticiper leur hedging futur. Si les "
            "clients achetent massivement des calls, les dealers sont short calls : ils devront "
            "acheter du spot si le prix monte, ce qui amplifie la hausse.",
            "Une inversion du flux : le sens du hedging dealer changerait avec.")

    # 8) CHANGEMENTS FORTS DEPUIS HIER
    for ch in (m.get("changes") or []):
        if ch["level"] == "fort":
            add(88, f"CHANGEMENT : {ch['text']}", ch["why"],
                "Bascule structurelle depuis hier. Toute lecture etablie la veille est a reevaluer : "
                "le comportement des dealers a change de nature, donc la strategie adaptee aussi.",
                "La persistance du changement demain : un aller-retour d'un jour est souvent du bruit.")

    out.sort(key=lambda x: -x["score"])
    return out[:6]



def detect_changes(asset, m):
    """Renvoie la liste des changements notables vs la veille, triés par gravité.
    [{'level':'fort|moyen|info', 'text':..., 'why':...}]"""
    today = dt.date.today().isoformat()
    prev = _read_prev_state(asset, today)
    cur = _capture_state(m)
    if not prev:
        return []
    out = []

    def num(a, b):
        return isinstance(a, (int, float)) and isinstance(b, (int, float))

    # 1) GEX qui change de signe = changement de RÉGIME (le plus important)
    if num(cur["gex"], prev.get("gex")) and (cur["gex"] < 0) != (prev["gex"] < 0):
        sens = "NÉGATIF (amplification)" if cur["gex"] < 0 else "POSITIF (amortissement)"
        out.append({"level": "fort",
                    "text": f"GEX passe {sens} : {prev['gex']:+.1f} → {cur['gex']:+.1f}M$",
                    "why": "les dealers changent de comportement : ils amplifient au lieu d'absorber (ou l'inverse)"})

    # 2) Spot qui franchit le gamma flip
    if num(cur["spot"], prev.get("spot")) and num(cur["flip"], prev.get("flip")):
        was, now = prev["spot"] > prev["flip"], cur["spot"] > cur["flip"]
        if was != now:
            out.append({"level": "fort",
                        "text": f"Spot {'repasse AU-DESSUS' if now else 'casse SOUS'} le gamma flip (${cur['flip']:,.0f})",
                        "why": "au-dessus = marché amorti, en-dessous = mouvements amplifiés"})

    # 3) Verdict de convergence
    if cur["conviction"] != prev.get("conviction") or cur["regime"] != prev.get("regime"):
        out.append({"level": "moyen",
                    "text": f"Verdict : {prev.get('regime')} {prev.get('conviction')} → {cur['regime']} {cur['conviction']}",
                    "why": "l'alignement des niveaux directionnels a changé"})

    # 4) Max pain qui se déplace fortement
    if num(cur["max_pain"], prev.get("max_pain")) and prev["max_pain"]:
        var = (cur["max_pain"] / prev["max_pain"] - 1) * 100
        if abs(var) >= 3:
            out.append({"level": "moyen",
                        "text": f"Max pain déplacé de {var:+.1f}% : ${prev['max_pain']:,.0f} → ${cur['max_pain']:,.0f}",
                        "why": "l'aimant du marché s'est déplacé — souvent après une grosse expiration"})

    # 5) IV : variation forte ou extrême de rank
    if num(cur["iv30"], prev.get("iv30")) and prev["iv30"]:
        var = cur["iv30"] - prev["iv30"]
        if abs(var) >= 5:
            out.append({"level": "moyen",
                        "text": f"IV 30j {var:+.1f} pts : {prev['iv30']:.1f}% → {cur['iv30']:.1f}%",
                        "why": "hausse = le marché price plus de risque ; baisse = complaisance"})
    if num(cur["iv_rank"], prev.get("iv_rank")):
        if cur["iv_rank"] >= 80 > prev.get("iv_rank", 0):
            out.append({"level": "moyen", "text": f"IV Rank entre en zone haute ({cur['iv_rank']}/100)",
                        "why": "optionalité chère — vendre de la vol devient plus favorable"})
        elif cur["iv_rank"] <= 20 < prev.get("iv_rank", 100):
            out.append({"level": "moyen", "text": f"IV Rank entre en zone basse ({cur['iv_rank']}/100)",
                        "why": "optionalité bon marché — acheter de la vol devient plus favorable"})

    # 6) Skew qui s'inverse
    if num(cur["rr"], prev.get("rr")) and (cur["rr"] > 0) != (prev["rr"] > 0):
        out.append({"level": "moyen",
                    "text": f"Skew 25Δ s'inverse : {prev['rr']:+.2f} → {cur['rr']:+.2f}",
                    "why": "bascule entre demande de protection (puts) et spéculation (calls)"})

    # 7) Funding : emballement ou détente
    if num(cur["funding_ann"], prev.get("funding_ann")):
        if abs(cur["funding_ann"]) >= 25 and abs(prev["funding_ann"]) < 25:
            out.append({"level": "moyen",
                        "text": f"Funding s'emballe : {prev['funding_ann']:+.0f}% → {cur['funding_ann']:+.0f}% annualisé",
                        "why": "levier déséquilibré — risque de reversal / liquidations en cascade"})
        elif abs(cur["funding_ann"]) < 10 <= abs(prev["funding_ann"]):
            out.append({"level": "info", "text": f"Funding se normalise ({cur['funding_ann']:+.0f}% annualisé)",
                        "why": "le levier se dégonfle, stress en baisse"})

    # 8) Flux client qui s'inverse
    if num(cur["flow_net"], prev.get("flow_net")) and (cur["flow_net"] > 0) != (prev["flow_net"] > 0):
        out.append({"level": "moyen",
                    "text": f"Flux client s'inverse : clients {'acheteurs' if cur['flow_net'] > 0 else 'vendeurs'} nets d'options ({cur['flow_net']:+.1f}M$)",
                    "why": "les dealers prennent l'autre côté — leur hedge change de sens"})

    # 9) Sens de la volatilité
    if num(cur["vol_pct"], prev.get("vol_pct")) and (cur["vol_pct"] > 5) != (prev["vol_pct"] > 5) \
            and abs(cur["vol_pct"] - prev["vol_pct"]) >= 10:
        out.append({"level": "info",
                    "text": f"Sens de la vol : clients {'acheteurs' if cur['vol_pct'] > 5 else 'vendeurs'} de volatilité ({cur['vol_pct']:+.0f}%)",
                    "why": "détermine si les dealers sont long ou short vega"})

    # 10) Bascule du mode de signe (fixe -> empirique)
    if cur["sign_mode"] != prev.get("sign_mode") and cur["sign_mode"] == "empirical":
        out.append({"level": "info", "text": "Signe dealer désormais MESURÉ sur le tape (mode empirique actif)",
                    "why": "les chiffres GEX/DEX ne sont plus basés sur une convention supposée"})

    order = {"fort": 0, "moyen": 1, "info": 2}
    out.sort(key=lambda x: order[x["level"]])
    return out

# === Contexte macro : corrélation BTC/SPX, proxy dollar, régime risk-on/off =======
# ZÉRO nouvelle source : tout vient des historiques quotidiens déjà collectés.
# Proxy dollar = -(0.81×FXE + 0.19×FXY) : EUR (57.6%) + JPY (13.6%) pèsent ~71% du
# DXY ; on renormalise leurs poids. Un vrai DXY exigerait une source dédiée.
def _spot_series(asset):
    try:
        return [(r["date"], r["spot"]) for r in _read_history_file(asset, "mensuel")
                if r.get("spot")]
    except Exception:
        return []

def _ret_pct(series, lag):
    if len(series) < lag + 1:
        return None
    a, b = series[-1][1], series[-1 - lag][1]
    return round((a / b - 1) * 100, 2) if b else None

def macro_context():
    """Situe le crypto dans le marché global. Fiable à mesure que l'historique grandit."""
    try:
        btc, spy, gld = _spot_series("BTC"), _spot_series("SPY"), _spot_series("GC")
        fxe, fxy = _spot_series("EU 6E"), _spot_series("JP 6J")
        # --- corrélation BTC/SPX sur rendements quotidiens (dates communes, fenêtre 30) ---
        db, ds = dict(btc), dict(spy)
        common = sorted(set(db) & set(ds))
        rb, rs = [], []
        for i in range(1, len(common)):
            b0, s0 = db[common[i - 1]], ds[common[i - 1]]
            if b0 and s0:
                rb.append(db[common[i]] / b0 - 1)
                rs.append(ds[common[i]] / s0 - 1)
        corr = None
        if len(rb) >= 5:
            c = np.corrcoef(rb[-30:], rs[-30:])[0, 1]
            corr = round(float(c), 2) if np.isfinite(c) else None
        # --- variations 5 jours (points d'historique, ~1 semaine de bourse) ---
        spy5, btc5, gld5 = _ret_pct(spy, 5), _ret_pct(btc, 5), _ret_pct(gld, 5)
        f5, y5 = _ret_pct(fxe, 5), _ret_pct(fxy, 5)
        dxy5 = (round(-(0.81 * f5 + 0.19 * y5), 2) if (f5 is not None and y5 is not None)
                else (round(-f5, 2) if f5 is not None else None))
        # --- vote risk-on/off : actions+, crypto+, dollar-, or- = appétit pour le risque ---
        votes = n_sig = 0
        for v, risk_on_when_up in [(spy5, True), (btc5, True), (dxy5, False), (gld5, False)]:
            if v is None:
                continue
            n_sig += 1
            if abs(v) >= 0.15:                    # variation significative seulement
                votes += 1 if (v > 0) == risk_on_when_up else -1
        if n_sig < 3:
            regime = "COLLECTE"
        elif votes >= 2:
            regime = "RISK-ON"
        elif votes <= -2:
            regime = "RISK-OFF"
        else:
            regime = "MIXTE"
        return {"corr_btc_spx": corr, "corr_n": len(rb), "spy_5d": spy5, "btc_5d": btc5,
                "gld_5d": gld5, "dxy_5d": dxy5, "regime": regime, "votes": votes,
                "n_signals": n_sig}
    except Exception:
        return None

# === Bougies de prix (OHLC) ======================================================
# CRYPTO : Deribit publie l'OHLC du perpetuel en TEMPS REEL et gratuitement
#   (get_tradingview_chart_data). Resolutions 1m a 1j. C'est la meme donnee que les
#   graphiques de la plateforme, sans delai.
# MACRO  : aucun flux intraday gratuit fiable pour les indices/ETF. On utilise donc
#   les bougies JOURNALIERES de Stooq (fin de seance, fiables) et on assume le
#   differe. Le prix "spot" affiche ailleurs vient de CBOE (differe 15 min).
_CANDLE_CACHE = {}
# Resolutions Deribit disponibles (en minutes). Pas de sous-minute : l'API
# ne descend pas en dessous de la bougie 1 minute.
RESOLUTIONS = {"1m": ("1", 1), "5m": ("5", 5), "15m": ("15", 15),
               "30m": ("30", 30), "1h": ("60", 60), "2h": ("120", 120),
               "4h": ("240", 240), "12h": ("720", 720), "1j": ("1D", 1440)}

def price_candles(asset, res="1h", bougies=5000):
    """[{t,o,h,l,c}] + metadonnees de fraicheur. Cache court (60s) pour ne pas
    marteler l'API a chaque rafraichissement de page."""
    import time as _t
    cfg = ASSETS.get(asset)
    if not cfg:
        return None
    res = res if res in RESOLUTIONS else "1h"
    key = (asset, res)
    now = _t.time()
    c = _CANDLE_CACHE.get(key)
    if c and now - c[0] < 60:
        return c[1]
    out = None
    try:
        if cfg["source"] == "deribit":
            code, minutes = RESOLUTIONS[res]
            inst = (f"{asset}_USDC-PERPETUAL" if asset in DERIBIT_LINEAR
                    else f"{asset}-PERPETUAL")
            end = int(now * 1000)
            bougies = max(50, min(bougies, 5000))
            start = end - bougies * minutes * 60 * 1000
            r = _deribit_get("public/get_tradingview_chart_data",
                             instrument_name=inst, start_timestamp=start,
                             end_timestamp=end, resolution=code)
            ticks = r.get("ticks") or []
            if ticks:
                out = {"res": res, "temps_reel": True,
                       "source": f"Deribit {inst} (temps reel)",
                       "instrument": inst,   # nom exact pour s'abonner au flux WebSocket live
                       "bougies": [{"t": ticks[i],
                                    "o": r["open"][i], "h": r["high"][i],
                                    "l": r["low"][i], "c": r["close"][i],
                                    "v": (r.get("volume") or [None] * len(ticks))[i]}
                                   for i in range(len(ticks))]}
        else:
            sym = (cfg.get("cboe") or asset).lower()
            rr = requests.get(f"https://stooq.com/q/d/l/?s={sym}.us&i=d", timeout=10,
                              headers={"User-Agent": "Mozilla/5.0"})
            if rr.status_code == 200 and "Date" in rr.text[:60]:
                rows = [l.split(",") for l in rr.text.strip().splitlines()[1:] if l]
                b = []
                for x in rows[-bougies:]:
                    if len(x) >= 5:
                        try:
                            ts = int(dt.datetime.strptime(x[0], "%Y-%m-%d")
                                     .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                            b.append({"t": ts, "o": float(x[1]), "h": float(x[2]),
                                      "l": float(x[3]), "c": float(x[4]),
                                      "v": float(x[5]) if len(x) > 5 and x[5] not in ("", "N/D") else None})
                        except ValueError:
                            continue
                if b:
                    out = {"res": "1j", "temps_reel": False,
                           "source": "Stooq (bougies journalieres, fin de seance)",
                           "bougies": b}
    except Exception:
        out = None
    if out:
        _CANDLE_CACHE[key] = (now, out)
    return out

# === Volume : options (flux d'attention) + sous-jacent (crédibilité du move) ======
# Volume d'options : déjà dans le book (Deribit 24h / CBOE séance) -> aucun fetch.
#   Stocké 1 ligne/jour pour comparer le jour à sa moyenne (activité anormale ?).
# Volume du sous-jacent : Hyperliquid (crypto, bougies 1h) / Stooq (macro, CSV daily
#   gratuit sans clé). Un mouvement de prix sans volume est moins crédible.
def _volu_path(asset):
    return os.path.join(HIST_DIR, f"{asset}_volume.csv")

def record_option_volume(asset, call_v, put_v):
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        date = dt.date.today().isoformat()
        path = _volu_path(asset)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                if any(line.startswith(date + ",") for line in f):
                    return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{date},{round(call_v, 1)},{round(put_v, 1)}\n")
    except Exception:
        pass

def option_volume(asset, book):
    """Volume d'options du jour + comparaison à la moyenne des jours précédents."""
    try:
        call_v = sum(o.get("volume", 0) or 0 for o in book if o["type"] == "C")
        put_v = sum(o.get("volume", 0) or 0 for o in book if o["type"] == "P")
        total = call_v + put_v
        record_option_volume(asset, call_v, put_v)
        hist = []
        today = dt.date.today().isoformat()
        try:
            with open(_volu_path(asset), encoding="utf-8") as f:
                for line in f:
                    p = line.strip().split(",")
                    if len(p) >= 3 and p[0] != today:
                        hist.append(float(p[1]) + float(p[2]))
        except OSError:
            pass
        hist = hist[-20:]
        avg = sum(hist) / len(hist) if hist else None
        ratio = round(total / avg, 2) if avg and avg > 0 else None
        if ratio is None:
            lab = f"collecte ({len(hist) + 1}j)"
        elif ratio >= 1.5:
            lab = "activité anormalement forte"
        elif ratio >= 1.15:
            lab = "activité soutenue"
        elif ratio <= 0.6:
            lab = "activité faible — marché endormi"
        else:
            lab = "activité normale"
        return {"call": round(call_v), "put": round(put_v), "total": round(total),
                "avg": round(avg) if avg else None, "ratio": ratio, "label": lab,
                "days": len(hist), "pcr": round(put_v / call_v, 2) if call_v > 0 else None}
    except Exception:
        return None

_UNDERLYING_VOL_CACHE = {}

def underlying_volume(asset, cfg):
    """Volume du sous-jacent : 24h crypto (Hyperliquid) ou dernière séance (Stooq),
    comparé à la moyenne 20 jours. None si source indisponible."""
    import time as _t
    now = _t.time()
    c = _UNDERLYING_VOL_CACHE.get(asset)
    if c and now - c[0] < 900:
        return c[1]
    try:
        cur = avg = None
        unit = ""
        if cfg["source"] == "deribit":
            end = int(now * 1000)
            r = requests.post("https://api.hyperliquid.xyz/info",
                              json={"type": "candleSnapshot",
                                    "req": {"coin": asset, "interval": "1h",
                                            "startTime": end - 21 * 24 * 3600 * 1000,
                                            "endTime": end}},
                              timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            candles = r.json()
            if not isinstance(candles, list) or len(candles) < 48:
                return None
            days = [candles[i:i + 24] for i in range(0, len(candles) - 23, 24)]
            tot = [sum(float(cd["v"]) * float(cd["c"]) for cd in d) for d in days]
            cur, prev = tot[-1], tot[:-1][-20:]
            avg = sum(prev) / len(prev) if prev else None
            unit = "$"
        else:
            sym = (cfg.get("cboe") or asset).lower()
            r = requests.get(f"https://stooq.com/q/d/l/?s={sym}.us&i=d", timeout=8,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or "Date" not in r.text[:60]:
                return None
            rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:] if ln]
            vols = [float(x[5]) for x in rows[-21:] if len(x) >= 6 and x[5] not in ("", "N/D")]
            if len(vols) < 5:
                return None
            cur, prev = vols[-1], vols[:-1]
            avg = sum(prev) / len(prev)
            unit = "titres"
        if not cur or not avg:
            return None
        ratio = round(cur / avg, 2)
        lab = ("volume exceptionnel — mouvement crédible" if ratio >= 1.5 else
               "volume soutenu" if ratio >= 1.15 else
               "volume faible — mouvement peu crédible" if ratio <= 0.6 else
               "volume normal")
        out = {"current": round(cur), "avg": round(avg), "ratio": ratio,
               "label": lab, "unit": unit}
        _UNDERLYING_VOL_CACHE[asset] = (now, out)
        return out
    except Exception:
        return None

# === Santé des collecteurs =========================================================
# Les pros surveillent leurs pipelines : un collecteur qui s'arrête en silence
# (PC éteint, tâche Windows en panne) fausse tout sans prévenir. Sentinelle = BTC
# (coté 7/7). Statut par âge de la dernière écriture : ok ≤1j · warn 2-3j · late >3j.
def _last_csv_date(path):
    try:
        last = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                d = line.split(",", 1)[0].strip()
                if len(d) == 10:
                    last = d
        return last
    except OSError:
        return None

def _last_snap_date(directory, asset):
    try:
        files = sorted(f for f in os.listdir(directory)
                       if f.startswith(asset + "_") and f.endswith(".json"))
        return files[-1][len(asset) + 1:-5] if files else None
    except OSError:
        return None

def data_health(sentinel="BTC"):
    snap_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    flux = [
        ("Historique quotidien (daily.py)", _last_csv_date(os.path.join(HIST_DIR, f"{sentinel}_mensuel_dexgex.csv"))),
        ("Photos OI par strike",            _last_snap_date(OI_DIR, sentinel)),
        ("Tape (signe empirique)",          _last_snap_date(TAPE_DIR, sentinel)),
        ("IV quotidienne (rank/percentile)", _last_csv_date(os.path.join(HIST_DIR, f"{sentinel}.csv"))),
        ("Book total (courbe)",             _last_csv_date(_total_hist_path(sentinel))),
        ("Snapshots JSON (archive)",        _last_snap_date(snap_dir, sentinel)),
    ]
    today = dt.date.today()
    rows = []
    worst = "ok"
    rank = {"ok": 0, "warn": 1, "late": 2, "never": 3}
    for name, last in flux:
        if last is None:
            st, age = "never", None
        else:
            try:
                age = (today - dt.date.fromisoformat(last)).days
            except ValueError:
                age = None
            st = "never" if age is None else ("ok" if age <= 1 else "warn" if age <= 3 else "late")
        rows.append({"flux": name, "last": last, "age_days": age, "status": st})
        if rank[st] > rank[worst]:
            worst = st
    return {"rows": rows, "worst": worst, "sentinel": sentinel}

# === Tape Deribit : flux client signé -> signe dealer EMPIRIQUE ===================
# La méthode pro : au lieu de SUPPOSER l'inventaire dealer (fixe) ou de le deviner via
# le skew (adaptatif), on le MESURE. Chaque trade Deribit porte le côté agresseur
# (taker) : si les clients achètent net des calls, les dealers sont short calls, etc.
# 1 photo/jour (idempotente) du flux client 24h par type ; cumul sur 14 jours.
TAPE_DIR = os.path.join(HIST_DIR, "tape_snap")
TAPE_MIN_DAYS = 3          # jours de tape minimum avant de faire confiance au signe
TAPE_MIN_MUSD = 5.0        # notionnel net minimal (M$) pour considérer un côté significatif

def _tape_files(asset):
    try:
        return sorted(f for f in os.listdir(TAPE_DIR)
                      if f.startswith(asset + "_") and f.endswith(".json"))
    except OSError:
        return []

def record_tape_snapshot(asset, ref_price):
    """Photo du flux client 24h : notionnel signé (achat +, vente -) agrégé par type C/P.
    BTC/ETH seulement (tape liquide). Idempotent, garde 14 fichiers."""
    if asset not in ("BTC", "ETH"):
        return
    try:
        os.makedirs(TAPE_DIR, exist_ok=True)
        date = dt.date.today().isoformat()
        path = os.path.join(TAPE_DIR, f"{asset}_{date}.json")
        if os.path.exists(path):
            return
        import time as _t
        cutoff = int(_t.time() * 1000) - 24 * 3600 * 1000
        res = _deribit_get("public/get_last_trades_by_currency",
                           currency=asset, kind="option", count=1000)
        flow = {"C": 0.0, "P": 0.0, "n": 0}
        for t in res.get("trades", []):
            if (t.get("timestamp") or 0) < cutoff:
                continue
            parts = t.get("instrument_name", "").split("-")
            if len(parts) != 4:
                continue
            cp = parts[3].upper()
            if cp not in ("C", "P"):
                continue
            ipx = t.get("index_price") or ref_price or 0
            notional = (t.get("amount") or 0) * ipx
            flow[cp] += notional if t.get("direction") == "buy" else -notional
            flow["n"] += 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(flow, f)
        for old in _tape_files(asset)[:-14]:
            try:
                os.remove(os.path.join(TAPE_DIR, old))
            except OSError:
                pass
    except Exception:
        pass

def empirical_signs(asset):
    """Signes dealers déduits du tape cumulé : dealer = opposé du flux client net.
    Renvoie {status:'ok', sc, sp, ...} ou {status:'collecting', have} ou None."""
    if asset not in ("BTC", "ETH"):
        return None
    try:
        files = _tape_files(asset)
        if len(files) < TAPE_MIN_DAYS:
            return {"status": "collecting", "have": len(files), "need": TAPE_MIN_DAYS}
        net_c = net_p = 0.0
        for fn in files:
            with open(os.path.join(TAPE_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            net_c += d.get("C", 0.0)
            net_p += d.get("P", 0.0)
        nc_m, np_m = net_c / 1e6, net_p / 1e6
        # côté non significatif (< TAPE_MIN_MUSD) -> on garde la convention fixe pour ce côté
        sc = (-1 if nc_m > 0 else 1) if abs(nc_m) >= TAPE_MIN_MUSD else SIGN_CALL
        sp = (-1 if np_m > 0 else 1) if abs(np_m) >= TAPE_MIN_MUSD else SIGN_PUT
        return {"status": "ok", "sc": sc, "sp": sp, "days": len(files),
                "net_call_musd": round(nc_m, 1), "net_put_musd": round(np_m, 1)}
    except Exception:
        return None

# === Snapshots OI par strike (pour la variation OI 24h) ===========================
# Une photo par jour de l'open interest agrégé par strike (toutes échéances), 1 fichier
# par jour et par actif. Idempotent : si la photo du jour existe déjà, on ne réécrit pas.
# La variation 24h compare le book live au dernier snapshot ANTÉRIEUR à aujourd'hui.
OI_DIR = os.path.join(HIST_DIR, "oi_snap")

def _oi_by_strike(book):
    """OI agrégé par strike, séparé call/put : {'C':{strike_str:oi}, 'P':{...}}."""
    out = {"C": {}, "P": {}}
    for o in book:
        cp = o.get("type")
        if cp not in ("C", "P"):
            continue
        k = str(int(round(o["strike"])))
        out[cp][k] = out[cp].get(k, 0.0) + float(o.get("oi", 0) or 0)
    return out

def _oi_files(asset):
    try:
        return sorted(f for f in os.listdir(OI_DIR)
                      if f.startswith(asset + "_") and f.endswith(".json"))
    except OSError:
        return []

def record_oi_snapshot(asset, book):
    """Écrit la photo OI du jour (1 fichier/jour/actif, idempotent). Garde les 12 derniers."""
    try:
        os.makedirs(OI_DIR, exist_ok=True)
        date = dt.date.today().isoformat()
        path = os.path.join(OI_DIR, f"{asset}_{date}.json")
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_oi_by_strike(book), f)
        for old in _oi_files(asset)[:-12]:
            try:
                os.remove(os.path.join(OI_DIR, old))
            except OSError:
                pass
    except Exception:
        pass

def oi_change_24h(asset, book):
    """Compare l'OI par strike d'aujourd'hui (book live) au dernier snapshot antérieur.
    Renvoie les plus gros mouvements (build/unwind) + agrégats net call/put.
    status='collecting' tant qu'aucun snapshot antérieur n'existe."""
    try:
        date = dt.date.today().isoformat()
        files = _oi_files(asset)
        prior = [f for f in files if f < f"{asset}_{date}.json"]
        if not prior:
            return {"status": "collecting", "have": len(files)}
        prev_path = os.path.join(OI_DIR, prior[-1])
        prev_date = prior[-1][len(asset) + 1:-5]
        with open(prev_path, encoding="utf-8") as f:
            prev = json.load(f)
        cur = _oi_by_strike(book)
        gap = max(1, (dt.date.today() - dt.date.fromisoformat(prev_date)).days)
        rows = []
        for cp in ("C", "P"):
            for k in set(cur[cp]) | set(prev.get(cp, {})):
                d = cur[cp].get(k, 0.0) - prev.get(cp, {}).get(k, 0.0)
                if abs(d) >= 0.5:
                    rows.append({"cp": cp, "strike": int(k), "doi": round(d, 1),
                                 "oi": round(cur[cp].get(k, 0.0), 1)})
        if not rows:
            return {"status": "flat", "asof": prev_date, "gap_days": gap}
        rows.sort(key=lambda r: abs(r["doi"]), reverse=True)
        net_call = sum(r["doi"] for r in rows if r["cp"] == "C")
        net_put = sum(r["doi"] for r in rows if r["cp"] == "P")
        return {"status": "ok", "asof": prev_date, "gap_days": gap, "top": rows[:6],
                "net_call_doi": round(net_call, 1), "net_put_doi": round(net_put, 1)}
    except Exception:
        return None

def fetch_chain(asset):
    """Récupère la chaîne d'options UNE fois (S, book). Permet à daily.py de calculer
    plusieurs horizons sans refetcher le réseau à chaque fois."""
    asset = asset.upper()
    cfg = ASSETS[asset]
    return (ingest_deribit(asset) if cfg["source"] == "deribit" else ingest_cboe(cfg))

def analyse(asset, catalyst_bias=None, dte_days=None, store_history=False, prefetched=None, horizon=None, sign_mode=None):
    asset = asset.upper()
    if asset not in ASSETS:
        raise ValueError(f"Actif inconnu : {asset}. Dispo : {list(ASSETS)}")
    cfg = ASSETS[asset]
    if prefetched is not None:
        S, book = prefetched
    else:
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

    # --- Signe dealer : figé (fixed) ou déduit du skew (adaptive). Heuristique, voir en-tête. ---
    # On prend le RR mensuel (skew structurel, moins bruité que le court terme) comme orientation.
    rr_for_sign = risk_reversal(book, 30)
    # === SIGNE DEALER : convention FIXE, toujours. ================================
    # C'est le standard publié par SpotGamma, Laevitas, CryptoGamma et les desks :
    # dealers longs calls / courts puts. Elle n'est pas "parfaite" (l'inventaire réel
    # des dealers n'est pas public) mais elle est STABLE et COMPARABLE aux sources
    # externes — un chiffre vérifiable vaut mieux qu'un chiffre plus malin que
    # personne d'autre ne calcule.
    #
    # Le tape (mode empirique) reste MESURÉ et affiché comme INFORMATION séparée
    # ("le flux client suggère X"), mais il ne pilote plus les chiffres : inverser
    # tout le GEX sur quelques jours de tape, c'est promouvoir du bruit en vérité.
    sc, sp, sign_info = dealer_signs(rr_for_sign, mode="fixed")
    sign_info["locked"] = True
    sign_info["reason"] = ("convention fixe (standard SpotGamma/Laevitas) — "
                           "comparable aux sources publiques")
    if crypto:
        emp = empirical_signs(asset)
        if emp and emp.get("status") == "ok":
            # information seulement : ce que le tape suggérerait, sans l'appliquer
            sign_info["tape_days"] = emp["days"]
            sign_info["tape_suggests_flip"] = ((emp["sc"], emp["sp"]) != (SIGN_CALL, SIGN_PUT))
            sign_info["tape_note"] = (
                f"tape {emp['days']}j : clients "
                f"{'acheteurs' if emp['net_call_musd'] > 0 else 'vendeurs'} nets de calls "
                f"({emp['net_call_musd']:+}M$), "
                f"{'acheteurs' if emp['net_put_musd'] > 0 else 'vendeurs'} nets de puts "
                f"({emp['net_put_musd']:+}M$)")
        elif emp:
            sign_info["pending_tape"] = f"{emp.get('have', 0)}/{emp.get('need', TAPE_MIN_DAYS)}"
    signs = (sc, sp)

    gex_total, gex_strikes = gamma_exposure(book_dte, S, csize, signs=signs)
    dex_total = delta_exposure(book_pos, S, csize)                      # DEX positionnement (brut)
    dex_dealer = delta_exposure_dealer(book_pos, S, csize, signs=signs) # DEX flux couverture (signé, mode actif)
    flip = gamma_flip(book_dte, S, csize, signs=signs)
    curve = term_structure(book, S)
    atm30 = min(curve, key=lambda c: abs(c["days"] - 30))["atm_iv"] if curve else None
    ivp, n = iv_percentile(asset, atm30) if atm30 else (None, 0)
    mp = max_pain(book_dte)
    _hd = horizon_dtes(book)                       # vraies échéances dominantes
    # Structure par terme du skew : RR sur court/mensuel/trimestriel, pour voir si la peur
    # est front-loaded (court terme) ou structurelle (long terme).
    skew_term = [{"label": lab, "days": round(_hd[lab]),
                  "rr": risk_reversal(book, max(1, round(_hd[lab])))}
                 for lab in ("court", "mensuel", "trimestriel")]

    metrics = {
        "asset": asset, "label": cfg["label"], "spot": round(S, 2),
        "version": VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "gex_total_musd": round(gex_total / 1e6, 1),
        "gex_per_pct_musd": round(gex_total / 1e6, 1),   # = hedging dealer par 1% de move
        "gex_regime": "ANCRAGE (range)" if gex_total > 0 else "AMPLIFICATION (cassure)",
        "dex_total_musd": round(dex_total / 1e6, 1),
        "dex_flux": "haussier" if dex_total > 0 else "baissier",
        "dex_dealer_musd": round(dex_dealer / 1e6, 1),                    # DEX signé dealer (flux de couverture)
        "dex_dealer_flux": "vendeur" if dex_dealer > 0 else "acheteur",   # dex_dealer>0 -> dealers vendent le spot
        "max_pain": mp, "max_pain_vs_spot_pct": round(100 * (mp - S) / S, 2),
        "gamma_flip": flip, "vol_trigger": flip,
        "flip_vs_spot_pct": (round(100 * (flip - S) / S, 2) if flip else None),
        "expected_move": expected_move(curve, S),
        "gamma_walls": gamma_walls(gex_strikes),
        "put_call": put_call_ratio(book_dte),
        "vol_smile": vol_smile(book_dte, S, band=(0.15 if crypto else 0.08)),
        "gex_by_expiry": gex_by_expiry(book_dte, S, csize, signs=signs),
        "gamma_profile": gamma_profile(book_dte, S, csize, span=(0.18 if crypto else 0.10), signs=signs),
        "rr_weekly": risk_reversal(book, max(1, round(eff_dte))),
        "rr_monthly": risk_reversal(book, 30),
        "rr_near_days": max(1, round(eff_dte)),
        "skew_term": skew_term,
        "charm_vanna_opex": charm_vanna_opex(book, S, csize, signs=signs, r=(0.0 if crypto else MACRO_RISK_FREE)),
        "vol_trigger_regime": ("range" if (flip and S >= flip) else "breakout" if flip else None),
        "term_regime": detect_term_regime(curve),
        "iv_percentile": ivp, "iv_history_points": n,
        "term_curve": curve[:9],
        "gex_by_strike": [{"strike": k, "gex_musd": round(v / 1e6, 2)}
                          for k, v in gex_strikes.items()],
        "n_options": len(book_dte), "n_options_total": len(book),
        "display_band": display_band,
        "dte_days": eff_dte, "dte_default": default_dte,
    }
    # --- Signe dealer : on calcule TOUJOURS les DEUX conventions, peu importe le mode actif ---
    # Le mode (DEALER_SIGN_MODE) décide seulement lequel alimente le score et l'affichage.
    # Mais les DEUX chiffres sont calculés et stockés chaque jour -> aucun trou si tu bascules.
    metrics["dealer_sign"] = sign_info          # {mode, rr, flipped, reason} du mode ACTIF
    fc, fp, _ = dealer_signs(rr_for_sign, mode="fixed")
    ac, ap, ainfo = dealer_signs(rr_for_sign, mode="adaptive")
    gex_fixed_tot,    _ = gamma_exposure(book_dte, S, csize, signs=(fc, fp))
    gex_adapt_tot,    _ = gamma_exposure(book_dte, S, csize, signs=(ac, ap))
    metrics["gex_fixed_musd"]    = round(gex_fixed_tot / 1e6, 1)   # GEX convention figée
    metrics["gex_adaptive_musd"] = round(gex_adapt_tot / 1e6, 1)   # GEX convention skew
    # DEX dealer dans les DEUX conventions aussi (même logique : on collecte tout, tout le temps)
    metrics["dex_dealer_fixed_musd"]    = round(delta_exposure_dealer(book_pos, S, csize, signs=(fc, fp)) / 1e6, 1)
    metrics["dex_dealer_adaptive_musd"] = round(delta_exposure_dealer(book_pos, S, csize, signs=(ac, ap)) / 1e6, 1)
    metrics["dealer_sign"]["adaptive_would"] = ainfo["reason"]
    metrics["dealer_sign"]["adaptive_diverges"] = ((ac, ap) != signs)

    metrics["expiry_calendar"] = expiry_calendar(book, S, csize)  # avant converge : L1 s'en sert
    metrics["rr_baseline"] = rr_baseline(asset, crypto)   # normal du skew pour CET actif
    metrics["convergence"] = converge(metrics, catalyst_bias)
    # Historique PAR HORIZON. Le label vient de daily.py (snappé sur la vraie échéance) ;
    # sinon on le déduit du DTE pour savoir quelle série montrer (vue interactive).
    hz = horizon or _horizon_for(eff_dte)
    metrics["horizon"] = hz
    metrics["horizon_dtes"] = _hd        # vraies échéances dominantes (jours)
    if store_history:
        _lv = metrics["convergence"].get("levels", {})
        _lvlist = [(_lv.get(k) or {}).get("verdict") for k in
                   ["L1_REGIME", "L2_POSITIONING", "L3_STRUCTURE", "L4_LIQUIDITE", "L5_CATALYST"]]
        dex_gex_history(asset, metrics["dex_total_musd"], metrics["gex_total_musd"],
                        metrics["convergence"]["score"], spot=S,
                        max_pain=metrics.get("max_pain"), rr=metrics.get("rr_weekly"),
                        store=True, horizon=hz, levels=_lvlist,
                        gex_fixed=metrics.get("gex_fixed_musd"),
                        gex_adaptive=metrics.get("gex_adaptive_musd"),
                        flip=metrics.get("gamma_flip"),
                        call_wall=(metrics.get("gamma_walls") or {}).get("call_wall"),
                        put_wall=(metrics.get("gamma_walls") or {}).get("put_wall"),
                        dex_dealer_fixed=metrics.get("dex_dealer_fixed_musd"),
                        dex_dealer_adaptive=metrics.get("dex_dealer_adaptive_musd"))
    # Renvoie les 3 séries (pour basculer dans le dashboard) + celle du profil actif par défaut.
    metrics["histories"] = all_histories(asset)
    metrics["history"] = metrics["histories"].get(hz, [])

    # --- Forecast dealer (charm/vanna) + matrice de scénarios : calculés sur le book ---
    charm_d, vanna_1 = charm_vanna_flow(book_dte, S, csize, signs=signs, r=(0.0 if crypto else MACRO_RISK_FREE))
    metrics["charm_musd"] = round(charm_d / 1e6, 1)
    metrics["vanna_musd"] = round(vanna_1 / 1e6, 1)
    metrics["scenario_matrix"] = scenario_matrix(book_dte, S, csize, signs=signs)
    if store_history:
        scenario_history(asset, hz, metrics["scenario_matrix"], store=True)
    metrics["scenario_history"] = scenario_history(asset, hz, store=False)
    # Version dense (pas de 0.5 %, -15 % a +15 %) pour l'habillage en zones sur /chart :
    # meme mecanique de hedge que scenario_matrix, juste beaucoup plus de points pour un
    # degrade continu (zones d'achat/vente) au lieu d'un tableau a 7 lignes.
    metrics["hedge_flow_curve"] = scenario_matrix(
        book_dte, S, csize, signs=signs,
        moves=tuple(round(i * 0.005, 4) for i in range(-30, 31)))
    metrics["iv30"] = atm30

    # --- Indicateur de confiance (qualité des données) ---
    metrics["data_quality"] = data_quality(book_dte, cfg["source"])

    # --- VRP : vol réalisée calculée sur l'historique des spots déjà stockés ---
    #     S'active automatiquement dès qu'il y a assez de jours (sinon affiche la progression).
    spots_hist = [h.get("spot") for h in metrics["history"]]
    rv = realized_vol(spots_hist, window=10, ann=(365 if crypto else 252))
    metrics["rv_status"] = rv                       # {ready, have, need, rv}
    metrics["rv30"] = rv["rv"] if rv["ready"] else None
    if rv["ready"] and atm30 is not None:
        metrics["vrp"] = round(atm30 - rv["rv"], 1)  # IV - RV : >0 options chères, <0 bradées
    else:
        metrics["vrp"] = None

    # --- Champs nécessitant une source externe non branchée : explicitement None ---
    # Le dashboard affiche "DONNÉE MANQUANTE" pour chacun.
    metrics["funding"] = (deribit_funding(asset) if cfg["source"] == "deribit" else None)
    metrics["funding_multi"] = (funding_multi(asset, metrics["funding"]) if cfg["source"] == "deribit" else None)
    metrics["coinbase_premium"] = (coinbase_premium(asset, S) if cfg["source"] == "deribit" else None)
    metrics["stablecoins"] = (stablecoins() if cfg["source"] == "deribit" else None)
    metrics["block_trades"] = (block_trades(asset, S) if cfg["source"] == "deribit" else None)
    record_oi_snapshot(asset, book)                 # photo OI du jour (collecte)
    metrics["oi_change_24h"] = oi_change_24h(asset, book)
    record_tape_snapshot(asset, S)                  # photo tape du jour (collecte, BTC/ETH)
    metrics["tape_signs"] = empirical_signs(asset)  # état du signe empirique (affichage)
    metrics["macro_context"] = macro_context()      # contexte global (mêmes données pour tous)
    metrics["liq_map"] = (hyperliquid_liq_map(asset) if cfg["source"] == "deribit" else None)
    metrics["data_health"] = data_health()
    metrics["flow_summary"] = (flow_summary(asset, S) if cfg["source"] == "deribit" else None)
    metrics["changes"] = detect_changes(asset, metrics)   # vs la veille (avant réécriture de l'état)
    save_state(asset, metrics)
    metrics["option_volume"] = option_volume(asset, book)
    metrics["underlying_volume"] = underlying_volume(asset, cfg)
    metrics["priorities"] = priorities(metrics, asset)   # quoi regarder aujourd'hui
    # Book TOTAL (toutes échéances) : la métrique comparable aux sites publics
    gex_book_tot, _ = gamma_exposure(book, S, csize, signs=signs)
    dex_book_tot = delta_exposure(book, S, csize)
    bt_dex = round(dex_book_tot / 1e6, 1)
    bt_gex = round(gex_book_tot / 1e6, 1)
    record_total_history(asset, bt_dex, bt_gex, round(S, 2))
    metrics["book_total"] = {"dex_musd": bt_dex, "gex_musd": bt_gex,
                             "n_options": len(book),
                             "history": read_total_history(asset)}
    metrics["histories"]["total"] = metrics["book_total"]["history"]
    # VEX + theta dealers (même tranche DTE que charm/vanna pour cohérence de la carte)
    vex_1pt, theta_daily = vega_theta_exposure(book_dte, S, csize, signs=signs, r=(0.0 if crypto else MACRO_RISK_FREE))
    metrics["vex_musd"] = round(vex_1pt / 1e6, 1)
    metrics["theta_musd"] = round(theta_daily / 1e6, 1)
    # Calendrier des échéances : book COMPLET (le but est de voir tout ce qui expire)
    # IV Rank : réutilise l'historique d'iv_percentile (aucune collecte séparée)
    metrics["iv_rank"] = iv_rank(asset, metrics.get("iv30"))
    # Basis futures datés (contango/backwardation) — crypto inverse seulement
    metrics["futures_basis"] = (futures_basis(asset) if cfg["source"] == "deribit" else None)
    metrics["oi_by_exchange"] = None      # OI Binance/Bybit/OKX -> autre source
    metrics["is_crypto"] = crypto         # pilote l'affichage des cartes crypto-only côté UI
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
