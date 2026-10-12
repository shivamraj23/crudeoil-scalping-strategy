# Project: Supertrend F&O intraday strategy

## Purpose
Scans the NSE F&O stock universe on a 10-minute timeframe and sends Discord alerts
when a Supertrend flip lines up with ADX / DMI filters.

## Layout
- `supertrend_fno_alerts.py` — the scanner (module + `main()`).
- `app.py` — Streamlit UI wrapping the scanner (imports it untouched).
- `crudeoil_onemin_alerts.py` — separate crude-oil mini 1-min alert script (untracked).
- `deploy/crudeoil-alerts.service`, `deploy/setup_ec2.sh` — systemd unit + one-time EC2 bootstrap for the crude bot.
- `.streamlit/config.toml`, `.streamlit/secrets.toml.example` — Streamlit theme + cloud secrets template.
- `requirements.txt` — pandas, numpy, requests, pyotp, python-dotenv, streamlit>=1.33, plotly.
- `.env` — local credentials (gitignored, never commit).

## Crude Oil Mini bot (crudeoil_onemin_alerts.py)
- 1-min MCX CRUDEOILM (mini) front-month futures, poll every 30s, two Discord webhooks
  (`CRUDEOIL_TRADE_WEBHOOK`, `CRUDEOIL_INDICATOR_WEBHOOK`).
- Trade entry = ADX(14) > 20 AND ADX(14) rising AND ADX(28) rising AND >= 4 of last 5 ADX(14)
  deltas non-decreasing (`ADX_CONFIRM_LOOKBACK=5`, `ADX_CONFIRM_MIN_NON_DECREASING=4`).
  Direction from +DI14 vs -DI14.
- Indicator alerts: ADX(14) crosses 20, ADX(28) starts rising/falling, direction flip.
- Contract roll: from the 15th of the month (`CONTRACT_ROLL_DAY=15`) switch to next month's
  contract, since Crude expires ~18-20 of the month.
- Trading window 09:00–00:00 IST (`TRADING_START_HOUR=9`, `TRADING_END_HOUR=24`). Outside it the bot
  sleeps (no Dhan calls, `SLEEP_CHUNK_SECONDS=60` idle checks) and posts wake/sleep notices
  to the indicator webhook on transitions.
- AWS hosting: `sudo bash deploy/setup_ec2.sh <repo-url>` then `journalctl -u crudeoil-alerts -f`.
  The unit requires a dedicated `crudeoil` system user and `/opt/crudeoil_scalping_strategy/.env`.

## Scanner config (supertrend_fno_alerts.py)
- Timeframe: 10m (resampled from Dhan 1m data). Warm-up 5 days, result window last 30 min.
- Supertrend(10, 3.0); ADX(14).
- Signal = Supertrend flip AND all of:
  - Absolute level: `ADX > 22` (`ADX_MIN`).
  - Slope: `>= 80%` of the last 5 ADX deltas rising (`ADX_SLOPE_LOOKBACK=5`, `ADX_SLOPE_RISING_PCT=0.80`).
  - DMI separation: leading DI ahead of the other by `>= 8` points (`DMI_SEPARATION_MIN=8`), direction-aware
    (+DI−-DI for bullish, -DI−+DI for bearish).
- Notification includes ADX, ADX min/max of the last 15 candles (`ADX_EXTREMES_LOOKBACK=15`), +DI, -DI, DMI gap.

## Env / secrets
- Required: `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET`, `DISCORD_WEBHOOK_SUPERTREND_FNO_STOCKS`.
- Optional: `DHAN_ACCESS_TOKEN` (skips TOTP login).
- Local: `.env`. Cloud: Streamlit `Settings > Secrets` (see `.streamlit/secrets.toml.example`).
- App maps Streamlit secrets into env before importing the scanner.

## Deployment status
- Git remote `fno` = `https://github.com/shivamraj23/intraday_fno_stocks_option_selling_strategy.git` (branch `main`).
- Pushed commit `e150430` ("Add F&O stock supertrend scanner and Streamlit UI"). Old remote `origin` is a different, older repo — do not push there.
- Streamlit Community Cloud deploy is a manual web step (main file `app.py`); pending on the user.

## Environment constraints
- Windows, PowerShell 5.1. No `gh` CLI and no GitHub token on this machine; GitHub repo creation must be done by the user.
- Run locally with `streamlit run app.py`.
