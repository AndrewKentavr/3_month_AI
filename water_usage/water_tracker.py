# First version - used AI to create this

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


CODEX_HOME = Path(__file__).resolve().parent.parent
CONFIG_PATH = CODEX_HOME / "water_config.json"
HISTORY_PATH = CODEX_HOME / "water_usage.jsonl"
DEBUG_LOG = CODEX_HOME / "water_tracker_debug.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=True) + "\n")


def append_debug_record(record: dict) -> None:
    """Best-effort logging: tracker failures must never block Codex."""
    try:
        append_jsonl(DEBUG_LOG, record)
    except OSError:
        pass


def read_jsonl(path: Path) -> list[dict]:
    entries = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def extract_turn_usage(transcript_path: Path, turn_id: str) -> dict | None:
    """Sum last-request usage records from this turn through the next turn."""
    entries = read_jsonl(transcript_path)
    start = None
    for index, entry in enumerate(entries):
        if entry.get("type") == "turn_context" and entry.get("payload", {}).get("turn_id") == turn_id:
            start = index

    if start is None:
        return None

    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "usage_event_count": 0,
    }
    for entry in entries[start + 1:]:
        if entry.get("type") == "turn_context":
            break

        payload = entry.get("payload", {})
        if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue

        last_usage = payload.get("info", {}).get("last_token_usage", {})
        if not isinstance(last_usage, dict):
            continue

        usage["input_tokens"] += int(last_usage.get("input_tokens", 0) or 0)
        usage["cached_input_tokens"] += int(last_usage.get("cached_input_tokens", 0) or 0)
        usage["output_tokens"] += int(last_usage.get("output_tokens", 0) or 0)
        usage["reasoning_tokens"] += int(last_usage.get("reasoning_output_tokens", 0) or 0)
        # Codex reports total_tokens independently; never add cached tokens again.
        usage["total_tokens"] += int(last_usage.get("total_tokens", 0) or 0)
        usage["usage_event_count"] += 1

    return usage if usage["usage_event_count"] else None


def load_liters_per_1000_tokens() -> float:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)
    value = config.get("liters_per_1000_tokens")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError("liters_per_1000_tokens must be a non-negative number")
    return float(value)


def already_recorded(session_id: str, turn_id: str) -> bool:
    if not HISTORY_PATH.exists():
        return False
    for record in read_jsonl(HISTORY_PATH):
        if record.get("session_id") == session_id and record.get("turn_id") == turn_id:
            return True
    return False


def main() -> int:
    try:
        raw_payload = sys.stdin.read()
        payload = json.loads(raw_payload) if raw_payload.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("Stop-hook payload is not a JSON object")

        session_id = payload.get("session_id")
        turn_id = payload.get("turn_id")
        transcript_value = payload.get("transcript_path")
        if not all(isinstance(value, str) and value for value in (session_id, turn_id, transcript_value)):
            raise ValueError("Stop-hook payload is missing session_id, turn_id, or transcript_path")

        if already_recorded(session_id, turn_id):
            append_debug_record({"timestamp": now(), "stage": "duplicate_turn_skipped", "turn_id": turn_id})
            return 0

        usage = extract_turn_usage(Path(transcript_value), turn_id)
        if usage is None:
            raise ValueError("no token_count event found for the current turn")

        liters_per_1000_tokens = load_liters_per_1000_tokens()
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "session_id": session_id,
            "turn_id": turn_id,
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "reasoning_tokens": usage["reasoning_tokens"],
            "total_tokens": usage["total_tokens"],
            "estimated_water_liters": usage["total_tokens"] * liters_per_1000_tokens / 1000,
        }
        append_jsonl(HISTORY_PATH, record)
        append_debug_record({"timestamp": now(), "stage": "water_usage_recorded", "record": record})
    except Exception as error:
        append_debug_record({
            "timestamp": now(),
            "stage": "water_tracker_error",
            "error": f"{type(error).__name__}: {error}",
        })

    print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
