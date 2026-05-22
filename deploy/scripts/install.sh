#!/usr/bin/env bash
#
# GenWatch installer for Raspberry Pi OS (Debian 12 bookworm or later).
#
# Idempotent — safe to re-run for upgrades. Performs:
#   1. Create system user `genwatch` (in dialout group for serial access)
#   2. Install python deps in /opt/genwatch/venv (system site-packages OFF)
#   3. Copy backend code to /opt/genwatch
#   4. Copy built frontend to /usr/share/genwatch/ui
#   5. Install systemd unit, enable + start
#   6. Provision /etc/genwatch/config.yaml if missing (with random secrets)
#   7. Provision /var/lib/genwatch for the SQLite DB
#
# Run from the repository root as root:
#   sudo deploy/scripts/install.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/genwatch
DATA_DIR=/var/lib/genwatch
ETC_DIR=/etc/genwatch
UI_DIR=/usr/share/genwatch/ui
USER=genwatch

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
  fi
}

log() { printf "\033[1;36m[genwatch]\033[0m %s\n" "$*"; }

require_root

log "Repository root: $REPO_ROOT"

# ─── 1. user ──────────────────────────────────────────────────────────────
if ! id -u "$USER" &>/dev/null; then
  log "Creating system user $USER (in dialout for serial access)"
  useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" \
          --groups dialout "$USER"
else
  # Make sure existing user has serial access
  usermod -a -G dialout "$USER" || true
fi

# ─── 2. directories ───────────────────────────────────────────────────────
log "Creating directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR" "$UI_DIR" /var/log/genwatch
chown -R "$USER:$USER" "$DATA_DIR" /var/log/genwatch
chown -R "$USER:$USER" "$APP_DIR"

# ─── 3. python deps + backend ─────────────────────────────────────────────
log "Installing Python dependencies"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3-venv python3-pip python3-dev build-essential

if [[ ! -d "$APP_DIR/venv" ]]; then
  log "Creating virtualenv at $APP_DIR/venv"
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet --upgrade -r "$REPO_ROOT/backend/requirements.txt"

log "Copying backend package"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
  "$REPO_ROOT/backend/genwatch/" "$APP_DIR/genwatch/"

chown -R "$USER:$USER" "$APP_DIR"

# ─── 4. frontend bundle ───────────────────────────────────────────────────
if [[ -d "$REPO_ROOT/frontend/dist" ]]; then
  log "Installing frontend bundle to $UI_DIR"
  rsync -a --delete "$REPO_ROOT/frontend/dist/" "$UI_DIR/"
else
  log "WARN: frontend/dist not built — run 'npm --prefix frontend run build' first"
fi

# ─── 5. config + secrets ──────────────────────────────────────────────────
if [[ ! -f "$ETC_DIR/config.yaml" ]]; then
  log "Provisioning $ETC_DIR/config.yaml with a random jwt_secret"
  cp "$REPO_ROOT/deploy/config.yaml.example" "$ETC_DIR/config.yaml"
  SECRET=$("$APP_DIR/venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')
  sed -i "s|^  jwt_secret:.*$|  jwt_secret: \"$SECRET\"|" "$ETC_DIR/config.yaml"
  chown "$USER:$USER" "$ETC_DIR/config.yaml"
  chmod 640 "$ETC_DIR/config.yaml"
  echo
  echo "============================================================"
  echo "  Now run:"
  echo "    sudo $APP_DIR/venv/bin/python -m genwatch hash 'yourpass'"
  echo "  and paste the output into admin_password_hash in:"
  echo "    $ETC_DIR/config.yaml"
  echo "============================================================"
  echo
else
  log "Config already exists at $ETC_DIR/config.yaml — not touching"
fi

# ─── 6. systemd unit ──────────────────────────────────────────────────────
log "Installing systemd unit"
install -m 0644 "$REPO_ROOT/deploy/systemd/genwatch.service" /etc/systemd/system/genwatch.service
systemctl daemon-reload
systemctl enable genwatch.service

if systemctl is-active --quiet genwatch.service; then
  log "Restarting genwatch.service"
  systemctl restart genwatch.service
else
  log "Starting genwatch.service"
  systemctl start genwatch.service || {
    echo "service failed to start — check journalctl -u genwatch -e"
    exit 1
  }
fi

log "Status:"
systemctl --no-pager status genwatch.service | head -12

echo
log "Install complete. Browse to http://$(hostname -I | awk '{print $1}'):8000"
