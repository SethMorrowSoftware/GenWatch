"""/ws/live — push updates to subscribed clients.

Each connected browser opens a single WS. The poller publishes
snapshot, transition, alarm and event messages onto the EventBus and
we fan them out here.

Auth: we require a valid session cookie OR a ?token=... query param.
Anyone with the cookie has at least viewer rights — viewers can read
live data, just not issue commands.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..services.auth import AuthError, decode_token

log = logging.getLogger("genwatch.ws")

router = APIRouter(tags=["ws"])


async def _authed(websocket: WebSocket, token: str | None) -> bool:
    secret = websocket.app.state.settings.auth.jwt_secret
    cookie = websocket.cookies.get("genwatch_session")
    raw = cookie or token
    if not raw:
        return False
    try:
        decode_token(secret=secret, token=raw)
        return True
    except AuthError:
        return False


@router.websocket("/ws/live")
async def live(websocket: WebSocket, token: str | None = Query(None)):
    if not await _authed(websocket, token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    bus = websocket.app.state.bus
    q = bus.subscribe()

    # Initial hello — let the client know the WS is live and what the
    # current state is, so it doesn't have to wait for the next poll.
    try:
        st = websocket.app.state
        snap = st.state_machine.snap
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "state": snap.engine_state,
                    "comms": {
                        "state": snap.comms.state,
                        "successPct": snap.comms.success_pct,
                        "rateMs": snap.comms.rate_ms,
                    },
                    "serverTs": snap.last_reading.ts,
                }
            )
        )

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                await websocket.send_text(json.dumps(msg))
            except asyncio.TimeoutError:
                # Keep-alive — many proxies drop idle WS at 30-60s.
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws error: %s", e)
    finally:
        bus.unsubscribe(q)
