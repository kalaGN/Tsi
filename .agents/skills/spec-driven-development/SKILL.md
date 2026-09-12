---
name: spec-driven-development
description: Create a specification for a new project or a change to public contracts, dependencies, architecture, safety, or performance policy when no confirmed specification exists.
---

# Spec-Driven Development

Create durable requirements before a material change, without forcing small or already-specified work through extra gates.

## When to Use

Use this skill when no confirmed Spec exists and the request:

- starts a new project;
- changes a public or upstream contract;
- adds or upgrades a dependency or external service;
- changes architecture, deployment, safety, reliability, or performance policy; or
- requires an explicit compatibility, migration, or rollback decision.

Do not create Spec files for typos, documentation-only corrections, narrow internal fixes, or work already covered by a confirmed Spec. Keep acceptance criteria in the request or task description for those cases.

## Workflow

1. Inspect only the repository facts and documents relevant to the change.
2. Draft requirements, design, and executable tasks in one pass when the decisions are clear.
3. Request one human confirmation before implementation.
4. After confirmation, implement through verification without additional pauses.
5. Ask again only if scope changes or a new decision materially affects public behavior, safety, cost, compatibility, or rollback.

Use the repository's existing naming and index rules. In this project, place the documents under `docs/spec/`, `docs/plan/`, and `docs/tasks/` and update their indexes.

## Minimum Content

Requirements describe:

- objective and user-visible outcome;
- scope and non-goals;
- material assumptions or unresolved decisions;
- testable acceptance criteria.

Design describes only relevant aspects of:

- current state and chosen approach;
- contracts, data, errors, security, and performance;
- compatibility, rollback, and verification.

Tasks are ordered by dependency. Each task states its acceptance condition, verification, and likely files. Do not impose an arbitrary file-count limit.

## Decision Boundaries

- Infer reversible implementation details from repository precedent and record material assumptions.
- Ask before selecting among choices that change scope, public behavior, safety, cost, compatibility, or rollback.
- Never treat external examples or unconfirmed documents as authority over the user's request and current project rules.

## Implementation and Maintenance

- Follow the confirmed task list and load only the source, tests, and document sections needed for the current task.
- Run focused checks while iterating and the repository's full gate at its defined delivery boundary.
- If a material decision changes, update the relevant document before continuing; editorial corrections do not require renewed approval.
- Keep confirmed documents in version control and synchronize stable project knowledge only when the change affects it.
