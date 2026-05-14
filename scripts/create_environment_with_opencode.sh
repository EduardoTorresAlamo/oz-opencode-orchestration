#!/usr/bin/env bash
set -eo pipefail

usage() {
  cat <<'EOF'
Create an Oz cloud environment configured to use OpenCode at runtime.

Usage:
  ./scripts/create_environment_with_opencode.sh --name <ENV_NAME> --repo <owner/repo> [options]

Required:
  --name <ENV_NAME>           Environment name
  --repo <owner/repo>         Repository to clone (repeatable)

Options:
  --image <DOCKER_IMAGE>      Docker image (default: warpdotdev/dev-full:latest-agents)
  --setup-command <COMMAND>   Additional setup command (repeatable)
  --team                      Create team-scoped environment
  --personal                  Create personal-scoped environment (default)
  -h, --help                  Show this help
EOF
}

NAME=""
IMAGE="${OZ_DOCKER_IMAGE:-warpdotdev/dev-full:latest-agents}"
SCOPE="personal"
REPOS=()
EXTRA_SETUP_COMMANDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:-}"
      shift 2
      ;;
    --repo)
      REPOS+=("${2:-}")
      shift 2
      ;;
    --image)
      IMAGE="${2:-}"
      shift 2
      ;;
    --setup-command)
      EXTRA_SETUP_COMMANDS+=("${2:-}")
      shift 2
      ;;
    --team)
      SCOPE="team"
      shift
      ;;
    --personal)
      SCOPE="personal"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Missing required argument: --name" >&2
  usage
  exit 1
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "Missing required argument: at least one --repo <owner/repo>" >&2
  usage
  exit 1
fi

cmd=(oz environment create --name "$NAME" --docker-image "$IMAGE" --output-format text)

if [[ "$SCOPE" == "team" ]]; then
  cmd+=(--team)
else
  cmd+=(--personal)
fi

for repo in "${REPOS[@]}"; do
  cmd+=(--repo "$repo")
done

cmd+=(--setup-command 'if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${GEMINI_API_KEY:-}" ]; then echo "No provider API key found. Add ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY as Oz secrets."; exit 1; fi')
cmd+=(--setup-command 'curl -fsSL https://opencode.ai/install | bash')
cmd+=(--setup-command 'export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"; command -v opencode >/dev/null 2>&1 || { echo "opencode is not in PATH after install"; exit 1; }; opencode --version')

for setup_cmd in "${EXTRA_SETUP_COMMANDS[@]}"; do
  cmd+=(--setup-command "$setup_cmd")
done

echo "Creating environment: $NAME"
echo "Scope: $SCOPE"
echo "Image: $IMAGE"
echo "Repos: ${REPOS[*]}"
"${cmd[@]}"
