"""
CryptoTerminal Pro - Signal Engine
==================================
یہ script:
1. Binance سے ٹاپ 100 USDT کوائنز کا ڈیٹا لیتی ہے
2. ہر کوائن کے لیے RSI اور MACD حساب لگاتی ہے
3. سخت معیار کے مطابق سگنل چیک کرتی ہے (ہر گھنٹے میں 1 سگنل کی حد)
4. نتیجہ Supabase کی positions اور last_signal ٹیبلز میں محفوظ کرتی ہے

یہ GitHub Actions کے ذریعے ہر 5 منٹ بعد خودکار چلتی ہے۔
"""

import os
import sys
import requests
from datetime import datetime, timezone
from supabase import create_client

# ---------------------------------------------------------
# Supabase کنکشن (URL اور Key GitHub Secrets سے آتی ہیں)
# ---------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL یا SUPABASE_KEY نہیں ملی۔ GitHub Secrets چیک کریں۔")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

BINANCE_BASE = "https://api.binance.com"

# سخت معیار کی حدیں (چاہیں تو یہاں تبدیل کریں)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
SIGNAL_COOLDOWN_SECONDS = 3600  # 1 گھنٹہ

# لیوریجڈ ٹوکنز جنہیں نظرانداز کرنا ہے (UP/DOWN/BULL/BEAR)
EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def get_top_usdt_symbols(limit=100):
    """Binance سے سب سے زیادہ ٹریڈ ہونے والے 100 USDT جوڑے حاصل کریں"""
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
    """کسی کوائن کی پرانی قیمتوں کی تاریخ (candles) حاصل کریں"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def calc_rsi(closes, period=14):
    """RSI (Relative Strength Index) حساب کریں"""
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
    """Exponential Moving Average"""
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for price in values[1:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD اور MACD Histogram حساب کریں"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return histogram


def check_signal(closes):
    """سخت معیار کے مطابق چیک کریں کہ BUY/SELL سگنل بنتا ہے یا نہیں"""
    rsi_values = calc_rsi(closes)
    macd_hist = calc_macd(closes)

    if rsi_values[-1] is None or len(macd_hist) < 2:
        return None, None, None

    last_rsi = rsi_values[-1]
    last_hist = macd_hist[-1]
    prev_hist = macd_hist[-2]

    # سخت شرط: RSI انتہائی حد پر ہو + MACD ابھی الٹا ہوا ہو (crossover)
    if last_rsi < RSI_OVERSOLD and prev_hist <= 0 < last_hist:
        return "BUY", last_rsi, last_hist
    if last_rsi > RSI_OVERBOUGHT and prev_hist >= 0 > last_hist:
        return "SELL", last_rsi, last_hist

    return None, last_rsi, last_hist


def get_seconds_since_last_signal():
    """آخری سگنل کب بنا تھا، اس کی بنیاد پر cooldown چیک کریں"""
    res = (
        supabase.table("last_signal")
        .select("created_at")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    last_time = datetime.fromisoformat(res.data[0]["created_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - last_time).total_seconds()


def save_signal(symbol, signal_type, entry_price, rsi, macd_hist):
    """نیا سگنل positions اور last_signal ٹیبلز میں محفوظ کریں"""
    supabase.table("positions").insert({
        "symbol": symbol,
        "signal_type": signal_type,
        "entry_price": entry_price,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "status": "open",
    }).execute()

    supabase.table("last_signal").insert({
        "symbol": symbol,
        "signal_type": signal_type,
    }).execute()

    print(f"✅ نیا سگنل محفوظ ہوا: {symbol} -> {signal_type} @ {entry_price}")


def main():
    print("سگنل انجن شروع ہو رہا ہے...")

    seconds_since_last = get_seconds_since_last_signal()
    if seconds_since_last is not None and seconds_since_last < SIGNAL_COOLDOWN_SECONDS:
        remaining = int(SIGNAL_COOLDOWN_SECONDS - seconds_since_last)
        print(f"⏳ Cooldown active — آخری سگنل کو ابھی {remaining} سیکنڈ نہیں گزرے۔ اس بار سگنل نہیں بنایا جائے گا۔")
        return

    symbols = get_top_usdt_symbols(100)
    print(f"📊 {len(symbols)} کوائنز چیک کیے جا رہے ہیں...")

    for symbol in symbols:
        try:
            klines = get_klines(symbol)
            closes = [float(k[4]) for k in klines]
            if len(closes) < 30:
                continue

            signal_type, rsi, macd_hist = check_signal(closes)
            if signal_type:
                current_price = closes[-1]
                save_signal(symbol, signal_type, current_price, rsi, macd_hist)
                break  # ایک رن میں صرف ایک سگنل (rate limit یقینی بنانے کے لیے)

        except Exception as e:
            print(f"⚠️ {symbol} میں خرابی: {e}")
            continue

    print("مکمل ہوا۔")


if __name__ == "__main__":
    main()
