"""Pi host health probes — CPU temp, disk, memory, load.

Surface "is the monitor itself healthy" alongside "is the generator
healthy" so operators monitor the monitor. Read from /proc and /sys
directly so we don't pull in psutil for what is, fundamentally, four
file reads per call.

All values are best-effort: if a path is missing (non-Linux dev box,
container without /sys access, etc.) the field comes back as None
rather than raising. The endpoint is meant to be safe to poll from the
Live view every ~10 s.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger("genwatch.system")


@dataclass
class SystemHealth:
    cpu_temp_c: float | None
    cpu_load_1m: float | None
    cpu_load_5m: float | None
    cpu_load_15m: float | None
    mem_total_mb: int | None
    mem_used_mb: int | None
    mem_used_pct: float | None
    disk_total_mb: int | None
    disk_used_mb: int | None
    disk_used_pct: float | None
    uptime_s: int | None
    proc_uptime_s: int | None
    proc_rss_mb: float | None
    pi_model: str | None
    throttled_raw: int | None
    throttled_flags: list[str]

    def to_ui(self) -> dict:
        return {
            "cpuTempC": self.cpu_temp_c,
            "cpuLoad1m": self.cpu_load_1m,
            "cpuLoad5m": self.cpu_load_5m,
            "cpuLoad15m": self.cpu_load_15m,
            "memTotalMb": self.mem_total_mb,
            "memUsedMb": self.mem_used_mb,
            "memUsedPct": self.mem_used_pct,
            "diskTotalMb": self.disk_total_mb,
            "diskUsedMb": self.disk_used_mb,
            "diskUsedPct": self.disk_used_pct,
            "uptimeS": self.uptime_s,
            "procUptimeS": self.proc_uptime_s,
            "procRssMb": self.proc_rss_mb,
            "piModel": self.pi_model,
            "throttledRaw": self.throttled_raw,
            "throttledFlags": self.throttled_flags,
        }


# vcgencmd get_throttled bit definitions — the Pi firmware sets these
# when the CPU has been throttled, under-volted, or capped. The "since
# last reboot" bits (16..) are the actionable ones; the "right now" bits
# (0..3) flicker as the SoC settles. We surface human labels for both.
_THROTTLED_BITS = [
    (0,  "under_voltage"),
    (1,  "arm_freq_capped"),
    (2,  "throttled"),
    (3,  "soft_temp_limit"),
    (16, "under_voltage_seen"),
    (17, "arm_freq_capped_seen"),
    (18, "throttled_seen"),
    (19, "soft_temp_limit_seen"),
]


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_cpu_temp() -> float | None:
    # /sys/class/thermal/thermal_zone0 is the convention on Linux SoCs
    # including the Pi (BCM2712). Value is millidegrees C.
    raw = _read_text("/sys/class/thermal/thermal_zone0/temp")
    if not raw:
        return None
    try:
        return round(int(raw) / 1000.0, 1)
    except ValueError:
        return None


def _read_load() -> tuple[float | None, float | None, float | None]:
    raw = _read_text("/proc/loadavg")
    if not raw:
        return (None, None, None)
    parts = raw.split()
    if len(parts) < 3:
        return (None, None, None)
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return (None, None, None)


def _read_meminfo() -> tuple[int | None, int | None, float | None]:
    """Returns (total_mb, used_mb, used_pct).

    "used" = total - available, matching what `free -h` calls "used"
    (so cached pages aren't counted as memory pressure).
    """
    raw = _read_text("/proc/meminfo")
    if not raw:
        return (None, None, None)
    fields: dict[str, int] = {}
    for line in raw.splitlines():
        try:
            k, v = line.split(":", 1)
            fields[k.strip()] = int(v.strip().split()[0])  # in kB
        except (ValueError, IndexError):
            continue
    total_kb = fields.get("MemTotal")
    avail_kb = fields.get("MemAvailable") or fields.get("MemFree")
    if total_kb is None or avail_kb is None:
        return (None, None, None)
    used_kb = max(0, total_kb - avail_kb)
    total_mb = total_kb // 1024
    used_mb = used_kb // 1024
    used_pct = round(100.0 * used_kb / total_kb, 1) if total_kb else None
    return (total_mb, used_mb, used_pct)


def _read_disk(path: str) -> tuple[int | None, int | None, float | None]:
    try:
        st = os.statvfs(path)
    except (FileNotFoundError, PermissionError, OSError):
        return (None, None, None)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = max(0, total - free)
    total_mb = total // (1024 * 1024)
    used_mb = used // (1024 * 1024)
    used_pct = round(100.0 * used / total, 1) if total else None
    return (total_mb, used_mb, used_pct)


def _read_uptime() -> int | None:
    raw = _read_text("/proc/uptime")
    if not raw:
        return None
    try:
        return int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return None


def _read_proc_rss_mb() -> float | None:
    raw = _read_text("/proc/self/status")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            try:
                kb = int(line.split()[1])
                return round(kb / 1024.0, 1)
            except (ValueError, IndexError):
                return None
    return None


def _read_pi_model() -> str | None:
    raw = _read_text("/proc/device-tree/model")
    if not raw:
        return None
    # /proc/device-tree files end with a NUL byte; strip it.
    return raw.rstrip("\x00").strip() or None


def _decode_throttled_flags(raw: int) -> list[str]:
    flags = []
    for bit, name in _THROTTLED_BITS:
        if raw & (1 << bit):
            flags.append(name)
    return flags


def _read_throttled() -> tuple[int | None, list[str]]:
    """Read /sys/devices/platform/soc/.../get_throttled equivalent.

    The Pi exposes the firmware throttled bitmap at the vcgencmd path
    in modern kernels. We don't shell out; we read the file directly.
    Absent on non-Pi hardware.
    """
    # Try the modern sysfs path first; fall back to nothing.
    candidates = [
        "/sys/devices/platform/soc/soc:firmware/get_throttled",
    ]
    for path in candidates:
        raw = _read_text(path)
        if raw is None:
            continue
        try:
            v = int(raw, 0)
            return (v, _decode_throttled_flags(v))
        except ValueError:
            continue
    return (None, [])


def collect(data_dir: str, started_at_wall: float) -> SystemHealth:
    """Snapshot the host's current health. Fast — no shell-outs."""
    load = _read_load()
    mem = _read_meminfo()
    disk = _read_disk(data_dir)
    throttled_raw, throttled_flags = _read_throttled()
    return SystemHealth(
        cpu_temp_c=_read_cpu_temp(),
        cpu_load_1m=load[0],
        cpu_load_5m=load[1],
        cpu_load_15m=load[2],
        mem_total_mb=mem[0],
        mem_used_mb=mem[1],
        mem_used_pct=mem[2],
        disk_total_mb=disk[0],
        disk_used_mb=disk[1],
        disk_used_pct=disk[2],
        uptime_s=_read_uptime(),
        proc_uptime_s=int(time.time() - started_at_wall) if started_at_wall else None,
        proc_rss_mb=_read_proc_rss_mb(),
        pi_model=_read_pi_model(),
        throttled_raw=throttled_raw,
        throttled_flags=throttled_flags,
    )
