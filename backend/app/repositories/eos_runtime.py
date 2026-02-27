from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    EosArtifact,
    EosPlanInstruction,
    EosRun,
    EosRunInputSnapshot,
    OutputSignalAccessState,
    RuntimePreference,
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


def list_running_runs(db: Session) -> list[EosRun]:
    return list(
        db.scalars(
            select(EosRun)
            .where(EosRun.status == "running")
            .order_by(EosRun.started_at.asc(), EosRun.id.asc())
        )
    )


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


def get_latest_artifact(
    db: Session,
    *,
    artifact_type: str,
    artifact_key: str | None = None,
) -> EosArtifact | None:
    stmt = select(EosArtifact).where(EosArtifact.artifact_type == artifact_type)
    if artifact_key is not None:
        stmt = stmt.where(EosArtifact.artifact_key == artifact_key)
    return db.scalars(stmt.order_by(EosArtifact.created_at.desc(), EosArtifact.id.desc())).first()


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


def get_runtime_preference(db: Session, *, key: str) -> RuntimePreference | None:
    return db.get(RuntimePreference, key)


def upsert_runtime_preference(
    db: Session,
    *,
    key: str,
    value_json: dict[str, Any] | list[Any] | str | int | float | bool | None,
) -> RuntimePreference:
    existing = get_runtime_preference(db, key=key)
    if existing is None:
        preference = RuntimePreference(
            key=key,
            value_json=value_json,
        )
        db.add(preference)
        db.commit()
        db.refresh(preference)
        return preference

    existing.value_json = value_json  # type: ignore[assignment]
    db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


def get_output_signal_access_state(
    db: Session,
    *,
    signal_key: str,
) -> OutputSignalAccessState | None:
    return db.get(OutputSignalAccessState, signal_key)


def list_output_signal_access_states(
    db: Session,
    *,
    signal_keys: list[str],
) -> list[OutputSignalAccessState]:
    if not signal_keys:
        return []
    return list(
        db.scalars(
            select(OutputSignalAccessState).where(
                OutputSignalAccessState.signal_key.in_(signal_keys)
            )
        )
    )


def upsert_output_signal_access_state(
    db: Session,
    *,
    signal_key: str,
    resource_id: str | None,
    last_fetch_ts: datetime,
    last_fetch_client: str | None,
) -> OutputSignalAccessState:
    existing = get_output_signal_access_state(db, signal_key=signal_key)
    if existing is None:
        state = OutputSignalAccessState(
            signal_key=signal_key,
            resource_id=resource_id,
            last_fetch_ts=last_fetch_ts,
            last_fetch_client=last_fetch_client,
            fetch_count=1,
        )
        db.add(state)
        db.commit()
        db.refresh(state)
        return state

    existing.resource_id = resource_id
    existing.last_fetch_ts = last_fetch_ts
    existing.last_fetch_client = last_fetch_client
    existing.fetch_count = int(existing.fetch_count or 0) + 1
    db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


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
