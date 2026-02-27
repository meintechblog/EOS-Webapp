from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


class EosApiError(RuntimeError):
    def __init__(self, *, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"EOS API error {status_code}: {detail}")


@dataclass(frozen=True)
class EosHealthSnapshot:
    payload: dict[str, Any]
    eos_last_run_datetime: datetime | None


class EosClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 10.0):
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds

    def get_health(self) -> EosHealthSnapshot:
        payload = self._request_json("GET", "v1/health")
        energy_mgmt = payload.get("energy-management")
        last_run_raw = None
        if isinstance(energy_mgmt, dict):
            last_run_raw = energy_mgmt.get("last_run_datetime")
        return EosHealthSnapshot(
            payload=payload,
            eos_last_run_datetime=_parse_datetime(last_run_raw),
        )

    def get_config(self) -> dict[str, Any]:
        return self._request_json("GET", "v1/config")

    def get_openapi(self) -> dict[str, Any]:
        payload = self._request_json("GET", "openapi.json")
        if not isinstance(payload, dict):
            raise EosApiError(status_code=502, detail="EOS OpenAPI payload is not a JSON object")
        return payload

    def put_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request_json("PUT", "v1/config", payload=payload)
        if isinstance(result, dict):
            return result
        raise EosApiError(status_code=502, detail="EOS config update returned non-object payload")

    def save_config_file(self) -> dict[str, Any]:
        result = self._request_json("PUT", "v1/config/file")
        if isinstance(result, dict):
            return result
        raise EosApiError(status_code=502, detail="EOS config file save returned non-object payload")

    def put_config_path(self, path: str, value: Any) -> Any:
        clean_path = path.strip("/")
        status_code, content_type, body = self._request_raw(
            "PUT",
            f"v1/config/{clean_path}",
            payload=value,
        )
        if status_code not in (200, 201, 202, 204):
            raise EosApiError(status_code=status_code, detail=body or "Unexpected EOS response")
        if "application/json" in content_type.lower():
            try:
                return json.loads(body) if body else None
            except json.JSONDecodeError as exc:
                raise EosApiError(status_code=502, detail=f"Invalid EOS JSON response: {exc}") from exc
        return body or None

    def get_prediction_keys(self) -> list[str]:
        payload = self._request_json("GET", "v1/prediction/keys")
        if isinstance(payload, list):
            return [str(item) for item in payload]
        return []

    def get_prediction_providers(self, *, enabled: bool | None = None) -> list[str]:
        query: dict[str, Any] | None = None
        if enabled is not None:
            query = {"enabled": _bool_to_query(enabled)}
        payload = self._request_json("GET", "v1/prediction/providers", query=query)
        if isinstance(payload, list):
            return [str(item) for item in payload]
        return []

    def get_prediction_series(
        self,
        *,
        key: str,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"key": key}
        if start_datetime is not None:
            query["start_datetime"] = _format_datetime_utc(start_datetime)
        if end_datetime is not None:
            query["end_datetime"] = _format_datetime_utc(end_datetime)
        return self._request_json("GET", "v1/prediction/series", query=query)

    def get_prediction_list(
        self,
        *,
        key: str,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
        interval: str | None = None,
    ) -> list[Any]:
        query: dict[str, Any] = {"key": key}
        if start_datetime is not None:
            query["start_datetime"] = _format_datetime_utc(start_datetime)
        if end_datetime is not None:
            query["end_datetime"] = _format_datetime_utc(end_datetime)
        if interval is not None:
            query["interval"] = interval
        payload = self._request_json("GET", "v1/prediction/list", query=query)
        if isinstance(payload, list):
            return payload
        return []

    def restart_server(self) -> dict[str, Any]:
        payload = self._request_json("POST", "v1/admin/server/restart")
        if isinstance(payload, dict):
            return payload
        return {"payload": payload}

    def trigger_prediction_update(
        self,
        *,
        force_update: bool = False,
        force_enable: bool = False,
    ) -> None:
        self._request_no_content(
            "POST",
            "v1/prediction/update",
            query={
                "force_update": _bool_to_query(force_update),
                "force_enable": _bool_to_query(force_enable),
            },
        )

    def trigger_prediction_update_provider(
        self,
        *,
        provider_id: str,
        force_update: bool = False,
        force_enable: bool = False,
    ) -> None:
        clean_provider_id = provider_id.strip()
        if clean_provider_id == "":
            raise ValueError("provider_id must not be empty")
        self._request_no_content(
            "POST",
            f"v1/prediction/update/{clean_provider_id}",
            query={
                "force_update": _bool_to_query(force_update),
                "force_enable": _bool_to_query(force_enable),
            },
        )

    def get_plan(self) -> dict[str, Any]:
        return self._request_json("GET", "v1/energy-management/plan")

    def get_solution(self) -> dict[str, Any]:
        return self._request_json("GET", "v1/energy-management/optimization/solution")

    def get_measurement_keys(self) -> list[str]:
        payload = self._request_json("GET", "v1/measurement/keys")
        if isinstance(payload, list):
            return [str(item) for item in payload if isinstance(item, (str, int, float))]
        return []

    def run_optimize(
        self,
        *,
        payload: dict[str, Any],
        start_hour: int | None = None,
        ngen: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, str] = {}
        if start_hour is not None:
            query["start_hour"] = str(start_hour)
        if ngen is not None:
            query["ngen"] = str(ngen)
        # Legacy optimize can run significantly longer than regular metadata/config calls.
        optimize_timeout_seconds = max(self._timeout_seconds, 120.0)
        return self._request_json(
            "POST",
            "optimize",
            query=query or None,
            payload=payload,
            timeout_seconds=optimize_timeout_seconds,
        )

    def put_measurement_value(
        self,
        *,
        key: str,
        value: float | int | str,
        datetime_utc: datetime,
    ) -> None:
        self._request_no_content(
            "PUT",
            "v1/measurement/value",
            query={
                "key": key,
                "value": value,
                "datetime": _format_datetime_utc(datetime_utc),
            },
        )

    def put_measurement_series(
        self,
        *,
        key: str,
        series: dict[str, Any],
        dtype: str = "float64",
        tz: str = "UTC",
    ) -> None:
        self._request_no_content(
            "PUT",
            "v1/measurement/series",
            query={"key": key},
            payload={
                "data": series,
                "dtype": dtype,
                "tz": tz,
            },
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        status_code, content_type, body = self._request_raw(
            method,
            path,
            query=query,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        if status_code not in (200, 201):
            raise EosApiError(status_code=status_code, detail=body or "Unexpected EOS response")

        if "application/json" not in content_type.lower():
            return body
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise EosApiError(status_code=502, detail=f"Invalid EOS JSON response: {exc}") from exc

    def _request_no_content(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        status_code, _, body = self._request_raw(
            method,
            path,
            query=query,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        if status_code not in (200, 201, 202, 204):
            raise EosApiError(status_code=status_code, detail=body or "Unexpected EOS response")

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        endpoint = path.lstrip("/")
        url = urljoin(self._base_url, endpoint)
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered, doseq=True)}"

        data_bytes: bytes | None = None
        headers: dict[str, str] = {}
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, method=method.upper(), data=data_bytes, headers=headers)
        request_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None and float(timeout_seconds) > 0.0
            else self._timeout_seconds
        )
        try:
            with urlopen(request, timeout=request_timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, response.headers.get("content-type", ""), body
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise EosApiError(status_code=exc.code, detail=detail)
        except URLError as exc:
            raise EosApiError(status_code=503, detail=str(exc))
        except TimeoutError as exc:
            raise EosApiError(status_code=504, detail=str(exc))


def _bool_to_query(value: bool) -> str:
    return "true" if value else "false"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _format_datetime_utc(value: datetime) -> str:
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()
