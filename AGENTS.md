# AGENTS.md - driver-core

Guidance for automated reviewers (Codex and any other agent) and for humans. This file names the
invariants that must not be broken and the fences that must not be crossed. If a change looks
correct but violates one of these, it is not correct.

## What this package is

A loopback REST service plus CLI that observes a Windows desktop through ordered sources, has
independent extractors agree on what they saw, and only then asks a decision model whether to act.
It executes nothing without a consent bound to one exact `(action, params)` pair.

## Invariants (breaking one is a bug even if every test passes)

1. **An unanswered slot is never a vote.** `consensus.tally` reports a shortfall (`INSUFFICIENT`)
   when fewer than `quorum` slots answer. Never convert a shortfall into agreement, never count a
   malformed answer as a dissent, and never report "1 of 3 agreed" as unanimity.
2. **No unverified state reaches the decision tier.** The gate order is capture -> extract ->
   agreement -> decision -> guardrails -> consent -> execute. A change that lets a state through
   the agreement gate unvalidated breaks the only guarantee this package exists to provide.
3. **The tally owns the verdict.** A model may describe or explain a tally; it may not overrule it.
4. **Provenance: parameters come from the caller, never from the model.** A model naming an action
   is safe because the vocabulary is closed. A model supplying a path, URL or parameter is the exact
   failure this design prevents.
5. **Consent is one exact pair.** There is no blanket write permission; a consent is a capability
   for one `(action, params)` pair, and it is echoed in resolved form before acting.
6. **A refusal with a known cause says the cause.** A stop reason that discards a reason it already
   computed - "0 of 2 slots answered" when it knows the slot had no key - is a defect. Every
   refusal names what was configured and what was missing.
7. **The audit chain is append-only and byte-reproducible.** `test_audit_compat` requires the same
   run to write identical bytes against a frozen golden: no timings, timestamps or PIDs in records.
   Anything non-reproducible belongs in a sidecar trace, never in the chain.
8. **Refusals are results, not exceptions**, unless the caller must branch on the type: a budget
   refusal is raised (`BudgetRefused`) precisely so a spend stop cannot be read as a bad capture.
9. **Settings are frozen.** Policy is resolved once and never mutated downstream; derive a new
   `Settings` rather than editing one.

## Fences (do not cross, in code or in a session)

- **Nothing builds locally.** Verification happens on the CircleCI rail at the exact commit head.
  Editing, git and read-only inspection are local; a local release-check or test battery that
  compiles or bundles is not. A *targeted* test run plus a runtime transcript is acceptable for a
  directly observable defect, disclosed as targeted and never as the green-bar battery. If no rail
  exists for a check, file the gap once; do not substitute a local build.
- **Port 8765 is never stopped or freed.** It belongs to the Harness UI (`harness.cli serve`), by
  owner decision. The driver service takes its own port (currently 8787). A service run is a run,
  not a build.
- **No production anything.** No deploys, no pushes to a remote beyond the lane's own branch and PR,
  no force-push, no rewriting published history.
- **Keys are presence-only.** Never commit, print, log or file a credential or a prefix of one.
  `/health` renders `settings.redacted()`, which omits both credentials by design.
- **Never stop a process you did not identify and file first.**

## Where things live

| area | module |
|---|---|
| agreement arithmetic | `driver_core/consensus.py` (pure, no network/clock/filesystem) |
| gate policy | `driver_core/policy.py` |
| pipeline | `driver_core/driver.py` |
| capture sources | `driver_core/adapters.py`, `driver_core/chain.py` |
| extraction pools | `driver_core/extractors/pool.py`, `.../vision.py` |
| OS surface | `driver_core/osal.py` |
| HTTP service | `driver_core/server.py` |
| CLI | `driver_core/cli.py` |

## Review expectations

- Findings are welcome at any priority and are worked, not deferred: fixed at cause, or rebutted
  with evidence on the PR.
- Prefer a test that fails before the change and passes after. A one-line fix with no test is
  acceptable only where the behaviour is observable directly and the transcript is filed.
- Numbers in a description must come from a command that was run; say which command.
- If you find the same defect shape in more than one place, name all of them - a fix that cures one
  instance of a discarded cause leaves the next one to be found by a user.
