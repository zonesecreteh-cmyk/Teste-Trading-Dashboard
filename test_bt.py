import flow_engine as fe, time, traceback
try:
    r = fe._deribit_get("public/get_last_trades_by_currency", currency="BTC", kind="option", count=1000)
    trades = r.get("trades", [])
    print("trades recus:", len(trades))
    cutoff = int(time.time()*1000) - 24*3600*1000
    n24 = sum(1 for t in trades if (t.get("timestamp") or 0) >= cutoff)
    print("dont <24h:", n24)
    blocks = []
    for t in trades:
        if (t.get("timestamp") or 0) < cutoff: continue
        p = t.get("instrument_name","").split("-")
        if len(p) != 4: continue
        notional = (t.get("amount") or 0) * (t.get("index_price") or 59426)
        if notional >= 1_000_000: blocks.append((p[3], p[2], round(notional)))
    print("blocks >=1M sur 24h:", len(blocks))
    print(blocks[:5])
    print("block_trades() renvoie:", repr(fe.block_trades("BTC", 59426))[:200])
except Exception:
    traceback.print_exc()
