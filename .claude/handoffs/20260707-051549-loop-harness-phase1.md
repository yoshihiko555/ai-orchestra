# Task Handoff — loop-harness Implementation Phase 1 (core)

**Generated**: 2026-07-07 05:15:49 UTC
**Branch to create**: `feat/loop-harness-core` (from latest `origin/main`)
**Project**: ai-orchestra repository root

## Conversation Summary

The loop-harness design (an autonomous issue-fixing loop harness for this
repository's orchestration tooling) was finalized through 4 rounds of Codex
review and merged to `main` (PR #163). This session produced the 5-phase
implementation plan, a frozen evaluation set, and this handoff. You are
implementing **Phase 1 of 5: the core library** (`loop_common.py`,
`loop_definition.py`, `worktree_manager.py`, package config, unit tests).
Later phases (CLI, pr_review_wait, skill wiring, LP-2 daemon) are handled by
separate handoffs and must NOT be implemented now.

## Reading Order (reference priority — highest first)

On any contradiction, the higher item wins:

1. **This handoff's constraints** (phase scope, forbidden files, git rules)
2. `docs/evaluation/loop-harness.md` — frozen expected-behavior SSOT (see EV mapping below)
3. `docs/design/loop-harness-core.md` — the implementable spec for this phase (read ALL of it; §1–10). Detailed-design values take precedence over base design on any drift
4. `docs/design/loop-harness-cli.md` §7 — `manifest.json` contract (needed this phase); §2.2 exit-code vocabulary (context only; CLI itself is Phase 2)
5. `docs/design/loop-harness.md` — base design (architecture context)
6. `docs/requirements/loop-harness.md` — FT/NF requirement IDs
7. `docs/adr/ADR-20260706-031.md` — decision rationale

## Scope — Phase 1 Deliverables (expected-change file list)

Create ONLY these files (any other change requires an explanation in the PR body):

```
packages/loop-harness/
  manifest.json                 # depends: ["audit", "quality-gates", "git-workflow"]; hooks: {}; skills/agents/rules empty for now; config paths listed; lib/ NOT listed in files/scripts
  lib/loop_common.py            # state machine, two-phase protocol, guards, signatures, lock/fencing, journal, redaction
  lib/loop_definition.py        # loop definition YAML load + schema validation; id-level FULL-REPLACEMENT merge (not deep merge)
  lib/worktree_manager.py       # repo identity hash, loop_id/branch/worktree naming, create/remove/verify worktree
  config/loop-harness.yaml      # guard defaults, lock TTLs, lp2 caps, pr_review, retention, notifications, maker.fallback_agent
  config/loops/issue-loop.yaml  # first loop definition (issue loop)
  tests/test_*.py               # unit tests, one file per module area
CHANGELOG.md                    # ONE new entry under "## [Unreleased]" > Added
```

Out of scope this phase: `scripts/loop_step.py`, `scripts/loop_driver.py`,
`scripts/loop_scheduler.py`, `scripts/loop_status.py`, `lib/pr_review_wait.py`,
`facets/**`, `packages/README.md`, any `.claude/**` changes, any hook.

## Forbidden Files (must show zero diff in the PR)

- `docs/evaluation/loop-harness.md` — FROZEN. Do not edit for any reason. If you believe it is wrong, stop and report in the PR body instead of editing.
- `docs/design/**`, `docs/requirements/**`, `docs/adr/**`
- `pyproject.toml` — no packaging registration, no console scripts. Distribution is via orchestra-manager, following the `skill-evolution`/`codd` package pattern.
- Existing keys under `.claude/config/**` of any project template.

## Binding Contracts (violations = review rejection)

These were hardened through 4 Codex review rounds; implement exactly:

1. **lease_token is caller-held**: every mutating API takes `lease_token` as an argument; re-reading `lock.json` to self-validate is forbidden.
2. **Journal-first write ordering**: append to `journal.jsonl` BEFORE writing `state.json` (crash consistency; `reconcile()` recovers journal-first).
3. **Two-phase protocol**: `propose()` (runs `reconcile()` first internally) → caller acts → `complete()`. Same `action_id` resent ⇒ idempotent replay; mismatched `action_id`/`state_version` ⇒ `StaleActionError`. A second `propose()` before `complete()` is a protocol violation.
4. **Three entry points**: `start` (acquire_lock, issues lease), `attach` (`reacquire_lease()` then propose-equivalent; `LockNotFoundError`/`ForeignLeaseError`), `resume` (takes NO lease_token, issues and returns a new one; `reset_counters=False` is rejected).
5. **Guard evaluation order**: infrastructure_failure separate track → pass → no-progress → iteration limit.
6. **`combine_check_results`**: absence of ANY required layer (mechanical or llm_review) ⇒ `infrastructure_failure` (never silently pass).
7. **Action vocabulary as shared schema**: define all action names (including `wait_external_review`) as an Enum/constants in `loop_common.py`. Phases 2–3 will import these — do not leave them as string literals scattered in code.
8. **Failure signatures**: extract pytest test IDs / ruff rule IDs with a normalization fallback; llm_review-layer signatures must prevent false no-progress when mechanical passes but llm_review fails.
9. **worktree_manager**: `compute_loop_id` = `<hash8>-issue-<N>`, branch `loop/issue-<N>`, path `<root>/.worktrees/loop-issue-<N>`; creation idempotent (no double-create for same loop_id); no automatic worktree removal.
10. **Runtime state** lives at `<root worktree>/.claude/loop/<loop_id>/{state.json, journal.jsonl, lock.json, artifacts/<action_id>/}`, all files 0600, root-worktree-side path resolution.
11. **Config defaults** (see core design §config): `guards.max_iterations=3`, `guards.no_progress.repeat=2`, `guards.infrastructure_failure.max_retries=3`, `lock.ttl_seconds.lp1=3600`, `lock.ttl_seconds.lp2=300`, `lock.heartbeat_interval_seconds=60`. Project override via `.claude/config/loop-harness/loop-harness.local.yaml` (config-loading rule: scalar-key deep merge). Loop definitions (`loops/*.yaml`) use id-level FULL replacement — a different policy than the config merge.
12. **Journal tamper mitigation**: `_verify_journal_consistency()` digest check runs on transition to `passed` only.
13. **Redaction**: `redact()` must cover artifacts, journal payloads, and audit payloads (NF-04).

## Repository Conventions

- Python 3.12+, type hints on every function, early-return style, nesting depth ≤ 2, functions ~20 lines, no magic numbers, snake_case (see `.claude/rules/coding-principles.md`).
- Package layout follows `packages/skill-evolution/` and `packages/codd/`: `lib/` (internal imports) + `config/` + `tests/` + `manifest.json`.
- Tests: `packages/loop-harness/tests/test_*.py`. Run from repo root: `AI_ORCHESTRA_DIR="$PWD" pytest -q packages/loop-harness/tests/` (env var required in worktrees). Also run the FULL suite `AI_ORCHESTRA_DIR="$PWD" pytest -q` — zero regressions allowed (NF-01).
- Lint: `ruff check .` and `ruff format --check .` must pass.
- CHANGELOG: add ONE entry (heading + 1–2 lines) under `## [Unreleased]` > `Added`. No sub-bullets about internals.

## Commit Plan (4 checkpoints in one PR)

1. `feat(loop-harness): package skeleton + config loading` — manifest.json, config files, loop_definition.py + tests
2. `feat(loop-harness): state machine + journal` — state/journal I/O, two-phase propose/complete, reconcile, action Enum + tests
3. `feat(loop-harness): guards + failure signatures` — evaluate_guards, combine_check_results, signature normalization + tests
4. `feat(loop-harness): lock/fencing + worktree manager` — acquire/reacquire/release/heartbeat/validate lease, worktree_manager, redaction + tests

## Acceptance

### Codex must run (before opening the PR)

- [ ] `AI_ORCHESTRA_DIR="$PWD" pytest -q` — full suite green (new + existing)
- [ ] `ruff check .` and `ruff format --check .` — clean
- [ ] Evaluation-set cross-check: every **must** EV in scope covered by a test. In-scope EVs: **EV-01–EV-21, EV-55, EV-56, EV-58, EV-61, EV-65–EV-68**, plus the loop_common-side foundations of **EV-71 (redaction)** and **EV-72 (audit payload shape)**. List the EV→test mapping in the PR body.
- [ ] `git diff origin/main --stat -- docs/` shows NO changes (frozen docs untouched)
- [ ] CHANGELOG Unreleased entry added

### Claude must verify after PR (do NOT self-certify these)

- Cross-review vs design docs (`/review` on Claude side)
- Hooks / agent-routing / audit emission live behavior (EV-62–64, EV-72 runtime side) — verified in later phases' E2E on the Claude Code runtime

## Git Rules

- Branch `feat/loop-harness-core` off latest `origin/main`. Commit freely on it.
- NEVER: push to `main`, force-push, merge the PR, or enable auto-merge. The human merges.
- Open the PR with `gh pr create` targeting `main`; put the EV→test mapping and any deviations in the body.

## Current Task State (from .claude/Plans.md)

- Phase 3 (this handoff): all tasks `cc:TODO` — flip to `cc:WIP`/`cc:done` in `.claude/Plans.md` as you progress.
- Later phases (4–7): out of scope; leave untouched.

## Design Decisions (context)

- 2026-07-06: Loop state uses dedicated state/journal as source of truth; audit gets `loop_*` events piggybacked (`EVENT_TYPES` additive only)
- 2026-07-06: Checker uses `failure_detector.analyze()` directly (deterministic), not quality-gates hooks
- 2026-07-06: Auto-pass criterion is Critical=0 AND High=0 across all reviewers
- 2026-07-07: Action vocabulary fixed in Phase 1 as Enum; Phases 2–3 import it
- 2026-07-07: No pyproject.toml registration; skill-evolution/codd-style package layout

## Instructions for Codex

You are implementing Phase 1 of the loop-harness package in a fresh session.
Read the documents in the Reading Order above (the core design volume is the
spec — do not improvise beyond it). Follow the Commit Plan checkpoints.
Complete every "Codex must run" acceptance item before opening the PR.
When done, update `.claude/Plans.md` markers for Phase 3 tasks and stop —
do not start Phase 2 (CLI).
