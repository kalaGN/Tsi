---
name: karpathy-guidelines
description: Review a concrete code change for unnecessary complexity or unrelated edits. Use when simplifying, refactoring, or investigating overengineering.
license: MIT
---

# Karpathy Guidelines

Use these checks on the concrete change in scope. Do not turn them into a mandatory workflow for unrelated tasks.

## Think Before Coding

- State only assumptions that materially affect behavior.
- Use repository precedent for safe, reversible choices.
- Ask only when ambiguity changes scope, public behavior, safety, or cost.
- Prefer the simplest approach that satisfies the request.

## Simplicity First

- Do not add unrequested features, configurability, or speculative abstractions.
- Avoid wrappers and error handling that do not protect a real boundary.
- If a shorter design is equally clear and correct, use it.

## Surgical Changes

- Touch only lines that trace to the request.
- Match existing style and avoid adjacent refactors.
- Remove only dead code created by the current change; report pre-existing dead code instead of deleting it.

## Verification

- Define observable success before editing.
- During iteration, run the smallest relevant check.
- Run the project's complete quality gate only when its rules require delivery-level verification.
