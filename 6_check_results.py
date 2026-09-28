import os
import re
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

# پوزیشن کھلنے کے بعد کی 5 منٹ کی کینڈلز کے High/Low دیکھے جائیں گے،
# تاکہ دو چیکس کے درمیان ہونے والی حرکت (wick) بھی نہ چھوٹے۔
CANDLE_INTERVAL = "5m"
CANDLE_MS = 5 * 60 * 1000


def parse_time(ts):
    """Supabase کا وقت پڑھتا ہے (اعشاریہ کے ہندسے کم زیادہ ہوں تب بھی چلتا ہے)"""
    ts = ts.replace("Z", "+00:00")
    m = re.match(r"(.+?)\.(\d+)(.*)", ts)
    if m:
        ts = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_open_positions():
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"status": "eq.open", "select": "*"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_candles_since(symbol, start_ms):
    candles = []
    while True:
        params = {
            "symbol": symbol,
            "interval": CANDLE_INTERVAL,
            "startTime": start_ms,
            "limit": 1000,
        }
        resp = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        candles.extend(batch)
        if len(batch) < 1000:
            break
        start_ms = batch[-1][0] + CANDLE_MS
    return candles


def evaluate_position(signal_type, stop_loss, target, candles):
    """
    کینڈلز کو وقت کی ترتیب سے دیکھتا ہے:
    - پہلے سٹاپ لاس لگا -> LOSS
    - پہلے ٹارگٹ لگا -> WIN
    - اگر ایک ہی کینڈل میں دونوں لگیں تو محتاط رہتے ہوئے LOSS شمار ہوگا
    """
    for c in candles:
        high = float(c[2])
        low = float(c[3])
        if signal_type == "BUY":
            hit_stop = low <= stop_loss
            hit_target = high >= target
        else:
            hit_stop = high >= stop_loss
            hit_target = low <= target

        if hit_stop:
            return "LOSS", stop_loss
        if hit_target:
            return "WIN", target
    return None, None


def result_exists(position_id):
    url = f"{SUPABASE_URL}/rest/v1/results"
    params = {"position_id": f"eq.{position_id}", "select": "id", "limit": "1"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return len(resp.json()) > 0


def save_result(position_id, symbol, signal_type, entry_price, exit_price, outcome):
    if signal_type == "BUY":
        pnl = (exit_price - entry_price) / entry_price * 100
    else:
        pnl = (entry_price - exit_price) / entry_price * 100

    url = f"{SUPABASE_URL}/rest/v1/results"
    payload = {
        "position_id": position_id,
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "outcome": outcome,
        "pnl_percent": round(pnl, 2),
        "profit_pct": round(pnl, 2),
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(url, headers=SB_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    print(f"Result saved: {symbol} {outcome} {pnl:.2f}%")


def close_position(position_id, outcome):
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"id": f"eq.{position_id}"}
    payload = {
        "status": "closed" if outcome == "WIN" else "stopped",
        "targets_hit": 1 if outcome == "WIN" else 0,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.patch(url, headers=SB_HEADERS, params=params, json=payload, timeout=20)
    r.raise_for_status()


def main():
    print("Checking open positions...")
    positions = get_open_positions()
    print(f"Open positions: {len(positions)}")

    for pos in positions:
        symbol = pos["symbol"]
        try:
            signal_type = pos["signal_type"]
            entry_price = float(pos["entry_price"])
            stop_loss = pos.get("stop_loss")
            target = pos.get("target_1")
            created_at = pos.get("created_at")

            if stop_loss is None or target is None or not created_at:
                print(f"Skip {symbol}: incomplete position data.")
                continue

            stop_loss = float(stop_loss)
            target = float(target)
            start_ms = int(parse_time(created_at).timestamp() * 1000)

            candles = get_candles_since(symbol, start_ms)
            outcome, exit_price = evaluate_position(signal_type, stop_loss, target, candles)

            if outcome is None:
                print(f"{symbol}: still open.")
                continue

            if not result_exists(pos["id"]):
                save_result(pos["id"], symbol, signal_type, entry_price, exit_price, outcome)
            close_position(pos["id"], outcome)

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    print("Done checking results.")


if __name__ == "__main__":
    main()
