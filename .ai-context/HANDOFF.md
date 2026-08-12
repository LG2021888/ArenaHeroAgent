---
schema: project-context/v1
updated_at: 2026-08-12T12:41:47+08:00
updated_by: Codex
branch: main
head: 076908544c427310deae144a49df582cc10d7e74
working_tree: shared workspace; business code clean; root context files are updated and the fixed local snapshot is merged and archived
---

# Current Handoff

## Current Goal

Keep the continuously running Arena Hero agent stable while validating the
completed population-expansion, combat-feedback, Core-pocket bridge, and
single-entrance half-duplex Cargo-lane changes under live conditions.

The implementation is committed. The next work is runtime observation rather
than another redesign: confirm deposits keep flowing through the one usable
entrance, critically wounded defenders retreat on a real damage event, and
combat Units no longer repeat failed moves into enemy-occupied cells.

## Completed

- Commit `cf67056` (`人口扩容和逻辑优化`) added optional post-target
  expansion. Automatic production still restores
  `4/1/1 -> 12/3/4 -> 16/6/8` first, then can grow proportionally toward
  `Worker:Vanguard:Ranger = 8:3:4`.
- Runtime expansion is enabled with no strategy hard cap, a base resource
  threshold of 150, and a six-Unit casualty-capacity buffer. The startup log
  records `Population expansion=ON(threshold=150,casualty_buffer=6)`.
- `CORE_RESOURCES_CAPTURED` no longer starts friendly recovery. It can still
  invalidate captured enemy-Core memory; only `CORE_LOST` or destruction of
  the friendly Core starts the recovery window.
- Cargo-return danger memory and escape scoring protect exposed Workers.
  Equal projected-damage escape choices first reduce Manhattan distance and
  then dominant-axis (Chebyshev) separation from the friendly Core, avoiding
  the historical WEST/DOWN tie that selected the wrong escape direction.
- Critical Vanguards (HP <= 2) and Rangers (HP <= 1) attempt an immediately
  safer legal step before normal combat and can enter Core-healing flow when
  resources and backup permit.
- Enemy-occupied destinations are hard combat-movement blockers.
  `UNIT_MOVE_FAILED` feedback applies to all Unit types, temporarily avoids
  contested/occupied destinations, and cools repeatedly failing stationary
  targets. This addresses the historical 284-Tick Vanguard failure loop.
- Bounded reverse flood-fill detects genuine closed Core pockets using an
  exhausted local component, admitted-Cargo flow evidence, externally
  statically reachable Cargo, stable Core/Cargo continuity, and two-Tick
  debounce. An admitted Cargo is one already on Core or adjacent to the Core
  component, not one whose occupied cell belongs to the component. A local
  weighted bridge and staged single owner handle closure.
- Commit `0769085` (`单口半双工交接修复`) added single-open-entrance control.
  `CargoLanePlan` now tracks `STARTUP_EVACUATION`, `INBOUND`, `DEPOSIT`, and
  `EGRESS`, a departing Worker, a complete egress path/target, startup
  evacuation IDs, and staging assignments.
- Startup evacuation serializes empty Workers already accumulated near Core.
  One Cargo owner enters; after deposit it must leave the full lane and reach
  an open dynamic component before the next owner is admitted. A dead side
  pocket does not count as completed egress.
- Diagnostics expose `CARGO_LANE_EGRESS` and `CARGO_LANE_YIELD`. Active lane
  control disables the older `core_lane_*` controller, reserves the full lane
  and egress path against Cargo/guards/staging traffic, and does not recursively
  create a `CORE_POCKET_BLOCKED` diagnosis.
- Core movement, inability to accept delivery, or threat states
  `PRE_EVADE` / `ENGAGED` / `BREAKOUT` invalidate or suspend lane use.

## Evidence And Runtime Snapshot

- Repository is on `main` at
  `076908544c427310deae144a49df582cc10d7e74`; `origin/main` is identical.
- Business code is clean. The working tree changes are the root context-file
  updates plus untracked `.ai-context/handoffs/`, which is local handoff-queue
  data and must not be deleted or committed by default.
- This handoff merges and archives snapshot
  `handoff-20260812T042348676Z-d14fda0a.md` (SHA-256
  `5febccd2dd6a50ad705100e930efc4d9f45c4941cfdf337d55f4e734f813e41c`).
  The source window performed read-only runtime review and made no business
  code change or service restart.
- Shared-workspace verification on 2026-08-12 passed:
  `py_compile` for both Python files, `Ran 157 tests ... OK`, and
  `git diff --check` with no errors.
- Historical regression fixtures cover Tick 89304 normal admitted delivery,
  Tick 89475's exact nine-cell closed pocket with seven external Cargo, and
  Tick 93715's single right-side entrance with seven nearby empty Workers.
- `run_arena_agent.bat` remains one process group started at
  2026-08-12 12:01:56 local time: `cmd.exe` PID 21560, project-venv Python PID
  28400, and Python runtime PID 11332.
- Active session is `1f5d354d98524ea8a5897f3566a0d249`; live roster is
  `17 Workers / 7 Vanguards / 9 Rangers = 33`.
- Stats through Tick 94124 recorded 150/150 accepted Ticks, 0 rejected,
  5 deposits, 9 harvests, 1922 successful Unit moves, and no recorded
  move-failure, damage, or death event. Tick 94124 was accepted with resources
  42, no threat, and no production.
- `production=NONE` at resources 42 is expected. At population 33 the
  post-target gate is `max(150, capacity 165 - 30) = 150`; production remains
  off until available resources reach that gate and normal price/reserve
  checks pass.
- The earlier 95-resource reduction was user-initiated manual spending and is
  not an unexplained strategy loss.
- Proven live handoff: Tick 93977 deposited (`37 -> 38`), owner `a975bdda`
  remained in `CARGO_LANE_EGRESS` through Tick 93982, and Tick 93983 admitted
  owner `6553c3ed` only after the old owner reached an open component. The
  window had zero `UNIT_MOVE_FAILED` events.
- Source-snapshot live evidence records a real startup evacuation beginning at
  Tick 93961: four nearby empty Workers were serialized, departing owner moved
  from `9a2a356b` to `6e8fe8f1`, and the lane reached `INBOUND` about ten Ticks
  later. Those earliest Ticks have since rotated out of the current trace, so
  this particular claim retains source-snapshot provenance.
- The same source replay measured the `CORE_POCKET` filter chain over 361
  Ticks: raw geometric closure 113 Ticks, no admitted Cargo 39, external static
  reachability 39, debounced activation 8 Ticks across 5 events, with zero
  overlap with deposit Ticks. This supports the admitted-Cargo suppression and
  debounce already covered by current code and regression tests.
- Current live lane at Tick 94124 is `INBOUND` with owner `9a2a356b`, no queued
  owner, and gateway `(-215,668)`.
- Tick 94030 had transient command-submission transport failures. The bounded
  retry/reconnect loop recovered without process replacement; Tick 94035 and
  all observed Ticks through 94081 were accepted.

## Accurate Breakpoint And Next Steps

1. Do not start a second Agent while the verified process group above, or its
   replacement, is active. Recheck executable paths and start times before
   stopping or restarting anything.
2. Continue observing the current owner `9a2a356b`. Verify
   `INBOUND -> DEPOSIT -> EGRESS`, full departure into an open component, and
   only then admission of any queued owner.
3. Watch for the first genuine closed-pocket bridge execution. The detector
   and debounce are live, but the source review found no confirmed real run of
   the r=8 weighted bridge. `CORE_POCKET_BLOCKED` must not accumulate merely
   because the active lane reserves cells.
4. On the next Defender `UNIT_DAMAGED` event, correlate the next action,
   projected danger, later `SHOT_HIT`, and healing. Unit tests cover critical
   retreat, but this restarted session has not yet produced damage evidence.
5. Watch `UNIT_MOVE_FAILED` per actor. A combat Unit must not accumulate tens
   or hundreds of retries at one enemy-occupied destination; validate the
   avoid window and stationary-target cooldown when a real failure occurs.
6. Validate staging-target reachability before changing the controller. The
   source snapshot flagged Tick 93990, where `static_reachable_cargo_ids` was
   empty while `c3ca0560` had stage target `(-220,668)`; current stage-target
   selection ranks estimated path cost but does not explicitly reject the
   unreachable sentinel.
7. Measure lane throughput only in a real backlog of at least four Cargo.
   Earlier 18-Tick deposit spacing was supply-limited, not enough to assess
   the half-duplex controller's maximum throughput.
8. Do not classify `production=NONE` below 150 resources as a fault. At the
   current `17/7/9` ratio the next proportional expansion choice is expected
   to be a Worker once the resource gate opens.
9. Before future code edits, stop only the verified project Agent if runtime
   mutation is necessary. Re-run `py_compile`, all 157+ tests, and
   `git diff --check`; restart only when requested.

## Blockers And Open Questions

- No code blocker is known.
- Live validation has proven one complete post-deposit egress/handoff cycle,
  and the merged source snapshot records a real four-Worker startup evacuation.
- Critical Defender retreat and combat move-failure feedback have regression
  tests but no new live-event evidence in the restarted session.
- The genuine r=8 closed-pocket bridge has not yet been observed executing in
  live play.
- Staging-target reachability at Tick 93990 remains a review finding, not a
  confirmed defect; reproduce it before editing policy.
- Half-duplex throughput under a backlog of at least four Cargo remains
  unmeasured.
- One physical entrance remains throughput-limited. The controller guarantees
  ordering and eventual evacuation, not simultaneous inbound/outbound flow.
- Never print, commit, or copy the live `ARENA_HERO_API_KEY`.

## Key Files

- `arena_agent.py`: strategy, Cargo lanes, route search, occupancy, combat,
  Core raid, production, SDK compatibility, retries, and diagnostics.
- `test_arena_agent.py`: 157 in-memory behavior and regression tests.
- `.env`: live local configuration and secret; never expose its contents.
- `.env.example`: non-secret defaults (`MAX_POPULATION=0`, expansion default
  OFF, threshold 150, casualty buffer 6).
- `README.md`: startup, dynamic pricing, expansion, roster, and strategy.
- `run_arena_agent.bat`: user-facing formal Agent launcher.
- `arena_agent.log`, `arena_agent_trace.jsonl`, and
  `arena_agent_stats.json`: current runtime evidence.
- `arena_agent_state.json`: persistent safe map memory.

## Verification

```powershell
.\.venv\Scripts\python.exe -m py_compile arena_agent.py test_arena_agent.py
.\.venv\Scripts\python.exe -m unittest test_arena_agent.py
git diff --check
```

Expected shared-workspace result at this handoff: `Ran 157 tests ... OK`.
These commands do not connect to Arena API and must not start
`run_arena_agent.bat`.

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
