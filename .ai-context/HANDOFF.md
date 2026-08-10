---
schema: project-context/v1
updated_at: 2026-08-10T17:45:17+08:00
updated_by: Codex
branch: unavailable (workspace is not a Git repository)
head: unavailable
working_tree: shared workspace; Git metadata unavailable
---

# Current Handoff

## Current Goal

Maintain the continuously running Arena Hero agent, preserve reliable Worker
resource logistics and bounded combat, restore the `12/3/4` baseline, then
expand Worker-first to `16 Workers / 6 Vanguards / 8 Rangers = 30 Units` under
the official dynamic pricing rules.

Population 30 is a strategy production cap, not a game rule. The game has no
population ceiling and no per-Tick upkeep.

## Completed

- `arena_agent.py` uses `arena-hero==0.2.9`, dynamic SDK prices, bounded
  transport/API retries, persistent map memory, replay JSONL, and session
  statistics.
- Worker blocking includes obstacles, danger cells, every visible enemy Unit,
  and non-persistent TTL-96 enemy Core memory. Enemy Core moves, expiry, or
  destruction invalidate the remembered cell.
- Worker missions use cached complete routes with
  `ROUTE_MAX_EXPANSIONS = 4096`. Budget fallback and true unreachability have
  separate diagnostics. Cargo Recovery starts after 16 genuinely unreachable
  Ticks and never discards cargo.
- Unit capacity follows the official rule: one Unit per normal cell; a Core
  can share with one Unit. Occupied and reserved destinations are hard
  blockers. Distant Cargo continues approaching an occupied Core but waits at
  the final step until the Unit slot clears.
- Guard posts use deterministic complete Chebyshev rings and padded
  minimum-cost assignment. Raid assignment uses legal Vanguard/Ranger attack
  cells and dummy columns; a Unit may keep its own position but cannot take a
  position occupied by another Unit.
- Enemy Core attacks use dedicated TTL memory and the state flow
  `CORE_TARGET_MEMORY -> CORE_SCOUTING -> CORE_STAGING -> CORE_RAID ->
  CORE_RECALL -> COOLDOWN`. Observer handoff, same-cell hostile durability,
  protected-target recall, home-pressure recall, and damaged-member recall are
  implemented without adding Core to normal combat enemies.
- Automatic production is staged: `4/1/1` early defense, `12/3/4` baseline,
  then Worker-first expansion to `16/6/8`. Ordinary production reserves are
  respectively 0, 10, and 15 resources; baseline losses are repaired before
  expansion continues.
- Defaults and runtime configuration are now
  `MAX_POPULATION=30`, `WORKER_TARGET=16`, `VANGUARD_TARGET=6`, and
  `RANGER_TARGET=8`. CLI help, `.env.example`, and README are synchronized.
- `run_arena_agent.bat` remains the user's real startup path. Tests use only
  FakeActor in memory and do not connect to Arena API.
- `新建文件夹/` remains a separate static travel-product prototype with no
  Arena Hero integration.

## Evidence And Runtime Snapshot

- Pending handoff queue is empty.
- Shared-workspace verification passed:
  `\.venv\Scripts\python.exe -m unittest test_arena_agent.py` ran 110 tests
  with 0 failures, and `py_compile` passed for both Python files.
- The user restarted the formal Agent after implementation. At this handoff,
  project PID `23576` (`E:\GptChat\003\.venv\Scripts\python.exe`) and its
  runtime process PID `7528` were active, both started at 17:14:04 local time.
- Active session ID is `006fd152ede84091987766b9e1e115fb`.
- `arena_agent_stats.json` at Tick 84162 records 110 accepted Ticks, 0
  rejected Ticks, population peak 23, 16 Workers, 3 Vanguards, 4 Rangers, 4
  successful spawns, 9 harvests, 4 deposits, and 1700 successful Unit moves.
  No move-failure event is recorded in that session snapshot.
- Latest log observed while saving is Tick 84171: resources 24, population 23,
  roster `16/3/4`, next quote Vanguard cost 13. Expansion reserve is 15, so
  ordinary production waits until available resources reach 28.
- This directory is not a Git repository. Branch, HEAD, recent commits, and
  Git diff are unavailable as provenance.

## Accurate Breakpoint And Next Steps

1. Do not start another Agent while PID `23576` or its replacement is active.
   Process identity can change after restart; verify executable path before
   stopping anything.
2. Monitor the current session until resources reach 28 and confirm the next
   successful spawn is a Vanguard, moving the roster from `16/3/4` toward
   `16/6/4`; the third added Vanguard at population 25 needs cost 17 plus the
   15-resource reserve.
3. Continue monitoring new-session `CELL_UNIT_LIMIT`,
   `MOVE_DESTINATION_OCCUPIED`, `HARVEST_SUCCEEDED`, and
   `DEPOSIT_SUCCEEDED`. Analyze only the current `session_id`, not cumulative
   events from old trace rotations.
4. After Vanguards reach 6, Rangers are the remaining expansion target. At
   populations 26-29 each Ranger quote is 20 and ordinary production requires
   35 available resources.
5. Before future code edits, stop only the verified project Agent. Run
   FakeActor tests afterward and leave the formal Agent stopped unless the
   user explicitly restarts it.

## Blockers And Open Questions

- No code blocker is known.
- Resource income remains the pacing constraint, but the active session is
  progressing and has already produced four additional Workers.
- Runtime behavior after reaching the larger combat roster still needs normal
  observation; do not change production order or reserves solely because a
  production quote is displayed while resources are below cost plus reserve.
- Never print, commit, or copy the live `ARENA_HERO_API_KEY`. It belongs only
  in local environment configuration.

## Key Files

- `arena_agent.py`: strategy, route search, occupancy, Core raid, production,
  SDK compatibility, retries, persistence, and diagnostics.
- `test_arena_agent.py`: 110 FakeActor unit and behavior tests.
- `.env`: live local configuration and secret; never expose its contents.
- `.env.example`: non-secret 30-Unit defaults.
- `README.md`: startup, pricing, roster, occupancy, and strategy documentation.
- `run_arena_agent.bat`: user-facing formal Agent launcher.
- `arena_agent.log`, `arena_agent_trace.jsonl`, and
  `arena_agent_stats.json`: current runtime evidence.
- `arena_agent_state.json`: persistent safe map memory.

## Verification

```powershell
.\.venv\Scripts\python.exe -m py_compile arena_agent.py test_arena_agent.py
.\.venv\Scripts\python.exe -m unittest test_arena_agent.py
```

Expected shared-workspace result at this handoff: `Ran 110 tests ... OK`.
These commands do not connect to Arena API and must not start
`run_arena_agent.bat`.

## User Constraints To Preserve

- `run_arena_agent.bat` is the real user runtime; memory tests must exit and
  must not leave a second Agent running.
- Preserve baseline-first, Worker-first expansion:
  `4/1/1 -> 12/3/4 -> 16/6/8`, with reserves `0/10/15`.
- Preserve official current-population pricing. Thirty is only the chosen
  strategy cap.
- Preserve one Unit per normal cell and Core plus one Unit; no full-Core
  Cargo bypass.
- Keep enemy Core raids separate from normal defense and do not use blind
  Ranger fire.
- Keep Chinese-friendly output, persistent diagnostics, and project-local
  map memory.
- Do not expose the live API key in code, logs, handoffs, tests, or responses.
