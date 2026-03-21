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
      .github/workflows/_publish_image_reusable.yml
                       |
     +-----------------+-------------------+
     |                                     |
     v                                     v
 resolve mode / branch / tag         build matrix per module
     |                                     |
     v                                     v
 optional hash gate                  optional smoke test
     |                                     |
     +-----------------+-------------------+
                       |
                       v
                 docker build/push
```

The two publishing modes behave differently:

- `latest` mode
  - scheduled or ad-hoc publish for the current main branch line
  - skips work when the source hash has not changed
  - updates the stored `LAST_*_HASH` variable after a successful publish

- `release` mode
  - manual publish from a versioned branch such as `release-1.7.0`
  - always publishes when invoked
  - derives the image tag from the release branch version

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

The common build logic already lives in [`.github/workflows/_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml), so the thin wrapper files mainly exist to keep the trigger semantics obvious and safe.

## Reusable Workflow Responsibilities

[`_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml) is the real implementation layer.

It handles:

- resolving `latest` vs `release` mode
- checking out the correct source commit
- deriving the image tag
- selecting per-module build settings from `build_matrix_json`
- enabling QEMU and Buildx when needed
- running optional smoke tests
- pushing the final image
- updating the latest-hash variable for `latest` mode only

Wrapper workflows only provide the source repository, branch, matrix definition, and mode-specific inputs.

## How To Extend

When adding a new image publishing workflow, follow the same pattern:

1. Create a thin `publish_latest_*.yml` wrapper if the image needs scheduled or hash-gated automatic publishing.
2. Create a matching `publish_release_*.yml` wrapper if the image also needs manual release publishing.
3. Put all shared build behavior into `_publish_image_reusable.yml` instead of duplicating Docker or checkout logic.
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

For example, [`.github/workflows/publish_latest_pd_store_server_image.yml`](./.github/workflows/publish_latest_pd_store_server_image.yml) has a more customized precheck-oriented flow than the standard image publishers.

## Current Workflow Map

- Standard reusable publish path:
  - [`.github/workflows/publish_latest_loader_image.yml`](./.github/workflows/publish_latest_loader_image.yml)
  - [`.github/workflows/publish_release_loader_image.yml`](./.github/workflows/publish_release_loader_image.yml)
  - [`.github/workflows/publish_latest_server_image.yml`](./.github/workflows/publish_latest_server_image.yml)
  - [`.github/workflows/publish_release_server_image.yml`](./.github/workflows/publish_release_server_image.yml)
  - [`.github/workflows/publish_latest_hubble_image.yml`](./.github/workflows/publish_latest_hubble_image.yml)
  - [`.github/workflows/publish_release_hubble_image.yml`](./.github/workflows/publish_release_hubble_image.yml)
  - [`.github/workflows/publish_latest_vermeer_image.yml`](./.github/workflows/publish_latest_vermeer_image.yml)
  - [`.github/workflows/publish_release_vermeer_image.yml`](./.github/workflows/publish_release_vermeer_image.yml)
  - [`.github/workflows/publish_latest_ai_image.yml`](./.github/workflows/publish_latest_ai_image.yml)
  - [`.github/workflows/publish_release_ai_image.yml`](./.github/workflows/publish_release_ai_image.yml)

- Legacy or special-case workflows:
  - [`.github/workflows/publish_hugegraph_hubble.yml`](./.github/workflows/publish_hugegraph_hubble.yml)
  - [`.github/workflows/publish_computer_image.yml`](./.github/workflows/publish_computer_image.yml)
  - [`.github/workflows/publish_latest_pd_store_server_image.yml`](./.github/workflows/publish_latest_pd_store_server_image.yml)

## Practical Notes

- `latest` workflows typically run on a schedule and accept manual dispatch.
- `release` workflows typically accept only manual dispatch with a branch input.
- Most image workflows inherit credentials and settings through the reusable workflow.
- If you change the shared publishing behavior, update `_publish_image_reusable.yml` first and then adjust wrappers only where their inputs change.
