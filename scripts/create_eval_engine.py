"""Creates a separate Agent Engine for evaluation runs.

Copies the Memory Bank config (topics, extraction settings) from the live
engine named by VERTEX_AGENT_ENGINE_ID, so eval results reflect the same
extraction behaviour without touching demo memories. Prints the new engine id
to put in .env as VERTEX_EVAL_AGENT_ENGINE_ID.

Usage:
  set -a; . ./.env; set +a
  PYTHONPATH=. ./.venv/bin/python3 scripts/create_eval_engine.py [--dry-run]

Delete the whole eval engine later with:
  curl -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://<location>-aiplatform.googleapis.com/v1beta1/projects/<project>/locations/<location>/reasoningEngines/<id>?force=true"
"""
import argparse
import json
import sys
import urllib.request

from backend.memory_bank import shared_memory_bank_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--display-name", default="ccb-fraud-desk-eval")
    parser.add_argument("--dry-run", action="store_true", help="print the request, create nothing")
    args = parser.parse_args()

    live = shared_memory_bank_client()
    memory_bank_config = live.get_engine().get("contextSpec", {}).get("memoryBankConfig")
    if not memory_bank_config:
        print("Live engine has no memoryBankConfig to copy; refusing to create an unconfigured engine.")
        return 1

    body = {"displayName": args.display_name, "contextSpec": {"memoryBankConfig": memory_bank_config}}
    url = "https://%s/%s/projects/%s/locations/%s/reasoningEngines" % (
        live.host, live.api_version, live.project, live.location)
    if args.dry_run:
        print("POST", url)
        print(json.dumps(body, indent=2))
        return 0

    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", "Bearer %s" % live.token())
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=120) as response:
        operation = json.loads(response.read())
    finished = live.wait_for_operation(operation, max_wait=600)
    name = finished.get("response", {}).get("name") or operation.get("name", "")
    engine_id = name.split("/reasoningEngines/")[-1].split("/")[0]
    print("Created:", name)
    print("Add to .env:  VERTEX_EVAL_AGENT_ENGINE_ID=%s" % engine_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
