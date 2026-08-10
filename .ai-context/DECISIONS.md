# Decision Log

Only decisions with direct repository or runtime evidence are recorded here.

## 2026-08-06 - Pin The Current SDK And Keep A Narrow Compatibility Shim

- **Decision:** Use `arena-hero==0.2.9` as the expected SDK and retain a
  compatibility parser only for the older payload that omitted known state
  fields.
- **Reason:** The server previously produced messages missing
  `population_tier` and `upkeep_next_tick`; broad protocol bypasses would hide
  real schema failures.
- **Rejected alternative:** Accept arbitrary invalid WebSocket messages or
  silently downgrade all protocol validation.
- **Impact:** Current protocol errors remain visible and fail safely, while
  the known legacy state shape can still be read.
- **Evidence:** `requirements.txt`, `arena_agent.py`, and the compatibility
  tests in `test_arena_agent.py`.

## 2026-08-06 - Use Dynamic Prices And A 12/3/4 Roster Target

- **Decision:** Keep `max_population=19` as the roster ceiling and use the
  SDK's population-aware unit cost helper; automatic production targets 12
  Workers, 3 Vanguards, and 4 Rangers.
- **Reason:** The current game rules use dynamic production prices rather than
  the old fixed maintenance interpretation.
- **Rejected alternative:** Treat 19 as a maintenance-cost limit or keep
  producing only Workers.
- **Impact:** Production must wait for authoritative resources and may remain
  below the roster while prices cannot be paid; the first defense screen is
  prioritized before the remaining Workers.
- **Evidence:** `arena_agent.py`, `README.md`, and the unit-cost/roster tests.

## 2026-08-07 - Separate Physical Scout Progress From Route-Cost Progress

- **Decision:** Keep a scout target for four Ticks without physical movement,
  while using ten Ticks without route-cost improvement only as a slower
  fallback; an unreachable route changes immediately.
- **Reason:** The earlier three-Tick path-cost-only rule abandoned targets when
  obstacle detours temporarily increased estimated cost. A single 8-12 Tick
  no-movement threshold would instead make a genuinely blocked Worker idle too
  long.
- **Rejected alternative:** Count every non-decreasing path cost as a full
  stall or simply raise the one threshold to 8-12 Ticks.
- **Impact:** Physical detours keep their target, while a stationary Worker is
  released quickly enough to explore another sector.
- **Evidence:** `ScoutProgress`, `TacticMemory.note_scout_progress`, the scout
  tests, and the earlier runtime trace showing route churn near obstacles.

## 2026-08-07 - Keep Units Away From A Beacon In RETREAT Mode

- **Decision:** Pass a ground Beacon to unit planners only under `HOLD`; under
  `RETREAT`, only Core retreat logic sees the Beacon.
- **Reason:** Otherwise Core could move away while Workers or defenders moved
  back toward the same Beacon.
- **Rejected alternative:** Let all units pursue every visible ground Beacon
  regardless of the Core policy.
- **Impact:** `RETREAT` avoids pulling the roster toward a hazardous Beacon;
  `HOLD` retains explicit pickup behavior.
- **Evidence:** `plan_turn`, Beacon policy tests, and `README.md`.

## 2026-08-07 - Keep Runtime Artifacts Separate By Purpose

- **Decision:** Store safe map memory in one JSON file, aggregate session
  metrics in another JSON file, and per-Tick replay records in JSONL.
- **Reason:** Persistent state must be compact and restart-safe, while trace
  data is append-oriented and stats should be easy to consume as one object.
- **Impact:** `arena_agent_state.json`, `arena_agent_stats.json`, and
  `arena_agent_trace.jsonl` can be inspected independently without mixing
  tactical memory with session history.
- **Evidence:** `TacticMemory`, `SessionRecorder`, the artifact tests, and the
  files produced by the active session.

## 2026-08-10 - Use Complete Worker Routes And Single-Unit Cell Capacity

- **Decision:** Cargo and other Worker missions use cached complete routes
  with a 4096-expansion budget. Visible enemy Units and TTL-96 enemy Core
  memory block routes. Normal cells accept one Unit; a Core may share with one
  Unit, and an occupied Core can be approached but not entered by a second
  Unit.
- **Reason:** Runtime evidence showed repeated collisions with enemy Core and
  Worker cells plus two-cell oscillation in concave obstacles. Official rules
  confirm that the earlier assumed two-Unit normal-cell capacity was wrong.
- **Rejected alternative:** Persist enemy Core cells as permanent obstacles,
  keep single-step lookahead for Cargo, or bypass a full Core destination.
- **Impact:** Route cache invalidates on real blockers, transient Core queues
  do not trigger Cargo Recovery, and guard/raid matrices allow a Unit to keep
  only its own occupied position.
- **Evidence:** Worker route, occupancy, Cargo Recovery, guard, and raid tests
  in `test_arena_agent.py`; current-session stats contain no move-failure
  events at this handoff.

## 2026-08-10 - Keep Enemy Core Raids Separate From Normal Defense

- **Decision:** Use non-persistent TTL-96 enemy Core memory and the dedicated
  `CORE_TARGET_MEMORY -> CORE_SCOUTING -> CORE_STAGING -> CORE_RAID ->
  CORE_RECALL` flow. Raid members require reachable legal attack positions;
  enemy Cores are passed as a separate raid target rather than normal combat
  enemies.
- **Reason:** Normal defense must not retreat from or blindly chase a Core,
  while a long-range Core attack needs observer handoff, formation checks,
  progress tracking, and immediate recall on protection or home pressure.
- **Rejected alternative:** Treat enemy Core as a Vanguard/Ranger target or
  use blind Ranger fire against remembered cells.
- **Impact:** A raid only launches with at least two assigned Vanguards and two
  assigned Rangers, preserves home defenders, counts same-cell hostile HP as
  target durability, and recalls on defined safety failures.
- **Evidence:** `CoreRaidPlan`, observer/assignment helpers, and Core raid
  behavior tests in `test_arena_agent.py`.

## 2026-08-10 - Expand From The 19-Unit Baseline To A 30-Unit Strategy Cap

- **Decision:** Keep `12/3/4` as the baseline and expand Worker-first to
  `16/6/8`, with a strategy cap of 30. Production reserves are 0 before early
  defense, 10 while restoring the baseline, and 15 during expansion.
- **Reason:** The official rules have no population cap or per-Tick upkeep.
  Earlier Workers improve resource throughput enough to prefer `W -> V -> R`
  expansion despite its 149-resource total cost.
- **Rejected alternative:** Keep 19 as the final strategy ceiling, use
  unlimited production, or choose `R -> V -> W` solely to save 14 production
  resources.
- **Impact:** Baseline losses are repaired before expansion resumes. The
  current-population SDK price table remains authoritative, and the strategy
  stops ordinary production at 30 even though game prices continue upward.
- **Evidence:** Defaults and production logic in `arena_agent.py`, runtime
  configuration, README, and expansion/reserve/price tests.
