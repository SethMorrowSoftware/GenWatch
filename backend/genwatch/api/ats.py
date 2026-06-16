"""ATS-Pi command endpoints — confirm-token gated (Phase 3).

Parallel to api/control.py. All four commands require a fresh confirm
token issued by GET /api/control/confirm (the token store is shared with
the H-100 control surface). Force-transfer additionally requires the
admin role and an explicit override flag when utility is available.

Returns 404 ats_disabled when the ATS-Pi integration isn't configured
(ats.enabled=false), so a non-ATS site gets a clean error rather than a
500.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..services.control import ControlError
from .deps import Principal, require_admin, require_operator

router = APIRouter(prefix="/api/ats", tags=["ats"])


class AtsPulseBody(BaseModel):
    """Momentary commands — test, bypass-delay."""

    confirm_token: str


class AtsMaintainedBody(BaseModel):
    """Maintained commands — inhibit, force-transfer.

    `assert` is a Python keyword, so it's aliased. The frontend sends
    `{"confirm_token": "...", "assert": true/false, "override": false}`.
    """

    model_config = ConfigDict(populate_by_name=True)

    confirm_token: str
    assert_: bool = Field(True, alias="assert")
    override: bool = False


def _svc(request: Request):
    svc = getattr(request.app.state, "ats_control", None)
    if svc is None:
        raise HTTPException(
            404,
            detail={"code": "ats_disabled", "message": "ATS-Pi integration is not enabled"},
        )
    return svc


async def _run(
    request: Request,
    command: str,
    token: str,
    p: Principal,
    *,
    assert_: bool = True,
    override: bool = False,
) -> dict:
    svc = _svc(request)
    # Per-operator rate limit on command actuation. The confirm-token flow
    # already blocks blind replay (single-use, 30 s TTL), but an authenticated
    # client could still loop token->command pairs and flap a maintained relay
    # faster than an operator can react. A small token bucket (burst 3, ~1/5 s)
    # is ample for human use.
    limiter = getattr(request.app.state, "command_limiter", None)
    if limiter is not None:
        key = f"ats:{p.operator}"
        if not limiter.check(key):
            retry = limiter.retry_after_s(key)
            request.app.state.db.write_audit(
                p.operator, f"ats.{command}", "rate_limited", token, "denied"
            )
            raise HTTPException(
                429,
                detail={"code": "rate_limited", "message": f"too many ATS commands; retry in {retry}s"},
            )
    try:
        return await svc.execute(
            command,
            token=token,
            operator=p.operator,
            role=p.role,
            assert_=assert_,
            override=override,
        )
    except ControlError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})


@router.post("/test")
async def ats_test(
    request: Request, body: AtsPulseBody, p: Principal = Depends(require_operator)
) -> dict:
    return await _run(request, "test", body.confirm_token, p)


@router.post("/inhibit")
async def ats_inhibit(
    request: Request, body: AtsMaintainedBody, p: Principal = Depends(require_operator)
) -> dict:
    return await _run(request, "inhibit", body.confirm_token, p, assert_=body.assert_)


@router.post("/force-transfer")
async def ats_force_transfer(
    request: Request, body: AtsMaintainedBody, p: Principal = Depends(require_admin)
) -> dict:
    return await _run(
        request, "force_transfer", body.confirm_token, p,
        assert_=body.assert_, override=body.override,
    )


@router.post("/bypass-delay")
async def ats_bypass_delay(
    request: Request, body: AtsPulseBody, p: Principal = Depends(require_operator)
) -> dict:
    return await _run(request, "bypass_delay", body.confirm_token, p)
