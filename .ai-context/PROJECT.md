# Project Context

## Project Identity

- Name: Arena Hero Agent workspace.
- Purpose: run a persistent Arena Hero game agent that explores, collects and
  deposits resources, expands a balanced roster, handles combat, and records
  enough state to diagnose long sessions.
- Repository: Git repository at `E:\GptChat\003`; primary branch is `main`
  with upstream `origin/main`.
- Primary project scope: the Python Arena Hero agent in the project root.
- Adjacent scope: `新建文件夹/` is a standalone static travel-product
  prototype. It is not referenced by the Agent and is not wired to Arena Hero
  logs, state, trace, or API endpoints.

## Long-Term Goal

Keep the Agent reliable during long-running Arena Hero sessions. Restore the
`12 Worker / 3 Vanguard / 4 Ranger` baseline, expand to the configured
`16/6/8` roster, and optionally continue balanced `8:3:4` population growth
when resources reach the configured floor. Preserve resource flow, casualty
capacity headroom, bounded combat exposure, and reliable Core logistics in
closed-pocket and single-entrance geometry.

## Technology And Structure

- Python 3.11+ with the pinned official dependency `arena-hero==0.2.9`.
- `arena_agent.py` is the entry point and owns planning, SDK compatibility,
  retries, console rendering, persistence, and session recording.
- `test_arena_agent.py` contains in-memory unit, behavior, regression, and
  historical-geometry tests.
- `run_arena_agent.bat` starts the project virtual environment on Windows.
- `.env` supplies live local configuration and the API key. Its contents must
  never be printed, committed, or copied into handoffs.
- `.env.example` and CLI arguments document non-secret configuration.
- `arena_agent.log` is the rotating diagnostic log.
- `arena_agent_state.json` stores persistent map, obstacle, resource, and
  exploration memory.
- `arena_agent_trace.jsonl` stores one replay-oriented JSON object per Tick.
- `arena_agent_stats.json` stores aggregate statistics for the current run.
- `新建文件夹/dev-server.js`, `index.html`, `app.js`, and `styles.css` belong
  only to the separate static prototype.

## Stable Conventions

- Prefer official SDK types and helpers. Keep compatibility code narrow and
  only for observed older server or SDK payloads.
- Use dynamic unit prices based on current living population; do not
  reintroduce fixed maintenance-cost simulation.
- Preserve phased automatic production: `4/1/1` early defense, `12/3/4`
  baseline recovery, then configured targets (currently `16/6/8`). Ordinary
  production reserves are 0, 10, and 15 resources for those stages.
- Treat `max_population=0` as no strategy hard cap. Optional post-target
  expansion is controlled separately and grows toward the `8:3:4` ratio only
  when available resources reach
  `max(configured_floor, capacity - 5 * casualty_buffer_units)`. The current
  base floor is 150 and the casualty buffer is 6 Units.
- A normal cell accepts one Unit. A Core may share its cell with one Unit
  because Core is not counted in Unit occupancy. Occupied and reserved Unit
  destinations cannot accept another Unit.
- Cargo return uses cached complete routes with a 4096-expansion budget,
  blocks visible enemies and remembered enemy Core cells, and enters recovery
  only after 16 genuinely unreachable Ticks. An occupied Core may be
  approached, but final entry waits for its Unit slot to clear.
- Keep closed-Core-pocket detection separate from single-entrance throughput.
  A closed pocket requires a naturally exhausted bounded Core component, no
  admitted moving Cargo, a statically reachable external Cargo, stable
  Core/Cargo continuity, and two-Tick debounce.
- A single usable Core entrance uses a half-duplex Cargo lane. Startup
  evacuation clears nearby empty Workers first; only one inbound owner is
  admitted; after deposit that owner follows a complete egress route into an
  open dynamic component before the next owner enters. Staging Cargo, guards,
  and other Workers reserve the lane and egress path. Egress planning may
  cross temporary friendly occupancy so those Units can yield, but it must end
  on a currently unoccupied open component and replan if that endpoint closes.
- Keep exploration targets sticky per Worker. Use four Ticks without physical
  movement or three Ticks without route-cost improvement as scout stall
  signals; unreachable routes switch immediately. Resource assignment keeps
  its separate six-Tick stall threshold.
- Treat visible enemy occupancy as a hard movement blocker. Server move
  failures feed back for every Unit type; contested or occupied destinations
  are avoided temporarily, and repeated stationary-clear failures cool the
  target instead of retrying forever.
- Critically wounded defenders prioritize an immediately safer legal step and
  register for Core healing when resources and backup strength permit.
- Keep Worker-local combat memory separate from Core-level threat. Retain all
  hostile attack positions for Worker escape and return routing, but refresh
  Core recent-attack state only when the Core is targeted or the attack is
  within 12 Manhattan cells of it. Attacks within 8 cells are `ENGAGED`, those
  at 9-12 cells are `ALERT`, and farther attacks do not force `GUARD`, clear a
  Cargo lane, start production caution, or move the Core. Core escape also
  requires a nearby visible enemy or a confirmed pursuing/preemptive enemy.
- Enemy Core attacks remain a specialized observer/staging/raid/recall flow;
  enemy Cores are not injected into normal mobile-combat defense targeting.
- `CORE_RESOURCES_CAPTURED` may invalidate captured enemy-Core memory but does
  not mean the friendly Core was lost. Recovery is triggered only by
  `CORE_LOST` or destruction of the friendly Core.
- `RETREAT` keeps units from pursuing a ground Beacon; `HOLD` allows deliberate
  Beacon approach and pickup.
- Preserve Chinese-friendly console output and persistent diagnostics.
- User actions can move Core or defenders and can spend resources. Correlate
  logs with user actions before classifying those changes as strategy faults.
- Keep secrets in the local environment only. Never copy the live token into
  source, logs, context files, tests, or responses.
- Use the project `.venv` for Python commands because system `python` may be
  unavailable in PowerShell.

## Success Criteria

- The SDK connection accepts normal Ticks without duplicate-agent or
  idempotency conflicts.
- Workers discover resources, harvest, and continue depositing through
  congested or single-entrance Core geometry without permanent deadlock.
- Single-entrance delivery preserves half-duplex ordering: startup evacuation,
  inbound owner, deposit, complete egress to an open component, then next owner.
- Production uses authoritative resources and dynamic SDK prices. Configured
  targets are repaired first; optional expansion respects its switch, resource
  floor, casualty buffer, ratio, and any explicit finite hard cap.
- Friendly movement and guard/raid assignment do not stack Units or create
  long repeated `MOVE_DESTINATION_OCCUPIED` / `MOVE_CONTESTED` loops.
- Enemy encounters trigger bounded defense, Worker evasion, critical Defender
  retreat, healing, and recall without unsafe chasing.
- A distant Worker attack preserves local escape memory without escalating the
  whole base, dismantling an active Cargo lane, or moving the Core.
- `CORE_RESOURCES_CAPTURED` from destroying an enemy Core does not put the
  friendly mission into recovery.
- Protocol/API/transport failures are classified and recorded without hiding
  permanent command or schema errors.
- Restarting the Agent preserves safe map memory and leaves replay/stat files
  useful for diagnosis.
- The static prototype passes JavaScript syntax checks when that separate UI
  is changed.
