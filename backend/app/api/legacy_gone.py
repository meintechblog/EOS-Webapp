from __future__ import annotations

from fastapi import APIRouter, HTTPException, status


router = APIRouter(tags=["legacy"])


def _gone(message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=message,
    )


@router.api_route("/api/input-channels", methods=["GET", "POST"])
def input_channels_gone() -> None:
    _gone("Legacy endpoint removed. Use /api/setup/fields and /eos/set/*.")


@router.api_route("/api/input-channels/{_channel_id}", methods=["PUT", "DELETE"])
def input_channels_item_gone(_channel_id: int) -> None:
    _gone("Legacy endpoint removed. Use /api/setup/fields and /eos/set/*.")


@router.post("/api/mappings/automap")
def automap_gone() -> None:
    _gone("Automap removed in HTTP-only mode. Use fixed /eos/set/* field paths.")


@router.api_route("/api/mappings", methods=["GET", "POST"])
def mappings_gone() -> None:
    _gone("Mapping API removed in HTTP-only mode. Use /api/setup/fields and /eos/set/*.")


@router.api_route("/api/mappings/{_mapping_id}", methods=["PUT", "DELETE"])
def mappings_item_gone(_mapping_id: int) -> None:
    _gone("Mapping API removed in HTTP-only mode. Use /api/setup/fields and /eos/set/*.")


@router.get("/api/live-values")
def live_values_gone() -> None:
    _gone("Live values API replaced. Use /api/setup/fields for unified live signal state.")


@router.get("/api/discovered-inputs")
def discovered_inputs_gone() -> None:
    _gone("Discovery removed in HTTP-only mode. Use /api/setup/fields.")


@router.get("/api/discovered-topics")
def discovered_topics_gone() -> None:
    _gone("Discovery removed in HTTP-only mode. Use /api/setup/fields.")


@router.api_route("/api/parameter-bindings", methods=["GET", "POST"])
def parameter_bindings_gone() -> None:
    _gone("Dynamic parameter bindings removed. Use /eos/set/param/* directly.")


@router.api_route("/api/parameter-bindings/{_binding_id}", methods=["PUT", "DELETE"])
def parameter_bindings_item_gone(_binding_id: int) -> None:
    _gone("Dynamic parameter bindings removed. Use /eos/set/param/* directly.")


@router.get("/api/parameter-bindings/events")
def parameter_binding_events_gone() -> None:
    _gone("Dynamic parameter bindings removed. Use /eos/set/param/* directly.")


@router.get("/api/setup/checklist")
def setup_checklist_gone() -> None:
    _gone("Checklist endpoint replaced by /api/setup/readiness.")
