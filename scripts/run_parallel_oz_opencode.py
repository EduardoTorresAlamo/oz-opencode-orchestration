#!/usr/bin/env python3
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

RUN_ID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "job"


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config root must be an object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Config must contain a non-empty jobs array")
    return data


def required(job: Dict[str, Any], key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Job '{job.get('name', '<unnamed>')}' is missing required '{key}'")
    return value.strip()


def build_prompt(job: Dict[str, Any]) -> str:
    repo = job.get("repo", "(unspecified repository)")
    task = required(job, "task")
    model_hint = job.get("opencode_model_hint", "")
    hint_line = f"- OpenCode model preference: {model_hint}" if model_hint else ""
    extra_instructions = job.get("opencode_extra_instructions", "")
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

    if scope == "team":
        cmd.append("--team")
    elif scope == "personal":
        cmd.append("--personal")
    else:
        raise ValueError(f"Invalid scope '{scope}' for job '{name}'. Use 'team' or 'personal'.")

    if open_after_submit:
        cmd.append("--open")

    return cmd


def extract_run_id(text: str) -> str:
    match = RUN_ID_PATTERN.search(text)
    return match.group(0) if match else ""


def run_one_job(job: Dict[str, Any], defaults: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
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

    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
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

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_root = pathlib.Path(args.output_dir).expanduser().resolve()
    run_dir = output_root / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    max_workers = max(1, min(args.max_workers, len(jobs)))
    print(
        f"Submitting {len(jobs)} job(s) with max_workers={max_workers} (dry_run={args.dry_run})"
    )

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_job = {
            executor.submit(run_one_job, job, defaults, args.dry_run): job for job in jobs
        }
        for future in concurrent.futures.as_completed(future_to_job):
            job = future_to_job[future]
            job_name = job.get("name", "<unnamed>")
            try:
                result = future.result()
            except Exception as exc:
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
                    print(f"[OK] {result['job_name']}: submitted (run ID not detected in output)")
                else:
                    print(
                        f"[ERROR] {result['job_name']}: exit_code={result['exit_code']}",
                        file=sys.stderr,
                    )

    results.sort(key=lambda r: r.get("job_name", ""))
    run_results_path = run_dir / "run-results.json"
    run_results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

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
