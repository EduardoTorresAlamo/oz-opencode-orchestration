#!/usr/bin/env python3
"""
run_parallel_oz_opencode.py

Reads a jobs JSON config and submits each job as a parallel Oz cloud run.
Each run instructs an Oz agent to delegate all code changes to OpenCode.

Usage:
    python3 scripts/run_parallel_oz_opencode.py --config jobs.json
    python3 scripts/run_parallel_oz_opencode.py --config jobs.json --max-workers 6
    python3 scripts/run_parallel_oz_opencode.py --config jobs.json --dry-run

Output artifacts are written to a timestamped subdirectory under --output-dir (default: runs/).
  runs/<timestamp>/
    <job-name>.log      -- per-job log with command, prompt, stdout, stderr
    run-results.json    -- full result objects for every job
    run-ids.json        -- just job_name + run_id pairs for jobs that were submitted
  runs/latest-run-ids.json  -- symlink-equivalent: always holds the last batch's run IDs
                               (used directly by watch_oz_runs.py)
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import textwrap
from typing import Any, Dict, List

# Regex that matches a UUID v4 anywhere in a string.
# Oz prints the run ID in its output text; we extract it from stdout + stderr combined.
RUN_ID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def slugify(name: str) -> str:
    """
    Convert a job name into a filesystem-safe slug.

    Replaces any run of non-alphanumeric characters (except '.', '_', '-')
    with a single hyphen, then strips leading/trailing hyphens.

    Args:
        name: The raw job name string.

    Returns:
        A slug safe for use as a filename component, or 'job' if the
        result would otherwise be empty.
    """
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "job"


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    """
    Parse and minimally validate the jobs JSON config file.

    The config must be a JSON object with at least a 'jobs' key containing
    a non-empty array. An optional 'defaults' object provides fallback values
    for fields not specified per job.

    Args:
        path: Resolved path to the JSON config file.

    Returns:
        The parsed config dict.

    Raises:
        ValueError: If the JSON root is not an object, or if 'jobs' is
                    missing, not a list, or empty.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config root must be an object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Config must contain a non-empty jobs array")
    return data


def required(job: Dict[str, Any], key: str) -> str:
    """
    Extract a required string field from a job dict, raising on failure.

    Args:
        job:  The job configuration dict.
        key:  The field name to retrieve.

    Returns:
        The stripped string value for the field.

    Raises:
        ValueError: If the field is absent, not a string, or blank.
    """
    value = job.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Job '{job.get('name', '<unnamed>')}' is missing required '{key}'")
    return value.strip()


def build_prompt(job: Dict[str, Any]) -> str:
    """
    Construct the natural-language prompt that Oz will pass to the cloud agent.

    The prompt instructs the agent to use the oz-platform skill and to
    delegate all code work to OpenCode rather than using Oz's built-in tools.
    Job-level fields (repo, task, opencode_model_hint, opencode_extra_instructions)
    are interpolated into the prompt body.

    Args:
        job: The job configuration dict.

    Returns:
        The fully assembled prompt string (no trailing newline).
    """
    repo = job.get("repo", "(unspecified repository)")
    task = required(job, "task")
    model_hint = job.get("opencode_model_hint", "")
    # Only include the model preference line when explicitly set in the job config.
    hint_line = f"- OpenCode model preference: {model_hint}" if model_hint else ""
    extra_instructions = job.get("opencode_extra_instructions", "")
    # Append extra instructions block only when present to keep the prompt clean.
    extra_block = f"\nAdditional instructions:\n{extra_instructions}" if extra_instructions else ""

    prompt = textwrap.dedent(
        f"""\
        Read the oz-platform skill for instructions on using OpenCode.
        You must delegate all repository analysis, code edits, test execution, and git operations to OpenCode.
        Do not use Warp built-in coding tools to perform code changes directly.
        If a pull request is created, ensure OpenCode prints the full PR URL and branch name, then parse them and call report_pr.

        Task scope:
        - Repository: {repo}
        - Task: {task}
        {hint_line}{extra_block}
        """
    ).strip()
    return prompt


def build_command(job: Dict[str, Any], defaults: Dict[str, Any]) -> List[str]:
    """
    Build the 'oz agent run-cloud' CLI invocation for a single job.

    Job-level values override the shared defaults dict for every optional
    field. The 'scope' field determines whether --team or --personal is
    appended; any other value raises immediately rather than silently using
    a wrong flag.

    Args:
        job:      The job configuration dict.
        defaults: The shared defaults dict from the config root.

    Returns:
        The argument list ready to pass to subprocess.run().

    Raises:
        ValueError: If a required field is missing or 'scope' is not
                    'team' or 'personal'.
    """
    name = required(job, "name")
    environment_id = required(job, "environment_id")
    model = str(job.get("oz_model", defaults.get("oz_model", "auto-efficient")))
    scope = str(job.get("scope", defaults.get("scope", "personal"))).strip().lower()
    open_after_submit = bool(job.get("open_after_submit", defaults.get("open_after_submit", False)))
    prompt = build_prompt(job)

    cmd = [
        "oz",
        "agent",
        "run-cloud",
        "--name",
        name,
        "--environment",
        environment_id,
        "--model",
        model,
        "--prompt",
        prompt,
        "--output-format",
        "text",
    ]

    # Oz requires exactly one scope flag; anything else is a misconfiguration.
    if scope == "team":
        cmd.append("--team")
    elif scope == "personal":
        cmd.append("--personal")
    else:
        raise ValueError(f"Invalid scope '{scope}' for job '{name}'. Use 'team' or 'personal'.")

    if open_after_submit:
        # --open tells Oz to open the run URL in the default browser after submission.
        cmd.append("--open")

    return cmd


def extract_run_id(text: str) -> str:
    """
    Scan a block of text for the first UUID and return it.

    Oz prints the assigned run ID somewhere in its output (stdout or stderr
    depending on the version). Searching the combined output handles both cases.

    Args:
        text: The combined stdout + stderr string from the oz invocation.

    Returns:
        The UUID string if found, or an empty string if none is present.
    """
    match = RUN_ID_PATTERN.search(text)
    return match.group(0) if match else ""


def run_one_job(job: Dict[str, Any], defaults: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """
    Submit a single Oz cloud run (or simulate it in dry-run mode).

    In dry-run mode the command is assembled and logged but subprocess.run()
    is never called, so no Oz run is actually created.

    In live mode the subprocess is launched with capture_output=True so that
    stdout and stderr are available for logging and run-ID extraction even when
    the caller is running multiple jobs concurrently on a thread pool.

    Args:
        job:      The job configuration dict.
        defaults: The shared defaults dict from the config root.
        dry_run:  When True, skip the actual subprocess call.

    Returns:
        A result dict containing: job_name, environment_id, repo, oz_model,
        scope, command, prompt, dry_run, exit_code, stdout, stderr, run_id.
    """
    cmd = build_command(job, defaults)
    prompt = build_prompt(job)
    result: Dict[str, Any] = {
        "job_name": required(job, "name"),
        "environment_id": required(job, "environment_id"),
        "repo": job.get("repo", ""),
        "oz_model": str(job.get("oz_model", defaults.get("oz_model", "auto-efficient"))),
        "scope": str(job.get("scope", defaults.get("scope", "personal"))),
        "command": cmd,
        "prompt": prompt,
    }

    if dry_run:
        result.update(
            {
                "dry_run": True,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "run_id": "",
            }
        )
        return result

    # check=False so we can capture and log non-zero exits rather than raising.
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    # Oz may print the run ID to either stream depending on version; search both.
    combined_output = f"{proc.stdout}\n{proc.stderr}"
    result.update(
        {
            "dry_run": False,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "run_id": extract_run_id(combined_output),
        }
    )
    return result


def write_logs(run_dir: pathlib.Path, result: Dict[str, Any]) -> pathlib.Path:
    """
    Write a human-readable log file for one job result.

    The log includes all metadata (command, prompt, exit code, run ID) followed
    by the raw stdout and stderr. The filename is the slugified job name so logs
    are stable and easy to find in the timestamped run directory.

    Args:
        run_dir: The per-batch output directory to write into.
        result:  The result dict returned by run_one_job().

    Returns:
        The path of the written log file.
    """
    log_path = run_dir / f"{slugify(result['job_name'])}.log"
    lines = [
        f"job_name: {result['job_name']}",
        f"environment_id: {result['environment_id']}",
        f"repo: {result.get('repo', '')}",
        f"oz_model: {result.get('oz_model', '')}",
        f"scope: {result.get('scope', '')}",
        f"dry_run: {result.get('dry_run', False)}",
        f"exit_code: {result.get('exit_code', '')}",
        f"run_id: {result.get('run_id', '')}",
        "",
        "command:",
        # list2cmdline quotes individual args correctly for display (Windows-style
        # quoting is still human-readable on macOS/Linux for inspection purposes).
        " ".join(subprocess.list2cmdline([part]) for part in result["command"]),
        "",
        "prompt:",
        result["prompt"],
        "",
        "stdout:",
        result.get("stdout", ""),
        "",
        "stderr:",
        result.get("stderr", ""),
        "",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def main() -> int:
    """
    Entry point: parse arguments, load config, submit jobs, write artifacts.

    Jobs are dispatched concurrently via a ThreadPoolExecutor. The worker
    count is capped at the number of jobs so we never spin up idle threads.
    Results are collected as futures complete (not necessarily in submission
    order), then sorted by job name before being written to JSON.

    Three JSON artifacts are produced after every live run:
      - run-results.json   full result objects
      - run-ids.json       {job_name, run_id} for successfully submitted runs
      - latest-run-ids.json  same content, always at a fixed path for watchers

    Returns:
        0 on success (all jobs submitted), 1 on any config or submission error.
    """
    parser = argparse.ArgumentParser(
        description="Launch parallel Oz cloud runs that delegate code changes to OpenCode."
    )
    parser.add_argument("--config", required=True, help="Path to jobs JSON config")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Maximum concurrent submissions (default: 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print and log generated commands without launching runs",
    )
    parser.add_argument(
        "--output-dir",
        default="runs",
        help="Directory for logs/results (default: runs)",
    )
    args = parser.parse_args()

    config_path = pathlib.Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Invalid config: {exc}", file=sys.stderr)
        return 1

    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        print("Invalid config: defaults must be an object", file=sys.stderr)
        return 1
    jobs: List[Dict[str, Any]] = config["jobs"]

    # UTC timestamp in the directory name makes batches easy to sort and identify.
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_root = pathlib.Path(args.output_dir).expanduser().resolve()
    run_dir = output_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # Cap workers at the number of jobs so we never spin up idle threads.
    max_workers = max(1, min(args.max_workers, len(jobs)))
    print(
        f"Submitting {len(jobs)} job(s) with max_workers={max_workers} (dry_run={args.dry_run})"
    )

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Map each future back to its originating job so we can log which job
        # raised an exception when a future fails unexpectedly.
        future_to_job = {
            executor.submit(run_one_job, job, defaults, args.dry_run): job for job in jobs
        }
        # as_completed yields futures in completion order, not submission order.
        for future in concurrent.futures.as_completed(future_to_job):
            job = future_to_job[future]
            job_name = job.get("name", "<unnamed>")
            try:
                result = future.result()
            except Exception as exc:
                # Build a minimal failure result so the job still appears in
                # logs and run-results.json with a descriptive error message.
                result = {
                    "job_name": job_name,
                    "environment_id": job.get("environment_id", ""),
                    "repo": job.get("repo", ""),
                    "oz_model": str(job.get("oz_model", defaults.get("oz_model", ""))),
                    "scope": str(job.get("scope", defaults.get("scope", ""))),
                    "command": [],
                    "prompt": "",
                    "dry_run": args.dry_run,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "run_id": "",
                }

            results.append(result)
            write_logs(run_dir, result)
            if args.dry_run:
                print(f"[DRY-RUN] {result['job_name']}: command prepared")
            else:
                if result["exit_code"] == 0 and result["run_id"]:
                    print(f"[OK] {result['job_name']}: run_id={result['run_id']}")
                elif result["exit_code"] == 0:
                    # Oz submitted the run but we could not find a UUID in output.
                    # Still a success; the user can check the Oz dashboard manually.
                    print(f"[OK] {result['job_name']}: submitted (run ID not detected in output)")
                else:
                    print(
                        f"[ERROR] {result['job_name']}: exit_code={result['exit_code']}",
                        file=sys.stderr,
                    )

    # Sort alphabetically so run-results.json is deterministic across runs.
    results.sort(key=lambda r: r.get("job_name", ""))
    run_results_path = run_dir / "run-results.json"
    run_results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    # run-ids.json only contains jobs where we captured a UUID; jobs that
    # submitted without a detectable ID are intentionally excluded here because
    # watch_oz_runs.py cannot poll them without a known run ID.
    run_ids_payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "runs": [
            {"job_name": r["job_name"], "run_id": r["run_id"]}
            for r in results
            if r.get("run_id")
        ],
    }
    run_ids_path = run_dir / "run-ids.json"
    run_ids_path.write_text(json.dumps(run_ids_payload, indent=2), encoding="utf-8")

    # Overwrite the fixed 'latest' path so watch_oz_runs.py can be pointed at
    # a stable location without needing to know the current timestamp.
    latest_path = output_root / "latest-run-ids.json"
    latest_path.write_text(json.dumps(run_ids_payload, indent=2), encoding="utf-8")

    print(f"\nArtifacts written to: {run_dir}")
    print(f"- Results: {run_results_path}")
    print(f"- Run IDs: {run_ids_path}")
    print(f"- Latest run IDs: {latest_path}")

    failures = [r for r in results if r.get("exit_code") != 0]
    if failures:
        print(f"{len(failures)} job(s) failed to submit.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
