# Workflow maintenance

For workflow inputs and everyday use, see the [README](../README.md).

## Registry access

### Docker Hub and GHCR

- Configure `DOCKERHUB_USERNAME` and `DOCKERHUB_PASSWORD` with write access to the destination Docker Hub repositories. The Helm repository is `hugegraph/hugegraph-helm` and must be public.
- GHCR publication uses `GITHUB_TOKEN` with `packages: write`. An existing package must grant `hugegraph/actions` write access and allow anonymous pulls.
- Latest image workflows can update their `LAST_*_HASH` repository variable after publication. Validation-only runs leave these variables unchanged.

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

Image entry workflows define the source, Dockerfiles, platforms and component settings. Use `FROM --platform=$BUILDPLATFORM` for portable Maven/Node build stages so packaging runs natively; runtime stages use the target platform. Native libraries and architecture-specific downloads still need appropriate builds and runtime tests. See [Docker's build strategies](https://docs.docker.com/build/building/multi-platform/).

The Helm smoke test uses 1 PD + 1 Store + 1 Server with memory limits of 1/2/2 GiB, persistent volumes, and Hubble disabled. These limits are for CI, not production sizing. Tools are checksum-pinned and test images are resolved to digests; published Chart image values stay as defined in source.

Python builds test wheel and source distributions in isolated environments. With `both`, MCP uses the client artifact from the same run before publication and the published client afterward. For TestPyPI publication, it selects the matching TestPyPI client wheel; other dependencies come from public PyPI.

Local checks require no Docker or publication:

```bash
python3 -m unittest discover -s tests -p 'test_helm*.py' -v
bash tests/test_publish_prepare.sh
actionlint .github/workflows/publish_helm.yml .github/workflows/publish_latest_*.yml

uv run --no-project --python 3.11 python -m unittest discover -s tests -p 'test_python_release.py' -v
actionlint .github/workflows/publish_python.yml .github/workflows/_build_python_reusable.yml
```
