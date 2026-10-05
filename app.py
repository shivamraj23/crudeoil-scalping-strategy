"""
Streamlit UI for the Supertrend option-selling bot.

The bot itself (supertrend_optionselling_10mintf.py) is NOT modified - it is imported
as a module, so this app re-uses exactly the same Dhan login, 1m -> 10m candle build,
Supertrend/ADX maths, strike picking and Discord alert code.

What the UI adds:
  * a Start / Stop monitor button - checks every 20s and alerts on each 10-minute
    candle close, for as long as the browser tab stays open
  * a "Run check once" button
  * live price / trend / ADX metrics, Supertrend + ADX chart, candle table
  * "Send day summary" and "Test Discord" buttons

Local:  streamlit run app.py          (credentials read from .env)
Cloud:  main file = app.py, credentials go in Settings > Secrets
        (see .streamlit/secrets.toml.example)
"""
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

HERE = Path(__file__).resolve().parent
BOT_SCRIPT = "supertrend_optionselling_10mintf"

TMP_DIR = Path(tempfile.gettempdir()) / "supertrend_streamlit"
STATS_FILE = TMP_DIR / "stats.json"

# newer Streamlit renamed use_container_width -> width; pick whatever this version takes
def _wide(fn):
    return {"width": "stretch"} if "width" in inspect.signature(fn).parameters \
        else {"use_container_width": True}


WIDE_BUTTON = _wide(st.button)
WIDE_FRAME = _wide(st.dataframe)
WIDE_CHART = _wide(st.plotly_chart)

SECRET_KEYS = {
    "DHAN_CLIENT_ID": ("dhan", "client_id"),
    "DHAN_PIN": ("dhan", "pin"),
    "DHAN_TOTP_SECRET": ("dhan", "totp_secret"),
    "DHAN_ACCESS_TOKEN": ("dhan", "access_token"),
    "DISCORD_WEBHOOK_URL": ("discord", "webhook_url"),
}


# =========================================================
# BOOTSTRAP: secrets -> env -> import the untouched bot
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
    bot = __import__(BOT_SCRIPT)

    # the bot keeps its Dhan token + "already alerted" state in files; on a cloud host the
    # checkout can be read-only and is wiped on redeploy, so keep them in the temp dir
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    bot.TOKEN_FILE = TMP_DIR / "dhan_token.json"
    bot.STATE_FILE = TMP_DIR / "alert_state.json"

    original_log = bot.log

    def log_to_ui(msg):
        original_log(msg)
        try:                                   # show the bot's own log lines in the UI
            lines = st.session_state["activity"]
            lines.append(f"{pd.Timestamp.now(tz=bot.IST):%d %b %H:%M:%S} | {msg}")
            del lines[:-200]
        except Exception:
            pass

    bot.log = log_to_ui
    return bot


st.set_page_config(page_title="Supertrend option selling", page_icon="chart_with_upwards_trend",
                   layout="wide")
bot = get_bot()
if "activity" not in st.session_state:
    st.session_state["activity"] = []


# =========================================================
# STATE (session_state + a temp file, so reloads keep the dedupe / day stats)
# =========================================================
def missing_vars():
    needed = {"DISCORD_WEBHOOK_URL": bot.WEBHOOK}
    if not (os.getenv("DHAN_ACCESS_TOKEN") or "").strip():
        needed.update({
            "DHAN_CLIENT_ID": bot.CLIENT_ID,
            "DHAN_PIN": bot.PIN,
            "DHAN_TOTP_SECRET": bot.TOTP_SECRET,
        })
    return [k for k, v in needed.items() if not v]


def load_stats(now):
    try:
        data = json.loads(STATS_FILE.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get("date") != now.strftime("%Y-%m-%d"):
        return {"date": now.strftime("%Y-%m-%d"), "events": []}
    data.setdefault("events", [])
    return data


def save_stats(stats):
    seen, events = set(), []
    for e in stats.get("events", []):
        key = (e[0], e[1])
        if key in seen:                        # a page reload can re-append the same flip
            continue
        seen.add(key)
        events.append(list(e))
    stats["events"] = events
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(stats))
    except Exception:
        pass


def last_closed_candle(now, lookback_days=6):
    """Most recent 10-minute candle close that has already closed (weekend/holiday safe)."""
    closes = []
    for back in range(lookback_days + 1):
        day = now.normalize() - pd.Timedelta(days=back)
        if day.weekday() < 5:
            closes.extend(bot.session_closes(day))
    ready = [c for c in closes if c + pd.Timedelta(seconds=bot.CHECK_DELAY_SEC) <= now]
    return max(ready) if ready else None


def analyse(close):
    """One check: the bot's own process_close (indicators + Discord + dedupe), then the
    same numbers once more for the chart and table."""
    stats = load_stats(bot.now_ist())
    bot.process_close(close, bot.load_state(), stats)
    save_stats(stats)

    now = bot.now_ist()
    start = now.normalize() - pd.Timedelta(days=bot.LOOKBACK_DAYS)
    data = {}
    for name, (sid, step) in bot.INDEXES.items():
        df1 = bot.fetch_1m(sid, start, now)
        res = bot.add_indicators(bot.make_candles(df1, now))
        if res.empty:
            continue
        day_open, prev_close = bot.day_refs(df1)
        last = res.iloc[-1]
        ref = (prev_close if bot.CHANGE_VS == "prev_close" else day_open).get(last.name.strftime("%Y-%m-%d"))
        data[name] = {
            "res": res,
            "spot": float(last["close"]),
            "change": (float(last["close"]) / ref - 1) * 100 if ref and pd.notna(ref) else None,
            "bull": last["trend"] == 1,
            "adx": None if pd.isna(last["adx"]) else float(last["adx"]),
            "close_time": last["end"],
            "ref": float(ref) if ref and pd.notna(ref) else None,
        }
    if not data:
        raise RuntimeError("Dhan returned no candles - market closed or a holiday?")
    return data


def summary_args(stats):
    ev = stats.get("events", [])
    n_trade = sum(1 for e in ev if e[3] == "TRADE")
    if ev:
        lines = [f"Flips today: {len(ev)} (TRADE {n_trade}, NORMAL {len(ev) - n_trade})"]
        lines += [f"{n} {t} {'BULLISH' if b else 'BEARISH'} - {k}" for n, t, b, k in ev]
    else:
        lines = ["No Supertrend flips today"]
    return ("Market closed - day summary", "\n".join(lines))


def run_check(close):
    """Run one check, remember the result. Returns (ok, message)."""
    try:
        st.session_state["data"] = analyse(close)
        st.session_state["last_close"] = close.isoformat()
        st.session_state["errors"] = 0
        st.session_state["last_error"] = None
        return True, f"Checked the {close:%H:%M} candle"
    except Exception as exc:
        bot.log(f"ERROR: {exc}")
        st.session_state["errors"] = st.session_state.get("errors", 0) + 1
        st.session_state["last_error"] = str(exc)
        return False, str(exc)


# =========================================================
# SIDEBAR (re-rendered on click; the monitor view below is the part that auto-refreshes)
# =========================================================
st.title("Supertrend option-selling alerts")
st.caption(
    f"Alert when Supertrend({bot.ST_PERIOD}, {bot.ST_MULT}) flips on a closed "
    f"{bot.CANDLE_MIN}-minute candle - ADX({bot.ADX_PERIOD}) > {bot.ADX_MIN} = TRADE alert, "
    f"below = NORMAL alert ({'on' if bot.SEND_NORMAL_ALERTS else 'off'})"
)

missing = missing_vars()
with st.sidebar:
    st.header("Monitor")
    if missing:
        st.error("Missing credentials:\n\n`" + "`\n`".join(missing) + "`\n\n"
                 "Set them in .env (local) or Settings > Secrets (cloud).")
    running = st.session_state.get("monitor_on", False)
    c1, c2 = st.columns(2)
    start = c1.button("Start", type="primary", **WIDE_BUTTON, disabled=bool(missing))
    stop = c2.button("Stop", **WIDE_BUTTON, disabled=not running)
    if start:
        st.session_state["monitor_on"] = True
        st.session_state["last_close"] = None
        st.session_state["errors"] = 0
    if stop:
        st.session_state["monitor_on"] = False
    if start or stop:
        st.rerun()

    if st.button("Run check once", **WIDE_BUTTON, disabled=bool(missing)):
        one = last_closed_candle(bot.now_ist())
        if one is None:
            st.error("No 10-minute candle has closed yet")
        else:
            with st.spinner("Fetching candles from Dhan..."):
                ok, msg = run_check(one)
            st.success(msg) if ok else st.error(msg)

    st.divider()
    st.header("Discord")
    if st.button("Send test message", **WIDE_BUTTON, disabled=bool(missing)):
        sent = bot.safe_post("Test message",
                             "Supertrend alert can reach Discord from this Streamlit app.", 0x5865F2)
        st.toast("Test message sent" if sent else "Send failed - see log")
    if st.button("Send day summary", **WIDE_BUTTON, disabled=bool(missing)):
        sent = bot.safe_post(*summary_args(load_stats(bot.now_ist())), 0x5865F2)
        st.toast("Summary sent" if sent else "Send failed - see log")

    st.divider()
    st.caption(f"Watching **{', '.join(bot.INDEXES)}** - strike step "
               f"{', '.join(str(v[1]) for v in bot.INDEXES.values())} - "
               f"strike offset {bot.STRIKE_OFFSET} - change vs {bot.CHANGE_VS}")


# =========================================================
# MONITOR VIEW - one fragment, so the timer refreshes the clock, the results and the log
# =========================================================
def draw_status(close, now):
    h1, h2 = st.columns([3, 2])
    if close is None:
        h1.metric("Last closed candle", "n/a")
    else:
        left = (close + pd.Timedelta(seconds=bot.CHECK_DELAY_SEC) - now).total_seconds()
        h1.metric("Last closed candle", f"{close:%H:%M} IST", None if left <= 0 else f"data in {left:,.0f}s")
    try:
        nxt, trig = bot.next_trigger(now)
        h2.metric("Next candle close", f"{nxt:%d %b %H:%M}",
                  f"in {(trig - now).total_seconds() / 60:,.1f} min")
    except Exception:
        h2.metric("Next candle close", "n/a")


def draw_results(data):
    for name, d in data.items():
        st.subheader(name)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Spot", f"{d['spot']:,.2f}", None if d["change"] is None else f"{d['change']:+.2f}%")
        m2.metric("Signal", "BULLISH - sell PUT" if d["bull"] else "BEARISH - sell CALL")
        m3.metric(f"ADX({bot.ADX_PERIOD})", "n/a" if d["adx"] is None else f"{d['adx']:.1f}")
        m4.metric("Candle closed", f"{d['close_time']:%d %b %H:%M}")

        res = d["res"].tail(90)
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=res.index, y=res["close"], name="Close",
                                 mode="lines", line=dict(color="#4c8bf5")), secondary_y=False)
        fig.add_trace(go.Scatter(x=res.index, y=res["supertrend"], name="Supertrend",
                                 mode="lines", line=dict(color="#f5a04c")), secondary_y=False)
        flipped = res[res["flip"].fillna(False).astype(bool)]
        for value, colour, label in ((1, "#2ecc71", "Flip BULLISH"), (-1, "#e74c3c", "Flip BEARISH")):
            f = flipped[flipped["trend"] == value]
            if not f.empty:
                fig.add_trace(go.Scatter(
                    x=f.index, y=f["close"], name=label, mode="markers",
                    marker=dict(color=colour, size=11,
                                symbol="triangle-up" if value == 1 else "triangle-down"),
                    hovertemplate="%{x|%H:%M} " + label + "<extra></extra>"), secondary_y=False)
        fig.add_trace(go.Scatter(x=res.index, y=res["adx"], name=f"ADX({bot.ADX_PERIOD})",
                                 mode="lines", line=dict(color="#9b59b6", width=1)), secondary_y=True)
        fig.add_hline(y=bot.ADX_MIN, line_dash="dash", line_color="#9b59b6",
                      annotation_text=f"ADX {bot.ADX_MIN}", secondary_y=True)
        fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10),
                          hovermode="x unified", legend=dict(orientation="h", y=1.12))
        fig.update_yaxes(title_text="Index", secondary_y=False)
        fig.update_yaxes(title_text="ADX", range=[0, max(60.0, float(res["adx"].max() or 0) * 1.2)],
                         secondary_y=True)
        st.plotly_chart(fig, **WIDE_CHART)

        table = res.tail(20).copy()
        table["dir"] = ["up" if t == 1 else "down" for t in table["trend"]]
        table["flip"] = table["flip"].map({True: "FLIP", False: ""})
        table = table[["dir", "open", "high", "low", "close", "supertrend", "adx", "flip", "end"]]
        table.columns = ["Dir", "Open", "High", "Low", "Close", "Supertrend", "ADX", "Flip", "Candle end"]
        st.dataframe(table.round(2), height=300, **WIDE_FRAME)

        _, lines = bot.build_alert(name, res.iloc[-1], bot.INDEXES[name][1], d["ref"])
        kind = "TRADE ALERT" if d["adx"] and d["adx"] > bot.ADX_MIN else "NORMAL ALERT"
        with st.expander(f"Discord message for the {d['close_time']:%H:%M} candle"):
            st.markdown(f"**{kind}**")
            st.markdown("\n".join("- " + line for line in lines))


def draw_day_stats(now):
    stats = load_stats(now)
    if stats["events"]:
        st.subheader(f"Flips today ({len(stats['events'])})")
        st.dataframe(pd.DataFrame([{"Index": n, "Time": t, "Direction": "BULLISH" if b else "BEARISH",
                                    "Type": k} for n, t, b, k in stats["events"]]),
                     hide_index=True, **WIDE_FRAME)
    activity = st.session_state["activity"]
    if activity:
        with st.expander("Log", expanded=bool(st.session_state.get("last_error"))):
            st.code("\n".join(activity[-40:]), language=None)


@st.fragment(run_every=20)
def monitor_view():
    now = bot.now_ist()
    close = last_closed_candle(now)
    draw_status(close, now)

    if not st.session_state.get("monitor_on", False):
        st.info("Monitor stopped. Press **Start** in the sidebar to check every candle close - "
                "it keeps going as long as this browser tab is open.")
    elif close is None:
        st.warning("No 10-minute candle has closed yet today.")
    elif st.session_state.get("last_close") == close.isoformat():
        st.caption(f"{close:%H:%M} candle already checked - waiting for the next one.")
    else:
        with st.spinner(f"Checking the {close:%H:%M} candle..."):
            run_check(close)
        if st.session_state.get("last_error"):
            st.warning(f"Check failed: {st.session_state['last_error']}")
        if st.session_state.get("errors", 0) >= 3:
            st.error(f"{st.session_state['errors']} failed checks in a row - "
                     f"last: {st.session_state.get('last_error')}")

    data = st.session_state.get("data")
    if data:
        draw_results(data)
    elif not missing:
        st.info("No check has run yet - press **Run check once** or **Start** in the sidebar.")
    draw_day_stats(now)


monitor_view()