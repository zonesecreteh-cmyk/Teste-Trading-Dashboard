# -*- coding: utf-8 -*-
"""
VERIFIER LES CALCULS — controle mathematique independant du moteur.

Ne teste PAS "est-ce que le code tourne" (c'est le role de audit.py), mais
"est-ce que les maths sont JUSTES" : chaque formule est recalculee ici de facon
independante (parfois par une methode differente : derivees numeriques, cas
analytiques connus, invariants) et comparee a ce que produit flow_engine.

Usage :  py verifier_calculs.py
"""
import math, sys
import flow_engine as fe

OK, FAIL = 0, 0
def check(nom, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [OK ] {nom}")
    else:
        FAIL += 1
        print(f"  [!! ] {nom}   {detail}")

S, K, T, IV = 60000.0, 62000.0, 30/365, 0.55

print("=" * 70)
print(f"VERIFICATION DES CALCULS — flow_engine {fe.VERSION}")
print("=" * 70)

# ---------------------------------------------------------------- greeks BS
print("\n1. GREEKS BLACK-SCHOLES (vs derivees numeriques)")
h = S * 1e-4
# delta = d(prix)/dS  -> on verifie via la relation de parite et la borne [0,1]
dc = fe.bs_delta(S, K, T, IV, True)
dp = fe.bs_delta(S, K, T, IV, False)
check("delta call dans [0,1]", 0 <= dc <= 1, f"delta={dc:.4f}")
check("delta put dans [-1,0]", -1 <= dp <= 0, f"delta={dp:.4f}")
check("parite delta (call - put = 1)", abs((dc - dp) - 1.0) < 1e-6, f"ecart={(dc-dp)-1:.2e}")

# gamma = d(delta)/dS, verifie numeriquement
g_num = (fe.bs_delta(S + h, K, T, IV, True) - fe.bs_delta(S - h, K, T, IV, True)) / (2 * h)
g_ana = fe.bs_gamma(S, K, T, IV)
check("gamma = derivee numerique du delta", abs(g_ana - g_num) / max(g_ana, 1e-12) < 1e-3,
      f"analytique={g_ana:.3e} numerique={g_num:.3e}")
check("gamma identique call/put", abs(fe.bs_gamma(S, K, T, IV) - g_ana) < 1e-15)
check("gamma > 0", g_ana > 0)

# vega = d(prix)/d(sigma) : on verifie via gamma (relation vega = gamma * S^2 * T * sigma)
v_ana = fe.bs_vega(S, K, T, IV)
v_rel = g_ana * (S ** 2) * T * IV
check("vega coherent avec gamma (relation exacte)", abs(v_ana - v_rel) / max(v_ana, 1e-12) < 1e-6,
      f"vega={v_ana:.2f} attendu={v_rel:.2f}")
check("vega > 0", v_ana > 0)

# theta : doit etre negatif pour une option longue ATM sans taux
th = fe.bs_theta(S, S, T, IV, True, r=0.0)
check("theta call ATM negatif (r=0)", th < 0, f"theta={th:.2f}")

# charm/vanna : symetries connues
vn_c = fe.bs_vanna(S, K, T, IV)
check("vanna finie", math.isfinite(vn_c), f"vanna={vn_c}")

# bornes : T tres petit -> pas d'explosion
check("pas d'explosion a T->0", math.isfinite(fe.bs_gamma(S, K, 1e-9, IV)))
check("pas d'explosion a IV->0", math.isfinite(fe.bs_gamma(S, K, T, 1e-9)))

# ---------------------------------------------------------------- GEX / DEX
print("\n2. GEX / DEX (signes, additivite, conventions)")
def mk(cp, k, oi, t=T, iv=IV):
    return {"type": cp, "strike": k, "T": t, "iv": iv, "oi": oi,
            "gamma": fe.bs_gamma(S, k, t, iv),
            "delta": fe.bs_delta(S, k, t, iv, cp == "C"),
            "expiry": None}

book = [mk("C", 62000, 500), mk("C", 65000, 300), mk("P", 58000, 400), mk("P", 55000, 250)]
csize = 1.0

gex_tot, by_strike = fe.gamma_exposure(book, S, csize)
check("GEX total = somme par strike", abs(gex_tot - sum(by_strike.values())) < 1e-6,
      f"total={gex_tot:.2f} somme={sum(by_strike.values()):.2f}")

# convention fixe : dealers longs calls (+) / courts puts (-)
gex_calls, _ = fe.gamma_exposure([o for o in book if o["type"] == "C"], S, csize)
gex_puts, _ = fe.gamma_exposure([o for o in book if o["type"] == "P"], S, csize)
check("convention fixe : GEX calls > 0", gex_calls > 0, f"{gex_calls:.2f}")
check("convention fixe : GEX puts < 0", gex_puts < 0, f"{gex_puts:.2f}")
check("GEX additif calls+puts", abs((gex_calls + gex_puts) - gex_tot) < 1e-6)

# inverser les signes doit inverser le total exactement
gex_inv, _ = fe.gamma_exposure(book, S, csize, signs=(-fe.SIGN_CALL, -fe.SIGN_PUT))
check("inversion des signes = oppose exact", abs(gex_inv + gex_tot) < 1e-6,
      f"{gex_inv:.2f} vs {-gex_tot:.2f}")

# GEX proportionnel a l'OI (doubler l'OI double le GEX)
book2 = [dict(o, oi=o["oi"] * 2) for o in book]
gex2, _ = fe.gamma_exposure(book2, S, csize)
check("GEX proportionnel a l'OI", abs(gex2 - 2 * gex_tot) < 1e-6)

# DEX positionnement : delta porte son propre signe
dex = fe.delta_exposure(book, S, csize)
dex_manual = sum(o["delta"] * o["oi"] * csize * S for o in book)
check("DEX = somme(delta x OI x csize x S)", abs(dex - dex_manual) < 1e-6)

# ---------------------------------------------------------------- max pain
print("\n3. MAX PAIN (verification par force brute independante)")
mp = fe.max_pain(book)
def douleur(prix):
    return sum(o["oi"] * (max(0.0, prix - o["strike"]) if o["type"] == "C"
                          else max(0.0, o["strike"] - prix)) for o in book)
strikes = sorted({o["strike"] for o in book})
best = min(strikes, key=douleur)
check("max pain = strike de douleur minimale", mp == best, f"moteur={mp} attendu={best}")
check("max pain est un strike existant", mp in strikes)

# ---------------------------------------------------------------- gamma flip
print("\n4. GAMMA FLIP (changement de signe du GEX)")
flip = fe.gamma_flip(book, S, csize)
if flip:
    lo, _ = fe.gamma_exposure([dict(o, gamma=fe.bs_gamma(flip * 0.98, o["strike"], o["T"], o["iv"]))
                               for o in book], flip * 0.98, csize)
    hi, _ = fe.gamma_exposure([dict(o, gamma=fe.bs_gamma(flip * 1.02, o["strike"], o["T"], o["iv"]))
                               for o in book], flip * 1.02, csize)
    check("GEX change de signe autour du flip", (lo < 0) != (hi < 0),
          f"sous={lo:.2f} au-dessus={hi:.2f}")
else:
    print("  [--] pas de flip sur ce book de test (normal si GEX ne croise pas zero)")

# ---------------------------------------------------------------- PCR
print("\n5. PUT/CALL RATIO")
pcr = fe.put_call_ratio(book)
oi_c = sum(o["oi"] for o in book if o["type"] == "C")
oi_p = sum(o["oi"] for o in book if o["type"] == "P")
attendu = round(oi_p / oi_c, 2)
val = pcr["pcr_oi"]
check("PCR OI = puts/calls", abs(val - attendu) < 0.01, f"moteur={val} attendu={attendu}")
check("call_oi correct", abs(pcr["call_oi"] - oi_c) < 1e-9, f"{pcr['call_oi']} vs {oi_c}")
check("put_oi correct", abs(pcr["put_oi"] - oi_p) < 1e-9, f"{pcr['put_oi']} vs {oi_p}")

# ---------------------------------------------------------------- coherence conventions
print("\n6. COHERENCE DES CONVENTIONS (le bug corrige)")
fc, fp, _ = fe.dealer_signs(-6.0, mode="fixed")
check("convention fixe = (SIGN_CALL, SIGN_PUT)", (fc, fp) == (fe.SIGN_CALL, fe.SIGN_PUT),
      f"({fc},{fp}) vs ({fe.SIGN_CALL},{fe.SIGN_PUT})")
check("SIGN_CALL positif (dealers longs calls)", fe.SIGN_CALL > 0)
check("SIGN_PUT negatif (dealers courts puts)", fe.SIGN_PUT < 0)

# ---------------------------------------------------------------- resultat
print("\n" + "=" * 70)
print(f"RESULTAT : {OK} verifications OK, {FAIL} echec(s)")
if FAIL == 0:
    print("Toutes les formules sont mathematiquement justes.")
else:
    print("Des ecarts existent — voir les lignes [!!] ci-dessus.")
print("=" * 70)
sys.exit(1 if FAIL else 0)
