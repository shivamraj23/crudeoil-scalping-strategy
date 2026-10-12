#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/crudeoil_scalping_strategy"
REPO_URL="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash setup_ec2.sh [REPO_URL]" >&2
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip git
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip git
else
    echo "Unsupported package manager" >&2
    exit 1
fi

if [ -n "${REPO_URL}" ]; then
    rm -rf "${APP_DIR}"
    git clone "${REPO_URL}" "${APP_DIR}"
else
    mkdir -p "${APP_DIR}"
    cp -r . "${APP_DIR}"
fi

cd "${APP_DIR}"

if ! id -u crudeoil >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin crudeoil
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

chown -R crudeoil:crudeoil "${APP_DIR}"

if [ ! -f .env ]; then
    echo "WARNING: .env not found. Create ${APP_DIR}/.env with:"
    echo "  DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET,"
    echo "  CRUDEOIL_TRADE_WEBHOOK, CRUDEOIL_INDICATOR_WEBHOOK"
fi

cp -f ./deploy/crudeoil-alerts.service /etc/systemd/system/crudeoil-alerts.service
systemctl daemon-reload
systemctl enable --now crudeoil-alerts
systemctl --no-pager status crudeoil-alerts