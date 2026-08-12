---
schema: project-context/v1
updated_at: 2026-08-12T17:17:00+08:00
updated_by: Codex
branch: main
head: 91d6f52c582ee60716773e1f1e19b2cba88259ba
working_tree: business code committed; root context files updated after the commit; no pending local handoff snapshots
---

# Current Handoff

## Current Goal

Keep the continuously running Arena Hero Agent stable while validating the
distance-gated Core threat fix and the existing single-entrance half-duplex
Cargo lane under live conditions.

The reported fault is fixed and committed. A hostile attack against a Worker
45 cells from Core must retain that Worker's local escape memory without
changing the base to `ENGAGED/GUARD`, dismantling an inbound Cargo lane, or
moving Core. The next work is observation of the live Agent, not another
combat-policy redesign.

## Completed

- Commit `91d6f52` (`限制远端受击的基地威胁升级`) separates Worker-local attack
  memory from Core-level threat. All hostile attack positions remain available
  to Worker return and escape planning, but only a direct Core hit or an attack
  within 12 Manhattan cells refreshes Core recent-attack state.
- The recent-attack thresholds are now explicit: within 8 cells is `ENGAGED`,
  9-12 cells is `ALERT`, and beyond 12 cells does not escalate the Core.
- `_assess_threat` fills `nearest_distance` from the closest visible enemy or
  qualifying attack position, so close-hit emergency production uses the
  actual nearby pressure distance.
- Moving visible enemies outside 12 cells no longer create `ALERT/GUARD` or
  the eight-Tick production caution window merely because they moved.
  Confirmed pursuit and 16-Tick time-to-range preemption are preserved.
- `_planned_core_move` independently filters pressure to visible enemies
  within 12 cells or IDs already confirmed as pursuing/preemptive. A stale or
  malformed `ENGAGED` state therefore cannot make Core flee from an unrelated
  distant visible enemy.
- Mission state no longer becomes `GUARD` merely because any combat enemy is
  visible; it follows the threat assessment.
- README documents the 8/12-cell policy. Tests cover the historical far Worker
  attack with an active `INBOUND` lane, the 10-cell alert boundary, the 7-cell
  engaged/movement case, close-attack emergency production with only a distant
  visible enemy, distant non-pursuing movement, and retained caution/pursuit
  behavior.
- Historical Tick 94853 offline replay produced `NORMAL/NONE`, mission
  `ECONOMY`, no Core move, an active Cargo lane with owner `f0c09f3e`
  preserved, no Core recent-attack refresh, and retained Worker danger memory.
- Commits `29c1386` and `fd859e5` remain in the base. Congested Cargo egress
  can plan through temporary friendly occupancy to create yield assignments,
  must finish on an unoccupied open component, and replans when an endpoint
  becomes dynamically closed.

## Evidence And Runtime Snapshot

- Repository is on `main` at
  `91d6f52c582ee60716773e1f1e19b2cba88259ba`, one local commit ahead of
  `origin/main` (`fd859e5`). The business-code commit has not been pushed.
- Shared-workspace verification on 2026-08-12 passed:
  `py_compile` for `arena_agent.py` and `test_arena_agent.py`,
  `Ran 164 tests ... OK`, and `git diff HEAD --check` with no errors.
  Unit tests do not connect to Arena API.
- The old 16:48 Agent process group was stopped exactly and replaced once so
  the live runtime loads `91d6f52`. The current group started at 17:11:42:
  `cmd.exe` PID 22680, project-venv Python PID 31048, and runtime Python PID
  24956. The two Python processes are one launcher/runtime chain, not two
  Agents.
- Active session is `f2989b94c8114fa0b03e6c13bfe454c6`. It accepted Ticks
  95165 onward without duplicate-agent, idempotency, or command-rejection
  errors.
- Live Cargo handoff completed after the restart: owner `dd996335` stayed
  `INBOUND` through Tick 95194, queued a one-resource deposit at Tick 95195,
  resources changed `75 -> 76` at Tick 95196, and the same owner remained in
  `EGRESS` through Tick 95202. Only at Tick 95203 did the lane admit the next
  owner `695266bb`. Throughout this sequence the Core stayed at `(-217,668)`,
  threat was `NORMAL`, and mission was `ECONOMY`. Persistent
  `exploration_anchor` is also `(-217,668)`.
- Current roster is `16 Workers / 7 Vanguards / 9 Rangers = 32`. Production
  below the configured post-target threshold of 150 resources is expected to
  remain off.
- The live session has not yet produced another far Worker attack while an
  inbound lane is active. The exact regression and historical offline replay
  prove the code path; a new live incident remains observation evidence, not a
  prerequisite for considering the defect fixed.

## Accurate Breakpoint And Next Steps

1. Do not start a second Agent while the verified 17:11:42 process group is
   active. Recheck executable paths and start times before any restart.
2. Follow the current owner `695266bb` through its delivery cycle. The previous
   `dd996335` handoff already proved complete `INBOUND -> DEPOSIT -> EGRESS ->
   next owner` ordering after the restart.
3. On the next far `UNIT_DAMAGED` event, correlate the attack position with
   Core distance. Beyond 12 cells, expect `NORMAL/NONE` unless another genuine
   near/pursuit threat exists; the lane owner and Core position must remain
   stable while the affected Worker keeps local danger/return behavior.
4. For attacks at 9-12 cells, expect `ALERT/GUARD` without Core movement or
   Cargo-lane clearing. Within 8 cells, expect existing `ENGAGED` defense,
   possible Core movement when a qualifying pressure enemy is visible, and
   the eight-Tick post-threat production caution.
5. Watch `UNIT_MOVE_FAILED` per combat actor and the next critically wounded
   Defender event; these mechanisms remain unit-tested but need additional
   live evidence.
6. Measure half-duplex throughput only under a real backlog of at least four
   Cargo. One physical entrance remains serial by design.
7. Before future business-code edits, stop only the verified project Agent,
   then rerun `py_compile`, all 164+ tests, and `git diff --check`; restart
   exactly once.

## Blockers And Open Questions

- No code blocker is known.
- The far-attack regression is fixed, committed, replayed offline, and loaded
  by the live Agent. A fresh live far-attack event has not yet occurred.
- The Core already moved from the original `(-217,666)` to `(-217,668)` before
  this fix. The repair prevents future false moves; it intentionally does not
  move Core back automatically. The exploration anchor has correctly followed
  the current Core to `(-217,668)`.
- Critical Defender retreat and combat move-failure feedback still have no new
  live-event evidence in the current session.
- The genuine r=8 closed-pocket bridge and half-duplex throughput under a
  four-Cargo backlog remain unmeasured live cases.
- Never print, commit, or copy the live `ARENA_HERO_API_KEY`.

## Key Files

- `arena_agent.py`: threat gating, Worker danger memory, Cargo lanes, routing,
  combat, production, retries, and diagnostics.
- `test_arena_agent.py`: 164 in-memory behavior and regression tests.
- `README.md`: documented 8/12-cell Core threat policy and runtime behavior.
- `.env`: live secret configuration; never expose its contents.
- `run_arena_agent.bat`: the only formal runtime launcher.
- `arena_agent.log`, `arena_agent_trace.jsonl`, `arena_agent_stats.json`, and
  `arena_agent_state.json`: current runtime evidence and persistent safe map
  memory.

## Verification

```powershell
.\.venv\Scripts\python.exe -m py_compile arena_agent.py test_arena_agent.py
.\.venv\Scripts\python.exe -m unittest test_arena_agent.py
git diff --check
```

Expected shared-workspace result at this handoff: `Ran 164 tests ... OK`.
These commands must not connect to Arena API or start another Agent.

## User Constraints To Preserve

- `run_arena_agent.bat` is the real runtime. Tests must not connect to Arena
  API or leave a second Agent running.
- Keep runtime population expansion enabled unless the user asks to disable
  it. Preserve the 150 base threshold, six-Unit casualty buffer, and `8:3:4`
  proportional growth after configured targets.
- Preserve baseline-first production and official current-population pricing.
- Preserve one Unit per normal cell and Core plus one Unit; no full-Core Cargo
  bypass.
- Preserve half-duplex single-entrance ordering and complete open-component
  egress before admitting the next Cargo owner.
- Keep Worker-local danger memory for far attacks while enforcing the Core
  threat gates at 8 and 12 Manhattan cells.
- Keep enemy Core raids separate from normal defense and do not use blind
  Ranger fire.
- Treat user-reported manual Core/Ranger movement and manual resource spending
  as authoritative context when interpreting logs.
- Keep Chinese-friendly output, persistent diagnostics, and project-local map
  memory.
- Do not expose the live API key in code, logs, handoffs, tests, or responses.

<!-- project-context:merge-manifest:start -->
## 追加交接汇总来源

- merge_id: `20260812T043850386Z-324f35ec`
- merged_at: `2026-08-12T04:43:14.347Z`
- sources:
  - `handoff-20260812T042348676Z-d14fda0a.md` (`5febccd2dd6a50ad705100e930efc4d9f45c4941cfdf337d55f4e734f813e41c`)
<!-- project-context:merge-manifest:end -->
