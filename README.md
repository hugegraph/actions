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
  - manual publish from a versioned branch such as `release-1.7.0`
  - always publishes when invoked
  - derives the image tag from the release branch version

## Critical Path: PD/Store/Server

`pd/store/server` is the most important publishing flow in this repository and uses a dedicated reusable workflow:
[`.github/workflows/_publish_pd_store_server_reusable.yml`](./.github/workflows/_publish_pd_store_server_reusable.yml).

```text
               source branch (master / release-x.y.z)
                              |
                              v
                         prepare job
           (resolve source SHA, version tag, hash gate)
                              |
                              v
                  integration_precheck (optional)
            (compose health check for pd/store/server-hstore)
                              |
                              v
                   publish_amd64 (matrix x4 modules)
         +-------------------------------------------------+
         | pd | store | server-hstore | server-standalone |
         +-------------------------------------------------+
                push x.y.z-amd64 (or latest-amd64)
                              |
                              v
                   publish_arm64 (matrix x4 modules)
                push x.y.z-arm64 (or latest-arm64)
                              |
                              v
                 publish_manifest (matrix x4 modules)
         merge amd64+arm64 => x.y.z (or latest) manifest
         then delete temporary -amd64 / -arm64 tags
                              |
                              v
             update_latest_hash (latest mode only, optional)
```

Tag behavior:

- If the `amd64` publish succeeds but the `arm64` publish fails, manifest is not created and the `*-amd64` tag remains available.
- If both amd64 and arm64 succeed, manifest publish runs and then removes temporary `*-amd64` and `*-arm64` tags.
- End users should primarily use `latest` or release version tags (`x.y.z`).

Execution note:

- `publish_arm64` runs after `publish_amd64` by design, so x86 users can get a usable image earlier and arm64 compute is not spent when amd64 fails.

## Why The Wrappers Stay Split

Although the `latest` and `release` wrappers look similar, they encode different release semantics.

- `latest` is the automatic path.
  - It is scheduled for daily publication and can also be triggered manually.
  - It uses the hash gate to avoid republishing unchanged sources.
  - It usually targets the main development branch for each repository.

- `release` is the intentional publication path.
  - It is triggered manually.
  - It expects a release branch and publishes that branch as a versioned image.
  - It should run even if the source is unchanged, because the operator is explicitly asking for a release publication.

Most wrappers use [`.github/workflows/_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml).

The pd/store/server wrappers use [`.github/workflows/_publish_pd_store_server_reusable.yml`](./.github/workflows/_publish_pd_store_server_reusable.yml), which adds integration precheck plus staged amd64/arm64 publish and manifest merge.

## Reusable Workflow Responsibilities

Reusable workflows are the real implementation layer.

`_publish_image_reusable.yml` handles the standard image flow:

- resolving `latest` vs `release` mode
- checking out the correct source commit
- deriving the image tag
- selecting per-module build settings from `build_matrix_json`
- enabling QEMU and Buildx when needed
- running optional smoke tests
- pushing the final image
- updating the latest-hash variable for `latest` mode only

`_publish_pd_store_server_reusable.yml` handles the pd/store/server flow:

- shared source SHA resolution and latest hash gate
- strict integration precheck for pd/store/server (hstore backend, `hugegraph/server`)
- staged image publication with `*-amd64` then `*-arm64`
- manifest merge to final tag (`latest` or release version)
- remove temporary `*-amd64` and `*-arm64` tags after successful manifest publish
- standalone server smoke test for `hugegraph/hugegraph`

Wrapper workflows provide the source repository, branch, and mode-specific inputs.
Standard wrappers may also pass `build_matrix_json`, while the pd/store/server matrix is defined inside `_publish_pd_store_server_reusable.yml`.

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
- `release` workflows typically accept only manual dispatch with a branch input.
- Most image workflows inherit credentials and settings through a reusable workflow.
- If you change shared standard behavior, update `_publish_image_reusable.yml` first.
- If you change pd/store/server behavior, update `_publish_pd_store_server_reusable.yml` first.
