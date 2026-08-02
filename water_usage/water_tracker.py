"""Codex Stop-hook tracker for token usage and approximate water use.

Formula to update after further research:
estimated_water_liters = total_tokens * liters_per_1000_tokens / 1000 - line 184

The configurable liters_per_1000_tokens constant lives in water_config.json.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# File locations are derived from this script so paths with spaces work safely.
CODEX_HOME = Path(__file__).resolve().parent.parent
CONFIG_PATH = CODEX_HOME / "water_config.json"
GLOBAL_HISTORY_PATH = CODEX_HOME / "water_usage.jsonl"
DEBUG_LOG_PATH = CODEX_HOME / "water_tracker_debug.jsonl"


def utc_timestamp() -> str:
    """Return a UTC timestamp for debug events."""
    return datetime.now(timezone.utc).isoformat()


def local_timestamp() -> str:
    """Return a local timestamp for user-facing usage-history records."""
    return datetime.now().astimezone().isoformat()


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object to a JSONL file without replacing old records."""
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True) + "\n")


def log_debug(stage: str, **details: Any) -> None:
    """Write diagnostics best-effort so a logging problem never stops Codex."""
    try:
        append_jsonl(DEBUG_LOG_PATH, {
            "timestamp": utc_timestamp(),
            "stage": stage,
            **details,
        })
    except OSError:
        pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON-object lines and safely skip blank or malformed records."""
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def require_string(payload: dict[str, Any], field_name: str) -> str:
    """Return one required non-empty Stop-payload field or raise an error."""
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Stop-hook payload is missing {field_name}")
    return value


def load_config() -> dict[str, Any]:
    """Load and validate every tracker setting from water_config.json."""
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    if not isinstance(config, dict):
        raise ValueError("water_config.json must contain a JSON object")

    liters_per_1000 = config.get("liters_per_1000_tokens")
    if not isinstance(liters_per_1000, (int, float)) or isinstance(liters_per_1000, bool):
        raise ValueError("liters_per_1000_tokens must be a number")
    if liters_per_1000 < 0:
        raise ValueError("liters_per_1000_tokens cannot be negative")

    for field_name in ("tracked_project_root", "project_history_path"):
        if not isinstance(config.get(field_name), str) or not config[field_name]:
            raise ValueError(f"{field_name} must be a non-empty path string")

    return config


def find_turn_start(entries: list[dict[str, Any]], turn_id: str) -> int | None:
    """Find the latest transcript index for the Stop hook's current turn."""
    matching_index = None
    for index, entry in enumerate(entries):
        if entry.get("type") != "turn_context":
            continue
        if entry.get("payload", {}).get("turn_id") == turn_id:
            matching_index = index
    return matching_index


def extract_turn_usage(transcript_path: Path, turn_id: str) -> dict[str, int] | None:
    """Sum token-count events from this turn context until the next turn starts."""
    entries = read_jsonl(transcript_path)
    turn_start = find_turn_start(entries, turn_id)
    if turn_start is None:
        return None

    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "usage_event_count": 0,
    }

    # One Codex turn can contain several model requests around tool calls.
    for entry in entries[turn_start + 1:]:
        if entry.get("type") == "turn_context":
            break

        event_payload = entry.get("payload", {})
        if entry.get("type") != "event_msg" or event_payload.get("type") != "token_count":
            continue

        last_usage = event_payload.get("info", {}).get("last_token_usage", {})
        if not isinstance(last_usage, dict):
            continue

        usage["input_tokens"] += int(last_usage.get("input_tokens", 0) or 0)
        usage["cached_input_tokens"] += int(last_usage.get("cached_input_tokens", 0) or 0)
        usage["output_tokens"] += int(last_usage.get("output_tokens", 0) or 0)
        usage["reasoning_tokens"] += int(last_usage.get("reasoning_output_tokens", 0) or 0)

        # Codex total_tokens already includes its own accounting. Cached input is a subset.
        usage["total_tokens"] += int(last_usage.get("total_tokens", 0) or 0)
        usage["usage_event_count"] += 1

    return usage if usage["usage_event_count"] else None


def is_duplicate_turn(session_id: str, turn_id: str) -> bool:
    """Prevent the global history from receiving the same completed turn twice."""
    if not GLOBAL_HISTORY_PATH.exists():
        return False
    return any(
        record.get("session_id") == session_id and record.get("turn_id") == turn_id
        for record in read_jsonl(GLOBAL_HISTORY_PATH)
    )


def is_inside_project(cwd: str, project_root: Path) -> bool:
    """Return True if cwd is the configured project root or one of its children."""
    try:
        Path(cwd).resolve(strict=False).relative_to(project_root.resolve(strict=False))
        return True
    except ValueError:
        return False


def build_history_record(
    session_id: str,
    turn_id: str,
    cwd: str,
    usage: dict[str, int],
    liters_per_1000_tokens: float,
) -> dict[str, Any]:
    """Build the shared record format used by global and project histories."""
    total_tokens = usage["total_tokens"]
    return {
        "timestamp": local_timestamp(),
        "session_id": session_id,
        "turn_id": turn_id,
        "project_cwd": cwd,
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "total_tokens": total_tokens,
        "estimated_water_liters": total_tokens * liters_per_1000_tokens / 1000,
    }


def process_stop_payload(payload: dict[str, Any]) -> None:
    """Extract one turn's usage and append global plus project-specific history."""
    session_id = require_string(payload, "session_id")
    turn_id = require_string(payload, "turn_id")
    transcript_path = Path(require_string(payload, "transcript_path"))
    cwd = require_string(payload, "cwd")

    if is_duplicate_turn(session_id, turn_id):
        log_debug("duplicate_turn_skipped", turn_id=turn_id)
        return

    usage = extract_turn_usage(transcript_path, turn_id)
    if usage is None:
        raise ValueError("no token_count event found for the current turn")

    config = load_config()
    record = build_history_record(
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        usage=usage,
        liters_per_1000_tokens=float(config["liters_per_1000_tokens"]),
    )

    # The project log records only D:\3_month_ai and its subfolders.
    if is_inside_project(cwd, Path(config["tracked_project_root"])):
        append_jsonl(Path(config["project_history_path"]), record)

    append_jsonl(GLOBAL_HISTORY_PATH, record)
    log_debug("water_usage_recorded", record=record)


def main() -> int:
    """Run the tracker without allowing tracker errors to block Codex stopping."""
    try:
        raw_payload = sys.stdin.read()
        payload = json.loads(raw_payload) if raw_payload.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("Stop-hook payload is not a JSON object")
        process_stop_payload(payload)
    except Exception as error:
        log_debug("water_tracker_error", error=f"{type(error).__name__}: {error}")

    # The Stop hook always succeeds, even if tracking or logging failed.
    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
