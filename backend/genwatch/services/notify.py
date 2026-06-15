"""systemd sd_notify support.

When the service runs under systemd with Type=notify, NOTIFY_SOCKET is
set in the environment. We send READY=1 after startup and WATCHDOG=1
periodically while the main loop is healthy. If we stop pinging the
watchdog before WatchdogSec elapses, systemd will kill -KILL and restart
us — exactly what we want for a generator monitoring service if the
poller hangs.

Outside systemd (tests, dev) NOTIFY_SOCKET is unset and every call is a
no-op, so this module is safe to import unconditionally.
"""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("genwatch.notify")


def _send(message: str) -> bool:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    # Linux abstract-namespace sockets use a leading '@' in NOTIFY_SOCKET.
    addr = "\0" + path[1:] if path.startswith("@") else path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode("utf-8"))
        return True
    except OSError as e:
        log.debug("sd_notify(%r) failed: %s", message, e)
        return False


def ready() -> bool:
    return _send("READY=1")


def watchdog() -> bool:
    return _send("WATCHDOG=1")


def stopping() -> bool:
    return _send("STOPPING=1")


def watchdog_interval_s() -> float | None:
    """Recommended interval to ping the watchdog (half of WatchdogSec).

    Returns None when running outside systemd or when WatchdogSec is unset.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    try:
        return float(usec) / 1_000_000 / 2.0
    except ValueError:
        return None


# Maximum time we tolerate "no prime poll has succeeded yet" before
# withholding the watchdog ping and letting systemd restart us. The
# legitimate use of the cold-start grace is a slow-to-boot Modbus
# bridge / network coming up after the Pi (DHCP, Lantronix firmware
# init, etc.) — those resolve well under a minute in practice. Five
# minutes leaves comfortable headroom for unusual cases while bounding
# how long a misconfigured host (wrong IP, wrong port) can keep the
# service "healthy" to systemd. Past the cap we withhold the ping so
# systemd's WatchdogSec elapses and SIGKILL+restarts the unit; with the
# start rate limiter disabled (StartLimitIntervalSec=0) it keeps restarting
# on the RestartSec cadence, so a misconfigured host surfaces as a visible
# restart loop in the journal rather than a silently-zombie "healthy"
# service.
WATCHDOG_COLD_START_GRACE_S = 300.0


def should_ping_watchdog(
    *,
    mono_last_prime_good: float | None,
    service_start_mono: float,
    now_mono: float,
    stale_after_s: float,
    cold_start_grace_s: float = WATCHDOG_COLD_START_GRACE_S,
) -> tuple[bool, str | None]:
    """Pure decision: should the watchdog loop ping systemd this tick?

    Returns ``(should_ping, withhold_reason)``. When ``should_ping`` is
    False the caller withholds the ping (so systemd's WatchdogSec elapses
    and SIGKILLs the unit) and logs ``withhold_reason`` at warning level
    for diagnostic context.

    Three regimes:

    1. **Cold-start grace** — ``mono_last_prime_good is None`` and we're
       still within ``cold_start_grace_s`` of service start. Ping
       (legitimate boot window before the first prime poll lands).
    2. **Cold-start cap exceeded** — ``mono_last_prime_good is None``
       past the grace. Withhold; this is the misconfigured-host case.
    3. **Steady state** — at least one prime poll has succeeded.
       Withhold iff that success was more than ``stale_after_s`` ago.
    """
    if mono_last_prime_good is None:
        if now_mono - service_start_mono <= cold_start_grace_s:
            return True, None
        return False, (
            f"no prime poll completed in {cold_start_grace_s:.0f}s since "
            "service start — check modbus_tcp.host / serial.device or run "
            "`sudo genwatch doctor`"
        )
    silence = now_mono - mono_last_prime_good
    if silence <= stale_after_s:
        return True, None
    return False, f"prime poll silent for {silence:.1f}s (>{stale_after_s:.1f}s)"
