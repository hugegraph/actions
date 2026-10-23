#!/usr/bin/env bash
# Install the same verified Helm tools locally and on the publication runner.
set -euo pipefail
with_kind=false
with_unittest=false
with_cosign=false
for option in "$@"; do
  case "$option" in
    --with-kind) with_kind=true ;;
    --with-unittest) with_unittest=true ;;
    --with-cosign) with_cosign=true ;;
    *) echo "Unknown option: $option (expected --with-kind, --with-unittest or --with-cosign)" >&2; exit 2 ;;
  esac
done
destination=${INSTALL_DIR:-"${RUNNER_TEMP:-/tmp}/hugegraph-helm-bin"}
platform="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
case "$platform" in
  linux-x86_64)
    platform=linux-amd64
    helm_sha=9318379b847e333460d33d291d4c088156299a26cd93d570a7f5d0c36e50b5bb
    kind_sha=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
    cosign_sha=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
    unittest_platform=linux-amd64
    unittest_sha=9d9df801afe72c1629f6b6e89f7a57bb9d795be0ccc10c9b7245091a56e80029 ;;
  darwin-arm64)
    helm_sha=56413c7fbb496d2789881039cab61d849727c7b35db00826fae7a2685a403344
    kind_sha=0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f
    cosign_sha=5cf948c2f4dfe59687bdd0b8523709067383e03982cc543475c8a7dc70e92a76
    unittest_platform=macos-arm64
    unittest_sha=637c541ebc01990caaba488832104c01ff0f89a41c32a44bbf00a6e0519f762b ;;
  *) echo "Unsupported tool platform: $platform" >&2; exit 1 ;;
esac
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
verify() {
  python3 - "$1" "$2" <<'PY'
import hashlib, pathlib, sys
assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == sys.argv[2], 'Tool checksum mismatch'
PY
}
mkdir -p "$destination"
curl --fail --silent --show-error --location --retry 3 \
  "https://get.helm.sh/helm-v3.16.2-$platform.tar.gz" -o "$temporary/helm.tgz"
verify "$temporary/helm.tgz" "$helm_sha"
tar -xzf "$temporary/helm.tgz" -C "$temporary" "$platform/helm"
install -m 0755 "$temporary/$platform/helm" "$destination/helm"
if "$with_kind"; then
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-$platform" -o "$temporary/kind"
  verify "$temporary/kind" "$kind_sha"
  install -m 0755 "$temporary/kind" "$destination/kind"
fi
if "$with_unittest"; then
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/helm-unittest/helm-unittest/releases/download/v1.0.0/helm-unittest-$unittest_platform-1.0.0.tgz" \
    -o "$temporary/unittest.tgz"
  verify "$temporary/unittest.tgz" "$unittest_sha"
  mkdir -p "$temporary/unittest"
  tar -xzf "$temporary/unittest.tgz" -C "$temporary/unittest"
  plugin_directory="$("$destination/helm" env HELM_PLUGINS)/unittest"
  mkdir -p "$plugin_directory"
  # Install the verified binary directly; do not execute the plugin download hook.
  install -m 0755 "$temporary/unittest/untt" "$plugin_directory/untt"
  install -m 0644 "$temporary/unittest/plugin.yaml" "$plugin_directory/plugin.yaml"
fi
if "$with_cosign"; then
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-$platform" -o "$temporary/cosign"
  verify "$temporary/cosign" "$cosign_sha"
  install -m 0755 "$temporary/cosign" "$destination/cosign"
fi
if [[ -n "${GITHUB_PATH:-}" ]]; then printf '%s\n' "$destination" >> "$GITHUB_PATH"; fi
"$destination/helm" version --short
if "$with_kind"; then "$destination/kind" version; fi
if "$with_unittest"; then "$destination/helm" plugin list; fi
if "$with_cosign"; then "$destination/cosign" version; fi
