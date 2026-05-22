"""Control endpoints — confirm-token gated."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..services.control import ControlError
from .deps import Principal, require_operator

router = APIRouter(prefix="/api/control", tags=["control"])


class ControlBody(BaseModel):
    confirm_token: str


@router.get("/confirm")
async def confirm(request: Request, p: Principal = Depends(require_operator)) -> dict:
    ctl = request.app.state.control
    tok = await ctl.issue_token(p.operator)
    return {
        "token": tok.token,
        "issuedAt": tok.issued_at,
        "expiresAt": tok.expires_at,
    }


async def _run(request: Request, verb: str, body: ControlBody, p: Principal) -> dict:
    ctl = request.app.state.control
    try:
        return await ctl.execute(verb, body.confirm_token, p.operator, p.role)
    except ControlError as e:
        raise HTTPException(e.http_status, detail={"code": e.code, "message": str(e)})


@router.post("/start")
async def start(request: Request, body: ControlBody, p: Principal = Depends(require_operator)) -> dict:
    return await _run(request, "start", body, p)


@router.post("/stop")
async def stop(request: Request, body: ControlBody, p: Principal = Depends(require_operator)) -> dict:
    return await _run(request, "stop", body, p)


@router.post("/exercise")
async def exercise(request: Request, body: ControlBody, p: Principal = Depends(require_operator)) -> dict:
    return await _run(request, "exercise", body, p)


@router.post("/transfer")
async def transfer(request: Request, body: ControlBody, p: Principal = Depends(require_operator)) -> dict:
    return await _run(request, "transfer", body, p)
