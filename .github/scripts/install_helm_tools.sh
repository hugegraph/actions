#!/usr/bin/env bash
# Install the same verified Helm tools locally and on the publication runner.
set -euo pipefail
destination=${INSTALL_DIR:-"${RUNNER_TEMP:-/tmp}/hugegraph-helm-bin"}
platform="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
case "$platform" in
  linux-x86_64)
    platform=linux-amd64
    helm_sha=9318379b847e333460d33d291d4c088156299a26cd93d570a7f5d0c36e50b5bb
    kind_sha=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d ;;
  darwin-arm64)
    helm_sha=56413c7fbb496d2789881039cab61d849727c7b35db00826fae7a2685a403344
    kind_sha=0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f ;;
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
if [[ "${1:-}" == --with-kind ]]; then
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-$platform" -o "$temporary/kind"
  verify "$temporary/kind" "$kind_sha"
  install -m 0755 "$temporary/kind" "$destination/kind"
fi
if [[ -n "${GITHUB_PATH:-}" ]]; then printf '%s\n' "$destination" >> "$GITHUB_PATH"; fi
"$destination/helm" version --short
if [[ "${1:-}" == --with-kind ]]; then "$destination/kind" version; fi
