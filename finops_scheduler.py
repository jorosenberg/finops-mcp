"""Friday-afternoon scheduler (Trigger B): invokes the finops-mcp pipeline
headlessly every Friday at 15:00 and logs to finops_pipeline.log.

Calls run_finops_cycle directly (in-process) rather than shelling out to an
agent CLI - simpler, no extra dependency on an agent runner being installed.
Requires: pip install schedule
"""

from __future__ import annotations

import json
import logging
import os
import time

import schedule

from finops_mcp import server

logging.basicConfig(
    filename="finops_pipeline.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

REPO_PATH = os.environ.get("FINOPS_REPO_PATH", os.getcwd())
PUSH_BRANCHES = os.environ.get("FINOPS_PUSH", "false").lower() == "true"


def job() -> None:
    logging.info("Initiating scheduled Friday afternoon FinOps optimization cycle...")
    try:
        raw = server.run_finops_cycle(
            repo_path=REPO_PATH, mode="scheduled", push=PUSH_BRANCHES
        )
        summary = json.loads(raw)
        logging.info(
            "FinOps cycle complete: %d drafted, %d failed, %d skipped (source=%s). Log: %s",
            summary.get("drafted", 0),
            summary.get("failed", 0),
            len(summary.get("skipped", [])),
            summary.get("telemetry_source"),
            summary.get("run_log", "finops_run_log.json"),
        )
    except Exception:
        logging.exception("FinOps cycle failed")


schedule.every().friday.at("15:00").do(job)

if __name__ == "__main__":
    logging.info("Continuous FinOps Optimization daemon started.")
    if os.environ.get("FINOPS_RUN_NOW", "false").lower() == "true":
        job()  # immediate run for testing
    while True:
        schedule.run_pending()
        time.sleep(60)
