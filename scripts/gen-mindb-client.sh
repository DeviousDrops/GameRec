#!/usr/bin/env bash
# Regenerate the Python MinDB client from the vendored schema.
# flatc must match the flatbuffers runtime version in requirements; see clients/README.md.
set -euo pipefail

FLATC="${FLATC:-flatc}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

want=25.12.19
have="$("$FLATC" --version | awk '{print $NF}')"
if [[ "$have" != "$want" ]]; then
  echo "flatc $want required, found $have. Set FLATC to the right binary." >&2
  exit 1
fi

rm -rf "$ROOT/clients/generated/mindb"
"$FLATC" --python --grpc -o "$ROOT/clients/generated" "$ROOT/clients/mindb.fbs"
echo "regenerated clients/generated/mindb from clients/mindb.fbs"
