"""Fuel consumption estimator + running totals.

The H-100's Modbus map exposes a fuel-level percentage on supported
units (gauge senders) but does NOT report instantaneous burn rate.
genmon's approach — and ours — is a simple load-curve model: estimate
gallons-per-hour from the kW reading, then integrate over time. The
model is configurable per fuel type because diesel, NG and LPG burn
very differently per kW delivered.

Accuracy expectation: ±10–15% on a steady load. The model intentionally
doesn't try to chase exact spec curves (which vary by engine, altitude,
temperature, and load history); it's a useful "are we burning more fuel
this month than last" signal, not a custody-transfer meter.

Model (kW → flow_per_hour at sea level, room temp):
  - diesel:  baseline_l_per_hr=0.0,  slope_l_per_hr_per_kw=0.07 gal/h/kW
             plus an idle term of 0.6 gal/h when running but kW≈0
  - lp:      ~1.4× diesel per kW (≈0.1 gal/h/kW) plus 0.85 idle
  - ng:      reported in scf/h (not gal); ≈10 scf per kWh ≈ 10 scf/h/kW
             plus 30 scf/h idle

Note on units: diesel/LP are in US gallons; NG is in standard cubic
feet (scf). The UI surfaces the right unit based on the fuel_type
in site config.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("genwatch.fuel")

FuelType = Literal["diesel", "lp", "ng"]


@dataclass(frozen=True)
class FuelModel:
    """Per-fuel coefficients for the burn-rate estimator."""
    fuel_type: FuelType
    unit_per_hour: str           # "gph" (gallons/hr) or "scfh" (cubic ft/hr)
    idle_per_hr: float           # consumption while running at ≈0 kW
    slope_per_hr_per_kw: float   # additional consumption per kW

    def estimate(self, kw: float | None) -> float:
        """Estimate flow rate (unit_per_hour) for a given kW load."""
        if kw is None or kw <= 0:
            return self.idle_per_hr
        return self.idle_per_hr + self.slope_per_hr_per_kw * float(kw)


MODELS: dict[FuelType, FuelModel] = {
    # Diesel: ~0.07 gal/kWh at typical load + a 0.6 gph idle term for
    # accessory drives. Real Cummins QSB7-G5 curves: ~14 gph at 200 kW.
    "diesel": FuelModel(fuel_type="diesel", unit_per_hour="gph",
                        idle_per_hr=0.6, slope_per_hr_per_kw=0.07),
    # LP (propane): less energy per gallon than diesel → ~0.10 gph/kW.
    "lp":     FuelModel(fuel_type="lp", unit_per_hour="gph",
                        idle_per_hr=0.85, slope_per_hr_per_kw=0.10),
    # Natural gas: report in scf/h. ~10 scf/kWh delivered.
    "ng":     FuelModel(fuel_type="ng", unit_per_hour="scfh",
                        idle_per_hr=30.0, slope_per_hr_per_kw=10.0),
}


@dataclass
class FuelAccumulator:
    """Tracks running totals using trapezoidal integration of the rate.

    Holds three windows simultaneously:
      - day_total:    since local midnight (resets at the boundary)
      - month_total:  since local first-of-month
      - lifetime:     since the service started (or a persisted seed)

    The state machine drives this by handing us (timestamp, kw,
    engine_running) on every base-tier poll callback. We only integrate
    while the engine is actually running — idle/stopped time doesn't
    burn measurable fuel for a standby genset.
    """
    model: FuelModel
    lifetime_units: float = 0.0
    day_units: float = 0.0
    month_units: float = 0.0
    last_ts: float | None = None
    last_rate: float = 0.0
    last_engine_running: bool = False
    # Day/month boundaries are stored as the local-wallclock start
    # epoch so a service restart in the middle of a day picks up where
    # it left off rather than zeroing.
    day_boundary_ts: float = 0.0
    month_boundary_ts: float = 0.0

    def reset_lifetime(self) -> None:
        self.lifetime_units = 0.0

    def update(self, ts: float, kw: float | None, engine_running: bool) -> None:
        """Integrate one sample into the running totals.

        Trapezoidal: use the average of the previous rate and the current
        rate over the elapsed interval. Skip if the engine has been off
        across this whole interval — no fuel burned.
        """
        cur_rate = self.model.estimate(kw) if engine_running else 0.0
        if self.last_ts is None:
            # First sample — establish baseline only.
            self.last_ts = ts
            self.last_rate = cur_rate
            self.last_engine_running = engine_running
            self._roll_boundaries(ts)
            return

        dt_s = max(0.0, ts - self.last_ts)
        if dt_s == 0.0:
            self.last_rate = cur_rate
            self.last_engine_running = engine_running
            return

        # Boundary roll: if we crossed midnight (or month) between
        # samples, attribute the pre-boundary portion to the old window
        # and the post-boundary portion to the new one. Cheap because
        # boundary crossings are rare.
        self._roll_boundaries(ts)

        # Average rate × elapsed hours.
        if self.last_engine_running and engine_running:
            avg_rate = (self.last_rate + cur_rate) / 2.0
        elif self.last_engine_running:
            avg_rate = self.last_rate / 2.0
        elif engine_running:
            avg_rate = cur_rate / 2.0
        else:
            avg_rate = 0.0
        delta = avg_rate * (dt_s / 3600.0)
        self.lifetime_units += delta
        self.day_units += delta
        self.month_units += delta

        self.last_ts = ts
        self.last_rate = cur_rate
        self.last_engine_running = engine_running

    def _roll_boundaries(self, ts: float) -> None:
        """Reset day/month accumulators on the local-time boundary crossings."""
        local = time.localtime(ts)
        # Start of today, local time
        day_start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                 0, 0, 0, 0, 0, -1))
        # Start of this month, local time
        month_start = time.mktime((local.tm_year, local.tm_mon, 1,
                                   0, 0, 0, 0, 0, -1))
        if day_start != self.day_boundary_ts:
            self.day_units = 0.0
            self.day_boundary_ts = day_start
        if month_start != self.month_boundary_ts:
            self.month_units = 0.0
            self.month_boundary_ts = month_start

    def projected_hours_remaining(self, tank_remaining_units: float | None) -> float | None:
        """At the current rate, how many hours until empty?"""
        if tank_remaining_units is None or self.last_rate <= 0:
            return None
        if not self.last_engine_running:
            return None
        return round(tank_remaining_units / self.last_rate, 1)

    def to_ui(self, tank_gal: int | None, tank_pct: float | None) -> dict:
        tank_remaining = None
        if tank_gal and tank_pct is not None:
            tank_remaining = float(tank_gal) * float(tank_pct) / 100.0
        return {
            "fuelType": self.model.fuel_type,
            "unit": self.model.unit_per_hour,
            "rateNow": round(self.last_rate, 2),
            "dayTotal": round(self.day_units, 2),
            "monthTotal": round(self.month_units, 2),
            "lifetimeTotal": round(self.lifetime_units, 2),
            "engineRunning": self.last_engine_running,
            "tankGal": tank_gal,
            "tankPct": tank_pct,
            "tankRemainingUnits": (
                round(tank_remaining, 1) if tank_remaining is not None else None
            ),
            "hoursRemaining": self.projected_hours_remaining(tank_remaining),
        }
