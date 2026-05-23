#!/usr/bin/env python3
"""
watch_oz_runs.py

Polls the Oz platform for the status of one or more cloud runs and prints a
formatted snapshot on each poll cycle. Exits automatically once every run
reaches a terminal state (succeeded, failed, error, cancelled).

Usage:
    python3 scripts/watch_oz_runs.py --runs-file runs/latest-run-ids.json
    python3 scripts/watch_oz_runs.py --runs-file runs/latest-run-ids.json --interval 30
    python3 scripts/watch_oz_runs.py --runs-file runs/latest-run-ids.json --once

The runs file is the JSON produced by run_parallel_oz_opencode.py. Both the
structured {'runs': [...]} format and a flat list of run IDs are supported.
"""

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

# States where the run has stopped and will not change further.
TERMINAL_STATES = {"succeeded", "failed", "error", "cancelled"}

# States that mean the run is still active and worth continuing to poll.
IN_PROGRESS_STATES = {"queued", "pending", "claimed", "in-progress", "blocked"}


def load_run_ids(path: pathlib.Path) -> List[Tuple[str, str]]:
    """
    Parse the runs JSON file and return a list of (job_name, run_id) tuples.

    Two file formats are accepted:
      1. Structured object: {"runs": [{"job_name": "...", "run_id": "..."}, ...]}
         This is the format produced by run_parallel_oz_opencode.py.
      2. Plain list: ["<uuid>", ...] or [{"run_id": "...", "job_name": "..."}, ...]
         Useful when run IDs are copied manually.

    Args:
        path: Resolved path to the runs JSON file.

    Returns:
        A list of (label, run_id) tuples. The label is the job_name when
        available, otherwise the run ID itself is used as the display label.

    Raises:
        ValueError: If the file format is not one of the two supported shapes.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Format 1: structured object with a 'runs' array (run_parallel output).
    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        result = []
        for run in data["runs"]:
            if not isinstance(run, dict):
                continue
            run_id = run.get("run_id")
            job_name = run.get("job_name", "")
            if isinstance(run_id, str) and run_id.strip():
                # Fall back to the run_id itself as the label if job_name is absent.
                result.append((job_name or run_id, run_id.strip()))
        return result

    # Format 2: flat list (plain UUIDs or dicts).
    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, str) and item.strip():
                # Plain UUID string; use it as both label and ID.
                result.append((item.strip(), item.strip()))
            elif isinstance(item, dict):
                run_id = item.get("run_id")
                name = item.get("job_name", run_id)
                if isinstance(run_id, str) and run_id.strip():
                    result.append((str(name), run_id.strip()))
        return result

    raise ValueError("Unsupported runs file format")


def run_get_json(run_id: str) -> Dict[str, Any]:
    """
    Call 'oz run get <run_id> --output-format json' and return the parsed payload.

    If the subprocess exits with a non-zero code, or if the output cannot be
    parsed as JSON, a synthetic error dict is returned instead of raising so
    that the polling loop can continue checking the remaining runs.

    Args:
        run_id: The UUID of the Oz run to query.

    Returns:
        The parsed JSON dict from Oz, or a synthetic dict with
        {"run_id": ..., "status": "error", "error": "<message>"} on failure.
    """
    cmd = ["oz", "run", "get", run_id, "--output-format", "json"]
    # check=False so we can handle non-zero exits gracefully without an exception.
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return {
            "run_id": run_id,
            "status": "error",
            "error": proc.stderr.strip() or proc.stdout.strip() or "oz run get failed",
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Oz may emit non-JSON preamble in some versions; treat as error rather
        # than crashing the watcher.
        return {
            "run_id": run_id,
            "status": "error",
            "error": "could not parse JSON output",
            "raw": proc.stdout,
        }
    return payload


def extract_status(payload: Dict[str, Any]) -> str:
    """
    Locate the run status string within an Oz API response payload.

    Oz has varied its response shape across versions; the status may live at
    the top level, inside a 'run' sub-object, or inside a 'task' sub-object.
    This function tries all known locations and returns the first non-empty
    string found, normalised to lowercase.

    Args:
        payload: The parsed JSON dict returned by run_get_json().

    Returns:
        The status string in lowercase, or 'unknown' if no recognisable
        status field is found anywhere in the payload.
    """
    candidates = []
    if isinstance(payload, dict):
        # Top-level status/state fields (most common in recent Oz versions).
        candidates.append(payload.get("status"))
        candidates.append(payload.get("state"))
        # Nested 'run' object (older Oz response shape).
        run = payload.get("run")
        if isinstance(run, dict):
            candidates.append(run.get("status"))
            candidates.append(run.get("state"))
        # Nested 'task' object (some Oz enterprise variants).
        task = payload.get("task")
        if isinstance(task, dict):
            candidates.append(task.get("status"))
            candidates.append(task.get("state"))

    # Return the first candidate that is a non-empty string.
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "unknown"


def print_snapshot(rows: List[Dict[str, str]]) -> None:
    """
    Print a formatted status table for all polled runs.

    Each row shows the job label, the run UUID, and the current status.
    A UTC timestamp header makes it easy to track when each snapshot was taken.

    Args:
        rows: List of dicts with keys 'job_name', 'run_id', and 'status'.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"\n[{now}] run status snapshot")
    print("-" * 72)
    for row in rows:
        # Column widths chosen to align UUIDs (36 chars) and keep output readable.
        print(f"{row['job_name']:<28} {row['run_id']:<36} {row['status']}")


def main() -> int:
    """
    Entry point: load run IDs, then poll Oz on a fixed interval until done.

    On each poll cycle every run is queried sequentially (not concurrently)
    since Oz rate-limits the API; the interval between cycles should account
    for the number of runs times the per-request latency.

    With --once the watcher prints a single snapshot and exits immediately,
    regardless of whether any run is still in progress.

    Returns:
        0 when all runs reach terminal states (or --once is used), 1 on error.
    """
    parser = argparse.ArgumentParser(description="Poll and print status for Oz cloud run IDs.")
    parser.add_argument(
        "--runs-file",
        required=True,
        help="Path to runs/latest-run-ids.json (or compatible JSON)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=20,
        help="Polling interval seconds (default: 20)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot and exit",
    )
    args = parser.parse_args()

    runs_file = pathlib.Path(args.runs_file).expanduser().resolve()
    if not runs_file.exists():
        print(f"Runs file not found: {runs_file}", file=sys.stderr)
        return 1

    try:
        runs = load_run_ids(runs_file)
    except Exception as exc:
        print(f"Could not load runs file: {exc}", file=sys.stderr)
        return 1

    if not runs:
        print("No run IDs found in runs file.", file=sys.stderr)
        return 1

    while True:
        rows: List[Dict[str, str]] = []
        # Assume terminal until we find a run that is not yet done.
        all_terminal = True

        for job_name, run_id in runs:
            payload = run_get_json(run_id)
            status = extract_status(payload)
            # If any single run is not in a terminal state, keep polling.
            if status not in TERMINAL_STATES:
                all_terminal = False
            rows.append({"job_name": job_name, "run_id": run_id, "status": status})

        print_snapshot(rows)

        if args.once:
            return 0
        if all_terminal:
            print("\nAll runs reached terminal states.")
            return 0
        # Enforce a minimum sleep of 1 second even if the user passes --interval 0.
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
