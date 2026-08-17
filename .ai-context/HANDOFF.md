---
schema: project-context/v1
updated_at: 2026-08-17T17:41:34+08:00
updated_by: Codex
branch: main
head: c301cf6527e91559a55910207cadf1b1d18d7955
working_tree: business code clean; root context files updated by this save; no pending local handoff snapshots
---

# Current Handoff

## Current Goal

Repair the remaining live liveness failures in the Arena Hero Agent without
weakening the single-entrance half-duplex invariant:

1. Cargo owners outside the physical lane must receive a complete rolling
   right-of-way and must not cycle between equally blocked candidates.
2. EGRESS watchdogs must cause deterministic progress instead of repeatedly
   replanning the same blocked departure.
3. An injured Worker must stage safely, keep its healing reservation while
   waiting for the three-deposit quota, then reach Core and heal.
4. A remote Worker being pursued must use remembered active enemies and avoid
   local dead ends even when the pursuer disappears from vision for one Tick.

The nearby Vanguard that caused the Tick 116556 Core move was a real threat at
9-10 Manhattan cells. Preserve that pre-evade behavior; only add diagnostics.
The repair plan was confirmed with the user but has not been implemented.

## Repository State

- Repository: `E:\GptChat\003`, branch `main`.
- HEAD and `origin/main`: `c301cf6527e91559a55910207cadf1b1d18d7955`
  (`update: 治疗策略修改`).
- Business-code working tree was clean before this handoff save. This save
  changes only `.ai-context/PROJECT.md`, `.ai-context/HANDOFF.md`, and
  `.ai-context/DECISIONS.md`.
- There are no pending local handoff snapshots.
- `test_arena_agent.py` currently contains 205 tests.
- Do not expose or inspect the live token in `.env`.

## Completed And Committed

- `91d6f52` distance-gates Core threat escalation. A hostile Worker attack
  beyond 12 cells retains Worker-local danger memory without forcing
  `ENGAGED/GUARD`, clearing Cargo, or moving Core. Attacks within 8 cells are
  `ENGAGED`; attacks at 9-12 cells are `ALERT` unless pursuit/time-to-range
  logic legitimately raises pre-evade pressure.
- `071bd26` bounds Cargo owner admission and fairness. Normal owners must be
  within eight route steps, wait credit is capped at eight Ticks and accrues
  only to staged denied Cargo, nearest reachable fallback preserves liveness,
  and a valid queued owner survives EGRESS for direct handoff.
- `c301cf6` adds scout-return stall cleanup, dead-ID pruning, injured Worker
  return/healing registration, defender healing, healing intent and priority,
  a single physical `CoreVisit`, three-deposit healing quota, phase start
  timestamps, INBOUND/EGRESS watchdog diagnostics, spawn exclusion while the
  physical Core slot is occupied, and Core self-heal independence.
- The committed suite includes regressions for remote healing intent not
  blocking Cargo, physical Core-slot exclusivity, healing route blockers,
  defender admission, queued handoff, INBOUND/EGRESS watchdog behavior, and
  distant Worker attack isolation.
- Before `c301cf6` was committed, the shared workspace reportedly passed
  `py_compile`, all 205 tests, and `git diff --check`. These commands were not
  rerun during this handoff-only turn.
- Prior live evidence at Tick 110707 produced
  `UNIT_HEAL_SUCCEEDED` for a Ranger and Cargo deposited again at Tick 110714.
  This proves the defender path can work, not that Worker healing is correct.

## Live Evidence And Current Breakpoint

### Active session

- Session `555a6ee201b54ed38a5a893aef96939b` started at 10:04:33 local time on
  2026-08-17. The running project-venv Python process was PID 7828, paired with
  runtime Python PID 11936. Treat this as one launcher/runtime chain and do not
  start another Agent without rechecking current PIDs and executable paths.
- The process loaded the same `arena_agent.py` contents later recorded by
  `c301cf6`; committing at 16:29 did not change the already loaded source.
- `arena_agent_stats.json` at Tick 122981 recorded 1730 accepted and zero
  rejected Ticks, population 50, 110 deposits, 111 harvests, no healing and no
  cargo-drop death in this session. One `TransportError` at Tick 121288 retried
  and recovered; there is no current traceback or persistent API error.
- Latest audited trace snapshot was Tick 122993: resources 192, roster
  27 Workers / 10 Vanguards / 13 Rangers, 16 Workers carrying Cargo,
  `NORMAL/NONE`, mission `ECONOMY`.

### Current blocking fault

- At Tick 122993 the lane had remained `EGRESS` since Tick 122955. There had
  been no deposit for 38 Ticks.
- Departing Worker `626fe5a3` was at `(-199,660)` with egress target
  `(-202,660)`. Queued owner `26f10c9a` was valid at `(-200,660)`.
- `EGRESS_DEPARTING_STALLED` fired at Ticks 122973 and 122990. The current
  watchdog replanned but did not force a different outcome, so this is an
  active liveness defect and must be included in the next Cargo repair before
  relying on the earlier INBOUND-only plan.
- One yield Worker was present and no combat Unit yield was pending. Preserve
  the rule that a departing Unit must reach the open component before queued
  Cargo enters; fix the blocker/path execution rather than bypassing egress.

### Repeated INBOUND owner stalls

- In prior session `cf46247fbc7d4a748c85aeafe838f02c`, repeated
  `INBOUND_OWNER_STALLED` events produced a maximum 63-Tick deposit gap.
- The common stalled owners were `57606495` at `(-199,656)` and `9a2a356b` at
  `(-200,656)`, both reporting `CAPACITY_BLOCKED`. They could be selected again
  without moving.
- `_refresh_cargo_lane_occupants` currently publishes only the final four
  cells nearest the gateway:
  `owner_route.path[-(CARGO_LANE_GATEWAY_STEPS + 1):]`. Blockers between the
  owner and that suffix receive no yield assignment. The 16-Tick watchdog then
  rotates owners without correcting the missing right-of-way.

### Worker healing loop

- Worker `9b89b44c` remains at 1 HP. In the prior session it received healing
  intent at Ticks 116570, 116594, and 116626, then lost it at 116582, 116614,
  and 116639 without healing.
- The intent starts a 12-Tick no-Manhattan-progress timeout immediately, while
  healing priority may require three normal deposits, approximately 36 Ticks.
  The two policies are incompatible.
- While Cargo owns the lane, the Worker still routes toward Core but is denied
  healing access to lane reservations. It moved from distance 4 to about 10,
  was classified as making no progress, entered cooldown, and repeated.
- In the active session it was still 1 HP near Core and no
  `UNIT_HEAL_SUCCEEDED` had occurred by the audited snapshot.

### Remote Worker death

- Worker `656d3944` carried Cargo near `(-190,817)`, about 162 Chebyshev cells
  from Core. An enemy Vanguard was visible and pursued it from Tick 116101.
- At Tick 116106 the enemy disappeared from visibility for one Tick, although
  its ID remained in `active_enemy_ids`. Worker planning used only currently
  visible combat enemies, resumed ordinary return, and entered a local pocket.
- At Tick 116107 the Worker was at `(-190,817)` with obstacles north, east, and
  west and the Vanguard south. No legal escape remained; attacks at Ticks
  116108-116109 killed it and dropped its Cargo.
- Current escape scoring favors Core distance before enemy separation and does
  not score open-area survivability, which helped lead the Worker into the
  dead end.

### Valid Core move and separate unexplained debit

- Tick 116554 showed an enemy Vanguard at `(-188,657)` while Core was
  `(-196,655)`, then at `(-188,656)`. `PRE_EVADE/TIME_TO_RANGE` and the one-cell
  Core move at Tick 116556 were legitimate, not a distant-attack regression.
- A separate Tick 116569 resource change `184 -> 34` had no spawn, healing,
  population, or failure event. It remains unexplained and is not part of the
  confirmed movement-policy repair.

## Confirmed Repair Plan

1. Publish a rolling owner approach path from the owner's current cell for up
   to `CARGO_LANE_OWNER_ADMISSION_STEPS`, rather than only the gateway suffix.
   Workers, Vanguards, and Rangers on that path yield before the owner acts.
2. Record a stalled owner's position and cooldown. Do not immediately select
   it again while it remains on the same cell and another valid candidate
   exists. Clear the penalty after movement, successful lane entry, or deposit.
3. Extend EGRESS recovery: after a stalled replan, identify the concrete
   blocker and publish its yield; if the route and position repeat, choose a
   different reachable open-component endpoint. Never promote queued Cargo
   while the departing Unit remains in the physical corridor.
4. Split Worker healing into `STAGING -> ADMITTED -> CORE_VISIT`. Before
   admission, park at an explicit safe staging target and keep intent alive
   while the candidate remains valid. Count the three-deposit quota from valid
   staging. Start the no-progress timeout only after admission.
5. Once admitted, publish one complete healing approach path and suppress new
   Cargo ownership until the healer enters or admission times out. Use route
   cost/path index for progress. Create physical `core_visit` only on Core.
6. Feed visible combat enemies plus nearby remembered `active_enemy_ids` into
   Worker escape. Score projected damage first, enemy separation and local
   open-area survivability next, and Core distance last. Continue evasion
   across brief visibility gaps before resuming Cargo return.
7. Keep the 8/12 Core threat gates and valid time-to-range pre-evade behavior.
   Add trace fields for pressure enemy ID, distance, ETA, and selected Core
   movement direction.

## Required Regression Coverage

- Historical owner/blocker coordinates must deposit without reaching the
  16-Tick INBOUND watchdog.
- Two stationary failed owners must not alternate indefinitely.
- A repeated EGRESS route/endpoint must switch or clear its blocker while
  preserving complete departure before handoff.
- A staged Worker may wait longer than 36 Ticks without losing healing intent,
  then receives admission after at most three completed deposits and heals.
- Cargo right-of-way and admitted healing right-of-way are never active at the
  same time; physical `core_visit` never has two holders.
- The Tick 116101 pursuit fixture must keep escaping through a one-Tick vision
  gap and must not enter the three-sided pocket.
- A genuine Vanguard approach at 9-10 cells still permits pre-evade Core
  movement, while a distant Worker attack still leaves Core and Cargo stable.

## Implementation And Rollout Constraints

1. During code editing, do not disturb the currently running
   `run_arena_agent.bat` process; it continues using already loaded code.
2. Immediately before real verification, identify and stop only the unique
   project Agent process tree. Do not kill unrelated Python or `cmd.exe`
   processes.
3. Run:

```powershell
.\.venv\Scripts\python.exe -m py_compile arena_agent.py test_arena_agent.py
.\.venv\Scripts\python.exe -m unittest test_arena_agent.py
git diff --check
```

4. Restart exactly once through `run_arena_agent.bat` immediately after tests.
5. Observe at least 100 live Ticks. Require stable deposits, no repeated
   INBOUND/EGRESS watchdog on the same actor/position, a successful Worker
   heal, no double Core visit, and no regression in real near-threat evasion.
6. Do not modify unrelated user artifacts. Preserve secrets and do not print
   `.env`.

## Key Files

- `arena_agent.py`: all planning, Cargo, healing, escape, threat, persistence,
  diagnostics, and runtime logic.
- `test_arena_agent.py`: 205 committed tests and the location for new
  regressions.
- `README.md`: current runtime policy and CLI documentation.
- `run_arena_agent.bat`: the only formal live launcher.
- `arena_agent.log`, rotated logs, `arena_agent_trace.jsonl*`,
  `arena_agent_stats.json`, and `arena_agent_state.json`: runtime evidence.
- `.env`: live secret configuration; never expose it.

<!-- project-context:merge-manifest:start -->
## 追加交接汇总来源

- merge_id: `20260812T043850386Z-324f35ec`
- merged_at: `2026-08-12T04:43:14.347Z`
- sources:
  - `handoff-20260812T042348676Z-d14fda0a.md` (`5febccd2dd6a50ad705100e930efc4d9f45c4941cfdf337d55f4e734f813e41c`)
<!-- project-context:merge-manifest:end -->
