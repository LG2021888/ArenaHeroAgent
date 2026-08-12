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

## 2026-08-12 - Make Post-Target Population Expansion Optional And Resource-Gated

- **Decision:** Supersede the fixed 30-Unit strategy stop with an independent
  optional expansion switch. Keep `16/6/8` as configured targets, then grow
  toward the `8:3:4` ratio only when available resources reach
  `max(configured_floor, capacity - 5 * casualty_buffer_units)`. Use 150 as
  the current base floor, 6 Units as the casualty buffer, and
  `max_population=0` for no strategy hard cap.
- **Reason:** A fixed 30-Unit stop allowed resources to approach capacity and
  stop flowing. Resource-gated production spends surplus while preserving a
  150-resource operating base and enough capacity headroom to avoid additional
  overflow loss after a plausible six-Unit casualty event.
- **Rejected alternative:** Keep production permanently disabled at 30, grow
  on a percentage-only threshold, or add unlimited Workers without preserving
  the combat ratio.
- **Impact:** Configured losses are still repaired before optional growth.
  Expansion naturally slows as dynamic prices rise, can be disabled without
  changing roster targets, and still honors an explicitly configured finite
  hard cap.
- **Evidence:** Commit `cf67056`, `_population_expansion_threshold`,
  `_proportional_expansion_unit`, `_choose_spawn_unit`, CLI/environment
  configuration, README, expansion tests, and the live startup message
  `Population expansion=ON(threshold=150,casualty_buffer=6)`.

## 2026-08-12 - Separate Closed-Pocket Recovery From Single-Entrance Throughput

- **Decision:** Use two related but distinct Core-logistics mechanisms. A
  debounced bounded-component detector and local bridge handle a genuinely
  closed Core pocket. A half-duplex Cargo lane handles an open Core with only
  one usable entrance: evacuate accumulated empty Workers, admit one Cargo
  owner, deposit, require complete egress into an open dynamic component, then
  admit the next owner.
- **Reason:** Runtime geometry showed both failure modes. Tick 89475 had a
  closed nine-cell Core component with seven Cargo outside; Tick 93715 had an
  open component but only the right-side entrance, so inbound and outbound
  Workers blocked each other. Pocket size alone also misclassified normal
  delivery, requiring admitted-Cargo flow evidence and debounce.
- **Rejected alternative:** Increase A* budget, use one-step movement away
  from Core as egress, treat every small Core component as blocked, or allow
  the next owner in as soon as the old owner leaves the Core cell.
- **Impact:** Lane phases and reservations serialize startup evacuation,
  inbound delivery, deposit, and egress. Dead side pockets do not count as
  departure, staging Cargo cannot enter early, and active lane reservations
  cannot recursively trigger `CORE_POCKET_BLOCKED`.
- **Evidence:** Commit `0769085`, `CorePocketStatus`, `CargoLanePlan`,
  `_update_core_pocket`, `_update_cargo_lane`, Tick 89304/89475/93715
  regression fixtures, all 157 tests, and the live Tick 93977-93983
  deposit/egress/next-owner sequence.

## 2026-08-12 - Close The Combat Movement Feedback Loop

- **Decision:** Treat visible enemy-occupied cells as hard movement blockers,
  apply authoritative move-failure feedback to all Unit types, and cool a
  stationary-clear target after repeated failed approaches. Critically wounded
  Vanguards and Rangers attempt a strictly safer legal movement before normal
  attack or guard behavior and may return for healing when supported.
- **Reason:** A Vanguard previously submitted a failed move toward an
  enemy-occupied Core cell on every Tick for 284 Ticks because command
  submission was mistaken for movement success and Worker-only feedback did
  not teach combat Units. Separately, a low-HP Defender could attack again
  while standing in lethal projected fire.
- **Rejected alternative:** Rely on enemies eventually moving, raise path
  budgets, or attach retry cooldown only to local planning success.
- **Impact:** Server `UNIT_MOVE_FAILED` events temporarily block the actual
  failed destination for the actor, repeated stationary cleanup changes
  target, and critical retreat has priority only when it finds a lower-damage
  legal cell, preserving attacks when no safer move exists.
- **Evidence:** Commits `cf67056` and `0769085`, combat move-failure memory and
  stationary-clear cooldowns in `arena_agent.py`, historical enemy-Core
  occupancy tests, damaged-defender tests, and the full 157-test suite.
