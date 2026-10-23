#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

extract_prepare_script() {
  awk '
    /^      run: \|/ { in_script = 1; next }
    in_script && $0 !~ /^        / && $0 !~ /^$/ { exit }
    in_script { sub(/^        /, ""); print }
  ' "$1"
}

run_prepare() {
  local workflow="$1"
  local mode="$2"
  local source_repository="$3"
  local allowed_source_repositories="$4"
  local source_ref="$5"
  local default_source="$6"
  local image_tag="$7"
  local publish="$8"
  local output_file="$tmp_dir/output-$RANDOM"
  local log_file="$tmp_dir/log-$RANDOM"

  {
    # These single-quoted strings intentionally emit literal shell code for the child process.
    # shellcheck disable=SC2016
    printf '%s\n' \
      'set -euo pipefail' \
      'git() {' \
      '  if [ "$1" = "check-ref-format" ]; then return 0; fi' \
      '  if [ "$1" = "ls-remote" ]; then printf "%s\n" "0123456789abcdef0123456789abcdef01234567 refs/heads/test-ref"; return 0; fi' \
      '  return 1' \
      '}' \
      'export -f git'
    extract_prepare_script "$workflow"
  } | MODE="$mode" \
    SOURCE_REPOSITORY="$source_repository" \
    ALLOWED_SOURCE_REPOSITORIES="$allowed_source_repositories" \
    SOURCE_REF="$source_ref" \
    DEFAULT_SOURCE="$default_source" \
    REQUESTED_IMAGE_TAG="$image_tag" \
    PUBLISH="$publish" \
    ENABLE_HASH_GATE=false \
    LAST_HASH_VALUE='' \
    DRY_RUN=false \
    RUNTIME_VARIANT="${TEST_RUNTIME_VARIANT:-standard}" \
    TOPLING_JNI_URL="${TEST_TOPLING_JNI_URL-https://example.org/immutable/topling.jar}" \
    TOPLING_JNI_SHA256="${TEST_TOPLING_JNI_SHA256-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" \
    STRICT_MODE="${TEST_STRICT_MODE:-true}" \
    GITHUB_OUTPUT="$output_file" \
    bash -s >"$log_file" 2>&1
  prepare_status=$?

  TEST_OUTPUT_FILE="$output_file"
  TEST_LOG_FILE="$log_file"
  return "$prepare_status"
}

assert_output() {
  local key="$1"
  local value="$2"
  grep -Fqx "$key=$value" "$TEST_OUTPUT_FILE" || {
    printf 'expected %s=%s in %s\n' "$key" "$value" "$TEST_OUTPUT_FILE" >&2
    cat "$TEST_LOG_FILE" >&2
    exit 1
  }
}

standard_workflow="$repo_root/.github/workflows/_publish_image_reusable.yml"
pd_store_workflow="$repo_root/.github/workflows/_publish_pd_store_server_reusable.yml"

run_prepare "$standard_workflow" latest \
  apache/hugegraph-toolchain apache/hugegraph-toolchain,hugegraph/hugegraph-toolchain \
  feature/latest-test apache/hugegraph-toolchain@master latest true
assert_output version_tag latest
assert_output publish_images true

run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph,hugegraph/hugegraph \
  feature/latest-test apache/hugegraph@master latest true
assert_output version_tag latest
assert_output publish_images true

run_prepare "$standard_workflow" latest \
  apache/hugegraph-toolchain apache/hugegraph-toolchain,hugegraph/hugegraph-toolchain \
  master apache/hugegraph-toolchain@master '' true
assert_output version_tag latest

if run_prepare "$standard_workflow" latest \
  apache/hugegraph-toolchain apache/hugegraph-toolchain,hugegraph/hugegraph-toolchain \
  feature/latest-test apache/hugegraph-toolchain@master '' true; then
  printf 'non-default source without image_tag unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx 'image_tag is required when publishing a non-default source' "$TEST_LOG_FILE"

if run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph,hugegraph/hugegraph \
  feature/latest-test apache/hugegraph@master '' true; then
  printf 'pd/store non-default source without image_tag unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fqx 'image_tag is required when publishing a non-default source' "$TEST_LOG_FILE"

run_prepare "$standard_workflow" release \
  apache/hugegraph-toolchain apache/hugegraph-toolchain,hugegraph/hugegraph-toolchain \
  release-1.7.0 '' 1.7.0 true
assert_output version_tag 1.7.0

run_prepare "$pd_store_workflow" release \
  apache/hugegraph apache/hugegraph,hugegraph/hugegraph \
  release-1.7.0 '' 1.7.0 true
assert_output version_tag 1.7.0

TEST_RUNTIME_VARIANT=topling
TEST_TOPLING_JNI_URL=''
TEST_TOPLING_JNI_SHA256=''
run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph,hugegraph/hugegraph \
  master apache/hugegraph@master '' true
assert_output version_tag latest-topling
assert_output cache_channel latest-topling
assert_output hash_gate_applied false
unset TEST_TOPLING_JNI_URL TEST_TOPLING_JNI_SHA256

run_prepare "$pd_store_workflow" latest \
  hugegraph/hugegraph apache/hugegraph,hugegraph/hugegraph \
  feature/topling apache/hugegraph@master smoke-topling false
assert_output version_tag smoke-topling
assert_output publish_images false

run_prepare "$pd_store_workflow" release \
  apache/hugegraph apache/hugegraph,hugegraph/hugegraph \
  release-1.8.0 '' 1.8.0 true
assert_output version_tag 1.8.0-topling

TEST_TOPLING_JNI_SHA256=''
if run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph master apache/hugegraph@master '' true; then
  printf '%s\n' 'Topling without a trusted artifact checksum unexpectedly succeeded' >&2
  exit 1
fi
unset TEST_TOPLING_JNI_SHA256

TEST_STRICT_MODE=false
if run_prepare "$pd_store_workflow" release \
  apache/hugegraph apache/hugegraph release-1.8.0 '' 1.8.0 true; then
  printf '%s\n' 'Topling without functional tests unexpectedly succeeded' >&2
  exit 1
fi
unset TEST_STRICT_MODE

TEST_RUNTIME_VARIANT=invalid
if run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph master apache/hugegraph@master '' true; then
  printf '%s\n' 'Invalid runtime unexpectedly succeeded' >&2
  exit 1
fi
unset TEST_RUNTIME_VARIANT

if run_prepare "$pd_store_workflow" latest \
  apache/hugegraph apache/hugegraph master apache/hugegraph@master latest-topling true; then
  printf '%s\n' 'Standard runtime using a Topling tag unexpectedly succeeded' >&2
  exit 1
fi

printf '%s\n' 'publish prepare regression tests passed'
