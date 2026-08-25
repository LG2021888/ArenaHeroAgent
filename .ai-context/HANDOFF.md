---
schema: project-context/v1
updated_at: 2026-08-24T18:35:00+08:00
updated_by: Codex
branch: main
head: ec63567
working_tree: business code clean; context files updated by this save; no pending local handoff snapshots
---

# Current Handoff

## Current Goal

Keep the Arena Hero Agent live-stable while preserving the single-entrance
Cargo half-duplex invariant. The current focus is single-entry Cargo
throughput, low-health Worker/Defender healing latency, and classification of
物流、治疗、网络与运行时错误.

## Repository State

- Repository: `E:\GptChat\003`, branch `main`.
- HEAD and `origin/main`: `ec63567` (`修复工人治疗等待与货运通道协调`).
- Business-code working tree is clean after the commit; this save changes only
  project-context files.
- `test_arena_agent.py` contains 219 tests.
- `.env` remains private and was not read or copied into this handoff.
- No pending local handoff snapshots.

## Completed And Verified

- Console and persistent diagnostics include Worker/Vanguard/Ranger counts,
  HP and low-health counts, attack/defense counts, Cargo phase/yield/watchdog,
  healing priority/admission/wait, Cargo throughput, deposit gaps, healing
  events, and runtime/network error categories.
- Cargo uses one physical Core visitor and a half-duplex single-entry lane:
  startup evacuation, one inbound owner, deposit, complete egress to an open
  component, then the next owner. Queued owners survive EGRESS handoff and
  stalled owners receive bounded cooldown/fairness treatment.
- Healing uses intent plus physical `core_visit`; remote intent cannot block a
  Cargo owner already on Core. Injured Workers and combat Defenders are
  tracked separately.
- Commit `ec63567` adds a Worker healing staging target outside active Cargo
  path/egress/stage cells. A remote low-health Worker moves to the safe outer
  ring, waits while Cargo owns the lane, and only then uses the normal HEAL
  route. The 12-Tick intent timeout is suspended while a living empty Worker
  is waiting for an active Cargo lane, so staging does not erase its intent.
- Regression coverage includes staging exclusion, intent survival during
  Cargo wait, physical Core-slot exclusivity, Cargo handoff/watchdog behavior,
  defender healing, and distant-threat isolation.

## Verification

- `py_compile`: passed.
- Full suite: `219/219 OK`.
- `git diff --check`: passed before commit.
- Live session after the final restart: `5760788568524aec9a31a06802dd6169`.
- Audited live trace reached Tick `161593`; the session had 40 accepted Ticks
  at the latest persisted stats snapshot, 2 deposits, maximum deposit gap 13
  Ticks, no Cargo watchdog, and no runtime/network errors.
- Worker `8905b634-67a2-4ad1-ab19-0502ca66fa89` remained at 1 HP with a live
  healing intent and `HEAL_RETURN` while Cargo completed EGRESS/INBOUND
  handoffs. No duplicate physical Core visitor was observed.
- A single startup reconnect produced expected `COMMAND_WINDOW_CLOSED` 409
  retries; it recovered and did not recur in the audited post-restart window.

## Open Observation

- The staging/timeout repair is confirmed by trace state and tests, but the
  audited post-restart window has not yet produced a new `UNIT_HEAL_SUCCEEDED`
  for Worker `8905…`. Continue observing until the Worker reaches Core and
  heals, or until a repeatable admission block is identified.
- Keep checking: EGRESS completion before next INBOUND, repeated
  `UNIT_MOVE_FAILED` coordinates, Cargo watchdog recurrence, low-health Ranger
  or Vanguard treatment, and one-slot `core_visit` invariance.
- Do not manually restart the current BAT unless code changes require a
  controlled rollout. Before any restart, identify only the unique project
  chain (`run_arena_agent.bat` -> project `.venv` Python -> runtime Python).

## Next Steps

1. Observe at least another 100 accepted live Ticks where practical; record
   deposits, maximum gap, healing success, watchdogs, and error categories.
2. If Worker healing still does not occur, inspect the exact staging-to-Core
   handoff and route blockers before changing Cargo ownership rules.
3. Preserve the 8/12-cell Core threat gates and valid near-threat pre-evade
   behavior while auditing any new movement changes.

## Key Files

- `arena_agent.py`: planning, Cargo lane, healing, escape, diagnostics,
  persistence, and runtime logic.
- `test_arena_agent.py`: unit and regression suite.
- `run_arena_agent.bat`: formal live launcher.
- `arena_agent.log`, `arena_agent_trace.jsonl*`, `arena_agent_stats.json`, and
  `arena_agent_state.json`: runtime evidence.
