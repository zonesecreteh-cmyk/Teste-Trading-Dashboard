#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_feed.py — Écoute permanente en tache de fond (liquidations réelles + CVD).

Contrairement au reste du projet (requêtes REST à la demande, cache court), ce module
tourne EN CONTINU dans des threads démons tant que flow_dashboard.py est ouvert :
  - liquidations réelles OKX (liquidation-orders) + Bybit (allLiquidation.*)
    -> journal append-only iv_history/{asset}_liq_events.jsonl
       (sert de corroboration aux poches "percées" de flow_engine.liq_pocket_status)
  - trade tape Bybit (publicTrade.*) -> CVD (Cumulative Volume Delta)
    -> checkpoints iv_history/{asset}_cvd.json toutes les 5 min

Binance est volontairement absent : ses flux WebSocket se sont révélés injoignables
depuis cet environnement lors des tests (connexion ouverte mais aucun message reçu
en 20-30s, sur deux endpoints différents) — best-effort abandonné plutôt que de
prétendre couvrir un exchange qui ne répond pas.

Pré-requis : pip install websocket-client
Utilisation : start() une seule fois au démarrage de flow_dashboard.py.
"""
import os, json, time, threading, datetime as dt

try:
    import websocket   # websocket-client
except ImportError:
    websocket = None

HIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iv_history")
ASSETS = ["BTC", "ETH", "SOL", "XRP", "AVAX", "TRX", "HYPE"]

_write_lock = threading.Lock()
_cvd_lock = threading.Lock()
_cvd_state = {a: 0.0 for a in ASSETS}          # cumul $ en memoire depuis le demarrage
_started = False

STATUS = {
    "okx": {"connected": False, "last_msg": None, "events": 0},
    "bybit": {"connected": False, "last_msg": None, "events": 0, "trades": 0},
}


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _liq_events_path(asset):
    return os.path.join(HIST_DIR, f"{asset}_liq_events.jsonl")


def _cvd_path(asset):
    return os.path.join(HIST_DIR, f"{asset}_cvd.json")


def _record_liq_event(asset, exchange, side, price, qty, usd):
    if asset not in ASSETS:
        return
    line = json.dumps({"ts": _now_iso(), "exchange": exchange, "side": side,
                        "price": price, "qty": qty, "usd": usd})
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        with _write_lock:
            with open(_liq_events_path(asset), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _trim_liq_events(asset, keep_days=14):
    path = _liq_events_path(asset)
    if not os.path.exists(path):
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    try:
        kept = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if dt.datetime.fromisoformat(row["ts"]) >= cutoff:
                        kept.append(line.rstrip("\n"))
                except Exception:
                    continue
        with _write_lock:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
            os.replace(tmp, path)
    except Exception:
        pass


def _checkpoint_cvd():
    now_iso = _now_iso()
    for asset in ASSETS:
        with _cvd_lock:
            cumul = _cvd_state.get(asset, 0.0)
        path = _cvd_path(asset)
        try:
            serie = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    serie = (json.load(f) or {}).get("serie") or []
            serie.append({"ts": now_iso, "cvd_musd": round(cumul / 1e6, 3)})
            serie = serie[-300:]
            out = {"cumulative_musd": round(cumul / 1e6, 3), "serie": serie, "updated": now_iso}
            os.makedirs(HIST_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(tmp, path)
        except Exception:
            pass


def _checkpoint_loop(stop_event):
    n = 0
    while not stop_event.is_set():
        stop_event.wait(300)
        if stop_event.is_set():
            break
        _checkpoint_cvd()
        n += 1
        if n % 24 == 0:                          # ~toutes les 2h (24 x 5 min)
            for a in ASSETS:
                _trim_liq_events(a)


def _okx_loop(stop_event):
    if websocket is None:
        return
    url = "wss://ws.okx.com:8443/ws/v5/public"
    args = [{"channel": "liquidation-orders", "instType": "SWAP", "instFamily": f"{a}-USDT"}
            for a in ASSETS]
    backoff = 5
    while not stop_event.is_set():
        try:
            def on_open(ws):
                STATUS["okx"]["connected"] = True
                ws.send(json.dumps({"op": "subscribe", "args": args}))

            def on_message(ws, message):
                nonlocal backoff
                backoff = 5
                STATUS["okx"]["last_msg"] = _now_iso()
                try:
                    msg = json.loads(message)
                except Exception:
                    return
                for item in msg.get("data") or []:
                    fam = item.get("instFamily", "")
                    asset = fam.split("-")[0]
                    if asset not in ASSETS:
                        continue
                    for d in item.get("details") or []:
                        try:
                            side = "long" if d.get("side") == "sell" and d.get("posSide") == "long" else \
                                   "short" if d.get("posSide") == "short" else d.get("side")
                            price = float(d.get("bkPx") or 0)
                            qty = float(d.get("sz") or 0)
                            if price <= 0 or qty <= 0:
                                continue
                            _record_liq_event(asset, "OKX", side, price, qty, None)
                            STATUS["okx"]["events"] += 1
                        except Exception:
                            continue

            def on_close(ws, *a):
                STATUS["okx"]["connected"] = False

            def on_error(ws, err):
                STATUS["okx"]["connected"] = False

            app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                         on_close=on_close, on_error=on_error)
            app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            STATUS["okx"]["connected"] = False
        if stop_event.is_set():
            break
        stop_event.wait(backoff)
        backoff = min(backoff * 2, 60)


def _bybit_loop(stop_event):
    if websocket is None:
        return
    url = "wss://stream.bybit.com/v5/public/linear"
    liq_topics = [f"allLiquidation.{a}USDT" for a in ASSETS]
    trade_topics = [f"publicTrade.{a}USDT" for a in ASSETS]
    backoff = 5
    while not stop_event.is_set():
        try:
            def on_open(ws):
                STATUS["bybit"]["connected"] = True
                ws.send(json.dumps({"op": "subscribe", "args": liq_topics + trade_topics}))

            def on_message(ws, message):
                nonlocal backoff
                backoff = 5
                STATUS["bybit"]["last_msg"] = _now_iso()
                try:
                    msg = json.loads(message)
                except Exception:
                    return
                topic = msg.get("topic") or ""
                data = msg.get("data")
                if not data:
                    return
                if topic.startswith("allLiquidation."):
                    sym = topic.split(".", 1)[1]
                    asset = sym[:-4] if sym.endswith("USDT") else sym
                    if asset not in ASSETS:
                        return
                    rows = data if isinstance(data, list) else [data]
                    for d in rows:
                        try:
                            side = "long" if d.get("side") == "Sell" else "short"
                            price = float(d.get("price") or 0)
                            qty = float(d.get("size") or 0)
                            if price <= 0 or qty <= 0:
                                continue
                            _record_liq_event(asset, "Bybit", side, price, qty,
                                              round(price * qty, 2))
                            STATUS["bybit"]["events"] += 1
                        except Exception:
                            continue
                elif topic.startswith("publicTrade."):
                    sym = topic.split(".", 1)[1]
                    asset = sym[:-4] if sym.endswith("USDT") else sym
                    if asset not in ASSETS:
                        return
                    rows = data if isinstance(data, list) else [data]
                    for d in rows:
                        try:
                            price = float(d.get("p") or 0)
                            qty = float(d.get("v") or 0)
                            signed = price * qty * (1 if d.get("S") == "Buy" else -1)
                            with _cvd_lock:
                                _cvd_state[asset] = _cvd_state.get(asset, 0.0) + signed
                            STATUS["bybit"]["trades"] += 1
                        except Exception:
                            continue

            def on_close(ws, *a):
                STATUS["bybit"]["connected"] = False

            def on_error(ws, err):
                STATUS["bybit"]["connected"] = False

            app = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                         on_close=on_close, on_error=on_error)
            app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            STATUS["bybit"]["connected"] = False
        if stop_event.is_set():
            break
        stop_event.wait(backoff)
        backoff = min(backoff * 2, 60)


_stop_event = threading.Event()


def start():
    """Démarre les threads d'écoute (idempotent). Sans effet si websocket-client
    n'est pas installé (le reste du dashboard continue de fonctionner normalement,
    juste sans corroboration réelle ni CVD)."""
    global _started
    if _started:
        return
    _started = True
    if websocket is None:
        print("  [live_feed] websocket-client absent -> CVD et corroboration liquidations desactives")
        print("              (pip install websocket-client pour les activer)")
        return
    threading.Thread(target=_okx_loop, args=(_stop_event,), daemon=True).start()
    threading.Thread(target=_bybit_loop, args=(_stop_event,), daemon=True).start()
    threading.Thread(target=_checkpoint_loop, args=(_stop_event,), daemon=True).start()
    print("  [live_feed] ecoute demarree : OKX + Bybit (liquidations reelles + CVD)")


def status():
    return {"okx": dict(STATUS["okx"]), "bybit": dict(STATUS["bybit"]),
            "available": websocket is not None}
