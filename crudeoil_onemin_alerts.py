import os
import time
import json
import math
import calendar
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
import pyotp
from dotenv import load_dotenv


# ============================================================
# ENVIRONMENT
# ============================================================

ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_FILE)

CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
PIN = os.getenv("DHAN_PIN")
TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")

TRADE_WEBHOOK = os.getenv("CRUDEOIL_TRADE_WEBHOOK")
INDICATOR_WEBHOOK = os.getenv("CRUDEOIL_INDICATOR_WEBHOOK")

TOKEN_FILE = Path(__file__).resolve().parent / "dhan_token.json"

IST = timezone(timedelta(hours=5, minutes=30))

INSTRUMENT_MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)

DHAN_INTRADAY_URL = "https://api.dhan.co/v2/charts/intraday"

ADX14_THRESHOLD = 20

# ADX(14) confirmation filter: at least 4 of the last 5 ADX(14)
# deltas must be non-decreasing (flat or rising). This guards
# against a single-spike "fake" signal.
ADX_CONFIRM_LOOKBACK = 5
ADX_CONFIRM_MIN_NON_DECREASING = 4

# Crude Oil expires around the 18th-20th of the month, so roll to
# the next contract well before month-end.
CONTRACT_ROLL_DAY = 15

# Crude Oil Mini contract token in the Dhan instrument master.
CONTRACT_SYMBOL = "CRUDEOILM"

POLL_SECONDS = 30

# Trading window 09:00 IST to midnight. Outside it the bot stays
# idle (no Dhan API calls) to conserve energy.
TRADING_START_HOUR = 9
TRADING_END_HOUR = 24

# While idle, check in this often so a wake is prompt.
SLEEP_CHUNK_SECONDS = 60

# Keep enough candles for ADX calculations
LOOKBACK_DAYS = 5


# ============================================================
# GLOBAL CONTRACT STATE
# ============================================================

CURRENT_CONTRACT_MONTH = None
CURRENT_SECURITY_ID = None
CURRENT_TRADING_SYMBOL = None

LAST_PROCESSED_CANDLE = None

# Used to avoid repeated indicator alerts
LAST_ADX14_ABOVE = None
LAST_ADX28_RISING = None
LAST_DIRECTION = None

# Tracks whether we are inside the active trading window
IN_TRADING_WINDOW = None


# ============================================================
# LOGGING
# ============================================================

def now_ist():
    return datetime.now(IST)


def log(message):
    print(
        f"{now_ist().strftime('%d %b %Y %H:%M:%S')} | {message}",
        flush=True
    )


# ============================================================
# TRADING WINDOW
# ============================================================

def is_in_trading_window():
    hour = now_ist().hour
    return (
        TRADING_START_HOUR
        <= hour
        < TRADING_END_HOUR
    )


def seconds_until_window_start():
    now = now_ist()

    start = now.replace(
        hour=TRADING_START_HOUR,
        minute=0,
        second=0,
        microsecond=0
    )

    if start <= now:
        start += timedelta(days=1)

    return (start - now).total_seconds()


# ============================================================
# VALIDATION
# ============================================================

required_vars = {
    "DHAN_CLIENT_ID": CLIENT_ID,
    "DHAN_PIN": PIN,
    "DHAN_TOTP_SECRET": TOTP_SECRET,
    "CRUDEOIL_TRADE_WEBHOOK": TRADE_WEBHOOK,
    "CRUDEOIL_INDICATOR_WEBHOOK": INDICATOR_WEBHOOK,
}

missing = [
    name
    for name, value in required_vars.items()
    if not value
]

if missing:
    raise RuntimeError(
        "Missing environment variables: "
        + ", ".join(missing)
    )


# ============================================================
# DHAN TOKEN
# ============================================================

def save_token(token):
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "access_token": token,
                    "saved_at": time.time(),
                },
                f
            )
    except Exception as e:
        log(f"Could not save token: {e}")


def load_cached_token():
    if not TOKEN_FILE.exists():
        return None

    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        token = data.get("access_token")

        if token:
            return token

    except Exception:
        pass

    return None


def get_token(force=False):
    """
    Generate Dhan access token using Client ID + PIN + TOTP.
    Cached token is used first unless force=True.
    """

    if not force:
        cached = load_cached_token()
        if cached:
            return cached

    totp = pyotp.TOTP(TOTP_SECRET.strip().replace(" ", ""))

    last_error = None

    # Retry only if the 30s TOTP window rolled over mid-request
    for attempt in range(2):
        try:
            otp = totp.now()

            response = requests.post(
                "https://auth.dhan.co/app/generateAccessToken",
                params={                      # <-- query params, not json=
                    "dhanClientId": CLIENT_ID,
                    "pin": PIN,
                    "totp": otp,
                },
                timeout=20,
            )

            if response.status_code == 200:
                data = response.json()
                token = data.get("accessToken") or data.get("access_token")

                if token:
                    save_token(token)
                    log("Dhan TOTP login successful")
                    return token

                last_error = f"No access token in response: {data}"

            else:
                last_error = f"HTTP {response.status_code}: {response.text}"

                if response.status_code == 429:
                    log("Dhan token API rate limited. Waiting 60s...")
                    time.sleep(60)
                    continue

        except Exception as e:
            last_error = str(e)

        # wait for next TOTP window before retrying
        time.sleep(31)

    raise RuntimeError(f"Dhan TOTP login failed: {last_error}")


# ============================================================
# DHAN HEADERS
# ============================================================

def dhan_headers(force=False):
    token = get_token(force=force)

    return {
        "access-token": token,
        "Content-Type": "application/json",
    }


# ============================================================
# TARGET CONTRACT MONTH
# ============================================================

def get_target_contract_month():
    """
    Contract rule:

    Crude Oil expires around the 18th-20th of the month, so we roll
    to the next contract from CONTRACT_ROLL_DAY onwards (well before
    month-end).

    1st - (CONTRACT_ROLL_DAY-1)  -> current month
    CONTRACT_ROLL_DAY - end      -> next month

    Examples (CONTRACT_ROLL_DAY = 15):

    Oct 1-14  -> October
    Oct 15-31 -> November
    Nov 1-14  -> November
    Nov 15-30 -> December
    Dec 15-31 -> January
    """

    today = now_ist().date()

    year = today.year
    month = today.month

    if today.day >= CONTRACT_ROLL_DAY:
        month += 1

        if month == 13:
            month = 1
            year += 1

    return datetime(year, month, 1).date()


# ============================================================
# DYNAMIC CRUDE OIL CONTRACT SELECTION
# ============================================================

def get_crudeoil_contract():

    global CURRENT_CONTRACT_MONTH
    global CURRENT_SECURITY_ID
    global CURRENT_TRADING_SYMBOL

    target_month = get_target_contract_month()

    # Already selected for this contract month
    if (
        CURRENT_CONTRACT_MONTH == target_month
        and CURRENT_SECURITY_ID is not None
    ):
        return (
            CURRENT_SECURITY_ID,
            CURRENT_TRADING_SYMBOL
        )

    log(
        f"Looking for Crude Oil contract "
        f"{target_month.strftime('%Y-%m')}"
    )

    log("Downloading Dhan instrument master...")

    try:
        df = pd.read_csv(
            INSTRUMENT_MASTER_URL,
            low_memory=False
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to download instrument master: {e}"
        )

    log(
        f"Instrument master loaded: "
        f"{len(df):,} rows"
    )

    # --------------------------------------------------------
    # Detect important columns
    # --------------------------------------------------------

    security_col = (
        "SECURITY_ID"
        if "SECURITY_ID" in df.columns
        else None
    )

    symbol_col = (
        "SYMBOL_NAME"
        if "SYMBOL_NAME" in df.columns
        else None
    )

    trading_col = (
        "SEM_TRADING_SYMBOL"
        if "SEM_TRADING_SYMBOL" in df.columns
        else None
    )

    expiry_col = (
        "SM_EXPIRY_DATE"
        if "SM_EXPIRY_DATE" in df.columns
        else None
    )

    log("Detected instrument master columns:")
    log(
        f"Security={security_col}, "
        f"Symbol={symbol_col}, "
        f"Trading={trading_col}, "
        f"Expiry={expiry_col}"
    )

    if security_col is None:
        raise RuntimeError(
            "SECURITY_ID column not found in "
            "Dhan instrument master"
        )

    if expiry_col is None:
        raise RuntimeError(
            "SM_EXPIRY_DATE column not found in "
            "Dhan instrument master"
        )

    # --------------------------------------------------------
    # Normalize expiry
    # --------------------------------------------------------

    df["_EXPIRY"] = pd.to_datetime(
        df[expiry_col],
        errors="coerce"
    )

    # --------------------------------------------------------
    # MCX
    # --------------------------------------------------------

    if "EXCH_ID" in df.columns:

        mcx = df[
            df["EXCH_ID"]
            .astype(str)
            .str.upper()
            .eq("MCX")
        ].copy()

    else:

        mcx = df.copy()

    log(
        f"MCX rows: {len(mcx):,}"
    )

    # --------------------------------------------------------
    # Commodity futures
    # --------------------------------------------------------

    if "INSTRUMENT" in mcx.columns:

        futures = mcx[
            mcx["INSTRUMENT"]
            .astype(str)
            .str.upper()
            .eq("FUTCOM")
        ].copy()

    else:

        futures = mcx.copy()

    log(
        f"MCX commodity futures: "
        f"{len(futures):,}"
    )

    # --------------------------------------------------------
    # Target expiry month
    # --------------------------------------------------------

    futures = futures[
        (futures["_EXPIRY"].dt.year == target_month.year)
        &
        (futures["_EXPIRY"].dt.month == target_month.month)
    ].copy()

    log(
        f"Futures expiring in "
        f"{target_month.strftime('%Y-%m')}: "
        f"{len(futures):,}"
    )

    # --------------------------------------------------------
    # Find Crude Oil
    #
    # Search multiple Dhan master columns because the
    # representation can differ between columns.
    # --------------------------------------------------------

    search_columns = [
        "SYMBOL_NAME",
        "DISPLAY_NAME",
        "SEM_CUSTOM_SYMBOL",
        "SEM_TRADING_SYMBOL",
        "UNDERLYING_SYMBOL",
    ]

    available_search_columns = [
        c
        for c in search_columns
        if c in futures.columns
    ]

    crude_mask = pd.Series(
        False,
        index=futures.index
    )

    for col in available_search_columns:

        crude_mask |= (
            futures[col]
            .astype(str)
            .str.upper()
            .str.contains(
                CONTRACT_SYMBOL,
                na=False
            )
        )

    crude = futures[
        crude_mask
    ].copy()

    # --------------------------------------------------------
    # If not found, print useful debug information
    # --------------------------------------------------------

    if crude.empty:

        log(
            f"No {CONTRACT_SYMBOL} match found."
        )

        log(
            "Available target-month futures "
            "(first 50 rows):"
        )

        debug_cols = [
            c
            for c in [
                security_col,
                "EXCH_ID",
                "INSTRUMENT",
                "SYMBOL_NAME",
                "DISPLAY_NAME",
                "SEM_CUSTOM_SYMBOL",
                "SEM_TRADING_SYMBOL",
                "UNDERLYING_SYMBOL",
                expiry_col,
            ]
            if c in futures.columns
        ]

        if not futures.empty:
            print(
                futures[
                    debug_cols
                ]
                .drop_duplicates()
                .head(50)
                .to_string(index=False),
                flush=True
            )

        raise RuntimeError(
            "No Crude Oil Mini futures found for "
            f"{target_month.strftime('%Y-%m')}"
        )

    # --------------------------------------------------------
    # Select contract
    # --------------------------------------------------------

    crude = crude.sort_values(
        "_EXPIRY"
    )

    selected = crude.iloc[0]

    security_id = str(
        selected[security_col]
    )

    trading_symbol = None

    # Prefer actual trading symbol
    for col in [
        "SEM_TRADING_SYMBOL",
        "DISPLAY_NAME",
        "SEM_CUSTOM_SYMBOL",
        "SYMBOL_NAME",
    ]:

        if col not in crude.columns:
            continue

        value = str(
            selected[col]
        ).strip()

        if (
            value
            and value.lower() != "nan"
        ):
            trading_symbol = value
            break

    expiry = selected["_EXPIRY"]

    # --------------------------------------------------------
    # Save current contract
    # --------------------------------------------------------

    CURRENT_CONTRACT_MONTH = target_month
    CURRENT_SECURITY_ID = security_id
    CURRENT_TRADING_SYMBOL = trading_symbol

    log("==========================================")
    log("CRUDE OIL MINI CONTRACT SELECTED")
    log(
        f"Trading Symbol: {trading_symbol}"
    )
    log(
        f"Security ID:    {security_id}"
    )
    log(
        f"Expiry:         {expiry}"
    )
    log("==========================================")

    return (
        security_id,
        trading_symbol
    )


# ============================================================
# FETCH 1-MINUTE DATA
# ============================================================

def fetch_1m(
    security_id,
    from_date,
    to_date
):

    payload = {
        "securityId": str(security_id),
        "exchangeSegment": "MCX_COMM",
        "instrument": "FUTCOM",
        "interval": "1",
        "oi": False,
        "fromDate": from_date,
        "toDate": to_date,
    }

    for attempt in range(2):

        try:

            headers = dhan_headers(
                force=(attempt == 1)
            )

            response = requests.post(
                DHAN_INTRADAY_URL,
                headers=headers,
                json=payload,
                timeout=30,
            )

            # Token expired
            if response.status_code == 401:

                log(
                    "Dhan API returned 401. "
                    "Refreshing token..."
                )

                continue

            if response.status_code != 200:

                raise RuntimeError(
                    f"Dhan API HTTP "
                    f"{response.status_code}: "
                    f"{response.text}"
                )

            data = response.json()

            if not data:
                return pd.DataFrame()

            if "timestamp" not in data:
                raise RuntimeError(
                    f"Unexpected Dhan response: "
                    f"{data}"
                )

            ts = np.asarray(
                data["timestamp"],
                dtype="float64"
            )

            # Dhan timestamp correction used in
            # the reference code.
            if len(ts) > 0 and ts.max() < 1.6e9:
                ts = ts + 315532800

            df = pd.DataFrame(
                {
                    "timestamp": ts,
                    "open": data.get(
                        "open",
                        []
                    ),
                    "high": data.get(
                        "high",
                        []
                    ),
                    "low": data.get(
                        "low",
                        []
                    ),
                    "close": data.get(
                        "close",
                        []
                    ),
                    "volume": data.get(
                        "volume",
                        []
                    ),
                }
            )

            if df.empty:
                return df

            df["datetime"] = pd.to_datetime(
                df["timestamp"],
                unit="s",
                utc=True
            ).dt.tz_convert(IST)

            df = df.sort_values(
                "datetime"
            ).reset_index(drop=True)

            return df

        except Exception as e:

            if attempt == 1:
                raise

            log(
                f"Fetch error: {e}. "
                "Retrying..."
            )

            time.sleep(2)

    return pd.DataFrame()


# ============================================================
# WILDER RMA
# ============================================================

def rma(series, length):

    series = pd.Series(
        series,
        dtype="float64"
    )

    result = pd.Series(
        np.nan,
        index=series.index,
        dtype="float64"
    )

    if len(series) < length:
        return result

    first_valid = series.iloc[
        :length
    ].mean()

    result.iloc[
        length - 1
    ] = first_valid

    alpha = 1.0 / length

    for i in range(
        length,
        len(series)
    ):

        previous = result.iloc[i - 1]
        current = series.iloc[i]

        result.iloc[i] = (
            alpha * current
            +
            (1 - alpha) * previous
        )

    return result


# ============================================================
# ADX / DI CALCULATION
# ============================================================

def calculate_adx(df, length):

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr1 = high - low

    tr2 = (
        high - prev_close
    ).abs()

    tr3 = (
        low - prev_close
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    # Directional movement
    up_move = (
        high - prev_high
    )

    down_move = (
        prev_low - low
    )

    plus_dm = pd.Series(
        np.where(
            (up_move > down_move)
            &
            (up_move > 0),
            up_move,
            0.0
        ),
        index=df.index
    )

    minus_dm = pd.Series(
        np.where(
            (down_move > up_move)
            &
            (down_move > 0),
            down_move,
            0.0
        ),
        index=df.index
    )

    atr = rma(
        tr,
        length
    )

    plus_dm_rma = rma(
        plus_dm,
        length
    )

    minus_dm_rma = rma(
        minus_dm,
        length
    )

    plus_di = (
        100
        *
        plus_dm_rma
        /
        atr.replace(0, np.nan)
    )

    minus_di = (
        100
        *
        minus_dm_rma
        /
        atr.replace(0, np.nan)
    )

    di_sum = (
        plus_di + minus_di
    )

    dx = (
        100
        *
        (plus_di - minus_di).abs()
        /
        di_sum.replace(0, np.nan)
    )

    adx = rma(
        dx,
        length
    )

    return (
        adx,
        plus_di,
        minus_di
    )


# ============================================================
# INDICATORS
# ============================================================

def calculate_indicators(df):

    df = df.copy()

    (
        df["adx14"],
        df["plus_di14"],
        df["minus_di14"]
    ) = calculate_adx(
        df,
        14
    )

    (
        df["adx28"],
        df["plus_di28"],
        df["minus_di28"]
    ) = calculate_adx(
        df,
        28
    )

    df["adx14_rising"] = (
        df["adx14"]
        >
        df["adx14"].shift(1)
    )

    df["adx14_falling"] = (
        df["adx14"]
        <
        df["adx14"].shift(1)
    )

    df["adx28_rising"] = (
        df["adx28"]
        >
        df["adx28"].shift(1)
    )

    df["adx28_falling"] = (
        df["adx28"]
        <
        df["adx28"].shift(1)
    )

    # Direction based on +DI vs -DI
    df["bullish"] = (
        df["plus_di14"]
        >
        df["minus_di14"]
    )

    df["bearish"] = (
        df["minus_di14"]
        >
        df["plus_di14"]
    )

    return df


# ============================================================
# COMPLETED CANDLE
# ============================================================

def get_completed_candles(df):

    if df.empty:
        return df

    current_time = now_ist()

    # Current minute is still forming.
    # Only use candles whose minute has completed.
    completed = df[
        df["datetime"]
        <
        current_time.replace(
            second=0,
            microsecond=0
        )
    ].copy()

    return completed


# ============================================================
# STATE
# ============================================================

def get_state(df):

    if len(df) < 100:
        return None, None

    df = calculate_indicators(
        df
    )

    df = df.dropna(
        subset=[
            "adx14",
            "adx28",
            "plus_di14",
            "minus_di14",
        ]
    )

    if len(df) < 2:
        return None, None

    current = df.iloc[-1]
    previous = df.iloc[-2]

    # --------------------------------------------------------
    # ADX(14) confirmation
    #
    # At least ADX_CONFIRM_MIN_NON_DECREASING of the last
    # ADX_CONFIRM_LOOKBACK deltas must be non-decreasing
    # (flat or rising).
    # --------------------------------------------------------

    adx14_deltas = (
        df["adx14"]
        .diff()
        .tail(ADX_CONFIRM_LOOKBACK)
        .dropna()
    )

    adx14_non_decreasing = int(
        (adx14_deltas >= 0).sum()
    )

    adx14_flat_confirmed = (
        len(adx14_deltas) >= ADX_CONFIRM_LOOKBACK
        and
        adx14_non_decreasing
        >= ADX_CONFIRM_MIN_NON_DECREASING
    )

    current_state = {
        "datetime": current["datetime"],
        "price": float(current["close"]),

        "adx14": float(
            current["adx14"]
        ),

        "adx28": float(
            current["adx28"]
        ),

        "plus_di14": float(
            current["plus_di14"]
        ),

        "minus_di14": float(
            current["minus_di14"]
        ),

        "adx14_rising": bool(
            current["adx14"]
            >
            previous["adx14"]
        ),

        "adx14_falling": bool(
            current["adx14"]
            <
            previous["adx14"]
        ),

        "adx28_rising": bool(
            current["adx28"]
            >
            previous["adx28"]
        ),

        "adx28_falling": bool(
            current["adx28"]
            <
            previous["adx28"]
        ),

        "bullish": bool(
            current["plus_di14"]
            >
            current["minus_di14"]
        ),

        "bearish": bool(
            current["minus_di14"]
            >
            current["plus_di14"]
        ),

        "adx14_non_decreasing": adx14_non_decreasing,
        "adx14_flat_confirmed": adx14_flat_confirmed,
    }

    previous_state = {
        "datetime": previous["datetime"],
        "price": float(previous["close"]),

        "adx14": float(
            previous["adx14"]
        ),

        "adx28": float(
            previous["adx28"]
        ),

        "plus_di14": float(
            previous["plus_di14"]
        ),

        "minus_di14": float(
            previous["minus_di14"]
        ),

        "adx14_rising": bool(
            previous["adx14"]
            >
            df.iloc[-3]["adx14"]
        ) if len(df) >= 3 else False,

        "adx14_falling": bool(
            previous["adx14"]
            <
            df.iloc[-3]["adx14"]
        ) if len(df) >= 3 else False,

        "adx28_rising": bool(
            previous["adx28"]
            >
            df.iloc[-3]["adx28"]
        ) if len(df) >= 3 else False,

        "adx28_falling": bool(
            previous["adx28"]
            <
            df.iloc[-3]["adx28"]
        ) if len(df) >= 3 else False,

        "bullish": bool(
            previous["plus_di14"]
            >
            previous["minus_di14"]
        ),

        "bearish": bool(
            previous["minus_di14"]
            >
            previous["plus_di14"]
        ),
    }

    return (
        current_state,
        previous_state
    )


# ============================================================
# DISCORD
# ============================================================

def send_discord(
    webhook,
    title,
    description,
    color=None
):

    if not webhook:
        return False

    embed = {
        "title": title,
        "description": description,
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    if color is not None:
        embed["color"] = color

    payload = {
        "embeds": [embed]
    }

    try:

        response = requests.post(
            webhook,
            json=payload,
            timeout=15
        )

        if response.status_code == 204:
            return True

        if response.status_code == 200:
            return True

        if response.status_code == 429:

            try:
                retry_after = response.json().get(
                    "retry_after",
                    1
                )
            except Exception:
                retry_after = 1

            time.sleep(
                float(retry_after)
            )

            response = requests.post(
                webhook,
                json=payload,
                timeout=15
            )

            return response.status_code in (
                200,
                204
            )

        log(
            f"Discord error "
            f"{response.status_code}: "
            f"{response.text}"
        )

        return False

    except Exception as e:

        log(
            f"Discord send error: {e}"
        )

        return False


# ============================================================
# TRADE ALERT
# ============================================================

def send_trade_alert(
    state,
    security_id,
    trading_symbol
):

    if state["bullish"]:

        direction = "🟢 BULLISH"

        direction_detail = (
            f"+DI14 {state['plus_di14']:.2f} "
            f"> -DI14 {state['minus_di14']:.2f}"
        )

        color = 0x00FF00

    else:

        direction = "🔴 BEARISH"

        direction_detail = (
            f"-DI14 {state['minus_di14']:.2f} "
            f"> +DI14 {state['plus_di14']:.2f}"
        )

        color = 0xFF0000

    description = (
        f"ADX(14) > {ADX14_THRESHOLD} & Rising | "
        "ADX(28) Rising | "
        f"ADX(14) {state['adx14_non_decreasing']}/"
        f"{ADX_CONFIRM_LOOKBACK} non-decreasing\n"
        f"{direction} | {direction_detail}\n"
        f"Crude Oil Mini: {state['price']:.2f}"
    )

    send_discord(
        TRADE_WEBHOOK,
        f"CRUDE OIL — {direction}",
        description,
        color
    )

    log(
        f"TRADE ALERT | "
        f"{direction} | "
        f"Price={state['price']:.2f} | "
        f"ADX14={state['adx14']:.2f} | "
        f"ADX28={state['adx28']:.2f}"
    )


# ============================================================
# INDICATOR ALERTS
# ============================================================

def process_indicator_alerts(
    current,
    previous
):

    global LAST_ADX14_ABOVE
    global LAST_ADX28_RISING
    global LAST_DIRECTION

    alerts = []

    # --------------------------------------------------------
    # ADX14 crosses above / below 20
    # --------------------------------------------------------

    current_above = (
        current["adx14"]
        > ADX14_THRESHOLD
    )

    previous_above = (
        previous["adx14"]
        > ADX14_THRESHOLD
    )

    if (
        current_above
        and not previous_above
    ):

        alerts.append(
            (
                "📈 ADX(14) crossed ABOVE 20",
                f"ADX(14): {current['adx14']:.2f}",
                0x00FF00
            )
        )

    elif (
        not current_above
        and previous_above
    ):

        alerts.append(
            (
                "📉 ADX(14) crossed BELOW 20",
                f"ADX(14): {current['adx14']:.2f}",
                0xFF0000
            )
        )

    # --------------------------------------------------------
    # ADX28 starts rising / falling
    # --------------------------------------------------------

    current_rising = (
        current["adx28"]
        >
        previous["adx28"]
    )

    current_falling = (
        current["adx28"]
        <
        previous["adx28"]
    )

    previous_rising = (
        previous["adx28_rising"]
    )

    previous_falling = (
        previous["adx28_falling"]
    )

    if (
        current_rising
        and not previous_rising
    ):

        alerts.append(
            (
                "📈 ADX(28) STARTED RISING",
                f"ADX(28): {current['adx28']:.2f}",
                0x00FF00
            )
        )

    elif (
        current_falling
        and not previous_falling
    ):

        alerts.append(
            (
                "📉 ADX(28) STARTED FALLING",
                f"ADX(28): {current['adx28']:.2f}",
                0xFF0000
            )
        )

    # --------------------------------------------------------
    # Direction flip
    # --------------------------------------------------------

    previous_direction = (
        "BULLISH"
        if previous["bullish"]
        else "BEARISH"
    )

    current_direction = (
        "BULLISH"
        if current["bullish"]
        else "BEARISH"
    )

    if (
        current_direction
        != previous_direction
    ):

        if current_direction == "BULLISH":

            title = "🟢 CRUDE OIL — BULLISH FLIP"

            description = (
                f"+DI14 {current['plus_di14']:.2f} "
                f"> -DI14 {current['minus_di14']:.2f}"
            )

            color = 0x00FF00

        else:

            title = "🔴 CRUDE OIL — BEARISH FLIP"

            description = (
                f"-DI14 {current['minus_di14']:.2f} "
                f"> +DI14 {current['plus_di14']:.2f}"
            )

            color = 0xFF0000

        alerts.append(
            (
                title,
                description,
                color
            )
        )

    # --------------------------------------------------------
    # Send alerts
    # --------------------------------------------------------

    for title, description, color in alerts:

        send_discord(
            INDICATOR_WEBHOOK,
            title,
            description,
            color
        )

        log(
            f"INDICATOR ALERT | "
            f"{title} | "
            f"{description}"
        )

    LAST_ADX14_ABOVE = current_above
    LAST_ADX28_RISING = current_rising
    LAST_DIRECTION = current_direction


# ============================================================
# MAIN DATA PROCESSING
# ============================================================

def process_market():

    global LAST_PROCESSED_CANDLE

    security_id, trading_symbol = (
        get_crudeoil_contract()
    )

    # Fetch enough historical candles to calculate ADX
    end_time = now_ist()

    start_time = (
        end_time
        - timedelta(days=LOOKBACK_DAYS)
    )

    from_date = start_time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    to_date = end_time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    df = fetch_1m(
        security_id,
        from_date,
        to_date
    )

    if df.empty:

        log(
            "No Crude Oil data returned."
        )

        return

    df = get_completed_candles(
        df
    )

    if df.empty:

        log(
            "No completed candles yet."
        )

        return

    current_candle_time = (
        df.iloc[-1]["datetime"]
    )

    # Avoid processing the same candle repeatedly
    if (
        LAST_PROCESSED_CANDLE
        is not None
        and
        current_candle_time
        <= LAST_PROCESSED_CANDLE
    ):
        return

    current_state, previous_state = (
        get_state(df)
    )

    if (
        current_state is None
        or previous_state is None
    ):

        log(
            "Not enough valid candles "
            "for ADX calculation."
        )

        LAST_PROCESSED_CANDLE = (
            current_candle_time
        )

        return

    LAST_PROCESSED_CANDLE = (
        current_candle_time
    )

    # --------------------------------------------------------
    # Indicator alerts
    # --------------------------------------------------------

    process_indicator_alerts(
        current_state,
        previous_state
    )

    # --------------------------------------------------------
    # Trade entry condition
    #
    # ADX14 > 20
    # ADX14 rising
    # ADX28 rising
    # --------------------------------------------------------

    current_valid = (
        current_state["adx14"]
        > ADX14_THRESHOLD
        and
        current_state["adx14_rising"]
        and
        current_state["adx28_rising"]
        and
        current_state["adx14_flat_confirmed"]
    )

    previous_valid = (
        previous_state["adx14"]
        > ADX14_THRESHOLD
        and
        previous_state["adx14_rising"]
        and
        previous_state["adx28_rising"]
    )

    # Only alert when condition becomes valid.
    fresh_entry = (
        current_valid
        and not previous_valid
    )

    if fresh_entry:

        send_trade_alert(
            current_state,
            security_id,
            trading_symbol
        )

    # --------------------------------------------------------
    # Console status
    # --------------------------------------------------------

    direction = (
        "BULLISH"
        if current_state["bullish"]
        else "BEARISH"
    )

    log(
        f"{trading_symbol or 'CRUDE OIL MINI'} "
        f"{current_state['datetime'].strftime('%H:%M')} | "
        f"Price={current_state['price']:.2f} | "
        f"ADX14={current_state['adx14']:.2f} "
        f"({'RISING' if current_state['adx14_rising'] else 'FALLING'}) | "
        f"ADX28={current_state['adx28']:.2f} "
        f"({'RISING' if current_state['adx28_rising'] else 'FALLING'}) | "
        f"{direction}"
    )


# ============================================================
# MAIN LOOP
# ============================================================

def run():

    global IN_TRADING_WINDOW

    log("==========================================")
    log("CRUDE OIL MINI 1-MINUTE ADX ALERT BOT")
    log("==========================================")
    log(
        "Trade channel: "
        "crudeoil-trade-alerts"
    )
    log(
        "Indicator channel: "
        "crudeoil-indicator-alerts"
    )
    log(
        "Strategy: "
        "ADX14 > 20 + ADX14 rising + ADX28 rising "
        f"+ ADX14 {ADX_CONFIRM_MIN_NON_DECREASING}/"
        f"{ADX_CONFIRM_LOOKBACK} non-decreasing"
    )
    log(
        "Contract rule: "
        f"1st-{CONTRACT_ROLL_DAY - 1} current / "
        f"{CONTRACT_ROLL_DAY}-end next month "
        "(expiry ~18-20)"
    )
    log(
        "Trading window: "
        f"{TRADING_START_HOUR:02d}:00 IST - "
        f"{TRADING_END_HOUR:02d}:00 IST"
    )

    # Login once at startup
    get_token()

    while True:

        try:

            in_window = (
                is_in_trading_window()
            )

            # ------------------------------------------------
            # Wake / sleep notifications on transitions
            # ------------------------------------------------

            if (
                in_window
                and not IN_TRADING_WINDOW
            ):

                log(
                    "Trading window OPENED."
                )

                send_discord(
                    INDICATOR_WEBHOOK,
                    "🌅 CRUDE OIL MINI — BOT WAKE UP",
                    (
                        "Trading window open "
                        f"({TRADING_START_HOUR:02d}:00 IST). "
                        "Monitoring for ADX signals until "
                        f"{TRADING_END_HOUR:02d}:00 IST."
                    ),
                    0x00FF00
                )

            elif (
                not in_window
                and IN_TRADING_WINDOW
            ):

                log(
                    "Trading window CLOSED."
                )

                send_discord(
                    INDICATOR_WEBHOOK,
                    "🌙 CRUDE OIL MINI — BOT SLEEPING",
                    (
                        "Trading window closed "
                        f"({TRADING_END_HOUR:02d}:00 IST). "
                        "Bot resumes "
                        f"{TRADING_START_HOUR:02d}:00 IST."
                    ),
                    0x808080
                )

            IN_TRADING_WINDOW = in_window

            if not in_window:

                # Idle: no API calls, just wait for next open
                time.sleep(
                    min(
                        seconds_until_window_start(),
                        SLEEP_CHUNK_SECONDS
                    )
                )

                continue

            # ------------------------------------------------
            # Automatically detect contract month change
            # ------------------------------------------------

            target_month = (
                get_target_contract_month()
            )

            if (
                CURRENT_CONTRACT_MONTH
                is not None
                and
                target_month
                != CURRENT_CONTRACT_MONTH
            ):

                log(
                    "Target contract month changed. "
                    "Refreshing Crude Oil contract..."
                )

                # Force contract re-selection
                globals()[
                    "CURRENT_SECURITY_ID"
                ] = None

                globals()[
                    "CURRENT_TRADING_SYMBOL"
                ] = None

                globals()[
                    "CURRENT_CONTRACT_MONTH"
                ] = None

            process_market()

        except KeyboardInterrupt:

            log(
                "Bot stopped by user."
            )

            break

        except Exception as e:

            log(
                f"ERROR: {type(e).__name__}: {e}"
            )

            # Don't kill the bot because of a
            # temporary API/network problem.
            time.sleep(10)

        # Poll every 30 seconds while window is open
        time.sleep(POLL_SECONDS)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    run()