import json
import logging


def _stringify(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _parse_scalar_payload(raw_payload: str) -> str | None:
    stripped = raw_payload.strip()
    if stripped == "":
        return None

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped

    return _stringify(decoded)


def parse_payload(raw_payload: str, payload_path: str | None, logger: logging.Logger) -> str | None:
    if not payload_path:
        return _parse_scalar_payload(raw_payload)

    try:
        decoded_json = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.warning("payload_path is set but payload is not valid JSON path=%s", payload_path)
        return None

    current: object = decoded_json
    for part in payload_path.split("."):
        if not isinstance(current, dict) or part not in current:
            logger.warning("payload_path not found path=%s payload=%s", payload_path, raw_payload)
            return None
        current = current[part]

    return _stringify(current)

