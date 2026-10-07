import os
import sys
import requests

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

FAPI_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://data-api.binance.vision"

EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# اسٹیبل کوائنز/فیاٹ: ان میں 24h % تبدیلی بے معنی ہے
EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "USD1", "RLUSD", "U", "USDE", "BFUSD",
    "XUSD", "PYUSD", "DAI", "USDS", "BUSD", "USDD", "FRAX", "LUSD",
    "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "ARS", "UAH", "PLN", "RON", "CZK", "JPY",
}

# --- حکمتِ عملی کی ترتیبات ---
TOP_N = 5                  # 24h gainers لسٹ میں ٹاپ کتنے نمبر تک سگنل شمار ہوگا
MIN_24H_CHANGE_PCT = 15.0  # کم از کم اتنی 24h تبدیلی ہو تبھی حقیقی pump شمار ہوگا
MIN_QUOTE_VOLUME = 20_000_000  # کم از کم 24h USDT ٹرن اوور (چھوٹے/پتلے کوائنز باہر)
MIN_RSI = 60                # RSI(14) اتنے سے اوپر ہو تو momentum کی تصدیق

RSI_PERIOD = 14
ATR_PERIOD = 14
# سٹاپ/ٹارگٹ اب ATR کی بجائے سیدھا قیمت کے فیصد پر مبنی ہیں — یہ زیادہ
# حقیقت پسندانہ اور قابلِ حصول ہیں، خاص طور پر ان کوائنز پر جو پہلے ہی بڑا
# pump کر چکے ہوتے ہیں (جہاں مزید چھوٹا سا منافع بھی حقیقی ہوتا ہے)۔
STOP_PCT = 0.02                                      # سٹاپ لاس: entry سے 2% نیچے
TARGET_PCTS = (0.034, 0.058, 0.073, 0.081, 0.091)    # پانچ ٹارگٹس، تدریجاً دور


def get_24h_tickers():
    # Spot API استعمال ہو رہا ہے (futures fapi.binance.com کبھی کبھار
    # GitHub Actions کے سرورز کو geo-block کر دیتا ہے — 451 error)
    resp = requests.get(f"{SPOT_BASE}/api/v3/ticker/24hr", timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_top_gainers():
    """
    Binance Futures کے تمام USDT جوڑوں میں سے 24h % تبدیلی کے حساب سے
    سب سے اوپر کے TOP_N کوائنز لوٹاتا ہے (فلٹرز لگانے کے بعد)۔
    """
    tickers = get_24h_tickers()
    candidates = []
    for t in tickers:
        symbol = t["symbol"]
        if not symbol.endswith("USDT") or symbol.endswith(EXCLUDE_SUFFIXES):
            continue
        base = symbol[:-4]
        if base in EXCLUDE_BASES:
            continue
        try:
            change_pct = float(t["priceChangePercent"])
            quote_volume = float(t["quoteVolume"])
        except (KeyError, ValueError):
            continue
        if change_pct < MIN_24H_CHANGE_PCT:
            continue
        if quote_volume < MIN_QUOTE_VOLUME:
            continue
        candidates.append((symbol, change_pct))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:TOP_N]


def get_klines(symbol, interval="1h", limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(f"{SPOT_BASE}/api/v3/klines", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def calc_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
    rsi = 100 - (100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_atr(highs, lows, closes, period=ATR_PERIOD):
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def has_open_position(symbol):
    url = f"{SUPABASE_URL}/rest/v1/positions"
    params = {"symbol": f"eq.{symbol}", "status": "eq.open", "select": "id", "limit": "1"}
    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return len(resp.json()) > 0


def calc_levels(entry_price):
    stop_loss = entry_price * (1 - STOP_PCT)
    targets = [entry_price * (1 + p) for p in TARGET_PCTS]
    return stop_loss, targets


def save_signal(symbol, rank, change_pct, entry_price, rsi, atr):
    stop_loss, targets = calc_levels(entry_price)
    positions_url = f"{SUPABASE_URL}/rest/v1/positions"
    payload = {
        "symbol": symbol,
        "signal_type": "BUY",
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
    print(
        f"Signal saved: {symbol} (rank #{rank}, 24h {change_pct:.1f}%) "
        f"-> BUY @ {entry_price} (ATR={atr}, SL={stop_loss}, TP1={targets[0]})"
    )


def main():
    print("Starting Top Gainers momentum engine...")

    top_gainers = get_top_gainers()
    print(f"Top gainers after filters: {top_gainers}")

    signals_found = 0

    for rank, (symbol, change_pct) in enumerate(top_gainers, start=1):
        try:
            if has_open_position(symbol):
                print(f"Skip {symbol}: already has an open position.")
                continue

            klines = get_klines(symbol, interval="1h", limit=100)
            klines = klines[:-1]  # ادھوری کینڈل ہٹائیں

            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]

            rsi = calc_rsi(closes)
            if rsi is None or rsi < MIN_RSI:
                print(f"Skip {symbol}: RSI {rsi} below {MIN_RSI}.")
                continue

            atr = calc_atr(highs, lows, closes)
            if not atr or atr <= 0:
                print(f"Skip {symbol}: invalid ATR.")
                continue

            entry_price = closes[-1]
            save_signal(symbol, rank, change_pct, entry_price, rsi, atr)
            signals_found += 1

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            continue

    print(f"Done. Signals found this run: {signals_found}")


if __name__ == "__main__":
    main()
