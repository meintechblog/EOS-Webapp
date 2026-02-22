from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_output_projection_service
from app.schemas.eos_runtime import EosOutputSignalsBundleResponse
from app.services.output_projection import OutputProjectionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["eos-output-signals"])


@router.get("/api/eos/output-signals", response_model=EosOutputSignalsBundleResponse)
def get_output_signals(
    run_id: int | None = None,
    db: Session = Depends(get_db),
    projection_service: OutputProjectionService = Depends(get_output_projection_service),
) -> EosOutputSignalsBundleResponse:
    bundle = projection_service.resolve_output_bundle(db, run_id=run_id)
    return EosOutputSignalsBundleResponse.model_validate(bundle)


@router.get("/eos/get/outputs", response_model=None)
def get_output_signals_external(
    request: Request,
    format: Literal["loxone", "json"] = Query(default="loxone"),
    run_id: int | None = None,
    db: Session = Depends(get_db),
    projection_service: OutputProjectionService = Depends(get_output_projection_service),
) -> PlainTextResponse | EosOutputSignalsBundleResponse:
    bundle = projection_service.resolve_output_bundle(db, run_id=run_id)
    signals_raw = bundle.get("signals")
    signal_entries = signals_raw if isinstance(signals_raw, dict) else {}
    client_id = _extract_client_id(request)
    fetch_state_by_signal = projection_service.record_bundle_fetch(
        db,
        signal_entries=signal_entries,
        client=client_id,
    )
    projection_service.apply_fetch_state_to_bundle(bundle, fetch_state_by_signal)

    status_summary = Counter(
        str(item.get("status") or "unknown")
        for item in signal_entries.values()
        if isinstance(item, dict)
    )
    logger.info(
        "output signals pull client=%s run_id=%s signal_count=%d status_summary=%s",
        client_id or "-",
        bundle.get("run_id"),
        len(signal_entries),
        dict(status_summary),
    )
    if format == "json":
        return EosOutputSignalsBundleResponse.model_validate(bundle)
    return PlainTextResponse(
        content=_render_loxone_signal_payload(signal_entries),
        media_type="text/plain; charset=utf-8",
    )


def _extract_client_id(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if isinstance(forwarded_for, str) and forwarded_for.strip() != "":
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    if request.client is not None and request.client.host:
        return request.client.host.strip() or None
    return None


def _render_loxone_signal_payload(signal_entries: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for signal_key in sorted(signal_entries.keys()):
        row = signal_entries.get(signal_key)
        requested_power_kw = row.get("requested_power_kw") if isinstance(row, dict) else None
        lines.append(f"{signal_key}:{_format_numeric_value(requested_power_kw)}")
    return "\n".join(lines)


def _format_numeric_value(value: Any) -> str:
    if isinstance(value, bool):
        return "0.0"
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric):
            compact = f"{numeric:.3f}".rstrip("0").rstrip(".")
            return f"{compact}.0" if "." not in compact else compact
    return "0.0"
