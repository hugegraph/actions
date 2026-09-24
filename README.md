# HugeGraph Actions

Shared workflows for publishing HugeGraph Docker images and Python packages, validating releases, and repository automation.

## Docker images

Thin component wrappers call either [the standard image publisher](.github/workflows/_publish_image_reusable.yml) or [the PD/Store/Server publisher](.github/workflows/_publish_pd_store_server_reusable.yml).

| Mode | Trigger | Source and tag | Unchanged source |
| --- | --- | --- | --- |
| Latest | Scheduled or manual | Default source uses `latest`; other refs can derive a tag or use `image_tag` | Skipped only for the configured default source without an explicit tag |
| Release | Manual | Explicit `source_ref`; standard images can derive the version, PD/Store/Server requires `image_tag=x.y.z` | Always runs |

Manual latest runs default to validation (`publish=false`): build all configured platforms and read caches without pushing images, exporting caches, or updating `LAST_*_HASH`. Scheduled runs publish automatically. Set `publish=true` to publish manually; use an explicit `image_tag` for branch builds. PD/Store/Server also supports `dry_run=true` to force a fresh validation of unchanged master.

Common image inputs:

- `source_repository`: source repository; built-in wrappers allow only the component's Apache and HugeGraph repositories.
- `source_ref`: branch, tag or commit, resolved to a fixed SHA.
- `image_tag`: destination tag, independent of the source ref.
- `publish`: controls image pushes and cache exports in latest workflows.

The standard publisher selects Dockerfiles, build contexts, platforms and optional smoke tests through `build_matrix_json`. Images carry OCI source/revision labels. Successful latest publications can update `LAST_*_HASH`.

### PD/Store/Server validation

```text
Resolve source SHA and tag
  → Build/load PD, Store, HStore Server and standalone Server (amd64 + arm64)
  → Run local compose, example graph and Gremlin CRUD checks
  → Smoke-test standalone Server
  → Push the same multi-platform candidates
  → Update latest hash, when enabled
```

Sources with `docker/bake.hcl` share one native Maven build and registry cache (`hugegraph/hugegraph:shared-<channel>`); older sources use the per-Dockerfile path. Compose uses `docker/docker-compose.dev.yml` with `pull_policy: never`, or the compatible `docker/docker-compose.yml` fallback.

The precheck imports the bundled `example.groovy`, verifies its 6 vertices/6 edges, and exercises Gremlin CRUD before returning to that baseline. Runtime checks use the locally loaded amd64 images. Only successful candidates are pushed, without rebuilding or temporary architecture tags.

The runner uses 1 PD + 1 Store + 1 Server, with 1 GiB each for PD/Store and 1.5 GiB for Server. These are CI limits, not production sizing. A full 3+3+3 topology requires a larger runner or another validated test setup.

### Multi-platform builds

Use `FROM --platform=$BUILDPLATFORM` for portable build stages so Maven/Node packaging runs natively; leave runtime stages on the target platform. Native libraries, JNI/CGO and architecture-specific downloads need cross-compilation or native target runners plus runtime tests. See [Docker's build strategies](https://docs.docker.com/build/building/multi-platform/) and [platform arguments](https://docs.docker.com/reference/dockerfile/#automatic-platform-args-in-the-global-scope).

## Python packages

[`publish_python.yml`](.github/workflows/publish_python.yml) publishes `hugegraph-python` from `apache/hugegraph-ai`.

| Input | Default | Meaning |
| --- | --- | --- |
| `source_ref` | `main` | Branch, tag or SHA; resolved to an immutable commit |
| `target` | `testpypi` | `testpypi` or `pypi`; production requires a tag exactly matching the package version |

Create environments `testpypi` and `pypi`, each containing `PYPI_API_TOKEN`. Only the upload step receives the selected token.

The build job uses uv, checks wheel/sdist with Twine, and runs isolated installation and client tests on Python 3.10/3.11. The publish job verifies the manifest and remote filenames/hashes before uploading those exact artifacts. Unexpected remote files or conflicting hashes fail; identical files are skipped. For partial uploads, **re-run failed jobs** to reuse the original artifacts.

Local checks (no upload):

```bash
uv run --no-project --python 3.11 python -m unittest discover -s tests -p 'test_python_release.py' -v
actionlint .github/workflows/publish_python.yml
```

A new manual workflow must first reach the default branch to register dispatch; subsequent runs can select another workflow branch with `gh workflow run --ref`.

## Workflow map and maintenance

| Area | Entry points |
| --- | --- |
| AI | [Latest](.github/workflows/publish_latest_ai_image.yml) · [Release](.github/workflows/publish_release_ai_image.yml) |
| Loader | [Latest](.github/workflows/publish_latest_loader_image.yml) · [Release](.github/workflows/publish_release_loader_image.yml) |
| Hubble | [Latest](.github/workflows/publish_latest_hubble_image.yml) · [Release](.github/workflows/publish_release_hubble_image.yml) |
| Vermeer | [Latest](.github/workflows/publish_latest_vermeer_image.yml) · [Release](.github/workflows/publish_release_vermeer_image.yml) |
| PD/Store/Server | [Latest](.github/workflows/publish_latest_pd_store_server_image.yml) · [Release](.github/workflows/publish_release_pd_store_server_image.yml) |
| Python | [Manual release](.github/workflows/publish_python.yml) |
| Apache source release | [Validation](.github/workflows/validate-release.yml) |
| Legacy/special cases | [Hubble](.github/workflows/publish_hugegraph_hubble.yml) · [Computer](.github/workflows/publish_computer_image.yml) |

Keep trigger policy and component settings in wrappers, shared build behavior in reusable workflows. Preserve the distinct latest/release semantics. Use a dedicated workflow for materially different checks or ordering, and record measured build timings in PRs or CI reports.
