# Project Context

## Project Identity

- Name: Arena Hero Agent workspace.
- Purpose: run a persistent Arena Hero game agent that explores, collects and
  deposits resources, grows a defensive roster, handles combat, and records
  enough state to diagnose long sessions.
- Repository status: this directory is not a Git repository. Branch, HEAD,
  commit history, and Git diff are unavailable.
- Primary project scope: the Python Arena Hero agent in the project root.
- Adjacent scope: `新建文件夹/` is a standalone static travel-product design
  prototype. It is not referenced by the Agent and is not wired to Arena Hero
  logs, state, trace, or API endpoints.

## Long-Term Goal

Keep the Agent reliable during long-running Arena Hero sessions, restore the
12 Worker / 3 Vanguard / 4 Ranger baseline, then expand under dynamic prices
to the strategy cap of 16 Workers, 6 Vanguards, and 8 Rangers (30 Units).

## Technology And Structure

- Python 3.11+ with the pinned official dependency `arena-hero==0.2.9`.
- `arena_agent.py` is the entry point and owns planning, SDK compatibility,
  retries, console rendering, persistence, and session recording.
- `test_arena_agent.py` contains unit and behavior tests.
- `run_arena_agent.bat` starts the project virtual environment on Windows.
- `.env` supplies the local API key and must never be printed or committed.
- `arena_agent.log` is the rotating diagnostic log.
- `arena_agent_state.json` stores persistent map, obstacle, resource, and
  exploration memory.
- `arena_agent_trace.jsonl` stores one replay-oriented JSON object per Tick.
- `arena_agent_stats.json` stores aggregate statistics for the current run.
- `新建文件夹/dev-server.js` is a dependency-free Node static file server,
  defaulting to `127.0.0.1:5173`.
- `新建文件夹/index.html`, `app.js`, and `styles.css` implement the separate
  travel-product prototype with native HTML, CSS, and JavaScript.

## Stable Conventions

- Prefer official SDK types and helpers; keep compatibility code narrow and
  only for observed older server/SDK payloads.
- Use dynamic unit prices based on living population; do not reintroduce a
  fixed maintenance-cost simulation.
- Preserve phased automatic production: `4/1/1` early defense, `12/3/4`
  baseline recovery, then Worker-first expansion to `16/6/8`. Ordinary
  production reserves are 0, 10, and 15 resources for those stages.
- Treat 30 as a strategy production cap, not a game population limit. Query
  the SDK price using the living population immediately before production.
- A normal cell accepts one Unit. A Core may share its cell with one Unit
  because Core is not counted in Unit occupancy; occupied and reserved Unit
  destinations cannot accept another Unit.
- Cargo return uses cached complete routes with a 4096-expansion budget,
  blocks visible enemies and remembered enemy Core cells, and enters recovery
  only after 16 genuinely unreachable Ticks. An occupied Core may be
  approached, but the final entry waits for the Unit slot to clear.
- Keep exploration targets sticky per Worker. Use the two-part stall policy:
  four Ticks without physical movement or three Ticks without route-cost
  improvement; unreachable routes switch immediately. Resource assignment
  retains its separate six-Tick stall threshold.
- Enemy Core attacks remain a specialized observer/staging/raid/recall flow;
  enemy Cores are not injected into normal mobile-combat defense targeting.
- `RETREAT` keeps units from pursuing a ground Beacon; `HOLD` allows deliberate
  Beacon approach and pickup.
- Preserve Chinese-friendly console output and persistent diagnostics.
- Keep secrets in the local environment only. Never copy the live token into
  source, logs, context files, tests, or responses.
- Use the project `.venv` for Python commands because the system `python`
  command may not be available in PowerShell.

## Success Criteria

- The SDK connection accepts normal Ticks without duplicate-agent or
  idempotency conflicts.
- Workers physically cover separate sectors, discover resources, harvest, and
  deposit cargo at the Core.
- Production uses current authoritative resources and dynamic SDK prices.
- The 19-Unit baseline is restored before Worker-first expansion proceeds
  toward the configured 30-Unit strategy cap without consuming reserves.
- Friendly movement and guard/raid assignment do not produce Unit stacking or
  repeated `CELL_UNIT_LIMIT` failures.
- Enemy encounters trigger bounded defense/evasion without unsafe chasing.
- Protocol/API/transport failures are classified and recorded without hiding
  permanent command or schema errors.
- Restarting the Agent preserves safe map memory and leaves replay/stat files
  useful for diagnosis.
- The static prototype passes JavaScript syntax checks and can be served by
  the Node helper when that separate UI is needed.
