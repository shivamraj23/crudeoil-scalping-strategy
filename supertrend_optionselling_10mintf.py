"""
Supertrend LIVE alert (Nifty 50, 10-minute candles) -> Discord

  TRADE ALERT  : Supertrend flipped AND ADX(14) > 20   -> entry signal
  NORMAL ALERT : Supertrend flipped, ADX(14) <= 20     -> exit-only signal
Every flip sends exactly ONE message. Exit rule: end of day, or ANY next flip.

How it works: 5 seconds after every 10-minute candle closes (09:25, 09:35 ... 15:25, 15:30)
it fetches data, checks the candle that just closed, and alerts if Supertrend flipped.

Needs these environment variables (or a .env file next to this script):
  DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DISCORD_WEBHOOK_URL
  (optional) DHAN_ACCESS_TOKEN - if set, TOTP login is skipped
"""
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyotp
import requests

try:                                   # .env is optional (on a cloud host you set env variables instead)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------- SETTINGS (edit these) ----------------
SEND_NORMAL_ALERTS = True        # False = send only TRADE ALERTs
SEND_DAILY_SUMMARY = True        # one message after the last candle (15:30) listing the day's flips
EXIT_AFTER_MARKET = True         # True = script ends after 15:30 (use with a daily scheduler)
                                 # False = keeps running forever (use on your own server)
SEND_TEST_MESSAGE = False        # True = send one test message at start (to check Discord works), then set back to False
CANDLE_MIN = 10
ST_PERIOD, ST_MULT = 10, 3.0     # Supertrend settings
ADX_PERIOD, ADX_MIN = 14, 20
# name: (Dhan security id, strike gap)
INDEXES = {"NIFTY 50": ("13", 50)}
# Strike to sell = ATM -/+ STRIKE_OFFSET strikes:
#   bullish flip -> sell PUT at ATM - offset ; bearish flip -> sell CALL at ATM + offset
STRIKE_OFFSET = 2
# "prev_close" = change vs previous day's close (what broker apps show); "open" = vs today's open
CHANGE_VS = "prev_close"
LOOKBACK_DAYS = 12               # calendar days of history fetched for indicator warm-up
CHECK_DELAY_SEC = 5              # wait after a candle closes before fetching
RETRY_EVERY_SEC = 5              # if the new candle isn't available yet
MAX_RETRIES = 8
# --------------------------------------------------------

CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
PIN = os.getenv("DHAN_PIN")
TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
IST = "Asia/Kolkata"
TOKEN_FILE = Path("dhan_token.json")
STATE_FILE = Path("alert_state.json")


def now_ist():
    return pd.Timestamp.now(tz=IST)


def log(msg):
    print(f"{now_ist():%d %b %H:%M:%S} | {msg}", flush=True)


def check_config():
    needed = {"DISCORD_WEBHOOK_URL": WEBHOOK}
    if not (os.getenv("DHAN_ACCESS_TOKEN") or "").strip():
        needed.update({"DHAN_CLIENT_ID": CLIENT_ID, "DHAN_PIN": PIN, "DHAN_TOTP_SECRET": TOTP_SECRET})
    missing = [k for k, v in needed.items() if not v]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))


# =========================================================
# DHAN LOGIN (token is saved and reused; Dhan allows 1 new token per 2 minutes)
# =========================================================
def get_token(force=False):
    manual = (os.getenv("DHAN_ACCESS_TOKEN") or "").strip()
    if manual:
        return manual

    if force:
        try:
            TOKEN_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        try:
            saved = json.loads(TOKEN_FILE.read_text())
            if time.time() - saved["time"] < 20 * 3600:
                return saved["token"]
        except Exception:
            pass

    totp = pyotp.TOTP(TOTP_SECRET.strip())
    for _ in range(2):
        for offset in (0, 30, -30):      # current code, then neighbours (covers a slightly wrong clock)
            r = requests.post(
                "https://auth.dhan.co/app/generateAccessToken",
                params={"dhanClientId": CLIENT_ID, "pin": PIN, "totp": totp.at(time.time() + offset)},
                timeout=20,
            )
            try:
                data = r.json()
            except ValueError:
                data = {}
            if data.get("accessToken"):
                try:
                    TOKEN_FILE.write_text(json.dumps({"token": data["accessToken"], "time": time.time()}))
                except Exception:
                    pass                 # read-only disk is fine, we just won't cache
                log("Dhan login OK")
                return data["accessToken"]
            log(f"Token attempt failed: {r.text[:150]}")
            if "2 minutes" in r.text or "once every" in r.text.lower():
                log("Dhan rate limit: waiting 2 minutes")
                time.sleep(125)
            else:
                time.sleep(2)
    raise RuntimeError("Could not get Dhan access token")


def fetch_1m(security_id, start, end):
    payload = {
        "securityId": security_id, "exchangeSegment": "IDX_I", "instrument": "INDEX",
        "interval": "1", "oi": False,
        "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for attempt in (1, 2):
        r = requests.post(
            "https://api.dhan.co/v2/charts/intraday",
            headers={"access-token": get_token(force=(attempt == 2)), "Content-Type": "application/json"},
            json=payload, timeout=30,
        )
        if r.status_code == 401 and attempt == 1:     # expired token: log in again and retry once
            continue
        r.raise_for_status()
        break
    d = r.json()
    if not d or not d.get("timestamp"):
        return pd.DataFrame()
    ts = np.asarray(d["timestamp"], dtype="float64")
    if ts.max() < 1.6e9:                              # some Dhan responses use a 1980 epoch
        ts = ts + 315532800
    df = pd.DataFrame(
        {"open": d["open"], "high": d["high"], "low": d["low"], "close": d["close"]},
        index=pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST),
    )
    df = df[~df.index.duplicated()].sort_index()
    df["ts"] = df.index
    return df


# =========================================================
# CANDLES + INDICATORS
# =========================================================
def make_candles(df1, now):
    """1-min -> 10-min candles (09:15, 09:25 ...; last candle of day 15:25-15:30). Closed candles only."""
    if df1.empty:
        return pd.DataFrame()
    bars = df1.resample(f"{CANDLE_MIN}min", origin="start_day", offset="15min").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), last_bar=("ts", "last"),
    ).dropna(subset=["open"])
    day_close = bars.index.normalize() + pd.Timedelta(hours=15, minutes=30)
    bars["end"] = [min(s + pd.Timedelta(minutes=CANDLE_MIN), c) for s, c in zip(bars.index, day_close)]
    closed = (bars["last_bar"] == bars["end"] - pd.Timedelta(minutes=1)) & (bars["end"] <= now)
    return bars[closed].drop(columns="last_bar")


def day_refs(df1):
    """Per-day opening value and previous day's closing value (for the % change)."""
    g = df1.groupby(df1.index.strftime("%Y-%m-%d"))
    return g["open"].first(), g["close"].last().shift(1)


def rma(s, n):
    """Wilder smoothing with SMA seed (same as TradingView)."""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    ok = np.flatnonzero(~np.isnan(v))
    if len(ok) == 0 or len(v) - ok[0] < n:
        return pd.Series(out, index=s.index)
    i0 = ok[0]
    out[i0 + n - 1] = v[i0:i0 + n].mean()
    for i in range(i0 + n, len(v)):
        out[i] = (out[i - 1] * (n - 1) + v[i]) / n
    return pd.Series(out, index=s.index)


def add_indicators(c):
    c = c.copy()
    h, l, cl = (c[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(c)
    prev = np.r_[np.nan, cl[:-1]]
    tr = pd.Series(np.nanmax(np.vstack([h - l, abs(h - prev), abs(l - prev)]), axis=0), index=c.index)

    atr = rma(tr, ST_PERIOD).to_numpy()
    hl2 = (h + l) / 2
    ub, lb = hl2 + ST_MULT * atr, hl2 - ST_MULT * atr
    fu, fl = np.full(n, np.nan), np.full(n, np.nan)
    trend, st = np.zeros(n, dtype=int), np.full(n, np.nan)     # 1 bull, -1 bear, 0 not ready
    ok = np.flatnonzero(~np.isnan(atr))
    if len(ok):
        for i in range(ok[0], n):
            if i == ok[0]:
                fu[i], fl[i] = ub[i], lb[i]
                trend[i] = -1 if cl[i] < lb[i] else 1
            else:
                fu[i] = ub[i] if (ub[i] < fu[i - 1] or cl[i - 1] > fu[i - 1]) else fu[i - 1]
                fl[i] = lb[i] if (lb[i] > fl[i - 1] or cl[i - 1] < fl[i - 1]) else fl[i - 1]
                if trend[i - 1] == -1:
                    trend[i] = 1 if cl[i] > fu[i] else -1
                else:
                    trend[i] = -1 if cl[i] < fl[i] else 1
            st[i] = fl[i] if trend[i] == 1 else fu[i]

    up, dn = np.r_[0.0, np.diff(h)], np.r_[0.0, -np.diff(l)]
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=c.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=c.index)
    atr14 = rma(tr, ADX_PERIOD)
    pdi = 100 * rma(pdm, ADX_PERIOD) / atr14
    mdi = 100 * rma(mdm, ADX_PERIOD) / atr14
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)

    c["supertrend"], c["trend"], c["adx"] = st, trend, rma(dx, ADX_PERIOD)
    c["adx_prev"] = c["adx"].shift(1)
    p = c["trend"].shift(1).fillna(0)
    c["flip"] = (p != 0) & (c["trend"] != p)
    return c


# =========================================================
# DISCORD
# =========================================================
def pick_strike(spot, step, bull):
    """ATM = nearest strike. Sell PUT below ATM (bullish) or CALL above ATM (bearish)."""
    atm = int(np.floor(spot / step + 0.5)) * step
    if bull:
        return atm, atm - STRIKE_OFFSET * step, "PE"
    return atm, atm + STRIKE_OFFSET * step, "CE"


def build_alert(name, row, step, ref):
    bull = row["trend"] == 1
    atm, strike, side = pick_strike(row["close"], step, bull)
    chg = f"{(row['close'] / ref - 1) * 100:+.2f}%" if pd.notna(ref) and ref else "n/a"
    if pd.isna(row["adx"]):
        adx_line = "ADX(14): n/a"
    else:
        direction = ""
        if pd.notna(row["adx_prev"]):
            direction = " (increasing)" if row["adx"] > row["adx_prev"] else " (decreasing)"
        adx_line = f"ADX(14): {row['adx']:.1f}{direction}"
    sign = "-" if bull else "+"
    return bull, [
        f"Supertrend flipped to **{'BULLISH' if bull else 'BEARISH'}**",
        f"{name}: {row['close']:,.2f} ({chg})",
        f"{row['end']:%d %b, %H:%M} IST",
        adx_line,
        f"Note: Sell {strike} {side} (ATM {atm} {sign} {STRIKE_OFFSET} strikes)",
    ]


def post_embed(title, description, color):
    embed = {"title": title, "description": description, "color": color}
    for _ in range(3):
        r = requests.post(WEBHOOK, json={"embeds": [embed]}, timeout=20)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 1))
            except Exception:
                wait = 1.0
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord rate limit: gave up after 3 tries")


def safe_post(title, description, color):
    try:
        post_embed(title, description, color)
        return True
    except Exception as exc:
        log(f"Discord send failed: {exc}")
        return False


# =========================================================
# SCHEDULE
# =========================================================
def session_closes(day):
    o = day + pd.Timedelta(hours=9, minutes=15)
    c = day + pd.Timedelta(hours=15, minutes=30)
    out, t = [], o + pd.Timedelta(minutes=CANDLE_MIN)
    while t < c:
        out.append(t)
        t += pd.Timedelta(minutes=CANDLE_MIN)
    out.append(c)
    return out


def next_trigger(now):
    day = now.normalize()
    for _ in range(8):
        if day.weekday() < 5:
            for close in session_closes(day):
                trig = close + pd.Timedelta(seconds=CHECK_DELAY_SEC)
                if trig > now:
                    return close, trig
        day += pd.Timedelta(days=1)
    raise RuntimeError("could not find next trading session")


def sleep_until(t):
    while True:
        left = (t - now_ist()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


# =========================================================
# ONE CHECK (runs after every candle close)
# =========================================================
def wait_for_candle(sid, expected_close):
    for _ in range(MAX_RETRIES):
        now = now_ist()
        df1 = fetch_1m(sid, now.normalize() - pd.Timedelta(days=LOOKBACK_DAYS), now)
        candles = make_candles(df1, now)
        if not candles.empty and candles["end"].iloc[-1] >= expected_close:
            return df1, candles
        time.sleep(RETRY_EVERY_SEC)
    return None, None


def process_close(expected_close, state, stats):
    """Returns True if fresh data was received for at least one index."""
    got_any = False
    for name, (sid, step) in INDEXES.items():
        df1, candles = wait_for_candle(sid, expected_close)
        if candles is None:
            log(f"{name}: no new candle for {expected_close:%H:%M}")
            continue
        got_any = True

        day_open, prev_close = day_refs(df1)
        res = add_indicators(candles)
        ts, last = res.index[-1], res.iloc[-1]
        adx_txt = "n/a" if pd.isna(last["adx"]) else f"{last['adx']:.1f}"
        log(f"{name} {last['end']:%H:%M} close={last['close']:.2f} "
            f"trend={'BULL' if last['trend'] == 1 else 'BEAR'} adx={adx_txt} flip={bool(last['flip'])}")

        if not last["flip"]:
            continue
        kind = "TRADE" if last["adx"] > ADX_MIN else "NORMAL"
        bull = last["trend"] == 1
        stats["events"].append((name, f"{last['end']:%H:%M}", bull, kind))

        key = last["end"].isoformat()
        if state.get(name) == key:
            continue                                   # already alerted (e.g. after a restart)
        if kind == "NORMAL" and not SEND_NORMAL_ALERTS:
            continue
        d = ts.strftime("%Y-%m-%d")
        ref = (prev_close if CHANGE_VS == "prev_close" else day_open).get(d)
        bull, lines = build_alert(name, last, step, ref)
        title = "🚨 TRADE ALERT" if kind == "TRADE" else "🔔 NORMAL ALERT"
        if safe_post(title, "\n".join(lines), 0x2ECC71 if bull else 0xE74C3C):
            state[name] = key
            save_state(state)
            log(f">>> {kind} ALERT sent for {name}")
    return got_any


def send_summary(stats):
    ev = stats["events"]
    n_trade = sum(1 for e in ev if e[3] == "TRADE")
    if ev:
        lines = [f"Flips today: {len(ev)} (TRADE {n_trade}, NORMAL {len(ev) - n_trade})"]
        lines += [f"{n} {t} {'BULLISH' if b else 'BEARISH'} - {k}" for n, t, b, k in ev]
    else:
        lines = ["No Supertrend flips today"]
    safe_post("📊 Market closed - day summary", "\n".join(lines), 0x5865F2)


# =========================================================
# MAIN LOOP
# =========================================================
def run():
    check_config()
    log(f"Starting Supertrend live alert | {', '.join(INDEXES)} | {CANDLE_MIN}m | "
        f"ST({ST_PERIOD},{ST_MULT}) | ADX({ADX_PERIOD})>{ADX_MIN}")
    get_token()

    if SEND_TEST_MESSAGE:
        safe_post("🧪 Test message", "Supertrend live alert can reach Discord from this machine.", 0x5865F2)

    state = load_state()
    stats = {"date": None, "events": []}
    announced = False
    stale = 0
    stale_warned = None
    fails = 0

    while True:
        now = now_ist()
        close, trig = next_trigger(now)
        if EXIT_AFTER_MARKET and trig.normalize() != now.normalize():
            log("No more candles today. Exiting.")
            return

        if not announced:
            safe_post("✅ Supertrend live alert started",
                      f"Watching {', '.join(INDEXES)}. Next check: {close:%H:%M} candle close.", 0x5865F2)
            announced = True

        sleep_until(trig)

        day = close.strftime("%Y-%m-%d")
        if stats["date"] != day:
            stats = {"date": day, "events": []}
            stale = 0

        try:
            got = process_close(close, state, stats)
            fails = 0
            stale = 0 if got else stale + 1
            if stale >= 3 and stale_warned != day:
                safe_post("⚠️ No fresh data", "No new Nifty candles from Dhan for 3 checks in a row "
                          "(market holiday or a data problem).", 0xF39C12)
                stale_warned = day
        except Exception as exc:
            fails += 1
            log(f"ERROR: {exc}")
            if fails == 3:
                safe_post("⚠️ Alert script is failing", f"3 errors in a row. Last: {str(exc)[:300]}", 0xF39C12)

        if close == close.normalize() + pd.Timedelta(hours=15, minutes=30):
            if SEND_DAILY_SUMMARY and stats["events"] is not None and got_any_today(stats, stale):
                send_summary(stats)
            if EXIT_AFTER_MARKET:
                log("Market closed. Done for today.")
                return


def got_any_today(stats, stale):
    """Skip the summary on days with no data at all (holidays)."""
    return stale < 3


if __name__ == "__main__":
    run()