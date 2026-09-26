#!/usr/bin/env bash
# Exercise installed distributions outside the source checkout.
set -euo pipefail

: "${SOURCE_DIR:?Set SOURCE_DIR to the absolute source checkout path}"
: "${DIST_DIR:?Set DIST_DIR to the absolute distribution directory}"
: "${VERSION:?Set VERSION to the expected MCP version}"
export VERSION
[[ "$SOURCE_DIR" = /* && "$DIST_DIR" = /* ]] || {
  echo 'SOURCE_DIR and DIST_DIR must be absolute paths.' >&2
  exit 1
}

shopt -s nullglob
wheels=("$DIST_DIR"/hugegraph_mcp-*.whl)
sdists=("$DIST_DIR"/hugegraph_mcp-*.tar.gz "$DIST_DIR"/hugegraph-mcp-*.tar.gz)
[[ ${#wheels[@]} -eq 1 && ${#sdists[@]} -eq 1 ]] || {
  echo 'Expected exactly one MCP wheel and one MCP sdist.' >&2
  exit 1
}

client_args=()
uvx_args=()
if [[ -n "${CLIENT_REQUIREMENT:-}" ]]; then
  client_args=("$CLIENT_REQUIREMENT")
  uvx_args=(--with "$CLIENT_REQUIREMENT")
  echo 'Dependency check: explicit client requirement; all other dependencies from PyPI.'
else
  echo 'Dependency check: all dependencies from public PyPI.'
fi

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
uv python install 3.10 3.11
for python_version in 3.10 3.11; do
  for artifact in "${wheels[@]}" "${sdists[@]}"; do
    work=$(mktemp -d "$scratch/install.XXXXXX")
    uv venv --python "$python_version" "$work/venv"
    uv pip install --python "$work/venv/bin/python" "$artifact" "${client_args[@]}"
    (
      cd "$work"
      "$work/venv/bin/python" -I -c 'import importlib.metadata as m, os; assert m.version("hugegraph-mcp") == os.environ["VERSION"]; print("Installed client:", m.version("hugegraph-python"))'
      uv pip install --python "$work/venv/bin/python" 'pytest==8.4.2'
      mkdir client-contracts
      cp "$SOURCE_DIR/hugegraph-python-client/src/tests/api/test_vertex_id_format.py" \
        "$SOURCE_DIR/hugegraph-python-client/src/tests/api/test_schema_contract.py" \
        "$SOURCE_DIR/hugegraph-python-client/src/tests/test_distribution.py" client-contracts/
      "$work/venv/bin/python" -I -m pytest --confcutdir="$work/client-contracts" client-contracts -v --tb=short
      "$work/venv/bin/python" -I "$SOURCE_DIR/hugegraph-mcp/tests/distribution_smoke.py" -- "$work/venv/bin/hugegraph-mcp"
    )
    rm -rf "$work"
  done
done

python311=$(uv python find 3.11)
(
  cd "$scratch"
  "$python311" -I "$SOURCE_DIR/hugegraph-mcp/tests/distribution_smoke.py" -- \
    uvx --python 3.11 --no-config --no-cache --index-url https://pypi.org/simple \
    --from "${wheels[0]}" "${uvx_args[@]}" hugegraph-mcp
)
