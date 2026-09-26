#!/usr/bin/env bash
# Prove a deployment actually works, from outside the cluster.
#
#   ./deploy/vm/smoke.sh https://gamerec.example.com
#   ./deploy/vm/smoke.sh http://127.0.0.1:8080        # after kubectl port-forward
#
# Run it after bootstrap.sh, after a rollout, and after a restore. It asks the four questions a
# rollout can fail at independently -- is the process up, can it reach MinDB, is the index the one
# expected, does a query come back -- and prints the MinDB kernel, because a number measured against
# the wrong kernel is the mistake ADR-0006 exists to prevent.
#
# Read-only: no writes, no ingest, one embedding. Safe against production.
set -euo pipefail

BASE="${1:?usage: smoke.sh <base-url>}"
QUERY="${2:-something short and relaxing}"
CURL=(curl --silent --show-error --fail-with-body --max-time 20)

fail() { echo "FAIL: $*" >&2; exit 1; }

# /livez must not touch MinDB, so this passing while /readyz fails is a meaningful state: the
# process is fine and its dependency is not (D7).
echo "== /livez"
"${CURL[@]}" "${BASE}/livez" || fail "the API process is not answering at all"
echo

echo "== /readyz"
# Retry: a rollout is visible downtime measured in seconds, and 503 with Retry-After is the
# documented answer during one. Failing the smoke test on the first 503 would just be impatience.
for attempt in 1 2 3 4 5 6; do
    if READY=$("${CURL[@]}" "${BASE}/readyz" 2>/dev/null); then break; fi
    [[ ${attempt} == 6 ]] && fail "/readyz never became ready; MinDB is probably down"
    echo "not ready yet, waiting 5s (${attempt}/6)"
    sleep 5
done
echo "${READY}"

echo "== /health"
HEALTH=$("${CURL[@]}" "${BASE}/health") || fail "/health did not answer"
python3 - "${HEALTH}" <<'PY'
import json
import sys

health = json.loads(sys.argv[1])
mindb = health["mindb"]
print(f"corpus      {health['corpus_size']} of {health['capacity']} vectors, {health['dims']} dims")
print(f"names       {health['name_index_size']}")
print(f"stamp       {health['model_stamp']} template {health['template_version']}")
flag = lambda value: str(value).lower()  # as /health reported it, not as Python spells it
print(f"mindb       kernel={mindb['kernel']} goarch={mindb['goarch']} "
      f"fast_int8={flag(mindb['fast_int8'])} wal_healthy={flag(mindb['wal_healthy'])}")

problems = []
# An empty index answers every query with nothing and looks healthy doing it, which is exactly the
# state a restore is meant to fix and the one worth failing loudly on.
if health["corpus_size"] == 0:
    problems.append("the index is empty: restore a generation or run ingest.reindex")
if health["dims"] != 384:
    problems.append(f"MinDB is {health['dims']}-dimensional, the model produces 384")
if mindb["wal_enabled"] and not mindb["wal_healthy"]:
    problems.append("MinDB reports an unhealthy WAL")
if problems:
    print("\n".join(f"FAIL: {p}" for p in problems), file=sys.stderr)
    sys.exit(1)
PY

echo "== /recommend"
# narrate=false on purpose: this checks retrieval. Narration depends on Groq, and a Groq outage is
# not a broken deployment -- it is the one dependency that fails open by design (D6).
RESULTS=$("${CURL[@]}" --get "${BASE}/recommend" \
    --data-urlencode "q=${QUERY}" --data-urlencode "narrate=false") \
    || fail "a query the corpus should be able to answer returned an error"
python3 - "${RESULTS}" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
results = payload.get("results", [])
if not results:
    print("FAIL: the query returned no results", file=sys.stderr)
    sys.exit(1)
for result in results:
    print(f"  {result['score']:.3f}  {result['name']}")
PY

echo
echo "OK"
