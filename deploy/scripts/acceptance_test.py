#!/usr/bin/env python3
"""GenWatch commissioning acceptance test — functionality + safety.

Run this ON the GenWatch Pi, against the live service, to verify the
deployment behaves correctly and that its SAFETY guards hold *before* you
trust the install. It needs no third-party packages — only the Python 3
standard library — so it runs with the system `python3`.

    python3 acceptance_test.py --password '<admin password>'
    # safer (keeps the password out of shell history / `ps`):
    GENWATCH_TEST_PASSWORD='<pw>' python3 acceptance_test.py
    # or just run it and you'll be prompted with no echo:
    python3 acceptance_test.py

Exit code is 0 when nothing FAILed (warnings are allowed), 1 otherwise —
so it can gate a commissioning script.

────────────────────────────────────────────────────────────────────────
SAFE BY DESIGN — read this before running near live hardware
────────────────────────────────────────────────────────────────────────
The default run is NON-ACTUATING. It exercises every command-safety gate
WITHOUT ever sending a valid confirm token to a command endpoint.

On both control paths (backend/genwatch/services/control.py and
services/ats_control.py) the confirm token is consumed inside the same
locked critical section *immediately before* the Modbus write. So a
request that carries no valid token can never reach the write — the
generator is never started/stopped and the ATS is never driven, even if
an upstream guard were broken. Every command test here deliberately sends
no token, a malformed body, a bad Origin, or no credentials.

The ONLY code path that completes a real command is the opt-in
--actuate-mock-generator flag, which:
  * refuses to run unless the service reports mock mode, and
  * drives only the SIMULATED H-100 (start then stop) — never the ATS.

There is deliberately NO automated path that actuates the ATS-Pi relays.
That belongs in the planned-outage Phase 8 golden-sequence run with an
operator present (see docs/COMMISSIONING.md).
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── result harness ────────────────────────────────────────────────────────
PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"
_COLOR = {
    PASS: "\033[1;32m", FAIL: "\033[1;31m", WARN: "\033[1;33m",
    SKIP: "\033[1;34m", INFO: "\033[1;36m",
}
_RESET = "\033[0m"


class Recorder:
    def __init__(self, color: bool = True):
        self.color = color and sys.stdout.isatty()
        self.rows: list[tuple[str, str, str, str]] = []
        self._section = ""

    def section(self, title: str) -> None:
        self._section = title
        print(f"\n=== {title} ===")

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((self._section, status, name, detail))
        tag = f"{_COLOR[status]}{status:4}{_RESET}" if self.color else f"{status:4}"
        line = f"  [{tag}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    def counts(self) -> dict[str, int]:
        c = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0, INFO: 0}
        for _, status, _, _ in self.rows:
            c[status] = c.get(status, 0) + 1
        return c


# ── minimal JSON HTTP client (isolated cookie jar per instance) ────────────
class Http:
    """One instance = one browser-equivalent session.

    Use a logged-OUT instance for the unauthenticated checks and a
    logged-IN one for everything else, so the two never cross-contaminate.
    """

    def __init__(self, base_url: str, insecure: bool = False, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base.startswith("https") and insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def __call__(self, method: str, path: str, body=None, headers=None):
        """Return (status_code | None, parsed_json_dict). None code = transport error."""
        h = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            return resp.getcode(), _read_json(resp)
        except urllib.error.HTTPError as e:
            return e.code, _read_json(e)
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            return None, {"_transport_error": str(getattr(e, "reason", e))}


def _read_json(resp) -> dict:
    try:
        raw = resp.read()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        out = json.loads(raw.decode("utf-8"))
        return out if isinstance(out, dict) else {"_list": out}
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:200].decode("utf-8", "replace")}


# ── helpers ────────────────────────────────────────────────────────────────
def resolve_password(args) -> str:
    if args.password:
        return args.password
    env = os.environ.get("GENWATCH_TEST_PASSWORD")
    if env:
        return env
    import getpass
    return getpass.getpass("GenWatch admin password: ")


def _rejected(code: int | None) -> bool:
    """A safety gate is satisfied when the request did NOT succeed."""
    return code is not None and code >= 400


def _detail_code(body: dict) -> str | None:
    d = body.get("detail") if isinstance(body, dict) else None
    return d.get("code") if isinstance(d, dict) else None


def _wait_state(http: Http, targets: set[str], timeout_s: float) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, st = http("GET", "/api/status")
        cur = (st or {}).get("state") or (st or {}).get("engineState")
        if cur in targets:
            return cur
        time.sleep(1.0)
    return None


def _systemctl(rec: Recorder, verb: str, unit: str, label: str) -> None:
    try:
        out = subprocess.run(
            ["systemctl", verb, unit], capture_output=True, text=True, timeout=5
        )
        val = (out.stdout or out.stderr).strip()
        good = (verb == "is-active" and val == "active") or (
            verb == "is-enabled" and val == "enabled"
        )
        rec.add(PASS if good else WARN, label, f"systemctl {verb} {unit} -> {val or out.returncode}")
    except FileNotFoundError:
        rec.add(SKIP, label, "systemctl not available on this host")
    except Exception as e:  # noqa: BLE001
        rec.add(WARN, label, f"systemctl {verb} failed: {e}")


# ── sections ────────────────────────────────────────────────────────────────
def section_connectivity(rec, anon, args) -> dict | None:
    rec.section("1. Connectivity & mode")
    code, health = anon("GET", "/api/health")
    if code != 200 or not health.get("ok"):
        rec.add(FAIL, "service reachable", f"GET /api/health -> {code} {health}")
        return None
    rec.add(PASS, "service reachable", f"{args.base_url}/api/health ok")

    mock = bool(health.get("mock"))
    rec.add(INFO, "mock mode", f"service reports mock={mock}")
    if args.expect_mock is not None:
        match = mock == args.expect_mock
        rec.add(PASS if match else FAIL, "mock matches --expect-mock",
                f"expected {args.expect_mock}, got {mock}")

    # Hardening: an anonymous /api/health must leak only {ok, mock}.
    leaked = [k for k in ("version", "dbBytes", "uptimeS", "comms", "engineState") if k in health]
    if leaked:
        rec.add(WARN, "anon health is minimal", f"anon caller saw extra keys: {leaked}")
    else:
        rec.add(PASS, "anon health is minimal", "anon caller sees only {ok, mock}")
    return {"mock": mock}


def section_auth(rec, anon, auth, args) -> bool:
    rec.section("2. Authentication & access control")

    code, _ = anon("GET", "/api/status")
    rec.add(PASS if code == 401 else FAIL, "unauthenticated GET /api/status refused",
            f"-> {code} (want 401)")

    # Command endpoints must require auth. No Origin header is sent, so the
    # CSRF middleware lets the request through to the auth dependency and we
    # observe the 401 (not a 403). A valid-shaped body avoids a 422 pre-empt.
    for path in ("/api/control/start", "/api/ats/test"):
        code, _ = anon("POST", path, {"confirm_token": "none"})
        rec.add(PASS if code == 401 else FAIL, f"unauthenticated POST {path} refused",
                f"-> {code} (want 401)")

    # Wrong password rejected. A 429 here means the login rate-limiter
    # engaged — itself a working safety control — so accept either.
    code, _ = anon("POST", "/api/auth/login", {"password": "wrong-" + os.urandom(4).hex()})
    if code == 401:
        rec.add(PASS, "wrong password refused", "-> 401")
    elif code == 429:
        rec.add(WARN, "wrong password refused", "-> 429 (login rate-limiter engaged)")
    else:
        rec.add(FAIL, "wrong password refused", f"-> {code} (want 401)")

    pw = resolve_password(args)
    code, _ = auth("POST", "/api/auth/login", {"password": pw})
    if code == 200:
        rec.add(PASS, "admin login", "session established")
    elif code == 429:
        rec.add(FAIL, "admin login", "-> 429 rate-limited; wait ~5 min, avoid running in a loop")
        return False
    elif code == 401:
        rec.add(FAIL, "admin login", "-> 401 wrong password (pass --password / $GENWATCH_TEST_PASSWORD)")
        return False
    else:
        rec.add(FAIL, "admin login", f"-> {code}")
        return False

    code, me = auth("GET", "/api/auth/me")
    rec.add(PASS if code == 200 else FAIL, "authenticated /api/auth/me",
            f"-> {code} role={me.get('role')}")
    code, _ = auth("GET", "/api/status")
    rec.add(PASS if code == 200 else FAIL, "authenticated GET /api/status", f"-> {code}")
    return True


def section_csrf(rec, auth) -> None:
    rec.section("3. CSRF protection")
    code, body = auth("POST", "/api/control/start", {"confirm_token": "none"},
                      headers={"Origin": "http://evil.example.com"})
    if code == 403:
        suffix = " csrf_blocked" if _detail_code(body) == "csrf_blocked" else ""
        rec.add(PASS, "cross-origin POST blocked", f"-> 403{suffix}")
    else:
        rec.add(FAIL, "cross-origin POST blocked", f"-> {code} (want 403)")


def section_command_safety(rec, auth) -> None:
    rec.section("4. Command safety — confirm-token gate (non-actuating)")

    # Missing confirm_token -> 422 (the field is required by the schema).
    code, _ = auth("POST", "/api/control/start", {})
    rec.add(PASS if code == 422 else FAIL, "control requires confirm_token",
            f"missing field -> {code} (want 422)")

    # Invalid token -> rejected, and crucially never actuates: the token is
    # consumed right before the Modbus write, so a bad token aborts first.
    bad = "INVALID" + "0" * 25
    code, _ = auth("POST", "/api/control/start", {"confirm_token": bad})
    rec.add(PASS if _rejected(code) else FAIL, "control rejects invalid token",
            f"-> {code} (want 4xx, never 200)")

    code, body = auth("POST", "/api/ats/test", {"confirm_token": bad})
    if code == 404 and _detail_code(body) == "ats_disabled":
        rec.add(SKIP, "ATS command rejects invalid token", "ATS integration not enabled")
    else:
        rec.add(PASS if _rejected(code) else FAIL, "ATS command rejects invalid token",
                f"-> {code} (want 4xx, never 200)")

    # The confirm endpoint must issue a token — but we deliberately DO NOT
    # spend it (it self-expires in 30 s), keeping this run non-actuating.
    code, tok = auth("GET", "/api/control/confirm?verb=start")
    issued = code == 200 and bool(tok.get("token"))
    rec.add(PASS if issued else FAIL, "confirm endpoint issues a token",
            f"-> {code} (token issued, intentionally not spent)")

    rec.add(INFO, "no actuation performed",
            "no valid confirm token was sent to any command endpoint")


def section_ats(rec, auth, args, status: dict) -> None:
    rec.section("5. ATS-Pi integration & authority")
    ats = (status or {}).get("ats") or {}
    if not ats.get("enabled"):
        rec.add(SKIP, "ATS integration", "ats.enabled is false — skipping ATS checks")
        return

    comms = (ats.get("comms") or {}).get("state")
    rec.add(PASS if comms == "healthy" else FAIL, "ATS comms healthy", f"comms.state={comms}")

    rec.add(PASS if ats.get("authoritative") is True else WARN, "ATS authoritative",
            f"authoritative={ats.get('authoritative')}")

    uid = ats.get("atsPiUnitId")
    rec.add(PASS if uid == args.expected_unit_id else WARN, "ATS unit-id matches",
            f"atsPiUnitId={uid} expected={args.expected_unit_id}")

    icd = ats.get("icdVersion")
    rec.add(PASS if icd == [1, 0] else WARN, "ICD version", f"icdVersion={icd} (want [1, 0])")

    faults = ats.get("faultCodes") or []
    rec.add(PASS if not faults else WARN, "no active ATS faults", f"faultCodes={faults}")

    pos = ats.get("position")
    valid = pos in ("utility", "generator", "transferring", "unknown")
    rec.add(PASS if valid else FAIL, "ATS position decoded",
            f"position={pos} normAvail={ats.get('normalAvailable')} "
            f"emergAvail={ats.get('emergencyAvailable')}")


def section_telemetry(rec, auth, mock: bool, status: dict) -> None:
    rec.section("6. Telemetry, state & events")
    es = (status or {}).get("state") or (status or {}).get("engineState")
    rec.add(PASS if es else WARN, "engine state present", f"state={es}")

    hcomms = ((status or {}).get("comms") or {}).get("state")
    if mock:
        rec.add(PASS if hcomms == "healthy" else WARN, "H-100 comms (mock)",
                f"comms.state={hcomms} (mock should read healthy)")
    else:
        rec.add(INFO, "H-100 comms", f"comms.state={hcomms}")

    code, ev = auth("GET", "/api/events")
    n = len(ev.get("_list", [])) if "_list" in ev else len(ev.get("events", []) or [])
    rec.add(PASS if code == 200 else WARN, "events feed", f"-> {code} ({n} entries)")

    code, _ = auth("GET", "/api/telemetry/columns")
    rec.add(PASS if code == 200 else WARN, "telemetry subsystem",
            f"/api/telemetry/columns -> {code}")


def section_local(rec) -> None:
    rec.section("7. Local system (systemd / watchdog)")
    _systemctl(rec, "is-active", "genwatch", "service active")
    _systemctl(rec, "is-enabled", "genwatch", "service enabled at boot")
    if os.path.exists("/dev/watchdog"):
        rec.add(PASS, "hardware watchdog device", "/dev/watchdog present")
    else:
        rec.add(WARN, "hardware watchdog device", "/dev/watchdog missing (HW watchdog inactive)")


def section_actuate_mock(rec, auth, mock: bool) -> None:
    rec.section("8. Actuation — MOCK GENERATOR ONLY (opt-in)")
    if not mock:
        rec.add(SKIP, "mock generator actuation",
                "refusing: service is NOT in mock mode (would drive real hardware)")
        return

    _, status = auth("GET", "/api/status")
    panel = ((status or {}).get("panel") or {}).get("mode")
    state = (status or {}).get("state") or (status or {}).get("engineState")
    if panel not in (None, "auto"):
        rec.add(SKIP, "control happy-path", f"panel mode is {panel}, need AUTO; skipping")
        return
    if state != "stopped":
        rec.add(SKIP, "control happy-path", f"engine is {state}, need 'stopped' to test start; skipping")
        return

    c, tok = auth("GET", "/api/control/confirm?verb=start")
    token = tok.get("token")
    if c != 200 or not token:
        rec.add(FAIL, "issue start token", f"-> {c}")
        return
    c, body = auth("POST", "/api/control/start", {"confirm_token": token})
    if c != 200:
        rec.add(FAIL, "mock start accepted", f"-> {c} {body}")
        return
    rec.add(PASS, "mock start accepted", "confirm -> start -> 200")
    new = _wait_state(auth, {"cranking", "running", "exercising"}, 15)
    rec.add(PASS if new else FAIL, "mock engine started",
            f"state -> {new or 'no transition within 15 s'}")

    c, tok = auth("GET", "/api/control/confirm?verb=stop")
    token = tok.get("token")
    c, _ = auth("POST", "/api/control/stop", {"confirm_token": token}) if token else (None, {})
    rec.add(PASS if c == 200 else FAIL, "mock stop accepted", f"-> {c}")
    back = _wait_state(auth, {"stopped", "cooling"}, 15)
    rec.add(PASS if back else WARN, "mock engine stopped/cooling",
            f"state -> {back or 'no transition within 15 s'}")


def summarize(rec: Recorder) -> int:
    c = rec.counts()
    print("\n" + "=" * 64)
    print(f"  PASS {c[PASS]}   FAIL {c[FAIL]}   WARN {c[WARN]}   SKIP {c[SKIP]}")
    print("=" * 64)
    if c[FAIL]:
        print("  VERDICT: NOT READY — resolve the FAIL items above before installing.")
        return 1
    if c[WARN]:
        print("  VERDICT: PASSED WITH WARNINGS — review WARN items (often environmental).")
        return 0
    print("  VERDICT: SAFE TO PROCEED — all functional & safety checks passed.")
    return 0


def _optbool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def main() -> int:
    p = argparse.ArgumentParser(
        description="GenWatch commissioning acceptance test (functionality + safety).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The default run is NON-ACTUATING and safe to run against any "
               "instance. See the module docstring for the safety model.",
    )
    p.add_argument("--base-url", default=os.environ.get("GENWATCH_BASE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--password", default=None,
                   help="admin password (prefer $GENWATCH_TEST_PASSWORD or the prompt)")
    p.add_argument("--expected-unit-id", type=int, default=23,
                   help="ATS-Pi unit id to expect (default 23)")
    p.add_argument("--expect-mock", type=_optbool, default=None,
                   help="assert the service mock state (true for a bench test, false for production)")
    p.add_argument("--actuate-mock-generator", action="store_true",
                   help="opt in to a real start/stop of the SIMULATED H-100 (refuses unless mock)")
    p.add_argument("--local-checks", action="store_true",
                   help="also check systemd unit + /dev/watchdog on this host")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed certs)")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args()

    rec = Recorder(color=not args.no_color)
    anon = Http(args.base_url, insecure=args.insecure)
    auth = Http(args.base_url, insecure=args.insecure)

    print("GenWatch acceptance test")
    print(f"  target : {args.base_url}")
    print("  safety : NON-ACTUATING (no valid confirm token is ever sent to a command)")
    if args.actuate_mock_generator:
        print("  actuate: --actuate-mock-generator ENABLED (mock H-100 start/stop only)")

    env = section_connectivity(rec, anon, args)
    if env is None:
        rec.add(INFO, "aborting", "service did not respond; start it and re-run")
        return summarize(rec)
    mock = env["mock"]

    if not section_auth(rec, anon, auth, args):
        return summarize(rec)

    section_csrf(rec, auth)
    section_command_safety(rec, auth)

    _, status = auth("GET", "/api/status")
    section_ats(rec, auth, args, status)
    section_telemetry(rec, auth, mock, status)

    if args.local_checks:
        section_local(rec)
    if args.actuate_mock_generator:
        section_actuate_mock(rec, auth, mock)

    return summarize(rec)


if __name__ == "__main__":
    sys.exit(main())
