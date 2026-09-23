#!/usr/bin/env bash
#
# run_local.sh - start the CCB Fraud & Disputes Desk against your own project.
#
# Reads .env, checks the four things that actually stop a first run, then
# starts Flask. Every check prints what it found; none of them guess.
#
#   ./scripts/run_local.sh            start the server
#   ./scripts/run_local.sh --check    run the preflight only, then exit
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

fail=0

echo "== preflight =================================================="

# 1. .env ---------------------------------------------------------------
if [[ ! -f .env ]]; then
  red "  .env missing. Copy .env.example to .env and fill in your own ids."
  exit 1
fi
set -a; . ./.env; set +a
green "  .env loaded"

# 2. the ids that have no default in code -------------------------------
for var in GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION VERTEX_AGENT_ENGINE_ID \
           BIGQUERY_DATASET DATAPLEX_GLOSSARY; do
  if [[ -z "${!var:-}" ]]; then
    red "  $var is unset. The backend raises on import rather than defaulting."
    fail=1
  else
    green "  $var = ${!var}"
  fi
done

# 3. interpreter --------------------------------------------------------
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  red "  no interpreter at .venv/bin/python"
  fail=1
else
  green "  python  $("$PY" --version 2>&1)"
fi

# 4. Application Default Credentials ------------------------------------
# Not the same credential as `gcloud auth login`. Missing ADC while gcloud
# works is the most common reason the first run fails.
if "$PY" - <<'EOF' 2>/dev/null
import google.auth
from google.auth.transport.requests import Request
c, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
c.refresh(Request())
EOF
then
  green "  ADC present and refreshable"
else
  red   "  no usable Application Default Credentials."
  dim   "      gcloud auth application-default login"
  dim   "      (separate from 'gcloud auth login' - you need both)"
  fail=1
fi

# 5. the engine answers -------------------------------------------------
if [[ -n "${GOOGLE_CLOUD_PROJECT:-}" && -n "${VERTEX_AGENT_ENGINE_ID:-}" ]]; then
  if PYTHONPATH="$ROOT" "$PY" - <<'EOF' 2>/dev/null
import os
from backend.memory_bank import MemoryBankClient
c = MemoryBankClient(
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    engine_id=os.environ["VERTEX_AGENT_ENGINE_ID"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    verbose=False,
)
c.list_sessions()
EOF
  then
    green "  Memory Bank reachable: projects/${GOOGLE_CLOUD_PROJECT}/locations/${GOOGLE_CLOUD_LOCATION}/reasoningEngines/${VERTEX_AGENT_ENGINE_ID}"
  else
    red   "  Memory Bank did not answer. Check the engine id and that"
    dim   "      aiplatform.googleapis.com is enabled on this project."
    fail=1
  fi
fi

echo "==============================================================="
if [[ $fail -ne 0 ]]; then
  red "preflight failed - see above. Nothing was started."
  exit 1
fi
green "preflight clean"

if [[ "${1:-}" == "--check" ]]; then
  exit 0
fi

echo
echo "starting on http://localhost:${PORT:-5055}"
echo
exec env PYTHONPATH="$ROOT" "$PY" backend/app.py
