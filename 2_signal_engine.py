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


# --- نیا حصہ: ATR (Average True Range) ---
# ATR بتاتا ہے کہ کوائن عام طور پر ایک کینڈل میں کتنا حرکت کرتا ہے۔
# اسے ہائی، لو اور پچھلے کلوز کی بنیاد پر نکالا جاتا ہے (Wilder's smoothing)۔
def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_prev_close = abs(highs[i] - closes[i - 1])
        low_prev_close = abs(lows[i] - closes[i - 1])
        true_ranges.append(max(high_low, high_prev_close, low_prev_close))

    # پہلا ATR = پہلی 'period' true ranges کا سادہ اوسط
    atr = sum(true_ranges[:period]) / period
    # اس کے بعد Wilder's smoothing سے آگے بڑھایا جاتا ہے
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


PULLBACK_TOLERANCE = 0.005  # EMA20 کے 0.5% اندر آنا "ٹچ" شمار ہوگا
PULLBACK_LOOKBACK = 4       # پچھلی کتنی کینڈلز میں pullback تلاش کریں
TREND_LOOKBACK = 10         # ٹرینڈ سمت جانچنے کے لیے EMA50 کتنی کینڈلز پیچھے دیکھیں

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.5   # سٹاپ لاس = entry ± (ATR × یہ عدد)
ATR_TARGET_MULTIPLIERS = (1.5, 3.0, 4.5, 6.0, 7.5)  # ٹارگٹس بھی ATR پر مبنی


def check_signal(symbol, highs, lows, closes):
    """
    Pullback Entry لاجک (ٹرینڈ کی سمت میں):
    - ٹرینڈ اپ (EMA50 اوپر جا رہا ہو) اور قیمت حال ہی میں EMA20 کے قریب آ کر
      واپس اوپر بند ہوئی + سبز کینڈل -> BUY
    - ٹرینڈ ڈاؤن (EMA50 نیچے جا رہا ہو) اور قیمت حال ہی میں EMA20 کے قریب آ کر
      واپس نیچے بند ہوئی + لال کینڈل -> SELL
    RSI صرف ریکارڈ/مانیٹرنگ کے لیے ساتھ محفوظ کیا جاتا ہے، فیصلے میں استعمال نہیں ہوتا۔
    """
    if len(closes) < 55:
        return None, None, None

    rsi_values = calc_rsi(closes, period=14)
    current_rsi = rsi_values[-1]

    atr = calc_atr(highs, lows, closes, period=ATR_PERIOD)

    ema20_values = ema(closes, 20)
    ema50_values = ema(closes, 50)

    last_close = closes[-1]
    prev_close = closes[-2]
    last_ema20 = ema20_values[-1]
    last_ema50 = ema50_values[-1]

    trend_up = last_ema50 > ema50_values[-TREND_LOOKBACK]
    trend_down = last_ema50 < ema50_values[-TREND_LOOKBACK]

    touched_ema20 = any(
        abs(closes[i] - ema20_values[i]) / ema20_values[i] <= PULLBACK_TOLERANCE
        for i in range(len(closes) - PULLBACK_LOOKBACK, len(closes) - 1)
    )

    if trend_up and touched_ema20 and last_close > last_ema20 and last_close > prev_close:
        return "BUY", current_rsi, atr

    if trend_down and touched_ema20 and last_close < last_ema20 and last_close < prev_close:
        return "SELL", current_rsi, atr

    return None, current_rsi, atr


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


# --- تبدیل شدہ حصہ: اب سٹاپ لاس اور ٹارگٹس فکسڈ % کی بجائے ATR پر مبنی ہیں ---
# جتنا کوائن زیادہ اچھلتا کودتا (volatile) ہوگا، اتنا ہی سٹاپ لاس خودکار دور ہوگا،
# اور جتنا پرسکون ہوگا اتنا ہی سٹاپ قریب رہے گا۔
def calc_levels(signal_type, entry_price, atr):
    stop_distance = atr * ATR_STOP_MULTIPLIER

    if signal_type == "BUY":
        stop_loss = entry_price - stop_distance
        targets = [entry_price + (atr * m) for m in ATR_TARGET_MULTIPLIERS]
    else:
        stop_loss = entry_price + stop_distance
        targets = [entry_price - (atr * m) for m in ATR_TARGET_MULTIPLIERS]

    return stop_loss, targets


def save_signal(symbol, signal_type, entry_price, rsi, atr):
    stop_loss, targets = calc_levels(signal_type, entry_price, atr)
    positions_url = f"{SUPABASE_URL}/rest/v1/positions"
    payload = {
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "rsi": rsi,
        "atr": atr,
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

    print(f"Signal saved: {symbol} -> {signal_type} @ {entry_price} (ATR={atr}, SL={stop_loss})")


def main():
    print("Starting signal engine...")

    symbols = get_top_usdt_symbols(limit=200)
    print(f"Scanning {len(symbols)} symbols...")

    signals_found = 0

    for symbol in symbols:
        try:
            klines = get_klines(symbol, interval="1h", limit=100)
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]
            volumes = [float(k[5]) for k in klines]

            signal_type, rsi, atr = check_signal(symbol, highs, lows, closes)

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
                if not atr or atr <= 0:
                    print(f"Skip {symbol}: invalid ATR.")
                    continue
                entry_price = closes[-1]
                save_signal(symbol, signal_type, entry_price, rsi, atr)
                signals_found += 1

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    print(f"Done. Signals found this run: {signals_found}")


if __name__ == "__main__":
    main()
