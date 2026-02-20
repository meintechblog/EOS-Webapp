from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    ControlTarget,
    EosArtifact,
    EosMqttOutputEvent,
    EosPlanInstruction,
    EosPredictionPoint,
    EosRun,
    EosRunInputSnapshot,
    OutputDispatchEvent,
    OutputTarget,
)


def create_run(
    db: Session,
    *,
    trigger_source: str,
    run_mode: str,
    eos_last_run_datetime: datetime | None,
    status: str = "running",
) -> EosRun:
    run = EosRun(
        trigger_source=trigger_source,
        run_mode=run_mode,
        eos_last_run_datetime=eos_last_run_datetime,
        status=status,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_run_by_id(db: Session, run_id: int) -> EosRun | None:
    return db.get(EosRun, run_id)


def get_run_by_eos_last_run_datetime(db: Session, eos_last_run_datetime: datetime) -> EosRun | None:
    return db.scalars(
        select(EosRun).where(EosRun.eos_last_run_datetime == eos_last_run_datetime)
    ).first()


def list_runs(db: Session, limit: int = 50) -> list[EosRun]:
    return list(db.scalars(select(EosRun).order_by(EosRun.created_at.desc()).limit(limit)))


def get_latest_successful_run(db: Session) -> EosRun | None:
    return db.scalars(
        select(EosRun)
        .where(EosRun.status == "success")
        .order_by(desc(EosRun.created_at), desc(EosRun.id))
    ).first()


def get_latest_successful_run_with_plan(db: Session) -> EosRun | None:
    plan_run_ids = select(EosPlanInstruction.run_id).distinct().subquery()
    return db.scalars(
        select(EosRun)
        .join(plan_run_ids, EosRun.id == plan_run_ids.c.run_id)
        .where(EosRun.status == "success")
        .order_by(desc(EosRun.created_at), desc(EosRun.id))
    ).first()


def update_run_status(
    db: Session,
    run: EosRun,
    *,
    status: str,
    error_text: str | None = None,
    finished_at: datetime | None = None,
) -> EosRun:
    run.status = status
    run.error_text = error_text
    run.finished_at = finished_at or datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def add_artifact(
    db: Session,
    *,
    run_id: int,
    artifact_type: str,
    artifact_key: str,
    payload_json: dict[str, Any] | list[Any],
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> EosArtifact:
    artifact = EosArtifact(
        run_id=run_id,
        artifact_type=artifact_type,
        artifact_key=artifact_key,
        payload_json=payload_json,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def list_artifacts_for_run(db: Session, run_id: int) -> list[EosArtifact]:
    return list(
        db.scalars(
            select(EosArtifact)
            .where(EosArtifact.run_id == run_id)
            .order_by(EosArtifact.created_at.asc(), EosArtifact.id.asc())
        )
    )


def get_latest_artifact_for_run(
    db: Session,
    *,
    run_id: int,
    artifact_type: str,
    artifact_key: str | None = None,
) -> EosArtifact | None:
    stmt = select(EosArtifact).where(
        EosArtifact.run_id == run_id,
        EosArtifact.artifact_type == artifact_type,
    )
    if artifact_key is not None:
        stmt = stmt.where(EosArtifact.artifact_key == artifact_key)
    return db.scalars(stmt.order_by(EosArtifact.created_at.desc(), EosArtifact.id.desc())).first()


def replace_prediction_points(
    db: Session,
    *,
    run_id: int,
    prediction_key: str,
    points: list[tuple[datetime, float | None]],
) -> None:
    db.query(EosPredictionPoint).filter(
        EosPredictionPoint.run_id == run_id,
        EosPredictionPoint.prediction_key == prediction_key,
    ).delete(synchronize_session=False)

    for ts, value in points:
        db.add(
            EosPredictionPoint(
                run_id=run_id,
                prediction_key=prediction_key,
                ts=ts,
                value=value,
            )
        )

    db.commit()


def list_prediction_points_for_run(db: Session, run_id: int) -> list[EosPredictionPoint]:
    return list(
        db.scalars(
            select(EosPredictionPoint)
            .where(EosPredictionPoint.run_id == run_id)
            .order_by(EosPredictionPoint.prediction_key.asc(), EosPredictionPoint.ts.asc())
        )
    )


def replace_plan_instructions(
    db: Session,
    *,
    run_id: int,
    instructions: list[dict[str, Any]],
    plan_id: str,
) -> None:
    db.query(EosPlanInstruction).filter(EosPlanInstruction.run_id == run_id).delete(
        synchronize_session=False
    )

    for idx, instruction in enumerate(instructions):
        db.add(
            EosPlanInstruction(
                run_id=run_id,
                plan_id=plan_id,
                instruction_index=idx,
                instruction_type=str(instruction.get("type") or "unknown"),
                resource_id=_as_optional_string(instruction.get("resource_id")),
                actuator_id=_as_optional_string(instruction.get("actuator_id")),
                starts_at=_parse_datetime(instruction.get("start_datetime") or instruction.get("starts_at")),
                ends_at=_parse_datetime(instruction.get("end_datetime") or instruction.get("ends_at")),
                execution_time=_parse_datetime(
                    instruction.get("execution_time")
                    or instruction.get("effective_at")
                    or instruction.get("start_datetime")
                    or instruction.get("starts_at")
                ),
                operation_mode_id=_as_optional_string(instruction.get("operation_mode_id")),
                operation_mode_factor=_as_optional_float(instruction.get("operation_mode_factor")),
                payload_json=instruction,
            )
        )

    db.commit()


def list_plan_instructions_for_run(db: Session, run_id: int) -> list[EosPlanInstruction]:
    return list(
        db.scalars(
            select(EosPlanInstruction)
            .where(EosPlanInstruction.run_id == run_id)
            .order_by(EosPlanInstruction.instruction_index.asc())
        )
    )


def add_output_event(
    db: Session,
    *,
    run_id: int,
    topic: str,
    payload_json: dict[str, Any] | list[Any],
    qos: int,
    retain: bool,
    publish_status: str,
    output_kind: str = "unknown",
    resource_id: str | None = None,
    error_text: str | None = None,
) -> EosMqttOutputEvent:
    event = EosMqttOutputEvent(
        run_id=run_id,
        topic=topic,
        payload_json=payload_json,
        qos=qos,
        retain=retain,
        output_kind=output_kind,
        resource_id=resource_id,
        publish_status=publish_status,
        error_text=error_text,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def upsert_run_input_snapshot(
    db: Session,
    *,
    run_id: int,
    parameter_profile_id: int | None,
    parameter_revision_id: int | None,
    parameter_payload_json: dict[str, Any] | list[Any],
    mappings_snapshot_json: dict[str, Any] | list[Any],
    live_state_snapshot_json: dict[str, Any] | list[Any],
    runtime_config_snapshot_json: dict[str, Any] | list[Any],
    assembled_eos_input_json: dict[str, Any] | list[Any],
) -> EosRunInputSnapshot:
    existing = db.scalars(
        select(EosRunInputSnapshot).where(EosRunInputSnapshot.run_id == run_id)
    ).first()

    if existing is None:
        snapshot = EosRunInputSnapshot(
            run_id=run_id,
            parameter_profile_id=parameter_profile_id,
            parameter_revision_id=parameter_revision_id,
            parameter_payload_json=parameter_payload_json,
            mappings_snapshot_json=mappings_snapshot_json,
            live_state_snapshot_json=live_state_snapshot_json,
            runtime_config_snapshot_json=runtime_config_snapshot_json,
            assembled_eos_input_json=assembled_eos_input_json,
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot

    existing.parameter_profile_id = parameter_profile_id
    existing.parameter_revision_id = parameter_revision_id
    existing.parameter_payload_json = parameter_payload_json
    existing.mappings_snapshot_json = mappings_snapshot_json
    existing.live_state_snapshot_json = live_state_snapshot_json
    existing.runtime_config_snapshot_json = runtime_config_snapshot_json
    existing.assembled_eos_input_json = assembled_eos_input_json
    db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


def get_run_input_snapshot(db: Session, run_id: int) -> EosRunInputSnapshot | None:
    return db.scalars(
        select(EosRunInputSnapshot).where(EosRunInputSnapshot.run_id == run_id)
    ).first()


def list_output_events(db: Session, limit: int = 100) -> list[EosMqttOutputEvent]:
    return list(
        db.scalars(
            select(EosMqttOutputEvent)
            .order_by(desc(EosMqttOutputEvent.published_at), desc(EosMqttOutputEvent.id))
            .limit(limit)
        )
    )


def list_output_targets(db: Session) -> list[OutputTarget]:
    return list(db.scalars(select(OutputTarget).order_by(OutputTarget.resource_id.asc())))


def get_output_target_by_id(db: Session, target_id: int) -> OutputTarget | None:
    return db.get(OutputTarget, target_id)


def get_output_target_by_resource_id(db: Session, resource_id: str) -> OutputTarget | None:
    return db.scalars(select(OutputTarget).where(OutputTarget.resource_id == resource_id)).first()


def create_output_target(
    db: Session,
    *,
    resource_id: str,
    webhook_url: str,
    method: str = "POST",
    headers_json: dict[str, Any] | list[Any] | None = None,
    enabled: bool = True,
    timeout_seconds: int = 10,
    retry_max: int = 2,
    payload_template_json: dict[str, Any] | list[Any] | None = None,
) -> OutputTarget:
    target = OutputTarget(
        resource_id=resource_id,
        webhook_url=webhook_url,
        method=method,
        headers_json=headers_json or {},
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        retry_max=retry_max,
        payload_template_json=payload_template_json,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def update_output_target(
    db: Session,
    target: OutputTarget,
    *,
    resource_id: str | None = None,
    webhook_url: str | None = None,
    method: str | None = None,
    headers_json: dict[str, Any] | list[Any] | None | object = ...,
    enabled: bool | None = None,
    timeout_seconds: int | None = None,
    retry_max: int | None = None,
    payload_template_json: dict[str, Any] | list[Any] | None | object = ...,
) -> OutputTarget:
    if resource_id is not None:
        target.resource_id = resource_id
    if webhook_url is not None:
        target.webhook_url = webhook_url
    if method is not None:
        target.method = method
    if headers_json is not ...:
        target.headers_json = headers_json or {}  # type: ignore[assignment]
    if enabled is not None:
        target.enabled = enabled
    if timeout_seconds is not None:
        target.timeout_seconds = timeout_seconds
    if retry_max is not None:
        target.retry_max = retry_max
    if payload_template_json is not ...:
        target.payload_template_json = payload_template_json  # type: ignore[assignment]

    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def get_output_dispatch_event_by_idempotency_key(
    db: Session,
    *,
    idempotency_key: str,
) -> OutputDispatchEvent | None:
    return db.scalars(
        select(OutputDispatchEvent).where(OutputDispatchEvent.idempotency_key == idempotency_key)
    ).first()


def has_output_dispatch_events_for_key_prefix(
    db: Session,
    *,
    idempotency_prefix: str,
) -> bool:
    row = db.execute(
        select(OutputDispatchEvent.id)
        .where(OutputDispatchEvent.idempotency_key.like(f"{idempotency_prefix}%"))
        .limit(1)
    ).first()
    return row is not None


def create_output_dispatch_event(
    db: Session,
    *,
    run_id: int | None,
    resource_id: str | None,
    execution_time: datetime | None,
    dispatch_kind: str,
    target_url: str | None,
    request_payload_json: dict[str, Any] | list[Any],
    status: str,
    http_status: int | None,
    error_text: str | None,
    idempotency_key: str,
) -> OutputDispatchEvent:
    event = OutputDispatchEvent(
        run_id=run_id,
        resource_id=resource_id,
        execution_time=execution_time,
        dispatch_kind=dispatch_kind,
        target_url=target_url,
        request_payload_json=request_payload_json,
        status=status,
        http_status=http_status,
        error_text=error_text,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def list_output_dispatch_events(
    db: Session,
    *,
    limit: int = 200,
    run_id: int | None = None,
    resource_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> list[OutputDispatchEvent]:
    stmt = select(OutputDispatchEvent)
    if run_id is not None:
        stmt = stmt.where(OutputDispatchEvent.run_id == run_id)
    if resource_id is not None:
        stmt = stmt.where(OutputDispatchEvent.resource_id == resource_id)
    if from_ts is not None:
        stmt = stmt.where(OutputDispatchEvent.execution_time >= from_ts)
    if to_ts is not None:
        stmt = stmt.where(OutputDispatchEvent.execution_time <= to_ts)
    stmt = stmt.order_by(desc(OutputDispatchEvent.created_at), desc(OutputDispatchEvent.id)).limit(limit)
    return list(db.scalars(stmt))


def list_control_targets(db: Session) -> list[ControlTarget]:
    return list(db.scalars(select(ControlTarget).order_by(ControlTarget.resource_id.asc())))


def get_control_target_by_id(db: Session, target_id: int) -> ControlTarget | None:
    return db.get(ControlTarget, target_id)


def get_control_target_by_resource_id(db: Session, resource_id: str) -> ControlTarget | None:
    return db.scalars(select(ControlTarget).where(ControlTarget.resource_id == resource_id)).first()


def create_control_target(
    db: Session,
    *,
    resource_id: str,
    command_topic: str,
    enabled: bool,
    dry_run_only: bool,
    qos: int,
    retain: bool,
    payload_template_json: dict[str, Any] | list[Any] | None,
) -> ControlTarget:
    target = ControlTarget(
        resource_id=resource_id,
        command_topic=command_topic,
        enabled=enabled,
        dry_run_only=dry_run_only,
        qos=qos,
        retain=retain,
        payload_template_json=payload_template_json,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def update_control_target(
    db: Session,
    target: ControlTarget,
    *,
    resource_id: str | None = None,
    command_topic: str | None = None,
    enabled: bool | None = None,
    dry_run_only: bool | None = None,
    qos: int | None = None,
    retain: bool | None = None,
    payload_template_json: dict[str, Any] | list[Any] | None | object = ...,
) -> ControlTarget:
    if resource_id is not None:
        target.resource_id = resource_id
    if command_topic is not None:
        target.command_topic = command_topic
    if enabled is not None:
        target.enabled = enabled
    if dry_run_only is not None:
        target.dry_run_only = dry_run_only
    if qos is not None:
        target.qos = qos
    if retain is not None:
        target.retain = retain
    if payload_template_json is not ...:
        target.payload_template_json = payload_template_json  # type: ignore[assignment]

    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
