"""Append-only JSONL event logger with ms timestamps."""

import threading
from pathlib import Path

import orjson

_lock = threading.Lock()


def append_event(filepath: Path, event: dict):
    line = orjson.dumps(event) + b"\n"
    with _lock:
        with open(filepath, "ab") as f:
            f.write(line)
