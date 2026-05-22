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
