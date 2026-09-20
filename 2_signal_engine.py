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
FUTURES_BASE = "https://fapi.binance.com"

EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def get_top_usdt_symbols(limit=100):
    resp = requests.get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=20)
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
    resp = requests.get(f"{FUTURES_BASE}/fapi/v1/klines", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_open_interest_hist(symbol, period="1h", limit=6):
    params = {"symbol": symbol, "period": period, "limit": limit}
    resp = requests.get(f"{FUTURES_BASE}/futures/data/openInterestHist", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_top_long_short_ratio(symbol, period="1h", limit=6):
    params = {"symbol": symbol, "period": period, "limit": limit}
    resp = requests.get(f"{FUTURES_BASE}/futures/data/topLongShortPositionRatio", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def has_volume_confirmation(volumes, lookback=20, multiplier=1.3):
    if len(volumes) < lookback + 1:
        return False
    last_volume = volumes[-1]
    avg_volume = sum(volumes[-(lookback + 1):-1]) / lookback
    return avg_volume > 0 and last_volume >= avg_volume * multiplier


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


RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30


def whales_still_active(symbol, direction):
    """
    direction: "up" (RSI overbought زون) یا "down" (RSI oversold زون)
    True: وہیلز ابھی اسی سمت میں سرگرم ہیں (OI اور متعلقہ Long/Short % دونوں بڑھ رہے ہیں) -> ٹرینڈ جاری رہنے کا امکان
    False: وہیلز کی سرگرمی کمزور پڑ رہی ہے -> ریورسل کا امکان
    None: Futures ڈیٹا دستیاب نہیں (شاید یہ کوائن Futures میں لسٹ نہیں)
    """
    try:
        oi_data = get_open_interest_hist(symbol)
        ls_data = get_top_long_short_ratio(symbol)
    except Exception:
        return None

    if len(oi_data) < 3 or len(ls_data) < 3:
        return None

    oi_values = [float(d["sumOpenInterest"]) for d in oi_data]
    long_pcts = [float(d["longAccount"]) for d in ls_data]

    # شور کم کرنے کے لیے شروع کی 2 اور آخر کی 2 ریڈنگز کی اوسط لیں
    oi_start = sum(oi_values[:2]) / 2
    oi_end = sum(oi_values[-2:]) / 2
    long_start = sum(long_pcts[:2]) / 2
    long_end = sum(long_pcts[-2:]) / 2

    oi_rising = oi_end > oi_start

    if direction == "up":
        long_pct_rising = long_end > long_start
        return oi_rising and long_pct_rising
    else:
        short_start = 1 - long_start
        short_end = 1 - long_end
        short_pcts_rising = short_end > short_start
        return oi_rising and short_pcts_rising


def check_signal(symbol, closes):
    """
    RSI(14) صرف ٹرگر زون کے طور پر:
    - RSI >= 70 (overbought) -> چیک کریں وہیلز ابھی اوپر سرگرم ہیں یا نہیں
        سرگرم -> BUY (ٹرینڈ جاری، ساتھ چلیں)
        غیر سرگرم -> SELL (ریورسل)
    - RSI <= 30 (oversold) -> چیک کریں وہیلز ابھی نیچے سرگرم ہیں یا نہیں
        سرگرم -> SELL (مزید نیچے جانے کا امکان)
        غیر سرگرم -> BUY (ریورسل)
    Futures ڈیٹا نہ ملے تو سگنل نہیں دیا جاتا (None)۔
    """
    rsi_values = calc_rsi(closes, period=14)
    current_rsi = rsi_values[-1]
    if current_rsi is None:
        return None, None

    if current_rsi >= RSI_OVERBOUGHT:
        active = whales_still_active(symbol, "up")
        if active is None:
            return None, current_rsi
        return ("BUY" if active else "SELL"), current_rsi

    if current_rsi <= RSI_OVERSOLD:
        active = whales_still_active(symbol, "down")
        if active is None:
            return None, current_rsi
        return ("SELL" if active else "BUY"), current_rsi

    return None, current_rsi


def has_open_position(symbol):
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"symbol": f"eq.{symbol}", "status": "eq.open", "select": "id", "limit": "1"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return len(resp.json()) > 0


COOLDOWN_HOURS = 12


def is_in_cooldown(symbol, cooldown_hours=COOLDOWN_HOURS):
    """
    اسی کوائن پر آخری سگنل (چاہے وہ بند ہو چکا ہو) کے بعد
    cooldown_hours گھنٹے مکمل نہیں ہوئے تو True لوٹائے گا۔
    """
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {
        "symbol": f"eq.{symbol}",
        "select": "created_at",
        "order": "created_at.desc",
        "limit": "1",
    }
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return False

    last_created = rows[0].get("created_at")
    if not last_created:
        return False

    last_time = datetime.fromisoformat(last_created.replace("Z", "+00:00"))
    elapsed_hours = (datetime.now(timezone.utc) - last_time).total_seconds() / 3600
    return elapsed_hours < cooldown_hours


def calc_levels(signal_type, entry_price):
    if signal_type == "BUY":
        stop_loss = entry_price * 0.95
        targets = [entry_price * (1 + p) for p in (0.05, 0.10, 0.15, 0.20, 0.25)]
    else:
        stop_loss = entry_price * 1.05
        targets = [entry_price * (1 - p) for p in (0.05, 0.10, 0.15, 0.20, 0.25)]
    return stop_loss, targets


def save_signal(symbol, signal_type, entry_price, rsi):
    stop_loss, targets = calc_levels(signal_type, entry_price)
    positions_url = f"{SUPABASE_URL}/rest/v1/positions"
    payload = {
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "rsi": rsi,
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
            volumes = [float(k[5]) for k in klines]

            signal_type, rsi = check_signal(symbol, closes)

            if signal_type is not None:
                if not has_volume_confirmation(volumes):
                    print(f"Skip {symbol}: no volume confirmation.")
                    continue
                if has_open_position(symbol):
                    print(f"Skip {symbol}: already has an open position.")
                    continue
                if is_in_cooldown(symbol):
                    print(f"Skip {symbol}: cooldown active.")
                    continue
                entry_price = closes[-1]
                save_signal(symbol, signal_type, entry_price, rsi)
                signals_found += 1

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    print(f"Done. Signals found this run: {signals_found}")


if __name__ == "__main__":
    main()
