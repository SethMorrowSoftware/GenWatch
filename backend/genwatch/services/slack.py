"""Slack alerting via the Web API (chat.postMessage).

Sends formatted notifications for alarms, alarm clears, state changes,
operator commands, and Modbus comms transitions. Each event type is
gated by a flag in SlackConfig so an operator can dial the chattiness
to their taste (e.g. alarms only, no per-transition spam).

Design notes
------------
* Authenticates with a Slack **bot token** (``xoxb-...``). User tokens
  (``xoxp-...``) are not supported — they have broader scopes than this
  service needs.
* Messages are placed on an in-process asyncio.Queue and drained by a
  worker task. A slow or unreachable Slack will never block the Modbus
  poller or the API event loop.
* Sends with ``urllib.request`` in ``asyncio.to_thread`` so we don't pull
  in an HTTP library just for this. The Slack endpoint is one POST.
* Retries with exponential backoff (up to 4 attempts) on transport
  errors. If Slack returns ``{"ok": false, "error": "..."}`` we don't
  retry — the error is a config/auth issue that retrying won't fix.
* Bot tokens are sensitive: GET /api/config returns only
  ``botTokenConfigured: bool``, never the literal token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import SlackConfig
from ..db import Database

log = logging.getLogger("genwatch.slack")

SLACK_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


@dataclass
class _PendingMessage:
    blocks: list[dict]
    fallback: str
    attempt: int = 0


class SlackNotifier:
    """Async-queued Slack notifier."""

    MAX_QUEUE = 200
    MAX_ATTEMPTS = 4
    BACKOFF_S = (1.0, 4.0, 16.0)
    SEND_TIMEOUT_S = 10.0

    def __init__(self, cfg: SlackConfig, db: Database, *, site_name: str = ""):
        self.cfg = cfg
        self.db = db
        self.site_name = site_name
        self._queue: asyncio.Queue[_PendingMessage] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._worker_task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._running = False

    # ---- lifecycle ----------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="slack-worker")
        log.info(
            "Slack notifier started (enabled=%s, channel=%s, token=%s)",
            self.cfg.enabled,
            self.cfg.channel or "(unset)",
            "set" if self.cfg.bot_token else "unset",
        )

    async def stop(self) -> None:
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker_task = None
        # Cancel any pending retry timers
        for t in list(self._retry_tasks):
            t.cancel()
        for t in list(self._retry_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._retry_tasks.clear()

    def update_config(self, cfg: SlackConfig) -> None:
        """Hot-reload the config — picked up on the next send."""
        self.cfg = cfg
        log.info(
            "Slack config updated (enabled=%s, channel=%s)",
            self.cfg.enabled, self.cfg.channel or "(unset)",
        )

    def is_enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.bot_token and self.cfg.channel)

    # ---- event helpers ------------------------------------------------
    async def alert_alarm(self, code: str, desc: str, severity: str, ts: float) -> None:
        if severity == "alarm" and not self.cfg.alert_on_alarm:
            return
        if severity == "warn" and not self.cfg.alert_on_warning:
            return
        if severity == "alarm":
            title = f":rotating_light: Alarm raised — {desc}"
        else:
            title = f":warning: Warning — {desc}"
        await self._enqueue(
            severity=severity,
            title=title,
            fields=[("Code", code), ("Site", self._site())],
            fallback=f"{severity.upper()}: {desc} ({code}) @ {self._site()}",
        )

    async def alert_alarm_cleared(self, code: str, desc: str, ts: float) -> None:
        if not self.cfg.alert_on_alarm_cleared:
            return
        await self._enqueue(
            severity="ok",
            title=f":white_check_mark: Alarm cleared — {desc or code}",
            fields=[("Code", code), ("Site", self._site())],
            fallback=f"Alarm cleared: {desc or code} ({code}) @ {self._site()}",
        )

    async def alert_state_change(self, old: str, new: str, ts: float) -> None:
        if not self.cfg.alert_on_state_change:
            return
        await self._enqueue(
            severity="info",
            title=f":arrows_counterclockwise: Engine state — {old} → {new}",
            fields=[("Site", self._site())],
            fallback=f"Engine state {old} → {new} @ {self._site()}",
        )

    async def alert_command(self, verb: str, operator: str, result: str, ts: float) -> None:
        if not self.cfg.alert_on_command:
            return
        sev = "info" if result == "ok" else "warn"
        emoji = ":joystick:" if result == "ok" else ":x:"
        await self._enqueue(
            severity=sev,
            title=f"{emoji} Control command — `{verb}` ({result})",
            fields=[("Operator", operator), ("Site", self._site())],
            fallback=f"Command {verb} by {operator}: {result} @ {self._site()}",
        )

    async def alert_comms_change(self, old: str, new: str, success_pct: float, ts: float) -> None:
        if not self.cfg.alert_on_comms_lost:
            return
        # Only fire on transitions involving non-healthy state, to keep
        # the channel quiet during normal jitter between healthy/degraded.
        if old == new:
            return
        if old == "healthy" and new == "degraded":
            # don't alert until either lost or recovered
            return
        recovered = new == "healthy"
        emoji = ":white_check_mark:" if recovered else ":satellite_antenna:"
        sev = "ok" if recovered else "warn"
        title = f"{emoji} Modbus comms — {old} → {new}"
        await self._enqueue(
            severity=sev,
            title=title,
            fields=[
                ("Success rate", f"{success_pct:.1f}%"),
                ("Site", self._site()),
            ],
            fallback=f"Modbus comms {old} → {new} ({success_pct:.0f}%) @ {self._site()}",
        )

    # ---- test ---------------------------------------------------------
    async def test(self) -> tuple[bool, str]:
        """Send a synchronous test message. Returns (ok, error_or_'ok')."""
        if not self.cfg.bot_token:
            return False, "bot_token not set"
        if not self.cfg.channel:
            return False, "channel not set"
        blocks = _build_blocks(
            severity="info",
            title=":test_tube: GenWatch test message",
            fields=[
                ("Site", self._site()),
                ("When", time.strftime("%Y-%m-%d %H:%M:%S %Z").strip() or "(unknown)"),
            ],
        )
        fallback = f"GenWatch test message from {self._site()}"
        ok, err = await self._send(blocks, fallback, timeout=self.SEND_TIMEOUT_S)
        return ok, "ok" if ok else err

    # ---- internals ----------------------------------------------------
    def _site(self) -> str:
        return self.cfg.site_label or self.site_name or "GenWatch"

    async def _enqueue(
        self,
        *,
        severity: str,
        title: str,
        fields: list[tuple[str, str]],
        fallback: str,
    ) -> None:
        if not self.is_enabled():
            return
        blocks = _build_blocks(severity=severity, title=title, fields=fields)
        try:
            self._queue.put_nowait(_PendingMessage(blocks=blocks, fallback=fallback))
        except asyncio.QueueFull:
            log.warning("Slack queue full — dropping notification: %s", fallback)

    async def _worker(self) -> None:
        while self._running:
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                ok, err = await self._send(msg.blocks, msg.fallback, timeout=self.SEND_TIMEOUT_S)
                if ok:
                    continue
                # Retry on transport errors only — slack_error responses
                # (auth, channel_not_found, etc.) are config issues.
                if err.startswith("slack_error"):
                    log.error("Slack rejected message: %s — %s", err, msg.fallback)
                    continue
                msg.attempt += 1
                if msg.attempt >= self.MAX_ATTEMPTS:
                    log.error(
                        "Slack send failed after %d attempts: %s — %s",
                        msg.attempt, err, msg.fallback,
                    )
                    continue
                backoff = self.BACKOFF_S[min(msg.attempt - 1, len(self.BACKOFF_S) - 1)]
                log.warning(
                    "Slack send failed (%s), retry %d/%d in %.1fs",
                    err, msg.attempt, self.MAX_ATTEMPTS, backoff,
                )
                t = asyncio.create_task(self._requeue_after(backoff, msg))
                self._retry_tasks.add(t)
                t.add_done_callback(self._retry_tasks.discard)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log.exception("Slack worker error: %s", e)

    async def _requeue_after(self, delay: float, msg: _PendingMessage) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("Slack queue full on retry — dropping: %s", msg.fallback)

    async def _send(
        self,
        blocks: list[dict],
        fallback: str,
        *,
        timeout: float,
    ) -> tuple[bool, str]:
        token = self.cfg.bot_token
        channel = self.cfg.channel
        if not token or not channel:
            return False, "not_configured"
        payload = {
            "channel": channel,
            "text": fallback,  # for notification previews & accessibility
            "blocks": blocks,
        }
        try:
            return await asyncio.to_thread(_post_slack, token, payload, timeout)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


def _post_slack(token: str, payload: dict, timeout: float) -> tuple[bool, str]:
    """Synchronous POST to Slack — run via asyncio.to_thread."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API_POST_MESSAGE,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "GenWatch/0.1 (+https://github.com/sethmorrowsoftware/genwatch)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        return False, f"http_{e.code} {raw[:200]}"
    except urllib.error.URLError as e:
        return False, f"url_error {e.reason!r}"
    except TimeoutError:
        return False, "timeout"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"invalid_json {raw[:200]}"

    if data.get("ok"):
        return True, "ok"
    return False, f"slack_error {data.get('error', 'unknown')}"


def _build_blocks(*, severity: str, title: str, fields: list[tuple[str, str]]) -> list[dict]:
    """Render a Block Kit message body.

    Slack Block Kit doesn't expose a sidebar color when using
    chat.postMessage with blocks (that was an 'attachments' concept),
    so we lean on emoji prefixes in the title and a context block at
    the bottom for severity + provenance.
    """
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}
    ]
    if fields:
        chunks = [{"type": "mrkdwn", "text": f"*{label}*\n{val}"} for label, val in fields]
        # Slack rejects more than 10 fields in one block; chunk if needed.
        for i in range(0, len(chunks), 10):
            blocks.append({"type": "section", "fields": chunks[i : i + 10]})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_severity:_ `{severity}` · _via GenWatch_"}
            ],
        }
    )
    return blocks
