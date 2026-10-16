#!/usr/bin/env bash
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Install and test the exact publication package in an isolated disposable cluster.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "Usage: $0 CHART.tgz EVIDENCE_DIRECTORY" >&2
  exit 2
fi
for tool in helm kind kubectl docker python3; do command -v "$tool" >/dev/null; done
package=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")
[[ -f "$package" ]] || { echo "Missing chart package: $package" >&2; exit 1; }
mkdir -p "$2"
evidence=$(cd "$2" && pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ci_dir="$script_dir/../helm-ci"
tmp=$(mktemp -d)
run_suffix="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
cluster="hg-helm-${run_suffix}"
namespace=helm-ci
release=helm-ci
node_image=${KINDEST_NODE_IMAGE:-kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d}
# Never modify the user's kubeconfig or select their active cluster.
export KUBECONFIG="$tmp/kubeconfig"
created=false
collect() {
  kubectl --request-timeout=15s get nodes -o wide >"$evidence/nodes.txt" 2>&1 || true
  kubectl --request-timeout=15s -n "$namespace" get pods,pvc -o wide >"$evidence/resources.txt" 2>&1 || true
  kubectl --request-timeout=15s -n "$namespace" get events --sort-by=.lastTimestamp >"$evidence/events.txt" 2>&1 || true
  kubectl --request-timeout=15s -n "$namespace" describe pvc >"$evidence/pvc-describe.txt" 2>&1 || true
  # Describe Pods without environment sections; never export Secrets or Helm values.
  kubectl --request-timeout=15s -n "$namespace" describe pods 2>&1 |
    sed '/^[[:space:]]*Environment:/,/^[[:space:]]*Mounts:/{/^[[:space:]]*Mounts:/!d;}' \
      >"$evidence/pod-describe.txt" || true
  local node pod
  while IFS= read -r node; do
    [[ -n "$node" ]] || continue
    kubectl --request-timeout=15s get --raw "/api/v1/nodes/$node/proxy/stats/summary" \
      >"$evidence/${node}-usage.json" 2>&1 || true
  done < <(kubectl --request-timeout=15s get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    kubectl --request-timeout=15s -n "$namespace" logs "$pod" --all-containers --timestamps \
      >"$evidence/${pod}.log" 2>&1 || true
    kubectl --request-timeout=15s -n "$namespace" logs "$pod" --all-containers --previous --timestamps \
      >"$evidence/${pod}-previous.log" 2>&1 || true
  done < <(kubectl --request-timeout=15s -n "$namespace" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
}
finish() {
  local result=$?
  trap - EXIT INT TERM
  if [[ "$created" == true ]]; then
    collect
    kind delete cluster --name "$cluster" >"$evidence/cluster-delete.log" 2>&1 || result=1
  fi
  rm -rf "$tmp"
  # The workflow requires this file as positive evidence; failures cannot leave stale success.
  if [[ $result -ne 0 ]]; then rm -f "$evidence/validation.json"; fi
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rm -f "$evidence/validation.json"
# Read just the single preset, without tar extraction or trusting archive paths.
python3 - "$package" "$tmp" "$ci_dir/smoke.groovy" "$run_suffix" <<'PY'
import pathlib, sys, tarfile
package, out, smoke, run = sys.argv[1:]
with tarfile.open(package, 'r:gz') as archive:
    members = archive.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
            raise SystemExit('Unsafe chart archive member: ' + member.name)
    presets = [m for m in members if len(pathlib.PurePosixPath(m.name).parts) == 2
               and pathlib.PurePosixPath(m.name).name == 'values-single.yaml' and m.isfile()]
    if len(presets) != 1:
        raise SystemExit('Package must contain exactly one root values-single.yaml')
    pathlib.Path(out, 'values-single.yaml').write_bytes(archive.extractfile(presets[0]).read())
pathlib.Path(out, 'smoke.groovy').write_text(
    pathlib.Path(smoke).read_text().replace('__HELM_CI_RUN__', 'helm-ci-' + run))
PY
docker info >/dev/null
# Mark before create so partial failures are also cleaned up.
created=true
# kind's --wait excludes image pulls; also bound the entire subprocess tree.
python3 - "$cluster" "$node_image" >"$evidence/cluster-create.log" 2>&1 <<'PY_KIND'
import os, signal, subprocess, sys
seconds = int(os.environ.get('KIND_CREATE_TIMEOUT_SECONDS', '600'))
if seconds < 1:
    raise SystemExit('KIND_CREATE_TIMEOUT_SECONDS must be positive')
process = subprocess.Popen(['kind', 'create', 'cluster', '--name', sys.argv[1],
                            '--image', sys.argv[2], '--wait', '180s'], start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=seconds))
except subprocess.TimeoutExpired:
    print('kind cluster creation timed out after %ss (including image pull)' % seconds, flush=True)
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    raise SystemExit(124)
PY_KIND
kubectl get storageclass standard >/dev/null
values=(-f "$tmp/values-single.yaml" -f "$ci_dir/values.yaml")
# The packaging helper may resolve mutable tags once and supply immutable image overrides.
if [[ -n "${HELM_IMAGE_VALUES:-}" ]]; then
  [[ -f "$HELM_IMAGE_VALUES" ]] || { echo 'HELM_IMAGE_VALUES does not exist' >&2; exit 1; }
  values+=(-f "$HELM_IMAGE_VALUES")
fi
helm install "$release" "$package" --namespace "$namespace" --create-namespace \
  "${values[@]}" --set-file "server.testGremlin=$tmp/smoke.groovy" \
  --wait --timeout 15m >"$evidence/install.log" 2>&1
for component in pd store server; do
  kubectl -n "$namespace" wait pod -l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=$component" \
    --for=condition=Ready --timeout=60s
done
kubectl -n "$namespace" get pods -o json >"$tmp/pods.json"
kubectl -n "$namespace" get pvc -o json >"$tmp/pvc.json"
# Validate actual topology, readiness, bound claims, and the Pod's PVC mount mapping.
python3 - "$tmp/pods.json" "$tmp/pvc.json" "$tmp/mounts.tsv" <<'PY'
import json, sys
pods, claims = (json.load(open(p))['items'] for p in sys.argv[1:3])
claims = {p['metadata']['name']: p for p in claims}
mounts = []
for component in ('pd', 'store', 'server'):
    matches = [p for p in pods if p['metadata']['labels'].get('app.kubernetes.io/component') == component]
    assert len(matches) == 1, (component, 'must have exactly one Pod')
    pod = matches[0]
    assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod['status']['conditions']), component
    container = next(c for c in pod['spec']['containers'] if c['name'] == component)
    if component == 'server':
        env = {v['name']: v for v in container.get('env', [])}
        assert 'secretKeyRef' in env['PASSWORD']['valueFrom'], 'Server auth must use a Secret'
        assert 'secretKeyRef' in env['HG_SERVER_AUTH_TOKEN_SECRET']['valueFrom'], 'Server JWT auth must be configured'
    if component in ('pd', 'store'):
        volumes = {v['name']: v['persistentVolumeClaim']['claimName'] for v in pod['spec']['volumes']
                   if 'persistentVolumeClaim' in v}
        assert len(volumes) == 1, (component, 'requires one PVC')
        volume, claim = next(iter(volumes.items()))
        assert claims[claim]['status']['phase'] == 'Bound', claim
        assert claims[claim]['spec']['storageClassName'] == 'standard', claim
        assert claims[claim]['spec']['resources']['requests']['storage'] == {'pd': '1Gi', 'store': '2Gi'}[component]
        mounted = [v for v in container['volumeMounts'] if v['name'] == volume]
        assert len(mounted) == 1 and not mounted[0].get('readOnly', False), component
        mounts.append((pod['metadata']['name'], component, mounted[0]['mountPath']))
assert len(claims) == 2, 'Expected exactly two persistent claims'
assert not any(p['metadata']['labels'].get('app.kubernetes.io/component') == 'hubble' for p in pods)
with open(sys.argv[3], 'w') as output:
    for row in mounts:
        output.write('\t'.join(row) + '\n')
PY
while IFS=$'\t' read -r pod component mount_path; do
  # shellcheck disable=SC2016 # Expand these variables inside the container.
  kubectl -n "$namespace" exec "$pod" -c "$component" -- sh -ec '
    awk -v p="$1" '\''$5 == p {found=1} END {exit !found}'\'' /proc/self/mountinfo
    file="$1/.helm-ci-$2"
    printf "helm-ci-persistence\n" > "$file"
    test "$(cat "$file")" = helm-ci-persistence
    rm "$file"
    df -k "$1"
  ' sh "$mount_path" "$run_suffix" >"$evidence/${component}-disk.txt"
done <"$tmp/mounts.tsv"
server_pod=$(kubectl -n "$namespace" get pod -l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=server" -o jsonpath='{.items[0].metadata.name}')
# shellcheck disable=SC2016 # The substitution must execute inside the container.
kubectl -n "$namespace" exec "$server_pod" -c server -- sh -ec '
  test "$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8080/graphs)" = 401
'
helm test "$release" --namespace "$namespace" --logs --timeout 5m \
  >"$evidence/helm-test.log" 2>&1
kubectl -n "$namespace" get pods -o json >"$tmp/final-pods.json"
python3 - "$package" "$tmp/final-pods.json" "$evidence/validation.json" "$node_image" <<'PY'
import hashlib, json, pathlib, sys
package, pods, target, node = sys.argv[1:]
manifest = json.loads(pathlib.Path(package).with_name('manifest.json').read_text())
package_sha = hashlib.sha256(pathlib.Path(package).read_bytes()).hexdigest()
assert manifest['package_sha256'] == package_sha, 'Package changed after preparation'
pods = json.load(open(pods))['items']
hooks = [p for p in pods if p['metadata']['labels'].get('app.kubernetes.io/component') == 'test']
assert len(hooks) == 1 and hooks[0]['status']['phase'] == 'Succeeded', 'Helm test Pod must succeed'
for component in ('pd', 'store', 'server'):
    core = [p for p in pods if p['metadata']['labels'].get('app.kubernetes.io/component') == component]
    assert len(core) == 1 and any(c['type'] == 'Ready' and c['status'] == 'True'
                                for c in core[0]['status']['conditions']), component + ' lost readiness'
images = []
for pod in pods:
    statuses = {c['name']: c for field in ('containerStatuses', 'initContainerStatuses', 'ephemeralContainerStatuses')
                for c in pod['status'].get(field, [])}
    for container in [c for field in ('containers', 'initContainers', 'ephemeralContainers')
                      for c in pod['spec'].get(field, [])]:
        status = statuses.get(container['name'], {})
        assert status.get('imageID'), 'Missing runtime imageID for ' + pod['metadata']['name']
        image = container['image']
        assert '@sha256:' in image, 'Runtime container image must be pinned: ' + image
        # Canonicalize Docker Hub shorthand before comparing with the sealed set.
        repository, image_digest = image.rsplit('@', 1)
        first = repository.split('/')[0]
        if '/' not in repository:
            repository = 'registry-1.docker.io/library/' + repository
        elif not ('.' in first or ':' in first or first == 'localhost'):
            repository = 'registry-1.docker.io/' + repository
        elif first in ('docker.io', 'index.docker.io'):
            repository = 'registry-1.docker.io/' + repository.split('/', 1)[1]
        assert repository + '@' + image_digest in manifest['pinned_images'], 'Unexpected runtime image: ' + image
        images.append({'pod': pod['metadata']['name'], 'container': container['name'],
                       'image': container['image'], 'imageID': status.get('imageID', '')})
pathlib.Path(target).write_text(json.dumps({'status': 'passed',
    'package_sha256': package_sha, 'source_sha': manifest['source_sha'],
    'node_image': node, 'topology': {'pd': 1, 'store': 1, 'server': 1},
    'persistence': 'two bound PVCs mounted and write checked',
    'helm_test': 'Succeeded (including committed CRUD)', 'images': images}, indent=2) + '\n')
PY
echo "Helm package passed persisted 1 PD + 1 Store + 1 Server and CRUD validation."
