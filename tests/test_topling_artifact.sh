#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
mkdir "$test_dir/docker"

awk '
  /^    - name: Resolve pinned Topling artifact$/ { selected = 1; next }
  selected && /^      run: \|/ { in_script = 1; next }
  in_script && $0 !~ /^        / && $0 !~ /^$/ { exit }
  in_script { sub(/^        /, ""); print }
' "$repo_root/.github/workflows/_publish_pd_store_server_reusable.yml" > "$test_dir/resolve.sh"

resolve() {
  (cd "$test_dir" && TOPLING_JNI_URL="${1:-}" TOPLING_JNI_SHA256="${2:-}" \
    GITHUB_ENV="$test_dir/output" bash resolve.sh > "$test_dir/log" 2>&1)
}

checksum=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
printf 'TOPLING_JNI_URL=https://example.org/immutable/jni.jar\nTOPLING_JNI_SHA256=%s\n' "$checksum" \
  > "$test_dir/docker/topling-runtime.env"
resolve
grep -Fxq 'TOPLING_JNI_URL=https://example.org/immutable/jni.jar' "$test_dir/output"
grep -Fxq "TOPLING_JNI_SHA256=$checksum" "$test_dir/output"

# Overrides work without a source pin, but incomplete overrides never do.
rm "$test_dir/docker/topling-runtime.env"
resolve https://example.org/another.jar "$checksum"
if resolve https://example.org/another.jar; then
  echo 'Incomplete artifact override unexpectedly succeeded' >&2
  exit 1
fi

cat > "$test_dir/docker/topling-runtime.env" <<'PIN'
TOPLING_JNI_URL=https://example.org/$(touch${IFS}executed)
TOPLING_JNI_SHA256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
PIN
resolve
test ! -e "$test_dir/executed"

cat > "$test_dir/docker/topling-runtime.env" <<'PIN'
TOPLING_JNI_URL=
TOPLING_JNI_URL=https://example.org/jni.jar
TOPLING_JNI_SHA256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
PIN
if resolve; then
  echo 'Duplicate artifact pin unexpectedly succeeded' >&2
  exit 1
fi

printf 'UNEXPECTED=value\n' > "$test_dir/docker/topling-runtime.env"
if resolve; then
  echo 'Unknown artifact pin key unexpectedly succeeded' >&2
  exit 1
fi

printf '%s\n' 'Topling artifact pin tests passed'
