import os
import sys
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL or SUPABASE_KEY missing.")
    sys.exit(1)

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

BINANCE_BASE = "https://data-api.binance.vision"


def get_open_positions():
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"status": "eq.open", "select": "*"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_current_price(symbol):
    resp = requests.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol}, timeout=20)
    resp.raise_for_status()
    return float(resp.json()["price"])


def close_position(position_id, targets_hit, status):
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"id": f"eq.{position_id}"}
    payload = {
        "status": status,
        "targets_hit": targets_hit,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.patch(url, headers=SB_HEADERS, params=params, json=payload, timeout=20)
    r.raise_for_status()


def save_result(symbol, signal_type, entry_price, exit_price, outcome):
    if signal_type == "BUY":
        profit_pct = (exit_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - exit_price) / entry_price * 100

    url = f"{SUPABASE_URL}/rest/v1/results"
    payload = {
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "outcome": outcome,
        "profit_pct": round(profit_pct, 2),
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(url, headers=SB_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"Result saved: {symbol} {outcome} {profit_pct:.2f}%")


def main():
    print("Checking open positions...")
    positions = get_open_positions()
    print(f"Open positions: {len(positions)}")

    for pos in positions:
        symbol = pos["symbol"]
        signal_type = pos["signal_type"]
        entry_price = float(pos["entry_price"])
        stop_loss = pos.get("stop_loss")
        targets = [pos.get(f"target_{i}") for i in range(1, 6)]

        if stop_loss is None or any(t is None for t in targets):
            continue

        try:
            current_price = get_current_price(symbol)
        except Exception as e:
            print(f"Error fetching price for {symbol}: {e}")
            continue

        hit_stop = (
            current_price <= stop_loss if signal_type == "BUY" else current_price >= stop_loss
        )
        if hit_stop:
            close_position(pos["id"], pos.get("targets_hit", 0), "stopped")
            save_result(symbol, signal_type, entry_price, current_price, "LOSS")
            continue

        targets_hit = 0
        for t in targets:
            reached = current_price >= t if signal_type == "BUY" else current_price <= t
            if reached:
                targets_hit += 1

        if targets_hit >= 5:
            close_position(pos["id"], targets_hit, "closed")
            save_result(symbol, signal_type, entry_price, current_price, "WIN")
        elif targets_hit > pos.get("targets_hit", 0):
            url = f"{SUPABASE_URL}/rest/v1/positions"
            params = {"id": f"eq.{pos['id']}"}
            requests.patch(url, headers=SB_HEADERS, params=params, json={"targets_hit": targets_hit}, timeout=20)

    print("Done checking results.")


if __name__ == "__main__":
    main()
