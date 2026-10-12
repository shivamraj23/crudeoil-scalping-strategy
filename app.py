"""
Streamlit UI for the F&O stock Supertrend intraday scanner.

The scanner (supertrend_fno_alerts.py) is imported untouched, so this app re-uses
exactly the same Dhan login, F&O universe download, 1m -> 10m candle build,
Supertrend + ADX/DMI maths and Discord alerts.

What the UI adds:
  * a "Scan and alert" button - downloads the F&O universe, walks every stock and
    sends a Discord alert for each qualifying flip
  * a "Scan only" button - same scan without touching Discord
  * a results table (ADX, ADX min/max of the last 15 candles, +DI/-DI, DMI gap)
  * "Test Discord" and "Clear results" buttons
  * a live progress bar / per-stock status and an error counter

Local:  streamlit run app.py          (credentials read from .env)
Cloud:  main file = app.py, credentials go in Settings > Secrets
        (see .streamlit/secrets.toml.example)
"""
import inspect
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

HERE = Path(__file__).resolve().parent
BOT_SCRIPT = "supertrend_fno_alerts"


# newer Streamlit renamed use_container_width -> width; pick whatever this version takes
def _wide(fn):
    return {"width": "stretch"} if "width" in inspect.signature(fn).parameters \
        else {"use_container_width": True}


WIDE_BUTTON = _wide(st.button)
WIDE_FRAME = _wide(st.dataframe)
WIDE_DOWNLOAD = _wide(st.download_button)

SECRET_KEYS = {
    "DHAN_CLIENT_ID": ("dhan", "client_id"),
    "DHAN_ACCESS_TOKEN": ("dhan", "access_token"),
    "DHAN_PIN": ("dhan", "pin"),
    "DHAN_TOTP_SECRET": ("dhan", "totp_secret"),
    "DISCORD_WEBHOOK_SUPERTREND_FNO_STOCKS": ("discord", "supertrend_fno_stocks"),
}


# =========================================================
# BOOTSTRAP: secrets -> env -> import the untouched scanner
# =========================================================
def load_secrets():
    """st.secrets (cloud) into os.environ. A real env var / .env always wins."""
    try:
        secrets = st.secrets.to_dict()
    except Exception:
        secrets = {}
    for key, (section, field) in SECRET_KEYS.items():
        if os.environ.get(key):
            continue
        value = secrets.get(key)
        if not value and isinstance(secrets.get(section), dict):
            value = secrets[section].get(field)
        if value:
            os.environ[key] = str(value)


@st.cache_resource
def get_bot():
    load_secrets()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return __import__(BOT_SCRIPT)


st.set_page_config(
    page_title="F&O Supertrend intraday alerts",
    page_icon="chart_with_upwards_trend",
    layout="wide",
)

try:
    bot = get_bot()
except Exception as exc:
    st.error(
        "Could not load the scanner. Check that Dhan credentials and the Discord "
        f"webhook are set (Settings > Secrets on the cloud, .env locally).\n\n`{exc}`"
    )
    st.stop()


# =========================================================
# HELPERS
# =========================================================
def missing_vars():
    needed = {
        "DHAN_CLIENT_ID": bot.DHAN_CLIENT_ID,
        "DISCORD_WEBHOOK_SUPERTREND_FNO_STOCKS": bot.DISCORD_WEBHOOK,
    }
    if not (os.getenv("DHAN_ACCESS_TOKEN") or "").strip():
        needed.update({
            "DHAN_PIN": bot.DHAN_PIN,
            "DHAN_TOTP_SECRET": bot.DHAN_TOTP_SECRET,
        })
    return [k for k, v in needed.items() if not v]


def signals_frame(signals):
    rows = []
    for s in signals:
        rows.append({
            "Symbol": s["symbol"],
            "Direction": s["direction"],
            "Signal candle": s["time"].strftime("%d-%b %H:%M"),
            "Close": round(float(s["close"]), 2),
            "ADX": round(float(s["adx"]), 2),
            "ADX min (15)": round(float(s["adx_min_15"]), 2),
            "ADX max (15)": round(float(s["adx_max_15"]), 2),
            "+DI": round(float(s["plus_di"]), 2),
            "-DI": round(float(s["minus_di"]), 2),
            "DMI sep": round(float(s["dmi_gap"]), 2),
        })
    if not rows:
        return pd.DataFrame(columns=[
            "Symbol", "Direction", "Signal candle", "Close", "ADX",
            "ADX min (15)", "ADX max (15)", "+DI", "-DI", "DMI sep",
        ])
    return pd.DataFrame(rows)


def run_scan(send_alerts, progress, status):
    """Walk the whole F&O universe. Returns (signals, scanned, errors)."""
    status.info("Downloading the Dhan instrument master...")
    progress.progress(0.0, text="Downloading the Dhan instrument master...")

    stocks = bot.get_fno_stocks()
    total = len(stocks)
    signals = []
    errors = 0

    for i, (_, row) in enumerate(stocks.iterrows()):
        symbol = str(row["UNDERLYING_SYMBOL"])
        security_id = str(row["UNDERLYING_SECURITY_ID"])

        text = f"Scanning {i + 1}/{total}: {symbol}"
        progress.progress((i + 1) / total, text=text)
        status.update(label=text)

        try:
            df = bot.fetch_intraday_data(security_id)
            if df is not None:
                found = bot.analyze_stock(symbol, security_id, df)
                if found:
                    signals.extend(found)
                    if send_alerts:
                        for signal in found:
                            bot.send_discord(signal)
                            time.sleep(0.5)
        except Exception:
            errors += 1

        time.sleep(bot.REQUEST_DELAY)

    signals.sort(key=lambda x: x["time"], reverse=True)
    return signals, total, errors


def send_test_message():
    message = (
        "🧪 **Test message**\n"
        "The F&O Supertrend scanner can reach Discord from this Streamlit app."
    )
    try:
        res = requests.post(bot.DISCORD_WEBHOOK, json={"content": message}, timeout=15)
        return res.status_code in (200, 204), res
    except Exception as exc:
        return False, exc


# =========================================================
# SIDEBAR
# =========================================================
st.title("F&O Supertrend intraday alerts")
st.caption(
    f"10-minute Supertrend({bot.ST_PERIOD}, {bot.ST_MULT}) flip + ADX({bot.ADX_PERIOD}) > "
    f"{bot.ADX_MIN} + ADX slope (>= {bot.ADX_SLOPE_RISING_PCT:.0%} of last "
    f"{bot.ADX_SLOPE_LOOKBACK} rising) + DMI separation >= {bot.DMI_SEPARATION_MIN:.0f}"
)

missing = missing_vars()
if "signals" not in st.session_state:
    st.session_state["signals"] = None

with st.sidebar:
    st.header("Scanner")

    if missing:
        st.error(
            "Missing credentials:\n\n`" + "`\n`".join(missing) + "`\n\n"
            "Set them in .env (local) or Settings > Secrets (cloud)."
        )

    disabled = bool(missing) or st.session_state.get("scanning", False)

    scan_alert = st.button(
        "Scan and alert", type="primary", **WIDE_BUTTON, disabled=disabled
    )
    scan_only = st.button("Scan only (no Discord)", **WIDE_BUTTON, disabled=disabled)

    if st.button("Clear results", **WIDE_BUTTON):
        st.session_state["signals"] = None
        st.rerun()

    st.divider()
    st.header("Discord")
    if st.button("Send test message", **WIDE_BUTTON, disabled=bool(missing)):
        ok, info = send_test_message()
        st.toast("Test message sent" if ok else f"Send failed: {info}")

    st.divider()
    st.caption(
        f"Result window: last {bot.RESULT_WINDOW_MIN} min - warm-up {bot.WARMUP_DAYS} days - "
        f"universe: NSE F&O stocks (FUTSTK/OPTSTK)"
    )
    st.caption("A full scan walks every F&O stock (~1s each), so it can take a few minutes.")


# =========================================================
# MAIN
# =========================================================
if scan_alert or scan_only:
    st.session_state["scanning"] = True
    progress = st.progress(0.0, text="Starting...")
    status = st.status("Scanning the F&O universe...", expanded=True)
    try:
        signals, scanned, errors = run_scan(scan_alert, progress, status)
        st.session_state["signals"] = signals
        st.session_state["scanned"] = scanned
        st.session_state["errors"] = errors
        st.session_state["alerted"] = scan_alert
        status.update(
            label=f"Done - scanned {scanned} stocks, {len(signals)} signal(s), "
                  f"{errors} error(s)",
            state="complete",
            expanded=False,
        )
    except Exception as exc:
        status.update(label=f"Scan failed: {exc}", state="error")
    finally:
        progress.empty()
        st.session_state["scanning"] = False

signals = st.session_state.get("signals")
if signals is not None:
    scanned = st.session_state.get("scanned", 0)
    errors = st.session_state.get("errors", 0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks scanned", f"{scanned}")
    c2.metric("Signals found", f"{len(signals)}")
    c3.metric("Fetch errors", f"{errors}")

    if signals:
        if st.session_state.get("alerted"):
            st.success(f"Discord alerts sent for {len(signals)} signal(s).")
        frame = signals_frame(signals)
        st.dataframe(frame, hide_index=True, **WIDE_FRAME)
        st.download_button(
            "Download CSV",
            frame.to_csv(index=False).encode("utf-8"),
            file_name=f"fno_supertrend_signals_{pd.Timestamp.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
            **WIDE_DOWNLOAD,
        )
    else:
        st.info(
            f"No qualifying signal in the last {bot.RESULT_WINDOW_MIN} minutes. "
            "A signal needs a Supertrend flip plus every ADX/DMI filter to line up."
        )
else:
    st.info("Press **Scan and alert** in the sidebar to look for signals.")
