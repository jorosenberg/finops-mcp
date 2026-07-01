"""Structured JSON run logging for scheduled (headless) executions."""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

LOG_FILENAME = "finops_run_log.json"


def append_run(repo_path: str, entry: dict[str, Any]) -> str:
    path = os.path.join(repo_path, LOG_FILENAME)
    runs: list[dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                runs = json.load(f)
        except (json.JSONDecodeError, OSError):
            runs = []
    entry = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), **entry}
    runs.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(runs, f, indent=2, default=str)
    return path
