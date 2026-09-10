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

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def get_top_usdt_symbols(limit=100):
    resp = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    usdt_pairs = [
        d for d in data
        if d["symbol"].endswith("USDT") and not d["symbol"].endswith(EXCLUDE_SUFFIXES)
    ]
    usdt_pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [p["symbol"] for p in usdt_pairs[:limit]]


def get_klines(symbol, interval="1h", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    rsi_values = [None] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
    rsi_values.append(100 - (100 / (1 + rs)))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi_values.append(100 - (100 / (1 + rs)))
    return rsi_values


def ema(values, period):
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for price in values[1:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return histogram


def check_signal(closes):
    rsi_values = calc_rsi(closes)
    macd_hist = calc_macd(closes)
    if rsi_values[-1] is None or len(macd_hist) < 3:
        return None, None, None

    last_rsi = rsi_values[-1]
    last_hist = macd_hist[-1]
    prev_hist = macd_hist[-2]

    if last_rsi < RSI_OVERSOLD and prev_hist <= 0 < last_hist:
        return "BUY", last_rsi, last_hist

    if last_rsi > RSI_OVERBOUGHT and prev_hist >= 0 > last_hist:
        return "SELL", last_rsi, last_hist

    return None, last_rsi, last_hist


def has_open_position(symbol):
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"symbol": f"eq.{symbol}", "status": "eq.open", "select": "id", "limit": "1"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return len(resp.json()) > 0


def calc_levels(signal_type, entry_price):
    if signal_type == "BUY":
        stop_loss = entry_price * 0.985
        targets = [entry_price * (1 + p) for p in (0.005, 0.010, 0.015, 0.020, 0.025)]
    else:
        stop_loss = entry_price * 1.015
        targets = [entry_price * (1 - p) for p in (0.005, 0.010, 0.015, 0.020, 0.025)]
    return stop_loss, targets


def save_signal(symbol, signal_type, entry_price, rsi, macd_hist):
    stop_loss, targets = calc_levels(signal_type, entry_price)
    positions_url = f"{SUPABASE_URL}/rest/v1/positions"
    payload = {
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "status": "open",
        "stop_loss": stop_loss,
        "target_1": targets[0],
        "target_2": targets[1],
        "target_3": targets[2],
        "target_4": targets[3],
        "target_5": targets[4],
        "targets_hit": 0,
    }
    r = requests.post(positions_url, headers=SB_HEADERS, json=payload, timeout=20)
    r.raise_for_status()

    last_signal_url = f"{SUPABASE_URL}/rest/v1/last_signal"
    payload2 = {"symbol": symbol, "signal_type": signal_type}
    r2 = requests.post(last_signal_url, headers=SB_HEADERS, json=payload2, timeout=20)
    r2.raise_for_status()

    print(f"Signal saved: {symbol} -> {signal_type} @ {entry_price}")


def main():
    print("Starting signal engine...")

    symbols = get_top_usdt_symbols(limit=200)
    print(f"Scanning {len(symbols)} symbols...")

    signals_found = 0

    for symbol in symbols:
        try:
            klines = get_klines(symbol, interval="1h", limit=100)
            closes = [float(k[4]) for k in klines]

            signal_type, rsi, macd_hist = check_signal(closes)

            if signal_type is not None:
                if has_open_position(symbol):
                    print(f"Skip {symbol}: already has an open position.")
                    continue
                entry_price = closes[-1]
                save_signal(symbol, signal_type, entry_price, rsi, macd_hist)
                signals_found += 1

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    print(f"Done. Signals found this run: {signals_found}")


if __name__ == "__main__":
    main()
    
