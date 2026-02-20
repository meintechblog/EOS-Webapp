from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_parameter_catalog_service, get_parameter_profile_service
from app.schemas.parameters import (
    ParameterApplyRequest,
    ParameterCatalogResponse,
    ParameterDraftUpdateRequest,
    ParameterExportResponse,
    ParameterImportPreviewResponse,
    ParameterImportRequest,
    ParameterProfileCreateRequest,
    ParameterProfileDetailResponse,
    ParameterProfileSummaryResponse,
    ParameterProfileUpdateRequest,
    ParameterValidationResponse,
)
from app.services.parameter_profiles import ParameterProfileService
from app.services.parameters_catalog import ParameterCatalogService


router = APIRouter(prefix="/api/parameters", tags=["parameters"])


@router.get("/catalog", response_model=ParameterCatalogResponse)
def get_parameter_catalog(
    catalog_service: ParameterCatalogService = Depends(get_parameter_catalog_service),
) -> ParameterCatalogResponse:
    return ParameterCatalogResponse.model_validate(catalog_service.build_catalog())


@router.get("/profiles", response_model=list[ParameterProfileSummaryResponse])
def get_profiles(
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> list[ParameterProfileSummaryResponse]:
    profiles = profile_service.list_profiles_summary(db)
    return [ParameterProfileSummaryResponse.model_validate(item) for item in profiles]


@router.post("/profiles", response_model=ParameterProfileDetailResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ParameterProfileCreateRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterProfileDetailResponse:
    try:
        detail = profile_service.create_profile(
            db,
            name=payload.name.strip(),
            description=payload.description.strip() if isinstance(payload.description, str) else payload.description,
            clone_from_profile_id=payload.clone_from_profile_id,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{exc.orig}")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return ParameterProfileDetailResponse.model_validate(detail)


@router.put("/profiles/{profile_id}", response_model=ParameterProfileDetailResponse)
def update_profile(
    profile_id: int,
    payload: ParameterProfileUpdateRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterProfileDetailResponse:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided")
    try:
        detail = profile_service.update_profile(
            db,
            profile_id=profile_id,
            name=updates.get("name"),
            description=updates.get("description"),
            is_active=updates.get("is_active"),
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{exc.orig}")
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    return ParameterProfileDetailResponse.model_validate(detail)


@router.get("/profiles/{profile_id}", response_model=ParameterProfileDetailResponse)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterProfileDetailResponse:
    try:
        detail = profile_service.get_profile_detail(db, profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return ParameterProfileDetailResponse.model_validate(detail)


@router.put("/profiles/{profile_id}/draft", response_model=ParameterProfileDetailResponse)
def put_profile_draft(
    profile_id: int,
    payload: ParameterDraftUpdateRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterProfileDetailResponse:
    try:
        detail = profile_service.save_profile_draft(
            db,
            profile_id=profile_id,
            payload_json=payload.payload_json,
            source="manual",
        )
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    return ParameterProfileDetailResponse.model_validate(detail)


@router.post("/profiles/{profile_id}/validate", response_model=ParameterValidationResponse)
def validate_profile_draft(
    profile_id: int,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterValidationResponse:
    try:
        outcome = profile_service.validate_profile_draft(db, profile_id=profile_id)
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    return ParameterValidationResponse(
        valid=outcome.valid,
        errors=outcome.errors,
        warnings=outcome.warnings,
        normalized_payload=outcome.normalized_payload,
    )


@router.post("/profiles/{profile_id}/apply", response_model=ParameterValidationResponse)
def apply_profile(
    profile_id: int,
    payload: ParameterApplyRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterValidationResponse:
    try:
        outcome = profile_service.apply_profile(
            db,
            profile_id=profile_id,
            set_active_profile=payload.set_active_profile,
        )
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    return ParameterValidationResponse(
        valid=outcome.valid,
        errors=outcome.errors,
        warnings=outcome.warnings,
        normalized_payload=outcome.normalized_payload,
    )


@router.get("/profiles/{profile_id}/export", response_model=ParameterExportResponse)
def export_profile(
    profile_id: int,
    revision: str = Query(default="draft", pattern="^(draft|applied)$"),
    include_secrets: bool = Query(default=False),
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterExportResponse:
    try:
        export_payload = profile_service.export_profile(
            db,
            profile_id=profile_id,
            revision_selector=revision,
            include_secrets=include_secrets,
        )
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    return ParameterExportResponse.model_validate(export_payload)


@router.post("/profiles/{profile_id}/import/preview", response_model=ParameterImportPreviewResponse)
def preview_import(
    profile_id: int,
    payload: ParameterImportRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterImportPreviewResponse:
    try:
        preview = profile_service.preview_import(
            db,
            profile_id=profile_id,
            package_json=payload.package_json,
        )
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    return ParameterImportPreviewResponse.model_validate(preview)


@router.post("/profiles/{profile_id}/import/apply", response_model=ParameterProfileDetailResponse)
def apply_import(
    profile_id: int,
    payload: ParameterImportRequest,
    db: Session = Depends(get_db),
    profile_service: ParameterProfileService = Depends(get_parameter_profile_service),
) -> ParameterProfileDetailResponse:
    try:
        detail = profile_service.apply_import(
            db,
            profile_id=profile_id,
            package_json=payload.package_json,
        )
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)
    return ParameterProfileDetailResponse.model_validate(detail)
