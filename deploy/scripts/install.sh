#!/usr/bin/env bash
#
# GenWatch installer for Raspberry Pi OS Bookworm (64-bit) on Pi 4 or Pi 5.
#
# Idempotent — safe to re-run for upgrades.
#
#   1. Verify root, OS, and Pi model
#   2. Install apt prerequisites (python venv, build tools, nodejs)
#   3. Create system user `genwatch` (in dialout for serial access)
#   4. Build the frontend bundle (if not already present)
#   5. Install Python deps into /opt/genwatch/venv
#   6. Copy backend package to /opt/genwatch
#   7. Copy built frontend to /usr/share/genwatch/ui
#   8. Install udev rule for /dev/genwatch-modbus symlink
#   9. Provision /etc/genwatch/config.yaml (with auto-generated jwt_secret)
#  10. Interactively prompt for admin password + Lantronix host
#  11. Install + enable systemd unit, run doctor, start the service
#
# Run from the repository root:
#   sudo deploy/scripts/install.sh
#
# Non-interactive / unattended installs: set these env vars and use sudo -E:
#   GENWATCH_ADMIN_PASSWORD=...    skips the password prompt
#   GENWATCH_LANTRONIX_HOST=...    skips the Lantronix host prompt (default 192.168.1.249)
#   GENWATCH_LANTRONIX_PORT=...    default 10001
#   GENWATCH_TRANSPORT=tcp|serial  default tcp
#   GENWATCH_NONINTERACTIVE=1      explicitly disables all prompts
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/genwatch
DATA_DIR=/var/lib/genwatch
ETC_DIR=/etc/genwatch
UI_DIR=/usr/share/genwatch/ui
LOG_DIR=/var/log/genwatch
USER=genwatch
UDEV_RULE=/etc/udev/rules.d/99-genwatch-modbus.rules
OLD_UDEV_RULE=/etc/udev/rules.d/99-genwatch-rs485.rules
UNIT_FILE=/etc/systemd/system/genwatch.service

# ─── helpers ──────────────────────────────────────────────────────────────
log()  { printf "\033[1;36m[genwatch]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[genwatch]\033[0m WARN: %s\n" "$*" >&2; }
err()  { printf "\033[1;31m[genwatch]\033[0m ERROR: %s\n" "$*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "must run as root (try: sudo $0)"
    exit 1
  fi
}

detect_pi() {
  local model="unknown"
  if [[ -r /proc/device-tree/model ]]; then
    # Pi model strings end with a NUL; tr scrubs it.
    model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)
  fi
  echo "$model"
}

detect_os() {
  local id="unknown" codename="unknown"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    id=${ID:-unknown}
    codename=${VERSION_CODENAME:-unknown}
  fi
  echo "${id}-${codename}"
}

ensure_apt_pkgs() {
  local pkgs=("$@")
  local missing=()
  for p in "${pkgs[@]}"; do
    if ! dpkg -s "$p" &>/dev/null; then
      missing+=("$p")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    log "Installing apt packages: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
  fi
}

retry() {
  local n=0 max=4 delay=2
  until "$@"; do
    n=$(( n + 1 ))
    if (( n >= max )); then
      err "command failed after $n attempts: $*"
      return 1
    fi
    warn "command failed, retrying in ${delay}s: $*"
    sleep "$delay"
    delay=$(( delay * 2 ))
  done
}

interactive() {
  # Prompts are enabled when stdin is a TTY and GENWATCH_NONINTERACTIVE isn't set.
  [[ -t 0 ]] && [[ -z "${GENWATCH_NONINTERACTIVE:-}" ]]
}

# Read a value with a default. Usage: ask_default VAR "Prompt" "default-value"
ask_default() {
  local __var=$1 prompt=$2 default=$3 reply
  read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  printf -v "$__var" '%s' "${reply:-$default}"
}

# Read a yes/no answer. Usage: ask_yes_no "Prompt?" default_yn  → returns 0 for yes, 1 for no
ask_yes_no() {
  local prompt=$1 default=${2:-n} reply hint
  case "$default" in
    y|Y) hint="[Y/n]" ;;
    *)   hint="[y/N]" ;;
  esac
  read -r -p "$prompt $hint: " reply </dev/tty || reply=""
  reply=${reply:-$default}
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

# Read a password silently, twice, and confirm they match. Prints the password
# on stdout (and only stdout) — caller is responsible for keeping it secret.
read_password_twice() {
  local pw1 pw2
  while true; do
    read -r -s -p "Choose an admin password (min 8 chars): " pw1 </dev/tty
    echo >/dev/tty
    if (( ${#pw1} < 8 )); then
      printf '  password too short — try again.\n' >/dev/tty
      continue
    fi
    read -r -s -p "Confirm password: " pw2 </dev/tty
    echo >/dev/tty
    if [[ "$pw1" != "$pw2" ]]; then
      printf '  passwords did not match — try again.\n' >/dev/tty
      continue
    fi
    printf '%s' "$pw1"
    return 0
  done
}

# Write a value into a top-level YAML key (one-level depth). Idempotent.
# Usage: yaml_set <file> <parent_key> <child_key> <new_value>
yaml_set_child() {
  local file=$1 parent=$2 child=$3 value=$4
  # Use Python to do this safely — sed gets tangled on quoting.
  "$APP_DIR/venv/bin/python" - "$file" "$parent" "$child" "$value" <<'PY'
import sys, yaml, pathlib
path, parent, child, value = sys.argv[1:]
p = pathlib.Path(path)
data = yaml.safe_load(p.read_text()) or {}
# Best-effort type coercion: bare ints stay ints
try:
    if value.isdigit():
        value = int(value)
except AttributeError:
    pass
data.setdefault(parent, {})[child] = value
with p.open("w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
PY
}

# Write a top-level scalar key (transport, mock, etc.). Idempotent.
yaml_set_root() {
  local file=$1 key=$2 value=$3
  "$APP_DIR/venv/bin/python" - "$file" "$key" "$value" <<'PY'
import sys, yaml, pathlib
path, key, value = sys.argv[1:]
p = pathlib.Path(path)
data = yaml.safe_load(p.read_text()) or {}
data[key] = value
with p.open("w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
PY
}

# TCP reachability probe. Returns 0 if connect succeeds within $3 seconds.
probe_tcp() {
  local host=$1 port=$2 timeout=${3:-3}
  "$APP_DIR/venv/bin/python" - "$host" "$port" "$timeout" <<'PY' 2>/dev/null
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(timeout)
try:
    s.connect((host, port))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

# ─── pre-flight ───────────────────────────────────────────────────────────
require_root

PI_MODEL=$(detect_pi)
OS_TAG=$(detect_os)

log "Repository root: $REPO_ROOT"
log "Host:            $PI_MODEL"
log "OS:              $OS_TAG"

case "$OS_TAG" in
  debian-bookworm|raspbian-bookworm|debian-trixie|raspbian-trixie)
    ;;
  *)
    warn "Unsupported OS tag '$OS_TAG' — proceeding anyway. Bookworm or newer is recommended."
    ;;
esac

if [[ "$PI_MODEL" != *"Raspberry Pi"* ]]; then
  warn "Host doesn't look like a Raspberry Pi ('$PI_MODEL'). Continuing anyway."
fi

# ─── 1. apt deps ──────────────────────────────────────────────────────────
ensure_apt_pkgs \
  python3-venv python3-pip python3-dev \
  build-essential pkg-config libffi-dev \
  rsync curl ca-certificates \
  nodejs npm

# Verify node version (Vite 6 requires >= 18)
NODE_VER=$(node --version 2>/dev/null | sed 's/v//; s/\..*//')
if [[ -z "$NODE_VER" || "$NODE_VER" -lt 18 ]]; then
  warn "node.js >= 18 not available in apt (got ${NODE_VER:-none}). Attempting to install nodejs 20 via NodeSource."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
fi

log "node: $(node --version 2>/dev/null || echo not-installed)"
log "npm:  $(npm --version 2>/dev/null || echo not-installed)"

# ─── 2. system user ───────────────────────────────────────────────────────
if ! id -u "$USER" &>/dev/null; then
  log "Creating system user $USER (in dialout for serial access)"
  useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" \
          --groups dialout "$USER"
else
  usermod -a -G dialout "$USER" || true
fi

# ─── 3. directories ───────────────────────────────────────────────────────
log "Creating directories"
install -d -m 0755 -o "$USER" -g "$USER" "$APP_DIR" "$DATA_DIR" "$LOG_DIR"
install -d -m 0755 "$UI_DIR"
install -d -m 0750 -o "$USER" -g "$USER" "$ETC_DIR"

# ─── 4. Frontend bundle ───────────────────────────────────────────────────
# Build if dist is missing or older than any source file.
build_needed=0
if [[ ! -f "$REPO_ROOT/frontend/dist/index.html" ]]; then
  build_needed=1
else
  newest_src=$(find "$REPO_ROOT/frontend/src" "$REPO_ROOT/frontend/package.json" \
                    "$REPO_ROOT/frontend/vite.config.ts" "$REPO_ROOT/frontend/index.html" \
                    -type f -printf "%T@\n" 2>/dev/null | sort -nr | head -1)
  newest_dist=$(stat -c "%Y" "$REPO_ROOT/frontend/dist/index.html" 2>/dev/null || echo 0)
  if [[ -n "$newest_src" ]] && awk -v a="$newest_src" -v b="$newest_dist" 'BEGIN{exit !(a > b)}'; then
    build_needed=1
  fi
fi

if (( build_needed )); then
  log "Building frontend bundle (this can take ~30 s on a Pi 4)"
  pushd "$REPO_ROOT/frontend" >/dev/null
  npm install --no-audit --no-fund --silent
  npm run build
  popd >/dev/null
fi

log "Installing frontend bundle to $UI_DIR"
rsync -a --delete "$REPO_ROOT/frontend/dist/" "$UI_DIR/"

# ─── 5. Python venv + deps ────────────────────────────────────────────────
if [[ ! -d "$APP_DIR/venv" ]]; then
  log "Creating virtualenv at $APP_DIR/venv"
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
retry "$APP_DIR/venv/bin/pip" install --quiet --upgrade -r "$REPO_ROOT/backend/requirements.txt"

log "Copying backend package to $APP_DIR/genwatch"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='.pytest_cache' \
  "$REPO_ROOT/backend/genwatch/" "$APP_DIR/genwatch/"

# Create a launcher symlink so `sudo -u genwatch genwatch <cmd>` works.
ln -sf "$APP_DIR/venv/bin/python" "$APP_DIR/python"
cat >"$APP_DIR/genwatch.sh" <<'SH'
#!/usr/bin/env bash
# Pin PYTHONPATH so `python -m genwatch` works from any cwd.
exec env PYTHONPATH=/opt/genwatch /opt/genwatch/venv/bin/python -m genwatch "$@"
SH
chmod +x "$APP_DIR/genwatch.sh"
ln -sf "$APP_DIR/genwatch.sh" /usr/local/bin/genwatch

chown -R "$USER:$USER" "$APP_DIR"

# ─── 6. udev rule ─────────────────────────────────────────────────────────
log "Installing udev rule for /dev/genwatch-modbus"
install -m 0644 "$REPO_ROOT/deploy/udev/99-genwatch-modbus.rules" "$UDEV_RULE"
# Clean up old rs485-named rule from earlier installs.
if [[ -f "$OLD_UDEV_RULE" ]]; then
  rm -f "$OLD_UDEV_RULE"
fi
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true

# ─── 7. config.yaml ───────────────────────────────────────────────────────
FIRST_INSTALL=0
NEEDS_PASSWORD=0
if [[ ! -f "$ETC_DIR/config.yaml" ]]; then
  log "Provisioning $ETC_DIR/config.yaml with a random jwt_secret"
  install -m 0640 -o "$USER" -g "$USER" "$REPO_ROOT/deploy/config.yaml.example" "$ETC_DIR/config.yaml"
  SECRET=$("$APP_DIR/venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')
  sed -i "s|^  jwt_secret:.*$|  jwt_secret: \"$SECRET\"|" "$ETC_DIR/config.yaml"
  FIRST_INSTALL=1
  NEEDS_PASSWORD=1
else
  log "Config already exists at $ETC_DIR/config.yaml — preserving operator edits"
  if grep -q '^  jwt_secret: "REPLACE_ME"' "$ETC_DIR/config.yaml"; then
    warn "jwt_secret is still REPLACE_ME in $ETC_DIR/config.yaml"
  fi
  if grep -q '^  admin_password_hash: "REPLACE_ME"' "$ETC_DIR/config.yaml"; then
    NEEDS_PASSWORD=1
  fi
fi

# ─── 8. interactive config: link + admin password ────────────────────────
# Only prompt on a first install (or if values are still placeholders), and
# only when we have a TTY. Env vars override prompts for unattended installs.

if (( FIRST_INSTALL )); then
  echo
  log "─── Link configuration ──────────────────────────────────"

  # Transport selection. Default tcp.
  TRANSPORT_DEFAULT=${GENWATCH_TRANSPORT:-tcp}
  if interactive && [[ -z "${GENWATCH_TRANSPORT:-}" ]]; then
    if ask_yes_no "Use TCP bridge (Lantronix / Moxa / ser2net) for the H-100 link?" y; then
      TRANSPORT=tcp
    else
      TRANSPORT=serial
    fi
  else
    TRANSPORT="$TRANSPORT_DEFAULT"
  fi
  yaml_set_root "$ETC_DIR/config.yaml" transport "$TRANSPORT"
  log "Transport: $TRANSPORT"

  if [[ "$TRANSPORT" == "tcp" ]]; then
    LAN_HOST=${GENWATCH_LANTRONIX_HOST:-192.168.1.249}
    LAN_PORT=${GENWATCH_LANTRONIX_PORT:-10001}
    if interactive; then
      ask_default LAN_HOST "Lantronix host (IP or hostname)" "$LAN_HOST"
      ask_default LAN_PORT "Lantronix TCP port" "$LAN_PORT"
    fi
    yaml_set_child "$ETC_DIR/config.yaml" modbus_tcp host "$LAN_HOST"
    yaml_set_child "$ETC_DIR/config.yaml" modbus_tcp port "$LAN_PORT"

    log "Probing $LAN_HOST:$LAN_PORT ..."
    if probe_tcp "$LAN_HOST" "$LAN_PORT" 3; then
      log "TCP reachable — bridge is listening."
    else
      warn "TCP NOT reachable at $LAN_HOST:$LAN_PORT (timeout/refused)."
      warn "  • Confirm the bridge is powered and on this LAN: ping $LAN_HOST"
      warn "  • Confirm Channel 1 → Connection → passive raw-TCP on port $LAN_PORT"
      warn "  • If a Windows machine is using this Lantronix via CPR, stop the CPR session first."
      warn "Install continues — fix the network, then run: sudo systemctl restart genwatch"
    fi
  else
    SERIAL_DEV=${GENWATCH_SERIAL_DEVICE:-/dev/genwatch-modbus}
    if interactive; then
      ask_default SERIAL_DEV "Serial device path" "$SERIAL_DEV"
    fi
    yaml_set_child "$ETC_DIR/config.yaml" serial device "$SERIAL_DEV"
  fi
fi

# ─── Admin password ───────────────────────────────────────────────────────
if (( NEEDS_PASSWORD )); then
  echo
  log "─── Admin password ──────────────────────────────────────"
  ADMIN_PASSWORD="${GENWATCH_ADMIN_PASSWORD:-}"
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    if interactive; then
      ADMIN_PASSWORD=$(read_password_twice)
    else
      warn "No GENWATCH_ADMIN_PASSWORD set and no TTY — leaving admin_password_hash unset."
    fi
  fi
  if [[ -n "$ADMIN_PASSWORD" ]]; then
    log "Generating bcrypt hash and writing to config.yaml"
    HASH=$(PYTHONPATH="$APP_DIR" "$APP_DIR/venv/bin/python" -m genwatch hash "$ADMIN_PASSWORD")
    yaml_set_child "$ETC_DIR/config.yaml" auth admin_password_hash "$HASH"
    unset ADMIN_PASSWORD
    NEEDS_PASSWORD=0
  fi
fi

# Make sure config ownership is right after our edits.
chown "$USER:$USER" "$ETC_DIR/config.yaml"
chmod 0640 "$ETC_DIR/config.yaml"

# ─── 9. systemd unit ──────────────────────────────────────────────────────
log "Installing systemd unit"
install -m 0644 "$REPO_ROOT/deploy/systemd/genwatch.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable genwatch.service

# ─── 10. pre-flight check ─────────────────────────────────────────────────
log "Running pre-flight diagnostics"
set +e
sudo -u "$USER" \
  GENWATCH_CONFIG_PATH="$ETC_DIR/config.yaml" \
  GENWATCH_DATA_DIR="$DATA_DIR" \
  PYTHONPATH="$APP_DIR" \
  "$APP_DIR/venv/bin/python" -m genwatch doctor
DOCTOR_RC=$?
set -e

# ─── 11. start service ────────────────────────────────────────────────────
if (( NEEDS_PASSWORD )); then
  echo
  echo "============================================================"
  echo "  ⚠  ADMIN PASSWORD NOT SET"
  echo
  echo "  Generate a bcrypt hash and paste into the config:"
  echo
  echo "    sudo genwatch hash 'your-strong-password'"
  echo "    sudo nano $ETC_DIR/config.yaml      # paste into admin_password_hash"
  echo "    sudo systemctl restart genwatch"
  echo "============================================================"
  echo
  log "Skipping systemctl start — set admin_password_hash first."
else
  if systemctl is-active --quiet genwatch.service; then
    log "Restarting genwatch.service"
    systemctl restart genwatch.service
  else
    log "Starting genwatch.service"
    if ! systemctl start genwatch.service; then
      err "service failed to start — see: journalctl -u genwatch -e"
      exit 1
    fi
  fi
  systemctl --no-pager status genwatch.service | head -12 || true
fi

# Detect primary IPv4 for the friendly URL hint
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
log "Install complete."
if (( DOCTOR_RC == 0 )) && (( ! NEEDS_PASSWORD )); then
  log "Link is live. Telemetry is flowing."
elif (( DOCTOR_RC != 0 )); then
  warn "doctor reported a problem — see output above. The service is enabled"
  warn "and will retry once the link is fixed."
fi
if [[ -n "$HOST_IP" ]]; then
  echo
  log "Open the operator console:"
  log "    http://${HOST_IP}:8000"
fi
echo
log "Useful commands:"
log "    sudo systemctl status genwatch     # service state"
log "    sudo journalctl -u genwatch -ef    # live log"
log "    sudo genwatch doctor               # re-run diagnostics"
log "    sudo nano $ETC_DIR/config.yaml     # edit config (restart service after)"
echo "============================================================"
