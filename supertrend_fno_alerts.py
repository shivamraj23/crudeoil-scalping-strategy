import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv


# ============================================================
# CONFIG

# ============================================================

load_dotenv()


DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN")

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_SUPERTREND_FNO_STOCKS"
)

# If you use your existing automatic TOTP/token system,
# this script can also obtain a token using the same setup.
DHAN_PIN = os.getenv("DHAN_PIN")
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")


# Strategy settings
CANDLE_MIN = 10

ST_PERIOD = 10
ST_MULT = 3.0

ADX_PERIOD = 14
ADX_MIN = 22

# ADX slope: at least 80% of the last 5 ADX points must be rising
ADX_SLOPE_LOOKBACK = 5
ADX_SLOPE_RISING_PCT = 0.80

# DMI separation: +DI must lead -DI by at least this margin
DMI_SEPARATION_MIN = 8.0

# Lookback for ADX max/min reported in the notification
ADX_EXTREMES_LOOKBACK = 15

# IMPORTANT:
# Only signals generated during this window are reported.
RESULT_WINDOW_MIN = 30

# Historical warm-up.
# This is NOT the result window.
WARMUP_DAYS = 5

# How often to wait between Dhan API requests
REQUEST_DELAY = 1.0


# ============================================================
# VALIDATE ENV
# ============================================================

if not DHAN_CLIENT_ID:
    raise RuntimeError("Missing DHAN_CLIENT_ID in .env")

if not DISCORD_WEBHOOK:
    raise RuntimeError(
        "Missing DISCORD_WEBHOOK_SUPERTREND_FNO_STOCKS in .env"
    )


# ============================================================
# DHAN TOKEN
# ============================================================

def get_dhan_token():

    # If access token exists, use it.
    if DHAN_ACCESS_TOKEN:
        print("Using DHAN_ACCESS_TOKEN from .env")
        return DHAN_ACCESS_TOKEN

    if not DHAN_PIN or not DHAN_TOTP_SECRET:
        raise RuntimeError(
            "Need either DHAN_ACCESS_TOKEN or "
            "DHAN_PIN + DHAN_TOTP_SECRET"
        )

    import pyotp

    # Dhan's current TOTP authentication endpoint
    login_url = (
        "https://auth.dhan.co/app/generateAccessToken"
    )

    otp = pyotp.TOTP(
        DHAN_TOTP_SECRET
    ).now()

    params = {
        "dhanClientId": DHAN_CLIENT_ID,
        "pin": DHAN_PIN,
        "totp": otp
    }

    response = requests.post(
        login_url,
        params=params,
        timeout=20
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Dhan login failed: "
            f"{response.status_code} {response.text}"
        )

    data = response.json()

    token = data.get("accessToken")

    if not token:
        raise RuntimeError(
            f"Could not find accessToken in response: {data}"
        )

    print("Dhan login successful")

    return token


# ============================================================
# HEADERS
# ============================================================

ACCESS_TOKEN = get_dhan_token()

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "access-token": ACCESS_TOKEN
}


# ============================================================
# DOWNLOAD F&O STOCK UNIVERSE
# ============================================================

def get_fno_stocks():

    print("\nDownloading Dhan instrument master...")

    url = (
        "https://images.dhan.co/api-data/"
        "api-scrip-master-detailed.csv"
    )

    df = pd.read_csv(url, low_memory=False)

    print(f"Instrument rows downloaded: {len(df):,}")

    required = [
        "EXCH_ID",
        "SEGMENT",
        "INSTRUMENT",
        "UNDERLYING_SECURITY_ID",
        "UNDERLYING_SYMBOL"
    ]

    missing = [c for c in required if c not in df.columns]

    if missing:
        raise RuntimeError(
            "Dhan instrument master is missing columns: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # NSE derivatives
    # --------------------------------------------------------

    fno = df[
        (df["EXCH_ID"] == "NSE") &
        (df["SEGMENT"] == "D")
    ].copy()

    # Stock futures/options only.
    # FUTSTK = stock futures
    # OPTSTK = stock options
    fno = fno[
        fno["INSTRUMENT"].isin(["FUTSTK", "OPTSTK"])
    ].copy()

    # Remove rows without underlying information
    fno = fno.dropna(
        subset=[
            "UNDERLYING_SECURITY_ID",
            "UNDERLYING_SYMBOL"
        ]
    )

    # --------------------------------------------------------
    # Unique underlying stocks
    # --------------------------------------------------------

    stocks = (
        fno[
            [
                "UNDERLYING_SECURITY_ID",
                "UNDERLYING_SYMBOL"
            ]
        ]
        .drop_duplicates()
        .sort_values("UNDERLYING_SYMBOL")
        .reset_index(drop=True)
    )

    stocks["UNDERLYING_SECURITY_ID"] = (
    pd.to_numeric(
        stocks["UNDERLYING_SECURITY_ID"],
        errors="coerce"
    )
    .astype("Int64")
    .astype(str)
    )   

    print(
        f"NSE F&O stocks found: {len(stocks)}"
    )

    return stocks


# ============================================================
# FETCH 1-MIN DATA
# ============================================================

def fetch_intraday_data(security_id):

    now = datetime.now()

    # Warm-up period.
    from_dt = now - timedelta(days=WARMUP_DAYS)

    url = "https://api.dhan.co/v2/charts/intraday"

    payload = {
        "securityId": str(security_id),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "interval": "1",
        "oi": False,

        "fromDate": from_dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "toDate": now.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    }

    response = requests.post(
        url,
        headers=HEADERS,
        json=payload,
        timeout=20
    )

    if response.status_code != 200:
        print(
            f"API error {security_id}: "
            f"{response.status_code}"
        )
        return None

    data = response.json()

    if not data:
        return None

    # Dhan v2 uses timestamp.
    timestamps = (
        data.get("timestamp")
        or data.get("start_Time")
        or data.get("startTime")
    )

    if not timestamps:
        return None

    try:

        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data.get(
                "volume",
                [0] * len(timestamps)
            )
        })

    except Exception as e:

        print(
            f"Data parsing error {security_id}: {e}"
        )

        return None

    # --------------------------------------------------------
    # Timestamp
    # --------------------------------------------------------

    # Dhan timestamps are Unix epoch seconds.
    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="s",
        utc=True
    ).dt.tz_convert("Asia/Kolkata")

    df = df.sort_values("timestamp")
    df = df.drop_duplicates("timestamp")

    df = df.set_index("timestamp")

    return df


# ============================================================
# RESAMPLE TO 10 MINUTES
# ============================================================

def make_10m_candles(df):

    if df is None or len(df) < 50:
        return None

    candles = df.resample(
        "10min",
        origin="start_day",
        offset="15min",
        label="right",
        closed="right"
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })

    candles = candles.dropna(
        subset=["open", "high", "low", "close"]
    )

    return candles


# ============================================================
# ATR
# ============================================================

def calculate_atr(df, period=10):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    return atr


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(
    df,
    period=10,
    multiplier=3.0
):

    df = df.copy()

    atr = calculate_atr(
        df,
        period
    )

    hl2 = (
        df["high"] +
        df["low"]
    ) / 2

    upper_basic = (
        hl2 +
        multiplier * atr
    )

    lower_basic = (
        hl2 -
        multiplier * atr
    )

    upper = upper_basic.copy()
    lower = lower_basic.copy()

    trend = pd.Series(
        index=df.index,
        dtype=int
    )

    trend.iloc[0] = 1

    for i in range(1, len(df)):

        # Final upper band
        if (
            upper_basic.iloc[i]
            < upper.iloc[i - 1]
            or df["close"].iloc[i - 1]
            > upper.iloc[i - 1]
        ):
            upper.iloc[i] = upper_basic.iloc[i]

        else:
            upper.iloc[i] = upper.iloc[i - 1]

        # Final lower band
        if (
            lower_basic.iloc[i]
            > lower.iloc[i - 1]
            or df["close"].iloc[i - 1]
            < lower.iloc[i - 1]
        ):
            lower.iloc[i] = lower_basic.iloc[i]

        else:
            lower.iloc[i] = lower.iloc[i - 1]

        # Trend
        if trend.iloc[i - 1] == -1:

            if df["close"].iloc[i] > upper.iloc[i]:
                trend.iloc[i] = 1
            else:
                trend.iloc[i] = -1

        else:

            if df["close"].iloc[i] < lower.iloc[i]:
                trend.iloc[i] = -1
            else:
                trend.iloc[i] = 1

    df["supertrend"] = trend

    return df


# ============================================================
# ADX + DI
# ============================================================

def calculate_adx(
    df,
    period=14
):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where(
            (up_move > down_move) &
            (up_move > 0),
            up_move,
            0.0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (down_move > up_move) &
            (down_move > 0),
            down_move,
            0.0
        ),
        index=df.index
    )

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    plus_dm_smoothed = plus_dm.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    minus_dm_smoothed = minus_dm.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    plus_di = (
        100 *
        plus_dm_smoothed /
        atr.replace(0, np.nan)
    )

    minus_di = (
        100 *
        minus_dm_smoothed /
        atr.replace(0, np.nan)
    )

    dx = (
        100 *
        (plus_di - minus_di).abs() /
        (plus_di + minus_di)
        .replace(0, np.nan)
    )

    adx = dx.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    return adx, plus_di, minus_di


# ============================================================
# ADX SLOPE
# ============================================================

def adx_slope_ok(
    adx_series,
    position,
    lookback=ADX_SLOPE_LOOKBACK,
    rising_pct=ADX_SLOPE_RISING_PCT
):

    # Need previous value for each comparison.
    start = position - lookback + 1

    if start < 1:
        return False

    rising = 0

    for j in range(start, position + 1):

        if adx_series.iloc[j] > adx_series.iloc[j - 1]:

            rising += 1

    return (rising / lookback) >= rising_pct


# ============================================================
# ANALYZE STOCK
# ============================================================

def analyze_stock(
    symbol,
    security_id,
    df
):

    candles = make_10m_candles(df)

    if candles is None or len(candles) < 50:
        return []

    candles = calculate_supertrend(
        candles,
        ST_PERIOD,
        ST_MULT
    )

    (
        candles["adx"],
        candles["plus_di"],
        candles["minus_di"]
    ) = calculate_adx(
        candles,
        ADX_PERIOD
    )

    # --------------------------------------------------------
    # ADX extremes over the last N candles
    # --------------------------------------------------------

    candles["adx_max_15"] = candles["adx"].rolling(
        ADX_EXTREMES_LOOKBACK,
        min_periods=1
    ).max()

    candles["adx_min_15"] = candles["adx"].rolling(
        ADX_EXTREMES_LOOKBACK,
        min_periods=1
    ).min()

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Remove the currently-forming candle.
    # We only act on completed 10m candles.
    # --------------------------------------------------------

    now = pd.Timestamp.now(
        tz="Asia/Kolkata"
    )

    latest_completed = (
        candles.index <= now
    )

    candles = candles.loc[
        latest_completed
    ].copy()

    if len(candles) < 5:
        return []

    # --------------------------------------------------------
    # Last 30 minutes ONLY
    #
    # With 10m candles:
    # latest 3 completed candles
    # --------------------------------------------------------

    cutoff = now - pd.Timedelta(
        minutes=RESULT_WINDOW_MIN
    )

    # Positions (integer) of completed candles inside the result window
    recent_positions = [
        k
        for k, ts in enumerate(candles.index)
        if ts > cutoff
    ]

    if not recent_positions:
        return []

    signals = []

    for i in recent_positions:

        if i < 1:
            continue

        current = candles.iloc[i]
        previous = candles.iloc[i - 1]

        # ----------------------------------------------------
        # Supertrend flip
        # ----------------------------------------------------

        flip = (
            current["supertrend"]
            != previous["supertrend"]
        )

        if not flip:
            continue

        # ----------------------------------------------------
        # Absolute level: ADX > 22
        # ----------------------------------------------------

        adx_ok = (
            current["adx"] > ADX_MIN
        )

        if not adx_ok:
            continue

        # ----------------------------------------------------
        # Slope: >= 80% of the last 5 ADX points rising
        # ----------------------------------------------------

        if not adx_slope_ok(
            candles["adx"],
            i
        ):
            continue

        # ----------------------------------------------------
        # Direction
        # ----------------------------------------------------

        if current["supertrend"] == 1:
            direction = "BULLISH"
            emoji = "🟢"
            dmi_gap = (
                current["plus_di"] -
                current["minus_di"]
            )
        else:
            direction = "BEARISH"
            emoji = "🔴"
            dmi_gap = (
                current["minus_di"] -
                current["plus_di"]
            )

        # ----------------------------------------------------
        # DMI separation: leading DI ahead by >= 8 points
        # ----------------------------------------------------

        if dmi_gap < DMI_SEPARATION_MIN:
            continue

        signals.append({
            "symbol": symbol,
            "security_id": security_id,
            "time": current.name,
            "close": current["close"],
            "adx": current["adx"],
            "adx_max_15": current["adx_max_15"],
            "adx_min_15": current["adx_min_15"],
            "plus_di": current["plus_di"],
            "minus_di": current["minus_di"],
            "dmi_gap": dmi_gap,
            "direction": direction,
            "emoji": emoji
        })

    return signals


# ============================================================
# DISCORD
# ============================================================

def send_discord(signal):

    timestamp = signal["time"].strftime(
        "%d-%b-%Y %H:%M"
    )

    message = (
        f"{signal['emoji']} **F&O STOCK SUPERTREND SIGNAL**\n"
        f"**Stock:** {signal['symbol']}\n"
        f"**Direction:** {signal['direction']}\n"
        f"**Timeframe:** 10m\n"
        f"**Signal Candle:** {timestamp} IST\n"
        f"**Close:** {signal['close']:.2f}\n"
        f"**ADX(14):** {signal['adx']:.2f}\n"
        f"**ADX Max (last 15):** {signal['adx_max_15']:.2f}\n"
        f"**ADX Min (last 15):** {signal['adx_min_15']:.2f}\n"
        f"**+DI:** {signal['plus_di']:.2f}  |  **-DI:** {signal['minus_di']:.2f}\n"
        f"**DMI Separation:** {signal['dmi_gap']:.2f}\n"
        f"**Condition:** ST(10,3) Flip + ADX > {ADX_MIN} + "
        f"ADX Slope >= 80% + DMI Sep >= {DMI_SEPARATION_MIN:.0f}\n\n"
    )

    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message},
        timeout=15
    )

    if response.status_code not in (200, 204):

        print(
            "Discord error:",
            response.status_code,
            response.text
        )

    else:

        print(
            f"Discord alert sent: "
            f"{signal['symbol']} "
            f"{signal['direction']}"
        )


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    print("=" * 70)
    print("F&O STOCK SUPERTREND SCANNER")
    print("=" * 70)

    print(
        f"Timeframe: {CANDLE_MIN}m"
    )

    print(
        f"Supertrend: ({ST_PERIOD}, {ST_MULT})"
    )

    print(
        f"ADX: ({ADX_PERIOD}) > {ADX_MIN}"
    )

    print(
        f"ADX slope: >= {ADX_SLOPE_RISING_PCT:.0%} of last "
        f"{ADX_SLOPE_LOOKBACK} points rising"
    )

    print(
        f"DMI separation: leading DI ahead by >= "
        f"{DMI_SEPARATION_MIN:.0f} points"
    )

    print(
        f"RESULT WINDOW: LAST {RESULT_WINDOW_MIN} MINUTES ONLY"
    )

    print(
        f"Warm-up: {WARMUP_DAYS} days"
    )

    print("=" * 70)

    # --------------------------------------------------------
    # Get universe
    # --------------------------------------------------------

    stocks = get_fno_stocks()

    print(
        f"\nStarting scan of "
        f"{len(stocks)} F&O stocks...\n"
    )

    all_signals = []

    # --------------------------------------------------------
    # Scan
    # --------------------------------------------------------

    for index, row in stocks.iterrows():

        symbol = str(
            row["UNDERLYING_SYMBOL"]
        )

        security_id = str(
            row["UNDERLYING_SECURITY_ID"]
        )

        print(
            f"[{index + 1}/{len(stocks)}] "
            f"{symbol}",
            end=" ... ",
            flush=True
        )

        try:

            df = fetch_intraday_data(
                security_id
            )

            if df is None:

                print("NO DATA")
                continue

            signals = analyze_stock(
                symbol,
                security_id,
                df
            )

            if signals:

                print(
                    f"SIGNAL x{len(signals)}"
                )

                for signal in signals:

                    all_signals.append(signal)

            else:

                print("no signal")

        except Exception as e:

            print(
                f"ERROR: {e}"
            )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # Results
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("FINAL RESULTS")
    print("=" * 70)

    if not all_signals:

        print(
            "\nNo F&O stock generated a qualifying "
            "signal in the last 30 minutes."
        )

        return

    # Sort newest first
    all_signals.sort(
        key=lambda x: x["time"],
        reverse=True
    )

    print(
        f"\nSignals found: {len(all_signals)}\n"
    )

    for signal in all_signals:

        print(
            f"{signal['emoji']} "
            f"{signal['symbol']:15} "
            f"{signal['direction']:8} "
            f"{signal['time'].strftime('%H:%M')} "
            f"ADX={signal['adx']:.2f} "
            f"(min={signal['adx_min_15']:.2f}, "
            f"max={signal['adx_max_15']:.2f}) "
            f"DMIsep={signal['dmi_gap']:.2f} "
            f"Close={signal['close']:.2f}"
        )

    # --------------------------------------------------------
    # Discord
    # --------------------------------------------------------

    print("\nSending Discord alerts...")

    for signal in all_signals:

        send_discord(signal)

        time.sleep(0.5)

    print("\nScan completed.")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()