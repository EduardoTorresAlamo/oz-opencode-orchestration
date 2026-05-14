#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

TERMINAL_STATES = {"succeeded", "failed", "error", "cancelled"}
IN_PROGRESS_STATES = {"queued", "pending", "claimed", "in-progress", "blocked"}


def load_run_ids(path: pathlib.Path) -> List[Tuple[str, str]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("runs"), list):
        result = []
        for run in data["runs"]:
            if not isinstance(run, dict):
                continue
            run_id = run.get("run_id")
            job_name = run.get("job_name", "")
            if isinstance(run_id, str) and run_id.strip():
                result.append((job_name or run_id, run_id.strip()))
        return result

    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, str) and item.strip():
                result.append((item.strip(), item.strip()))
            elif isinstance(item, dict):
                run_id = item.get("run_id")
                name = item.get("job_name", run_id)
                if isinstance(run_id, str) and run_id.strip():
                    result.append((str(name), run_id.strip()))
        return result

    raise ValueError("Unsupported runs file format")


def run_get_json(run_id: str) -> Dict[str, Any]:
    cmd = ["oz", "run", "get", run_id, "--output-format", "json"]
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
        return {
            "run_id": run_id,
            "status": "error",
            "error": "could not parse JSON output",
            "raw": proc.stdout,
        }
    return payload


def extract_status(payload: Dict[str, Any]) -> str:
    candidates = []
    if isinstance(payload, dict):
        candidates.append(payload.get("status"))
        candidates.append(payload.get("state"))
        run = payload.get("run")
        if isinstance(run, dict):
            candidates.append(run.get("status"))
            candidates.append(run.get("state"))
        task = payload.get("task")
        if isinstance(task, dict):
            candidates.append(task.get("status"))
            candidates.append(task.get("state"))

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "unknown"


def print_snapshot(rows: List[Dict[str, str]]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"\n[{now}] run status snapshot")
    print("-" * 72)
    for row in rows:
        print(f"{row['job_name']:<28} {row['run_id']:<36} {row['status']}")


def main() -> int:
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
        all_terminal = True

        for job_name, run_id in runs:
            payload = run_get_json(run_id)
            status = extract_status(payload)
            if status not in TERMINAL_STATES:
                all_terminal = False
            rows.append({"job_name": job_name, "run_id": run_id, "status": status})

        print_snapshot(rows)

        if args.once:
            return 0
        if all_terminal:
            print("\nAll runs reached terminal states.")
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    sys.exit(main())
