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
#  10. Install + enable systemd unit
#
# Run from the repository root:
#   sudo deploy/scripts/install.sh
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
HWWD_FILE=/etc/systemd/system.conf.d/10-genwatch-hwwatchdog.conf

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
    # Pre-Bookworm distros ship systemd <243, which doesn't understand
    # `RebootWatchdogSec` in our hwwatchdog drop-in (and may reject
    # other modern hardening directives). Continuing on a "looks
    # plausible" install previously left those features silently
    # inactive — the operator only found out via journalctl. Fail
    # fast with an actionable message instead. Override at your own
    # risk by setting GENWATCH_ALLOW_UNSUPPORTED_OS=1.
    if [[ "${GENWATCH_ALLOW_UNSUPPORTED_OS:-0}" == "1" ]]; then
      warn "Unsupported OS tag '$OS_TAG' but GENWATCH_ALLOW_UNSUPPORTED_OS=1 — proceeding."
    else
      err "Unsupported OS tag '$OS_TAG'. GenWatch supports Raspberry Pi OS / Debian Bookworm or Trixie."
      err "Set GENWATCH_ALLOW_UNSUPPORTED_OS=1 to override at your own risk (hardware watchdog may not configure correctly)."
      exit 1
    fi
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

# `install -d` doesn't reach inside a pre-existing dir, so any db.sqlite
# / *.log / config.yaml left over from a previous run (possibly created
# as root by a manual `genwatch doctor`) would keep its old owner and
# break the service with "attempt to write a readonly database". Force
# consistent ownership on every install.
#
# DATA_DIR / LOG_DIR: recursive chown is fine — we own everything in
# them (SQLite DBs, logs we wrote). ETC_DIR: only chown files that
# WE manage. An operator may drop a sensitive file (root:root 0600)
# into /etc/genwatch (e.g. a hand-managed secrets shim, an Ansible-
# placed override). Recursively chowning to genwatch:genwatch would
# silently widen its trust boundary, exposing anything inside to a
# future RCE in the service. Scoping the chown to our own files
# preserves operator-placed artifacts intact.
chown -R "$USER:$USER" "$DATA_DIR" "$LOG_DIR"
chown "$USER:$USER" "$ETC_DIR"
if [[ -f "$ETC_DIR/config.yaml" ]]; then
  chown "$USER:$USER" "$ETC_DIR/config.yaml"
fi

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
  # `npm ci` (rather than `npm install`) requires package-lock.json,
  # refuses to write to it, and installs exactly the locked versions
  # — reproducible builds across reinstalls and across customer Pis.
  # `--ignore-scripts` blocks any pre/postinstall lifecycle scripts from
  # running (the installer is root). The build itself (`npm run build`)
  # still executes vite/esbuild, so we drop privileges to the invoking
  # user for the whole step when possible — npm is installed system-wide
  # by this script, so it's on every user's PATH.
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] \
       && sudo -u "$SUDO_USER" sh -c 'command -v npm' >/dev/null 2>&1; then
    log "Building frontend as $SUDO_USER (build tooling does not run as root)"
    # A prior root build (or an older installer that built as root) can
    # leave a root-owned node_modules *or* dist/ that blocks the
    # unprivileged build: `npm ci` can't clean a root-owned node_modules,
    # and vite's emptyDir can't unlink files in a root-owned dist/ — both
    # fail with EACCES. Re-own them to the build user best-effort first.
    chown -R "$SUDO_USER" \
      "$REPO_ROOT/frontend/node_modules" \
      "$REPO_ROOT/frontend/dist" 2>/dev/null || true
    sudo -u "$SUDO_USER" sh -c "cd '$REPO_ROOT/frontend' && npm ci --no-audit --no-fund --ignore-scripts --silent && npm run build"
  else
    warn "Building frontend as root — build-time deps run with full privileges. Run install.sh via 'sudo' from your normal user to drop them."
    pushd "$REPO_ROOT/frontend" >/dev/null
    npm ci --no-audit --no-fund --ignore-scripts --silent
    npm run build
    popd >/dev/null
  fi
fi

log "Installing frontend bundle to $UI_DIR"
rsync -a --delete "$REPO_ROOT/frontend/dist/" "$UI_DIR/"

# ─── 5. Python venv + deps ────────────────────────────────────────────────
if [[ ! -d "$APP_DIR/venv" ]]; then
  log "Creating virtualenv at $APP_DIR/venv"
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
# Prefer the hash-pinned lockfile when present (Bookworm-fresh installs
# from the repo). The lockfile pins every transitive dep with a sha256
# so a compromised mirror or typosquat can't substitute a different
# wheel for a pinned version. Fall back to requirements.txt for older
# clones that predate the lockfile (the next `git pull && install.sh`
# will pick up the lock automatically).
if [[ -f "$REPO_ROOT/backend/requirements.lock" ]]; then
  retry "$APP_DIR/venv/bin/pip" install --quiet --require-hashes -r "$REPO_ROOT/backend/requirements.lock"
elif [[ "${GENWATCH_ALLOW_UNPINNED:-0}" == "1" ]]; then
  warn "requirements.lock not found — installing UNPINNED from requirements.txt (GENWATCH_ALLOW_UNPINNED=1)."
  retry "$APP_DIR/venv/bin/pip" install --quiet -r "$REPO_ROOT/backend/requirements.txt"
else
  # The lockfile is committed to the repo, so a missing one means a broken
  # or partial checkout — not a normal state. Refuse rather than silently
  # install unverified wheels (the exact supply-chain risk the lock
  # defends against). The previous silent fallback also used --upgrade,
  # which could drift transitive versions on every re-run.
  err "backend/requirements.lock not found — refusing to install without hash-pinned dependencies."
  err "Re-clone the repo (this usually means a partial checkout). To override at your own risk: GENWATCH_ALLOW_UNPINNED=1."
  exit 1
fi

log "Copying backend package to $APP_DIR/genwatch"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='.pytest_cache' \
  "$REPO_ROOT/backend/genwatch/" "$APP_DIR/genwatch/"

# Create a launcher symlink so `sudo -u genwatch genwatch <cmd>` works.
ln -sf "$APP_DIR/venv/bin/python" "$APP_DIR/python"
cat >"$APP_DIR/genwatch.sh" <<'SH'
#!/usr/bin/env bash
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
NEEDS_PASSWORD=0
if [[ ! -f "$ETC_DIR/config.yaml" ]]; then
  log "Provisioning $ETC_DIR/config.yaml with a random jwt_secret"
  install -m 0640 -o "$USER" -g "$USER" "$REPO_ROOT/deploy/config.yaml.example" "$ETC_DIR/config.yaml"
  SECRET=$("$APP_DIR/venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')
  sed -i "s|^  jwt_secret:.*$|  jwt_secret: \"$SECRET\"|" "$ETC_DIR/config.yaml"
  NEEDS_PASSWORD=1
else
  log "Config already exists at $ETC_DIR/config.yaml — not touching"
  # Verify it has a usable jwt_secret; warn if not
  if grep -q '^  jwt_secret: "REPLACE_ME"' "$ETC_DIR/config.yaml"; then
    warn "jwt_secret is still REPLACE_ME in $ETC_DIR/config.yaml"
  fi
  if grep -q '^  admin_password_hash: "REPLACE_ME"' "$ETC_DIR/config.yaml"; then
    NEEDS_PASSWORD=1
  fi
fi

# ─── 8. systemd unit ──────────────────────────────────────────────────────
log "Installing systemd unit"
install -m 0644 "$REPO_ROOT/deploy/systemd/genwatch.service" "$UNIT_FILE"

# Hardware watchdog drop-in for pid 1. systemd will pet /dev/watchdog so
# the SoC resets the Pi if userspace deadlocks beneath the GenWatch
# service's own software watchdog. The drop-in lives in system.conf.d,
# which only takes effect after daemon-reexec — defer that until after
# the unit is in place so we reexec exactly once.
install -d -m 0755 "$(dirname "$HWWD_FILE")"
install -m 0644 \
  "$REPO_ROOT/deploy/systemd/system.conf.d/10-genwatch-hwwatchdog.conf" \
  "$HWWD_FILE"

systemctl daemon-reload
# Re-exec pid 1 so the new RuntimeWatchdogSec setting is picked up. This
# is the documented way to apply system.conf changes without a reboot
# and is safe to call repeatedly.
systemctl daemon-reexec || warn "systemctl daemon-reexec failed — HW watchdog will activate on next reboot"
# Confirm the hardware watchdog actually armed. A silent miss here leaves
# the top-level "Pi resets on a kernel hang" guarantee inactive until the
# next reboot — exactly when nobody expects it to be off. RuntimeWatchdogUSec=0
# means not armed.
if systemctl show -p RuntimeWatchdogUSec 2>/dev/null | grep -q 'RuntimeWatchdogUSec=0$'; then
  warn "Hardware watchdog NOT active yet (RuntimeWatchdogUSec=0). Reboot to apply, then verify: systemctl show -p RuntimeWatchdogUSec"
else
  log "Hardware watchdog active ($(systemctl show -p RuntimeWatchdogUSec 2>/dev/null || echo '?'))"
fi
systemctl enable genwatch.service

# Sanity-check the hardware watchdog is actually present. Absent on
# x86/QEMU dev rigs; warn only — the service is still useful there.
if [[ ! -c /dev/watchdog ]]; then
  warn "/dev/watchdog not present — hardware watchdog will be inactive."
  warn "On a real Pi this means the bcm2835_wdt kernel module didn't load."
fi

# ─── 8b. time sync (ICD §9.4/§11: <5 s skew vs the ATS-Pi is a hard contract) ─
# GenWatch raises TIME_SKEW if its clock and the ATS-Pi's differ by >5 s. The Pi
# has no battery RTC, so after a power cut it boots at a wrong time until NTP
# corrects it. Enable a time-sync service and report status.
if command -v timedatectl &>/dev/null; then
  log "Enabling NTP time sync (timedatectl set-ntp true)"
  timedatectl set-ntp true 2>/dev/null || warn "could not enable NTP via timedatectl"
  systemctl enable --now systemd-timesyncd 2>/dev/null || true
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
    log "Clock is NTP-synchronized"
  else
    warn "Clock NOT yet NTP-synchronized. On an air-gapped OT VLAN, point this Pi"
    warn "  and the ATS-Pi at the same source (or each other) per ICD §11, or"
    warn "  GenWatch will raise persistent TIME_SKEW alarms."
  fi
else
  warn "timedatectl not found — ensure NTP/chrony keeps this Pi within 5 s of the ATS-Pi (ICD §11)."
fi

# ─── 9. pre-flight check ──────────────────────────────────────────────────
log "Running pre-flight diagnostics"
set +e
sudo -u "$USER" \
  GENWATCH_CONFIG_PATH="$ETC_DIR/config.yaml" \
  GENWATCH_DATA_DIR="$DATA_DIR" \
  PYTHONPATH="$APP_DIR" \
  "$APP_DIR/venv/bin/python" -m genwatch doctor || true
set -e

# ─── 10. start service ────────────────────────────────────────────────────
if (( NEEDS_PASSWORD )); then
  echo
  echo "============================================================"
  echo "  ⚠  ADMIN PASSWORD NOT SET"
  echo
  echo "  Generate a bcrypt hash and paste into the config:"
  echo
  echo "    sudo genwatch hash                  # prompts for the password (no echo)"
  echo "    sudo nano $ETC_DIR/config.yaml      # paste into admin_password_hash"
  echo "    sudo systemctl restart genwatch"
  echo
  echo "  The service will not start until a password is configured."
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
log "Install complete."
if [[ -n "$HOST_IP" ]]; then
  log "Browse to: http://${HOST_IP}:8000"
fi
log "Service log: journalctl -u genwatch -e"
log "Diagnostics: sudo genwatch doctor"
