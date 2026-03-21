# AGENTS.md

## Purpose

This repository contains shared GitHub Actions workflows for HugeGraph projects.
Its main purpose is to publish Docker images, validate releases, and host small automation workflows used across HugeGraph repositories.

## Repository Model

- `latest` publishing is the automated path: scheduled or manually triggered, with hash gating to skip unchanged sources.
- `release` publishing is the manual path: it publishes from a versioned branch and should run even if the source is unchanged.
- Shared image publishing logic lives in [`.github/workflows/_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml).
- Thin `publish_latest_*.yml` and `publish_release_*.yml` files are wrappers that define trigger policy and per-image inputs.

## Editing Rules

- Prefer changing the reusable workflow first when the build or publish behavior is shared.
- Keep wrapper workflows thin and explicit.
- Do not merge `latest` and `release` wrappers unless the trigger semantics are truly identical.
- Keep special-case workflows separate when they need extra prechecks, custom ordering, or non-standard release flow.

## Important Files

- [`README.md`](./README.md): human-facing overview of the workflow design.
- [`.github/workflows/_publish_image_reusable.yml`](./.github/workflows/_publish_image_reusable.yml): shared publish implementation.
- [`.github/workflows/publish_latest_*.yml`](./.github/workflows): automated publish wrappers.
- [`.github/workflows/publish_release_*.yml`](./.github/workflows): manual release publish wrappers.

## Before Making Changes

- Read the relevant workflow and the reusable workflow together.
- Preserve existing trigger semantics unless the task explicitly asks for a behavioral change.
- Check whether the workflow is a standard publisher or a legacy / special-case flow before refactoring.

