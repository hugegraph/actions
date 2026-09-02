# HugeGraph Actions

HugeGraph Actions is the shared CI/CD workspace for HugeGraph repositories.
It mainly hosts GitHub Actions workflows for publishing Docker images, validating releases, and coordinating repository-specific automation.

## Core Design

The image publishing workflows are intentionally split into two layers:

```text
                     Trigger
                        |
        +---------------+----------------+
        |                                |
   scheduled / manual                manual release
   latest publish                    publish from branch
        |                                |
        v                                v
  publish_latest_*.yml            publish_release_*.yml
        \                                /
         \                              /
          +------------+---------------+
                       |
                       v
         reusable workflow implementation
                       |
     +-----------------+----------------------------+
     |                                              |
     v                                              v
_publish_image_reusable.yml         _publish_pd_store_server_reusable.yml
     |                                              |
     v                                              v
standard single-image flow          pd/store/server specialized flow
```

The two publishing modes behave differently:

- `latest` mode
  - scheduled or ad-hoc publish for the current default branch line (master in `apache/hugegraph`)
  - skips work when the source hash has not changed
  - updates the stored `LAST_*_HASH` variable after a successful publish

- `release` mode
  - manual publish from an explicitly selected source ref
  - always publishes when invoked
  - uses `image_tag` when provided; standard images otherwise derive `x.y.z`
    from the source ref (PD/Store/Server requires an explicit `x.y.z` tag)

## Multi-Platform Build Performance

BuildKit exposes automatic platform arguments such as `BUILDPLATFORM` and
`TARGETPLATFORM`. A multi-stage Dockerfile can pin an architecture-independent
build stage to the native builder while leaving the runtime stage on the target
platform:

```dockerfile
FROM --platform=$BUILDPLATFORM maven:3.9.0-eclipse-temurin-11 AS build
RUN mvn package ...

FROM eclipse-temurin:11-jre-jammy
COPY --from=build /pkg/dist/ /app/
```

Without the explicit build platform, every unqualified `FROM` defaults to the
requested target platform. An amd64 GitHub runner therefore executes the entire
arm64 Maven, Node, compression, or packaging workload through QEMU. Pinning the
portable build stage prevents emulation while the final JRE/base image remains
architecture correct.

This is a BuildKit-only feature. Buildx requires Docker Engine 19.03 or newer,
and the automatic platform arguments are documented in the Dockerfile frontend:

- [Docker multi-platform build strategies](https://docs.docker.com/build/building/multi-platform/)
- [Dockerfile automatic platform arguments](https://docs.docker.com/reference/dockerfile/#automatic-platform-args-in-the-global-scope)

Use this optimization only when the copied build output is portable or is
explicitly cross-compiled for `TARGETOS` / `TARGETARCH`. Native C/C++, CGO,
JNI, platform-classifier artifacts, and downloaded executables must be audited
and covered by real target-platform smoke tests. When the output is inherently
target-specific, use a native target runner instead of forcing `BUILDPLATFORM`.

The primary performance gain comes from moving portable build work off QEMU.
Registry cache improvements are additional to the native-build or
cross-compilation gains. Keep measured timings in pull requests or dated CI
reports rather than this design document.

### Read-Only Branch Validation

Latest wrappers use two execution policies:

- default branch (`master`, or `main` for AI): publish images, export registry
  caches, create manifests, and update the corresponding `LAST_*_HASH` variable.
- non-default ref with `publish=false`: force validation checks, import existing
  caches read-only, build all configured platforms, and skip image pushes, cache
  exports, manifests, and hash updates.

Set `publish=true` with an explicit `image_tag` to publish a branch build, for
example `source_repository=hugegraph/hugegraph`, `source_ref=helm-dev`, and
`image_tag=helm-dev`.

This allows an upstream Dockerfile branch to be benchmarked before merge without
changing public images or production cache state.

For pd/store/server, a manual `master` run can also set `dry_run=true`. This
forces a fresh exact-master multi-platform build and integration check even when
the source hash is unchanged, while disabling image pushes, cache exports, and
hash updates.

## Critical Path: PD/Store/Server

`pd/store/server` is the most important publishing flow in this repository and uses a dedicated reusable workflow:
[`.github/workflows/_publish_pd_store_server_reusable.yml`](./.github/workflows/_publish_pd_store_server_reusable.yml).

One candidate job builds PD, Store, HStore Server, and standalone Server as
amd64/arm64 images and loads both variants into Docker's containerd image store.
Source revisions that provide `docker/bake.hcl` use one shared BuildKit graph:
the native Maven stage runs once, the four target-platform runtime images fan
out in parallel, and one shared registry cache is exported. Older source
revisions keep the serial per-Dockerfile compatibility path.
It starts the upstream `docker/docker-compose.dev.yml` topology with
`pull_policy: never`, and runs a functional graph check before any image is
published. Compatible source revisions that have the same service contract but
only contain `docker/docker-compose.yml` use that legacy file as a fallback. The
check executes the Server image's bundled
`/hugegraph-server/scripts/example.groovy` file, verifies the six-vertex,
six-edge sample graph, then performs separate Gremlin read, create, update, and
delete requests. The final query must return to the original 6V/6E baseline.
The same loaded standalone candidate then passes its smoke test. Docker selects
the local amd64 variants for these checks. Only after all enabled checks succeed
are the already loaded multi-platform final tags pushed; the publishing stage
does not rebuild images and does not create temporary architecture tags.

The precheck override constrains the three JVMs and Store buffers for a small
CI workload. PD and Store are limited to 1 GiB each, Server to 1.5 GiB, while
heap, direct memory, RocksDB, Raft, and worker queues use a low-memory test
profile. These settings are functional-test limits, not production guidance or
a performance baseline.

```text
                        source ref
                              |
                              v
                         prepare job
       (resolve source SHA, explicit image tag, hash gate)
                              |
                              v
           build_test_publish_multiarch (one job)
                  shared Maven build stage
                              |
         +--------------------+----------------------------+
         | pd | store | server-hstore | server-standalone |
         +--------------------+----------------------------+
        build and load linux/amd64 + linux/arm64 variants
       low-memory compose + bundled graph + Gremlin CRUD
                    standalone smoke test
           push loaded x.y.z (or latest) indexes
                              |
                              v
             update_latest_hash (latest mode only, optional)
```

Tag behavior:

- Final tags contain both `linux/amd64` and `linux/arm64` variants.
- No temporary `*-amd64` or `*-arm64` tags are created.
- Failed builds or functional checks stop the job before any candidate is pushed.

Execution note:

- All four current Dockerfiles use `FROM --platform=$BUILDPLATFORM` for their
  portable build stages. The x86 runner therefore performs Maven work natively
  and limits QEMU to ARM target-image runtime steps.
- Compatible source revisions use `docker/bake.hcl` to deduplicate that native
  Maven stage and export it once as `hugegraph/hugegraph:shared-<channel>`.
- The single job shares checkout, Docker, QEMU, Buildx, and login setup. Dry-runs
  read existing caches but never export new ones.

## Why The Wrappers Stay Split

Although the `latest` and `release` wrappers look similar, they encode different release semantics.

- `latest` is the automatic path.
  - It is scheduled for daily publication and can also be triggered manually.
  - It uses the hash gate to avoid republishing unchanged sources.
  - It usually targets the main development branch for each repository.

- `release` is the intentional publication path.
  - It is triggered manually.
  - Its source `source_ref` and destination `image_tag` are independent.
  - It should run even if the source is unchanged, because the operator is explicitly asking for a release publication.

Most wrappers use [`.github/workflows/_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml).

The pd/store/server wrappers use [`.github/workflows/_publish_pd_store_server_reusable.yml`](./.github/workflows/_publish_pd_store_server_reusable.yml), which adds an integration precheck and single-job multi-platform publication.

## Reusable Workflow Responsibilities

Reusable workflows are the real implementation layer.

`_publish_image_reusable.yml` handles the standard image flow:

- resolving `latest` vs `release` mode
- checking out the correct source commit
- deriving the image tag
- selecting per-module build settings from `build_matrix_json`
- enabling QEMU and Buildx when needed
- running optional smoke tests
- stamping `org.opencontainers.image.revision` and `org.opencontainers.image.source` with the resolved source commit and repository
- pushing the final image
- updating the latest-hash variable for `latest` mode only

`_publish_pd_store_server_reusable.yml` handles the pd/store/server flow:

- shared source SHA resolution and latest hash gate
- build and locally load multi-platform candidates followed by strict low-memory integration precheck for pd/store/server (hstore backend, `hugegraph/server`)
- import of the Server image's bundled `example.groovy` graph and Gremlin CRUD validation
- publication of the loaded amd64/arm64 index directly to the final tag
- independent release source ref and destination image tag inputs
- standalone server smoke test for `hugegraph/hugegraph`

The current precheck intentionally uses a 1 PD + 1 Store + 1 Server topology so
it fits standard GitHub-hosted runners. A full 3 PD + 3 Store + 3 Server compose
gate remains a TODO for a larger runner or a reliable lower-resource simulation.

Wrapper workflows provide the common source and publication contract:

- `source_repository`: source repository in `owner/name` format
- `allowed_source_repositories`: comma-separated trusted repositories accepted by the wrapper
- `source_ref`: source branch, tag, or commit
- `image_tag`: optional image tag; the configured default source uses `latest`, other latest refs derive a tag when omitted, and release mode derives or validates a version
- `publish`: whether to push images and registry caches

Only the component's Apache and HugeGraph source repositories are accepted by
the built-in wrappers. Manual runs always respect `publish`; scheduled runs
enable it automatically. Latest hash gating is limited to the configured
default source with no explicit `image_tag`.

Standard wrappers may also pass `build_matrix_json`; the specialized
pd/store/server workflow defines its four image builds directly. Manual latest
dispatches default to validation for every ref; set `publish=true` and provide
`image_tag` to publish a branch build such as `helm-dev`. An explicit
`image_tag=latest` is also allowed for a non-default source.

## How To Extend

When adding a new image publishing workflow, follow the same pattern:

1. Create a thin `publish_latest_*.yml` wrapper if the image needs to be scheduled or hash-gated for automatic publishing.
2. Create a matching `publish_release_*.yml` wrapper if the image also needs manual release publishing.
3. Put shared build behavior into the appropriate reusable workflow instead of duplicating Docker or checkout logic.
4. Put image-specific values in the wrapper via `build_matrix_json`, especially:
   - module name
   - Dockerfile path
   - build context
   - image repository name
   - platform list
   - optional smoke test command

Use the reusable workflow for behavior, and the wrapper for policy.

### When To Keep A Special-Case Workflow Separate

Keep a dedicated workflow file when the publishing flow has materially different behavior, for example:

- integration prechecks before publishing
- multiple images with custom dependency ordering
- different trigger semantics that do not fit the `latest` / `release` split
- legacy workflows that still require bespoke setup

For example, [`.github/workflows/publish_latest_pd_store_server_image.yml`](./.github/workflows/publish_latest_pd_store_server_image.yml) and [`.github/workflows/publish_release_pd_store_server_image.yml`](./.github/workflows/publish_release_pd_store_server_image.yml) use a dedicated reusable workflow for specialized precheck and publish sequencing.

## Current Workflow Map

- Standard reusable publish path:
  - [`.github/workflows/publish_latest_loader_image.yml`](./.github/workflows/publish_latest_loader_image.yml)
  - [`.github/workflows/publish_release_loader_image.yml`](./.github/workflows/publish_release_loader_image.yml)
  - [`.github/workflows/publish_latest_hubble_image.yml`](./.github/workflows/publish_latest_hubble_image.yml)
  - [`.github/workflows/publish_release_hubble_image.yml`](./.github/workflows/publish_release_hubble_image.yml)
  - [`.github/workflows/publish_latest_vermeer_image.yml`](./.github/workflows/publish_latest_vermeer_image.yml)
  - [`.github/workflows/publish_release_vermeer_image.yml`](./.github/workflows/publish_release_vermeer_image.yml)
  - [`.github/workflows/publish_latest_ai_image.yml`](./.github/workflows/publish_latest_ai_image.yml)
  - [`.github/workflows/publish_release_ai_image.yml`](./.github/workflows/publish_release_ai_image.yml)

- Dedicated reusable publish path:
  - [`.github/workflows/publish_latest_pd_store_server_image.yml`](./.github/workflows/publish_latest_pd_store_server_image.yml)
  - [`.github/workflows/publish_release_pd_store_server_image.yml`](./.github/workflows/publish_release_pd_store_server_image.yml)

- Other legacy or special-case workflows:
  - [`.github/workflows/publish_hugegraph_hubble.yml`](./.github/workflows/publish_hugegraph_hubble.yml)
  - [`.github/workflows/publish_computer_image.yml`](./.github/workflows/publish_computer_image.yml)

## Practical Notes

- `latest` workflows typically run on a schedule and accept manual dispatch.
- `release` workflows typically accept only manual dispatch. They use the same
  `source_repository` / `source_ref` contract and accept an optional
  independent `image_tag`.
- Most image workflows inherit credentials and settings through a reusable workflow.
- If you change shared standard behavior, update `_publish_image_reusable.yml` first.
- If you change pd/store/server behavior, update `_publish_pd_store_server_reusable.yml` first.
