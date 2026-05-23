# oz-opencode-orchestration

Scripts for launching multiple Oz cloud runs in parallel, where each run delegates all code changes to an OpenCode agent.

The pattern: Oz spins up a cloud environment for a given repo, and the agent's prompt instructs it to hand off all analysis, edits, test execution, and git operations to OpenCode. You submit many of these jobs at once and watch them run concurrently.

## What it does

1. Takes a JSON config listing jobs, where each job maps a named task to an Oz environment.
2. Builds the `oz agent run-cloud` command for each job, injecting a structured prompt that tells the Oz agent to use OpenCode for all code work.
3. Submits all jobs in parallel using a thread pool.
4. Writes run IDs and structured logs to a timestamped `runs/` directory.
5. Provides a watcher script to poll `oz run get` until all runs reach a terminal state.

## Prerequisites

- [Oz CLI](https://warp.dev/oz) — installed and on your PATH
- Python 3.9+
- At least one provider API key stored as an Oz secret (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`)
- An Oz environment already created for each repo you want to target (see Step 3 below)

No Python dependencies beyond the standard library.

## Quickstart

### Step 1 — Authenticate

```bash
oz login
```

### Step 2 — Create provider secrets

Store your LLM API keys as Oz secrets so they are injected automatically into cloud runs:

```bash
oz secret create --team ANTHROPIC_API_KEY
oz secret create --team OPENAI_API_KEY
oz secret create --team GEMINI_API_KEY
```

Use `--personal` instead of `--team` for user-scoped secrets. Create only the keys you need.

### Step 3 — Create one environment per repo

The `create_environment_with_opencode.sh` script creates an Oz environment, clones the target repo, and installs OpenCode during setup:

```bash
./scripts/create_environment_with_opencode.sh --name my-repo-env --repo your-org/my-repo --team
```

Run this once per repo. The environment ID printed at the end goes into your `jobs.json`.

Options:

| Flag | Description |
|------|-------------|
| `--name` | Environment name (required) |
| `--repo` | GitHub repo to clone, e.g. `your-org/repo-a` (required, repeatable) |
| `--image` | Docker image (default: `warpdotdev/dev-full:latest-agents`) |
| `--setup-command` | Extra setup commands to run (repeatable) |
| `--team` / `--personal` | Scope for the environment |

### Step 4 — Prepare your jobs config

```bash
cp jobs.example.json jobs.json
```

Edit `jobs.json` with your environment IDs and tasks. Keep `jobs.json` out of version control — it contains real environment IDs. See `jobs.example.json` for the schema.

### Step 5 — Dry run

Preview the commands that will be built without submitting anything:

```bash
python3 scripts/run_parallel_oz_opencode.py --config jobs.json --dry-run
```

### Step 6 — Launch

```bash
python3 scripts/run_parallel_oz_opencode.py --config jobs.json --max-workers 6
```

Output artifacts are written to:

```
runs/<timestamp>/run-results.json   # full result for each job
runs/<timestamp>/run-ids.json       # job name -> run ID map
runs/<timestamp>/<job-name>.log     # per-job log
runs/latest-run-ids.json            # symlink-equivalent, always points to latest
```

### Step 7 — Watch run status

```bash
python3 scripts/watch_oz_runs.py --runs-file runs/latest-run-ids.json --interval 20
```

Polls every 20 seconds until all runs reach a terminal state (`succeeded`, `failed`, `error`, or `cancelled`). Use `--once` to print a single snapshot and exit.

## Jobs config schema

```jsonc
{
  "defaults": {
    "scope": "team",              // "team" or "personal"
    "oz_model": "auto-efficient", // default Oz orchestrator model
    "open_after_submit": false    // open in Warp UI after submitting
  },
  "jobs": [
    {
      "name": "repo-a-auth-fixes",                        // required, unique
      "environment_id": "REPLACE_WITH_ENV_ID_FOR_REPO_A", // required
      "repo": "your-org/repo-a",                          // required
      "task": "Fix authentication token refresh bugs, run tests, and open a PR.", // required
      "oz_model": "claude-4-6-sonnet-high",               // optional override
      "opencode_model_hint": "Use an Anthropic Sonnet-class model." // optional prompt hint
    }
  ]
}
```

`opencode_model_hint` is passed as guidance in the prompt so the cloud agent can apply your intended OpenCode model strategy. Actual OpenCode provider selection is driven by the provider secrets available in the environment.

## Script reference

### `scripts/run_parallel_oz_opencode.py`

Submits jobs from a config file as parallel Oz cloud runs.

```
usage: run_parallel_oz_opencode.py --config PATH [--max-workers N] [--dry-run] [--output-dir DIR]

  --config PATH       Path to jobs JSON config (required)
  --max-workers N     Maximum concurrent submissions (default: 4)
  --dry-run           Build and log commands without launching
  --output-dir DIR    Directory for logs and results (default: runs)
```

### `scripts/watch_oz_runs.py`

Polls `oz run get` for each run ID in a runs file and prints status snapshots until all runs are done.

```
usage: watch_oz_runs.py --runs-file PATH [--interval N] [--once]

  --runs-file PATH    Path to runs/latest-run-ids.json or compatible JSON (required)
  --interval N        Polling interval in seconds (default: 20)
  --once              Print one snapshot and exit
```

### `scripts/create_environment_with_opencode.sh`

Creates an Oz environment with OpenCode pre-installed. Run once per target repo before your first job batch.

## Files

```
.
├── scripts/
│   ├── run_parallel_oz_opencode.py      # parallel job submitter
│   ├── watch_oz_runs.py                 # run status watcher
│   └── create_environment_with_opencode.sh  # environment setup
├── jobs.example.json                    # config template (safe to commit)
├── jobs.json                            # your actual config (do not commit)
├── runs/                                # output artifacts (gitignored)
└── USAGE.txt                            # step-by-step workflow
```

## Notes

- `jobs.json` contains real environment IDs — keep it out of version control.
- The `runs/` directory may contain run IDs and task output — also gitignored by default.
- The prompt injected into each Oz run explicitly instructs the agent to delegate all code operations to OpenCode and not use Warp's built-in coding tools directly.
- If a PR is created, OpenCode is instructed to print the PR URL and branch name so the Oz orchestrator can capture and report it.

## Troubleshooting

**Environment creation fails**
Run with `--dry-run` to validate your `jobs.json` before submitting. Check that the Oz CLI is authenticated (`oz auth status`).

**A run hangs or never completes**
Use `watch_oz_runs.py` with `--timeout 900` (15 min). Oz environments have a default idle timeout — if the agent stalls, the run will be garbage-collected automatically.

**Partial success (some edits applied, others not)**
Re-run only the failed jobs by creating a new `jobs.json` with just those entries. OpenCode is idempotent for most edits.

**Rate limits**
If submitting many jobs at once, reduce `--max-workers` to 2–3. Oz may throttle environment creation under heavy load.

**Expected run time:** 5–15 minutes per job depending on repo size and task complexity.
