# Workflow maintenance

For workflow inputs and everyday use, see the [README](../README.md).

## Registry access

### Docker Hub and GHCR

- Configure `DOCKERHUB_USERNAME` and `DOCKERHUB_PASSWORD` with write access to the destination Docker Hub repositories. The Helm repository is `hugegraph/hugegraph-helm` and must be public.
- GHCR publication uses `GITHUB_TOKEN` with `packages: write`. An existing package must grant `hugegraph/actions` write access and allow anonymous pulls.
- Latest image workflows can update their `LAST_*_HASH` repository variable after publication. Validation-only runs leave these variables unchanged.
- Daily Helm runs use the same `PERSONAL_ACCESS_TOKEN` secret as latest image publishers to create or update `LAST_HELM_HASH`. Its value is the last commit affecting Apache's `helm/hugegraph` directory at the tested source revision. It advances only after both registries are published, signed and verified. No initial variable value is required; a missing value triggers the first publication. Manual runs and release runs leave it unchanged.

### Python indexes

Create GitHub environments `testpypi` and `pypi`, each with a `PYPI_API_TOKEN` that can upload every selected package. A token scoped only to `hugegraph-python` cannot publish `hugegraph-mcp`.

Restrict the `pypi` environment to this repository's `master` branch, require maintainer approval, and disable administrator bypass. This restriction applies to the workflow branch, not the selected source ref. Allow branch testing in `testpypi`.

Validation-only runs do not request approval or receive an upload token. Publishing both packages requests one approval; the token is available only to the upload steps.

## Helm and Python publication recovery

Use **Re-run failed jobs** to resume publication with the retained artifacts. Do not repackage an existing version: the bytes may change, and conflicting registry contents are rejected.

The Helm publication bundle is retained for 30 days and Python artifacts for 14 days, subject to repository policy. If those artifacts have expired or been deleted, recovery fails; publish a new version instead. The two Helm registries have no shared transaction, so one copy may already exist when the other fails.

## Artifact Hub metadata

[Publish Helm repository metadata](../.github/workflows/publish_helm_metadata.yml) updates the repository identity from [artifacthub-repo.yml](../.github/helm-ci/artifacthub-repo.yml). Run it when that identity changes. It uses the separate `artifacthub.io` OCI tag and does not modify Chart versions.

## Build and test notes

### Topling builds

PD/Store/Server formal images always use `apache/hugegraph` source. The release
wrapper checks out the selected Apache ref once and passes that immutable commit
to both runtime variants. One dispatch publishes standard `x.y.z` and Topling
`x.y.z-topling`; both variants require their functional checks. Scheduled latest
runs stay standard. Select `runtime_variant=topling` in a manual latest run when
an on-demand Topling development image is needed. Version tags use the paired
release workflow, rather than the latest workflow.

Release validation publishes both variants to reserved
`ci-release-<version>-<sha12>-<run_id>` candidate tags. Only after both validation
jobs succeed does one promotion job verify and publish all eight final tags.
The run ID stays unchanged on **Re-run failed jobs**, so the successful sibling's
candidates remain available. Failed or cancelled runs retain candidates; cleanup
runs only after successful promotion. A new dispatch gets different candidate
tags. Ordinary latest inputs cannot use the reserved `ci-release-*` namespace.

The selected source pins an immutable JNI JAR URL and SHA-256 in
`docker/topling-runtime.env`. To validate another trusted build of
[hugegraph/toplingdb](https://github.com/hugegraph/toplingdb/actions/workflows/topling-jni.yml),
supply both `topling_jni_url` and `topling_jni_sha256` as overrides.
The JAR must support `TOPLINGDB_EASY_MIGRATE_CONF`; similarly named upstream JARs may
lack that entry point. The source distribution's `prepare-topling.sh` verifies the
checksum, dependencies and a real configuration probe while the image is built.
There is no native-library download at container startup.

The source must provide the current `docker/bake.hcl` runtime contract. This reuses
the same Maven build and four image repositories. Topling requires Linux amd64,
a Haswell-compatible CPU, and a compatible glibc runtime; the Dockerfiles select
the matching runtime base. The
HStore Server image stays on `backend=hstore` without local Topling JNI, while PD,
Store and the standalone Server use the prepared Topling runtime.

The workflow adds `-topling` to the selected image tag once: `latest` becomes
`latest-topling`, and release `1.8.0` becomes `1.8.0-topling`. Standard builds reject
Topling tags. Topling runs do not read or update `LAST_SERVER_HASH`, because a source
hash alone cannot identify the chosen native artifact.

Uncheck `publish` in the latest workflow to build and test without writing images,
registry caches or source markers. Topling requires `strict_mode=true`: publication
checks the runtime labels and checksum receipt, verifies the loaded JNI path in PD,
Store and standalone Server, and tests schema/graph CRUD plus persistence after a
normal stop and restart for both standalone and 1 PD + 1 Store + 1 Server.
The Topling publication gate also creates RocksDB data with standard images from
the same source commit, stops them normally, and verifies the Topling images can
read and update that data on the same persisted volumes.
The standalone seed uses temporary small buffer settings to force business SSTs.
Before image replacement, the workflow restores just those settings to their
original values, including unset defaults, and preserves all other graph options.
It verifies the original settings under Topling and after returning to standard.
An API-created graph must retain its persisted definition, nickname, data and
new writes across both image replacements, with its provider selected by each
image's environment. The local regression covers legacy image-layer graph files
and persistent graph-directory/file symlinks; real container qualification still
requires a matching Server source containing the persistent graph-config contract.
After pushing, Topling runs pull each returned immutable digest and verify its amd64
image configuration matches the tested image, including source commit and runtime
labels. The run summary records those verified digest references for deployment.

After publication, select the image set with `HUGEGRAPH_VERSION=latest-topling` in
the normal Compose files, or `--set global.runtimeVariant=topling` with the matching
Helm chart. For a RocksDB-to-Topling switch, stop the existing services normally and
back up their data, then preserve the data volumes while selecting the qualified
Topling images. The compatibility gate covers the tested source and pinned JNI
pair; qualify a copy of representative production data before switching. Pin
published image digests when a reproducible deployment is required.

### Fork validation

Use a manual latest run with `publish=false` to build and test an allowed fork
without registry writes. To publish temporary candidate images, select the fork
and explicitly set an image tag beginning with `ci-`, such as `ci-<source-sha>`.
Topling adds its normal `-topling` suffix. Candidate validation
cannot use official version/latest tags or update `LAST_SERVER_HASH`; it cannot
run in release mode. This facility supports cross-repository integration testing
before the source changes reach Apache.

### Shared build conventions

Image entry workflows define the source, Dockerfiles, platforms and component settings. Use `FROM --platform=$BUILDPLATFORM` for portable Maven/Node build stages so packaging runs natively; runtime stages use the target platform. Native libraries and architecture-specific downloads still need appropriate builds and runtime tests. See [Docker's build strategies](https://docs.docker.com/build/building/multi-platform/).

The shared PD/Store/Server job locks the final image tag across latest and release
workflows for its entire build, test, push and pull verification. Runs publishing the
same tag cannot interleave updates to the four repositories; different tags can run independently.

The Helm smoke test uses 1 PD + 1 Store + 1 Server with memory limits of 1/2/2 GiB, persistent volumes, and Hubble disabled. These limits are for CI, not production sizing. Tools are checksum-pinned and test images are resolved to digests; published Chart image values stay as defined in source.

Python builds test wheel and source distributions in isolated environments. With `both`, MCP uses the client artifact from the same run before publication and the published client afterward. For TestPyPI publication, it selects the matching TestPyPI client wheel; other dependencies come from public PyPI.

Local checks require no Docker or publication:

```bash
python3 -m unittest discover -s tests -p 'test_helm*.py' -v
bash tests/test_publish_prepare.sh
bash tests/test_topling_artifact.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_pd_store_release.py -v
actionlint .github/workflows/publish_helm.yml .github/workflows/publish_latest_*.yml

uv run --no-project --python 3.11 python -m unittest discover -s tests -p 'test_python_release.py' -v
actionlint .github/workflows/publish_python.yml .github/workflows/_build_python_reusable.yml
```
