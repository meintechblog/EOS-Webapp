from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ParameterProfile, ParameterProfileRevision


def list_parameter_profiles(db: Session) -> list[ParameterProfile]:
    return list(db.scalars(select(ParameterProfile).order_by(ParameterProfile.name.asc())))


def get_parameter_profile_by_id(db: Session, profile_id: int) -> ParameterProfile | None:
    return db.get(ParameterProfile, profile_id)


def get_parameter_profile_by_name(db: Session, name: str) -> ParameterProfile | None:
    return db.scalars(select(ParameterProfile).where(ParameterProfile.name == name)).first()


def get_active_parameter_profile(db: Session) -> ParameterProfile | None:
    return db.scalars(
        select(ParameterProfile).where(ParameterProfile.is_active.is_(True))
    ).first()


def create_parameter_profile(
    db: Session,
    *,
    name: str,
    description: str | None,
    is_active: bool = False,
) -> ParameterProfile:
    profile = ParameterProfile(
        name=name,
        description=description,
        is_active=is_active,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def update_parameter_profile(
    db: Session,
    profile: ParameterProfile,
    *,
    name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
) -> ParameterProfile:
    if name is not None:
        profile.name = name
    if description is not None:
        profile.description = description
    if is_active is not None:
        profile.is_active = is_active
    profile.updated_at = datetime.now(timezone.utc)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def set_active_parameter_profile(db: Session, profile_id: int) -> None:
    db.query(ParameterProfile).update({ParameterProfile.is_active: False}, synchronize_session=False)
    profile = get_parameter_profile_by_id(db, profile_id)
    if profile is None:
        db.rollback()
        raise ValueError("Profile not found")
    profile.is_active = True
    profile.updated_at = datetime.now(timezone.utc)
    db.add(profile)
    db.commit()


def list_profile_revisions(
    db: Session,
    *,
    profile_id: int,
    limit: int = 30,
) -> list[ParameterProfileRevision]:
    return list(
        db.scalars(
            select(ParameterProfileRevision)
            .where(ParameterProfileRevision.profile_id == profile_id)
            .order_by(ParameterProfileRevision.revision_no.desc())
            .limit(limit)
        )
    )


def get_profile_revision_by_id(
    db: Session,
    *,
    profile_id: int,
    revision_id: int,
) -> ParameterProfileRevision | None:
    return db.scalars(
        select(ParameterProfileRevision).where(
            ParameterProfileRevision.profile_id == profile_id,
            ParameterProfileRevision.id == revision_id,
        )
    ).first()


def get_profile_revision_by_no(
    db: Session,
    *,
    profile_id: int,
    revision_no: int,
) -> ParameterProfileRevision | None:
    return db.scalars(
        select(ParameterProfileRevision).where(
            ParameterProfileRevision.profile_id == profile_id,
            ParameterProfileRevision.revision_no == revision_no,
        )
    ).first()


def get_current_draft_revision(
    db: Session,
    *,
    profile_id: int,
) -> ParameterProfileRevision | None:
    return db.scalars(
        select(ParameterProfileRevision).where(
            ParameterProfileRevision.profile_id == profile_id,
            ParameterProfileRevision.is_current_draft.is_(True),
        )
    ).first()


def get_last_applied_revision(
    db: Session,
    *,
    profile_id: int,
) -> ParameterProfileRevision | None:
    return db.scalars(
        select(ParameterProfileRevision).where(
            ParameterProfileRevision.profile_id == profile_id,
            ParameterProfileRevision.is_last_applied.is_(True),
        )
    ).first()


def get_latest_revision(db: Session, *, profile_id: int) -> ParameterProfileRevision | None:
    return db.scalars(
        select(ParameterProfileRevision)
        .where(ParameterProfileRevision.profile_id == profile_id)
        .order_by(ParameterProfileRevision.revision_no.desc())
        .limit(1)
    ).first()


def create_profile_revision(
    db: Session,
    *,
    profile_id: int,
    source: str,
    payload_json: dict[str, Any],
    validation_status: str = "unknown",
    validation_issues_json: dict[str, Any] | list[Any] | None = None,
    set_current_draft: bool = True,
) -> ParameterProfileRevision:
    next_revision_no = _next_profile_revision_no(db, profile_id=profile_id)

    if set_current_draft:
        db.query(ParameterProfileRevision).filter(
            ParameterProfileRevision.profile_id == profile_id,
            ParameterProfileRevision.is_current_draft.is_(True),
        ).update({ParameterProfileRevision.is_current_draft: False}, synchronize_session=False)

    revision = ParameterProfileRevision(
        profile_id=profile_id,
        revision_no=next_revision_no,
        source=source,
        payload_json=payload_json,
        validation_status=validation_status,
        validation_issues_json=validation_issues_json,
        is_current_draft=set_current_draft,
        is_last_applied=False,
    )
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return revision


def update_profile_revision_validation(
    db: Session,
    revision: ParameterProfileRevision,
    *,
    validation_status: str,
    validation_issues_json: dict[str, Any] | list[Any] | None,
) -> ParameterProfileRevision:
    revision.validation_status = validation_status
    revision.validation_issues_json = validation_issues_json
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return revision


def mark_revision_as_last_applied(
    db: Session,
    *,
    profile_id: int,
    revision_id: int,
    applied_at: datetime | None = None,
) -> ParameterProfileRevision:
    db.query(ParameterProfileRevision).filter(
        ParameterProfileRevision.profile_id == profile_id,
        ParameterProfileRevision.is_last_applied.is_(True),
    ).update({ParameterProfileRevision.is_last_applied: False}, synchronize_session=False)

    revision = get_profile_revision_by_id(db, profile_id=profile_id, revision_id=revision_id)
    if revision is None:
        db.rollback()
        raise ValueError("Revision not found")

    revision.is_last_applied = True
    revision.applied_at = applied_at or datetime.now(timezone.utc)
    revision.is_current_draft = True
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return revision


def _next_profile_revision_no(db: Session, *, profile_id: int) -> int:
    current = db.scalar(
        select(func.max(ParameterProfileRevision.revision_no)).where(
            ParameterProfileRevision.profile_id == profile_id
        )
    )
    if current is None:
        return 1
    return int(current) + 1
