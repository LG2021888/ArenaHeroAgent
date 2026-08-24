from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from arena_agent import (
    AgentConfig,
    EnemyMotion,
    PlanReport,
    SessionStats,
    ScoutProgress,
    ScoutReturnProgress,
    SessionRecorder,
    TacticMemory,
    UnitType,
    CORE_TARGET_MEMORY_TTL,
    CARGO_LANE_INBOUND_WATCHDOG_TICKS,
    CRITICAL_DEFENDER_HEAL_MAX_WAIT_TICKS,
    HEAL_PRIORITY_MAX_HOLD_TICKS,
    IDLE_ASSIGNMENT_COST,
    PATH_COST_UNREACHABLE,
    ROUTE_MAX_EXPANSIONS,
    MOVE_CONTESTED_AVOID_TICKS,
    MOVE_OCCUPIED_AVOID_TICKS,
    CoreRaidPlan,
    CoreVisit,
    CargoLanePlan,
    EnemyCoreObservation,
    _assign_guard_posts,
    _assign_raid_positions,
    _chebyshev_ring_positions,
    _cached_worker_route_step,
    _choose_spawn_unit,
    _complete_route,
    _cargo_lane_egress_complete,
    _cargo_lane_egress_route,
    _cargo_lane_yield_direction,
    _queue_cargo_lane_defender_yield,
    _cargo_lane_temporary_release_route,
    _cargo_lane_startup_workers,
    _core_escape_direction,
    _friendly_cell_occupancy,
    _raid_target_durability,
    _render_turn,
    _refresh_cargo_lane_occupants,
    _refresh_healing_defenders,
    _refresh_healing_worker_stage_target,
    _estimated_path_cost,
    _is_retryable_protocol_error,
    _is_retryable_api_error,
    _load_tactic_memory,
    _next_position,
    _parse_stream_message_with_compatibility,
    _parse_args,
    _queue_move,
    _plan_workers,
    _reconnect_delay,
    _save_tactic_memory,
    _select_cargo_lane_owner,
    _submit_turn_with_retry,
    _trace_event,
    _update_core_pocket,
    _update_cargo_lane,
    _single_open_cargo_lane_path,
    _unit_cost,
    _worker_escape_direction,
    _worker_mode,
    choose_step_direction,
    plan_turn,
)
from arena_hero import (
    APIError,
    BeaconStatus,
    CoreState,
    Direction,
    ProtocolError,
    TransportError,
)


class FakeActor:
    def __init__(
        self,
        actor_id,
        position,
        unit_type=UnitType.WORKER,
        cargo=0,
        shield=None,
    ):
        self.id = actor_id
        self.position = position
        self.unit_type = unit_type
        self.cargo = cargo
        self.hp = 2
        self.shield = shield
        self.view = SimpleNamespace(state=CoreState.NORMAL)
        self.actions = []

    def move(self, direction):
        self.actions.append(("MOVE", direction))

    def harvest(self):
        self.actions.append(("HARVEST",))

    def deposit(self):
        self.actions.append(("DEPOSIT",))

    def pickup_beacon(self):
        self.actions.append(("PICKUP_BEACON",))

    def wait(self):
        self.actions.append(("WAIT",))

    def sweep(self, direction):
        self.actions.append(("SWEEP", direction))

    def shoot(self, target):
        self.actions.append(("SHOOT", target.id))

    def heal(self):
        self.actions.append(("HEAL",))

    def repair_shield(self):
        self.actions.append(("REPAIR_SHIELD",))

    def spawn(self, unit_type):
        self.actions.append(("SPAWN", unit_type))

    def start_move(self, direction):
        self.actions.append(("START_MOVE", direction))

    def cancel_move(self):
        self.actions.append(("CANCEL_MOVE",))


class FlakyTurn:
    tick = 42

    def __init__(self):
        self.calls = []

    def submit(self, *, idempotency_key):
        self.calls.append(idempotency_key)
        if len(self.calls) == 1:
            raise TransportError("temporary transport failure")
        return "accepted"


class RetryableApiTurn:
    tick = 43

    def __init__(self):
        self.calls = []

    def submit(self, *, idempotency_key):
        self.calls.append(idempotency_key)
        if len(self.calls) == 1:
            raise APIError(
                status_code=503,
                error="SERVICE_UNAVAILABLE",
                message="temporary server failure",
            )
        return "accepted"


class PermanentApiTurn:
    tick = 44

    def __init__(self):
        self.calls = []

    def submit(self, *, idempotency_key):
        self.calls.append(idempotency_key)
        raise APIError(
            status_code=422,
            error="INVALID_COMMAND",
            message="command rejected",
            details={"field": "core_action"},
        )


def make_turn(*, worker, resources=frozenset(), obstacles=frozenset(), core=None, enemies=()):
    if core is not None and core.shield is None:
        core.shield = 5
    state = SimpleNamespace(population=1 if worker else 0)
    beacon = SimpleNamespace(position=None, status=None)
    return SimpleNamespace(
        tick=10,
        resources=0,
        state=state,
        units=(worker,) if worker else (),
        workers=(worker,) if worker else (),
        vanguards=(),
        rangers=(),
        core=core,
        visible_enemies=enemies,
        resource_cells=resources,
        obstacle_cells=obstacles,
        beacon=beacon,
        events=(),
        )


def make_roster_turn(worker_count, vanguard_count, ranger_count, *, resources):
    workers = tuple(
        FakeActor(f"worker-{index}", (index + 1, 20))
        for index in range(worker_count)
    )
    vanguards = tuple(
        FakeActor(
            f"vanguard-{index}",
            (index + 1, 30),
            unit_type=UnitType.VANGUARD,
        )
        for index in range(vanguard_count)
    )
    for vanguard in vanguards:
        vanguard.hp = 4
    rangers = tuple(
        FakeActor(
            f"ranger-{index}",
            (index + 1, 40),
            unit_type=UnitType.RANGER,
        )
        for index in range(ranger_count)
    )
    core = FakeActor("core", (0, 0), shield=5)
    core.hp = 5
    turn = make_turn(worker=workers[0] if workers else None, core=core)
    turn.workers = workers
    turn.vanguards = vanguards
    turn.rangers = rangers
    turn.units = workers + vanguards + rangers
    turn.state.population = len(turn.units)
    turn.resources = resources
    return turn, core


def make_pocket_snapshot(tick, obstacles, worker_specs, guard_specs):
    core = FakeActor("core", (-217, 665), shield=5)
    core.hp = 5
    workers = tuple(
        FakeActor(worker_id, position, cargo=cargo)
        for worker_id, position, cargo in worker_specs
    )
    vanguards = tuple(
        FakeActor(worker_id, position, unit_type=UnitType.VANGUARD)
        for worker_id, position in guard_specs[:6]
    )
    rangers = tuple(
        FakeActor(worker_id, position, unit_type=UnitType.RANGER)
        for worker_id, position in guard_specs[6:]
    )
    turn = make_turn(worker=workers[0], core=core, obstacles=frozenset(obstacles))
    turn.tick = tick
    turn.workers = workers
    turn.vanguards = vanguards
    turn.rangers = rangers
    turn.units = workers + vanguards + rangers
    turn.state.population = len(turn.units)
    return turn


def apply_synchronous_actions(turn):
    """Apply FakeActor actions with the server's no-swap Unit occupancy rule."""

    occupied = {
        actor.position: str(actor.id)
        for actor in turn.units
        if actor.position is not None
    }
    moves = {}
    destination_owners = {}
    deposit_ids = []
    for actor in turn.units:
        move_actions = [action for action in actor.actions if action[0] == "MOVE"]
        if len(move_actions) > 1:
            raise AssertionError(f"multiple moves for {actor.id}: {move_actions}")
        if move_actions:
            destination = _next_position(actor.position, move_actions[0][1])
            previous_owner = destination_owners.get(destination)
            if previous_owner is not None:
                raise AssertionError(
                    f"contested destination {destination}: "
                    f"{previous_owner} and {actor.id}"
                )
            occupying_id = occupied.get(destination)
            if occupying_id is not None and occupying_id != str(actor.id):
                raise AssertionError(
                    f"{actor.id} enters occupied {destination} held by {occupying_id}"
                )
            destination_owners[destination] = str(actor.id)
            moves[str(actor.id)] = destination
        if any(action[0] == "DEPOSIT" for action in actor.actions):
            deposit_ids.append(str(actor.id))

    actors_by_id = {str(actor.id): actor for actor in turn.units}
    for actor_id, destination in moves.items():
        actors_by_id[actor_id].position = destination
    for actor_id in deposit_ids:
        actors_by_id[actor_id].cargo = 0
    turn.resources += len(deposit_ids)
    turn.tick += 1
    turn.events = tuple(
        SimpleNamespace(
            event_type="DEPOSIT_SUCCEEDED",
            reason_code=None,
            actor_id=actor_id,
            target_id=str(turn.core.id),
            position=turn.core.position,
        )
        for actor_id in deposit_ids
    )
    for actor in (*turn.units, turn.core):
        actor.actions.clear()
    return moves, tuple(deposit_ids)


def make_blocked_pocket_snapshots(guard_specs):
    workers_89474 = (
        ("0f13adab", (-222, 661), 1),
        ("626fe5a3", (-214, 690), 0),
        ("6553c3ed", (-211, 665), 1),
        ("656d3944", (-217, 664), 0),
        ("8bb2ffda", (-217, 649), 1),
        ("9a2a356b", (-210, 637), 0),
        ("a975bdda", (-214, 655), 0),
        ("b722cb72", (-217, 666), 0),
        ("b865048a", (-216, 666), 0),
        ("c3ca0560", (-212, 702), 1),
        ("ca727ec5", (-216, 663), 0),
        ("cee8bd78", (-213, 664), 1),
        ("d3139ba8", (-216, 659), 0),
        ("dd996335", (-215, 661), 1),
        ("f0c09f3e", (-217, 657), 1),
    )
    workers_89475 = (
        ("0f13adab", (-221, 661), 1),
        ("626fe5a3", (-214, 691), 0),
        ("6553c3ed", (-212, 665), 1),
        ("656d3944", (-216, 664), 0),
        ("8bb2ffda", (-217, 650), 1),
        ("9a2a356b", (-210, 637), 0),
        ("a975bdda", (-214, 654), 0),
        ("b722cb72", (-217, 666), 0),
        ("b865048a", (-216, 666), 0),
        ("c3ca0560", (-212, 701), 1),
        ("ca727ec5", (-216, 663), 0),
        ("cee8bd78", (-213, 665), 1),
        ("d3139ba8", (-216, 658), 0),
        ("dd996335", (-215, 660), 1),
        ("f0c09f3e", (-217, 656), 1),
    )
    obstacles_89474 = {
        (-225, 667), (-223, 669), (-221, 660), (-221, 662),
        (-221, 670), (-220, 664), (-220, 666), (-219, 657),
        (-219, 659), (-219, 665), (-219, 669), (-217, 670),
        (-216, 665), (-216, 671), (-215, 659), (-215, 665),
        (-215, 670), (-213, 660), (-213, 663), (-212, 666),
        (-211, 661), (-211, 668), (-210, 666),
    }
    obstacles_89475 = {
        (-225, 667), (-223, 669), (-221, 660), (-221, 662),
        (-221, 670), (-220, 664), (-220, 666), (-219, 659),
        (-219, 665), (-219, 669), (-217, 670), (-216, 665),
        (-216, 671), (-215, 659), (-215, 665), (-215, 670),
        (-213, 659), (-213, 660), (-213, 663), (-212, 666),
        (-211, 661), (-211, 668), (-210, 666),
    }
    return (
        make_pocket_snapshot(89474, obstacles_89474, workers_89474, guard_specs),
        make_pocket_snapshot(89475, obstacles_89475, workers_89475, guard_specs),
        obstacles_89474,
        obstacles_89475,
    )


class IdempotencyConflictTurn:
    tick = 45

    def __init__(self):
        self.calls = []

    def submit(self, *, idempotency_key):
        self.calls.append(idempotency_key)
        raise APIError(
            status_code=409,
            error="IDEMPOTENCY_CONFLICT",
            message="same key was used for a different command",
        )


class ArenaAgentTests(unittest.TestCase):
    POCKET_GUARDS = (
        ("3df43662", (-216, 667)),
        ("3f0bd5b9", (-214, 667)),
        ("6a24bb5a", (-217, 662)),
        ("70f06c55", (-215, 667)),
        ("87f1d6a2", (-214, 666)),
        ("fb8b4510", (-214, 661)),
        ("23f6dad8", (-220, 667)),
        ("3059ebff", (-218, 664)),
        ("3b92991a", (-217, 667)),
        ("4a49109c", (-218, 667)),
        ("5ee3e17e", (-219, 667)),
        ("7e94b747", (-220, 663)),
        ("839247ac", (-219, 662)),
        ("8527c65c", (-218, 662)),
        ("e2790ccf", (-220, 662)),
    )

    def test_transport_retry_reuses_idempotency_key(self):
        turn = FlakyTurn()
        errors = []

        result = _submit_turn_with_retry(
            turn,
            attempts=2,
            sleep_fn=lambda _: None,
            error_callback=errors.append,
        )

        self.assertEqual(result, "accepted")
        self.assertEqual(turn.calls[0], turn.calls[1])
        self.assertTrue(turn.calls[0].startswith("arena-agent-"))
        self.assertTrue(turn.calls[0].endswith("-42"))
        self.assertEqual(errors, ["submit_transport"])

    def test_retryable_api_error_reuses_idempotency_key(self):
        turn = RetryableApiTurn()
        errors = []

        result = _submit_turn_with_retry(
            turn,
            attempts=2,
            sleep_fn=lambda _: None,
            error_callback=errors.append,
        )

        self.assertEqual(result, "accepted")
        self.assertEqual(turn.calls[0], turn.calls[1])
        self.assertTrue(turn.calls[0].startswith("arena-agent-"))
        self.assertTrue(turn.calls[0].endswith("-43"))
        self.assertEqual(errors, ["submit_api"])

    def test_permanent_api_error_is_not_retried(self):
        turn = PermanentApiTurn()

        with self.assertRaises(APIError) as caught:
            _submit_turn_with_retry(
                turn,
                attempts=3,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(len(turn.calls), 1)
        self.assertTrue(turn.calls[0].endswith("-44"))

    def test_idempotency_conflict_fails_fast_without_retry(self):
        turn = IdempotencyConflictTurn()

        with self.assertRaises(APIError) as caught:
            _submit_turn_with_retry(
                turn,
                attempts=3,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(caught.exception.error, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(turn.calls), 1)

    def test_api_retry_classification_and_backoff_are_bounded(self):
        self.assertTrue(
            _is_retryable_api_error(
                APIError(status_code=503, error="SERVICE_UNAVAILABLE")
            )
        )
        self.assertFalse(
            _is_retryable_api_error(
                APIError(status_code=409, error="IDEMPOTENCY_CONFLICT")
            )
        )
        self.assertEqual(_reconnect_delay(1, 30), 1.0)
        self.assertEqual(_reconnect_delay(6, 30), 30.0)
        self.assertEqual(_reconnect_delay(10_000, 30), 30.0)

    def test_reconnect_attempt_limit_argument_is_configurable(self):
        args = _parse_args(["--max-reconnect-attempts", "2"])

        self.assertEqual(args.max_reconnect_attempts, 2)

    def test_population_expansion_arguments_are_configurable(self):
        args = _parse_args(
            [
                "--population-expansion",
                "ON",
                "--expansion-threshold",
                "153",
                "--expansion-casualty-buffer",
                "7",
            ]
        )

        self.assertEqual(args.population_expansion, "ON")
        self.assertEqual(args.expansion_threshold, 153)
        self.assertEqual(args.expansion_casualty_buffer, 7)

    def test_tactic_memory_persists_safe_map_observations(self):
        memory = TacticMemory(
            obstacle_memory={(1, 2)},
            resource_observation_memory={(3, 4): 20},
            dropped_cargo_observation_memory={(5, 6): 19},
            scout_chunk_last_seen={(0, 0): 20},
            exploration_anchor=(10, 10),
            resource_intents={"old-worker": (3, 4)},
            scout_stages={"old-worker": 17},
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            _save_tactic_memory(memory, path, current_tick=20)
            restored = _load_tactic_memory(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(restored.obstacle_memory, {(1, 2)})
        self.assertEqual(restored.resource_observation_memory, {(3, 4): 20})
        self.assertEqual(restored.dropped_cargo_observation_memory, {(5, 6): 19})
        self.assertEqual(restored.scout_chunk_last_seen, {(0, 0): 20})
        self.assertEqual(restored.exploration_anchor, (10, 10))
        self.assertEqual(restored.resource_intents, {})
        self.assertEqual(restored.scout_stages, {})
        self.assertNotIn("old-worker", json.dumps(payload))

    def test_worker_position_history_keeps_only_six_recent_cells(self):
        worker = FakeActor("worker", (0, 0))
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory()

        for tick in range(8):
            worker.position = (tick, 0)
            turn.tick = tick
            memory.observe(turn)

        self.assertEqual(
            memory.worker_position_history["worker"],
            [(2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0)],
        )

    def test_session_recorder_writes_replay_and_stats(self):
        worker = FakeActor("worker", (1, 2), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        report = PlanReport(10, 0, 1, 1, 0, 0, 0, 0, 0, mission="SCOUT")
        memory = TacticMemory(
            scout_progress={
                "worker": ScoutProgress(
                    (-10, 0),
                    12,
                    last_position=(1, 2),
                    path_stalled_turns=2,
                    last_cost=14,
                )
            },
            scout_return_targets={"worker": (0, 0)},
            worker_position_history={"worker": [(3, 2), (2, 2), (1, 2)]},
        )

        with tempfile.TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            stats_path = Path(directory) / "stats.json"
            recorder = SessionRecorder(str(trace_path), str(stats_path))
            recorder.record_turn(turn, report, True, memory)
            recorder.record_runtime_error("submit_transport")
            recorder.close()
            trace = json.loads(trace_path.read_text(encoding="utf-8").strip())
            stats = json.loads(stats_path.read_text(encoding="utf-8"))

        self.assertEqual(trace["tick"], 10)
        self.assertEqual(trace["workers"][0]["position"], [1, 2])
        self.assertEqual(trace["mission"], "SCOUT")
        self.assertEqual(trace["worker_modes"], {"worker": "SCOUT_RETURN"})
        self.assertEqual(trace["scout_return_targets"], {"worker": [0, 0]})
        self.assertEqual(trace["scout_progress"]["worker"]["route_cost"], 14)
        self.assertEqual(
            trace["scout_progress"]["worker"]["path_stalled_ticks"],
            2,
        )
        self.assertEqual(
            trace["worker_recent_positions"]["worker"],
            [[3, 2], [2, 2], [1, 2]],
        )
        self.assertEqual(stats["ticks"], 1)
        self.assertEqual(stats["accepted_ticks"], 1)
        self.assertEqual(stats["mission_counts"], {"SCOUT": 1})
        self.assertEqual(stats["current_injured_units"], 0)
        self.assertEqual(stats["max_deposit_gap_ticks"], 0)
        self.assertEqual(stats["cargo_watchdog_counts"], {})
        self.assertEqual(stats["runtime_error_counts"], {"submit_transport": 1})

    def test_session_stats_tracks_logistics_healing_and_network_metrics(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        worker.hp = 1
        ranger = FakeActor("ranger", (2, 0), unit_type=UnitType.RANGER)
        ranger.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        turn = make_turn(worker=worker, core=core)
        turn.workers = (worker,)
        turn.rangers = (ranger,)
        turn.units = (worker, ranger)
        turn.events = (SimpleNamespace(event_type="DEPOSIT_SUCCEEDED"),)
        memory = TacticMemory(
            heal_priority_started_tick=5,
            healing_defender_timeout_tick=10,
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                watchdog_tick=10,
                watchdog_reason="INBOUND_OWNER_STALLED",
            ),
        )
        stats = SessionStats("test-session")
        stats.record(turn, PlanReport(10, 0, 2, 1, 0, 1, 0, 0, 0), True, memory)

        turn.tick = 18
        turn.events = (SimpleNamespace(event_type="DEPOSIT_SUCCEEDED"),)
        memory.cargo_lane.watchdog_tick = -1
        stats.record(turn, PlanReport(18, 0, 2, 1, 0, 1, 0, 0, 0), True, memory)
        stats.record_runtime_error("reconnect_transport")

        payload = stats.as_dict()
        self.assertEqual(payload["max_deposit_gap_ticks"], 8)
        self.assertEqual(payload["current_injured_units"], 2)
        self.assertEqual(payload["max_injured_units"], 2)
        self.assertEqual(payload["max_heal_priority_wait_ticks"], 13)
        self.assertEqual(payload["healing_admission_timeouts"], 1)
        self.assertEqual(
            payload["cargo_watchdog_counts"],
            {"INBOUND_OWNER_STALLED": 1},
        )
        self.assertEqual(
            payload["runtime_error_counts"],
            {"reconnect_transport": 1},
        )

    def test_trace_event_keeps_move_failure_identity_and_position(self):
        event = SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_CONTESTED",
            actor_id="worker",
            target_id="enemy-worker",
            position=(1, 0),
        )

        self.assertEqual(
            _trace_event(event),
            {
                "type": "UNIT_MOVE_FAILED",
                "reason_code": "MOVE_CONTESTED",
                "actor_id": "worker",
                "target_id": "enemy-worker",
                "position": [1, 0],
            },
        )

    def test_trace_event_keeps_core_resource_capture_details(self):
        event = SimpleNamespace(
            event_type="CORE_RESOURCES_CAPTURED",
            reason_code=None,
            actor_id="ranger",
            target_id="enemy-core",
            position=(8, 4),
            values={
                "amount": 7,
                "available": 10,
                "destroyed": 3,
                "capacity": 7,
            },
        )

        self.assertEqual(
            _trace_event(event),
            {
                "type": "CORE_RESOURCES_CAPTURED",
                "actor_id": "ranger",
                "target_id": "enemy-core",
                "position": [8, 4],
                "values": {
                    "amount": 7,
                    "available": 10,
                    "destroyed": 3,
                    "capacity": 7,
                },
            },
        )

    def test_only_initial_state_order_protocol_error_is_retryable(self):
        self.assertTrue(
            _is_retryable_protocol_error(ProtocolError("state arrived before tick"))
        )
        self.assertFalse(
            _is_retryable_protocol_error(
                ProtocolError("invalid Arena Hero WebSocket message")
            )
        )

    def test_older_state_payload_gets_only_missing_compatibility_defaults(self):
        raw = (
            '{"type":"state","data":{'
            '"status":"ACTIVE","resources":0,"population":0,'
            '"champion_beacon":{"position":[0,0],"status":"GROUND"},'
            '"objects":[],"events":[]}}'
        )

        state = _parse_stream_message_with_compatibility(raw)

        if hasattr(state, "population_tier"):
            self.assertEqual(state.population_tier, 0)
            self.assertEqual(state.upkeep_next_tick, 0)
        else:
            self.assertEqual(state.population, 0)

    def test_step_uses_alternate_axis_when_preferred_cell_is_blocked(self):
        self.assertEqual(
            choose_step_direction((0, 0), (2, 1), {(1, 0)}),
            Direction.DOWN,
        )

    def test_step_routes_around_a_blocked_direct_cell(self):
        self.assertIn(
            choose_step_direction((0, 0), (2, 0), {(1, 0)}),
            {Direction.UP, Direction.DOWN},
        )

    def test_step_prefers_lower_cost_route_around_cargo_return_obstacle(self):
        blocked = {
            (-236, 638),
            (-235, 637),
        }

        self.assertEqual(
            choose_step_direction((-236, 636), (-216, 658), blocked),
            Direction.RIGHT,
        )

    def test_step_discourages_recent_scout_positions(self):
        self.assertEqual(
            choose_step_direction(
                (0, 0),
                (2, 2),
                set(),
                discouraged={(1, 0)},
            ),
            Direction.DOWN,
        )

    def test_scout_can_detour_instead_of_revisiting_its_only_direct_cell(self):
        self.assertEqual(
            choose_step_direction(
                (0, 0),
                (2, 0),
                set(),
                discouraged={(1, 0)},
            ),
            Direction.UP,
        )

    def test_bounded_path_cost_returns_frontier_estimate(self):
        self.assertEqual(
            _estimated_path_cost((0, 0), (100, 0), set(), max_expansions=1),
            100,
        )

    def test_cargo_worker_queues_route_aware_return_move(self):
        worker = FakeActor("worker", (-236, 636), cargo=1)
        core = FakeActor("core", (-216, 658))
        turn = make_turn(
            worker=worker,
            core=core,
            obstacles=frozenset({(-236, 638), (-235, 637)}),
        )

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_worker_deposits_when_sharing_core_cell(self):
        worker = FakeActor("worker", (2, 2), cargo=1)
        core = FakeActor("core", (2, 2))
        turn = make_turn(worker=worker, core=core)

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("DEPOSIT",)])

    def test_deposit_owns_the_only_core_visit_and_blocks_same_tick_spawn(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        turn.resources = 0
        turn.resource_capacity = 10
        turn.resource_space = 10
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(max_population=2))

        self.assertEqual(memory.core_visit, CoreVisit("worker", "DEPOSIT", turn.tick))
        self.assertEqual(worker.actions, [("DEPOSIT",)])
        self.assertEqual(core.actions, [("WAIT",)])

    def test_core_visit_blocks_spawn_across_ticks(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        turn.resources = 0
        turn.resource_capacity = 10
        turn.resource_space = 10
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(max_population=2))
        worker.actions.clear()
        core.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, AgentConfig(max_population=2))

        self.assertEqual(core.actions, [("WAIT",)])
        self.assertNotIn("SPAWN", {action[0] for action in core.actions})

    def test_core_self_healing_is_independent_of_core_visit(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 3
        turn = make_turn(worker=worker, core=core)
        turn.resources = 12
        turn.resource_capacity = 30
        memory = TacticMemory(core_visit=CoreVisit("worker", "DEPOSIT", turn.tick))

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(core.actions, [("HEAL",)])

    def test_core_visit_never_grants_two_physical_visitors(self):
        first = FakeActor("first", (0, 0), cargo=1)
        second = FakeActor("second", (0, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=first, core=core)
        turn.workers = (first, second)
        turn.units = (first, second)
        turn.state.population = 2
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        deposits = sum(("DEPOSIT",) in worker.actions for worker in turn.workers)
        self.assertEqual(deposits, 1)
        self.assertIn(memory.core_visit.unit_id, {"first", "second"})

    def test_worker_healing_candidate_reaches_core_and_heals(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        worker.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        turn.resources = 20
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIn("worker", memory.healing_worker_ids)
        self.assertEqual(memory.core_visit, CoreVisit("worker", "HEAL", turn.tick))
        self.assertEqual(worker.actions, [("HEAL",)])

    def test_distant_worker_healing_moves_instead_of_waiting(self):
        worker = FakeActor("worker", (-212, 664), cargo=0)
        worker.hp = 1
        core = FakeActor("core", (-217, 666))
        turn = make_turn(worker=worker, core=core)
        turn.resources = 137
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIn("worker", memory.healing_worker_ids)
        self.assertEqual(worker.actions, [("MOVE", Direction.DOWN)])
        self.assertEqual(memory.heal_intent_id, "worker")
        self.assertIsNone(memory.core_visit.unit_id)

    def test_cargo_on_core_deposits_while_remote_healer_has_intent(self):
        cargo = FakeActor("cargo", (0, 0), cargo=1)
        healer = FakeActor("healer", (5, 0), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=cargo, core=core)
        turn.workers = (cargo, healer)
        turn.units = (cargo, healer)
        turn.state.population = 2
        turn.resources = 20
        turn.resource_capacity = 30
        turn.resource_space = 10
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            core_visit_forced_purpose="HEAL",
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(cargo.actions, [("DEPOSIT",)])
        self.assertEqual(memory.core_visit, CoreVisit("cargo", "DEPOSIT", turn.tick))
        self.assertEqual(memory.heal_intent_id, "healer")

    def test_remote_heal_visit_times_out_and_rotates_intent(self):
        stale = FakeActor("stale", (5, 0), cargo=0)
        stale.hp = 1
        next_worker = FakeActor("next", (2, 0), cargo=0)
        next_worker.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=stale, core=core)
        turn.workers = (stale, next_worker)
        turn.units = (stale, next_worker)
        turn.state.population = 2
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"stale"},
            heal_intent_id="stale",
            heal_intent_tick=turn.tick - 12,
            core_visit=CoreVisit("stale", "HEAL", turn.tick - 12),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIsNone(memory.core_visit.unit_id)
        self.assertEqual(memory.heal_intent_id, "next")
        self.assertIn("stale", memory.healing_worker_ids)
        self.assertIn("next", memory.healing_worker_ids)

    def test_heal_visit_releases_after_healer_leaves_core(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        worker.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        turn.resources = 20
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        self.assertEqual(memory.core_visit.purpose, "HEAL")
        worker.position = (1, 0)
        turn.tick += 1
        memory.observe(turn)

        self.assertIsNone(memory.core_visit.unit_id)

    def test_worker_healing_is_blocked_by_existing_core_visitor(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        worker.hp = 1
        guard = FakeActor("guard", (0, 0), unit_type=UnitType.VANGUARD)
        guard.hp = 2
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = (guard,)
        turn.units = (worker, guard)
        turn.state.population = 2
        turn.resources = 20
        memory = TacticMemory(
            core_visit=CoreVisit("guard", "HEAL", turn.tick),
            healing_defender_ids={"guard"},
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotEqual(worker.actions, [("HEAL",)])
        self.assertEqual(memory.core_visit.unit_id, "guard")

    def test_cargo_return_is_reported_as_economy_mission(self):
        worker = FakeActor("worker", (0, 1), cargo=1)
        core = FakeActor("core", (0, 0))

        report = plan_turn(
            make_turn(worker=worker, core=core),
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.mission, "ECONOMY")

    def test_empty_worker_harvests_current_visible_resource(self):
        worker = FakeActor("worker", (3, 3), cargo=0)
        turn = make_turn(worker=worker, resources=frozenset({(3, 3)}))

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("HARVEST",)])

    def test_hidden_beacon_is_not_used_as_a_worker_target(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        turn = make_turn(worker=worker)
        turn.beacon = SimpleNamespace(position=(4, 4), status=None)

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])

    def test_recent_resource_memory_is_an_exploration_target(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        turn = make_turn(worker=worker)
        turn.tick = 10
        memory = TacticMemory(resource_observation_memory={(2, 0): 9})

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_dropped_cargo_is_prioritized_over_a_normal_resource(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (10, 10))
        turn = make_turn(
            worker=worker,
            core=core,
            resources=frozenset({(0, 1), (2, 0)}),
        )
        memory = TacticMemory(
            dropped_cargo_observation_memory={(2, 0): 10},
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.resource_intents["worker"], (2, 0))
        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_worker_cargo_drop_event_is_remembered(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        turn.events = (
            SimpleNamespace(
                event_type="WORKER_CARGO_DROPPED",
                reason_code=None,
                actor_id="worker",
                target_id=None,
                position=(3, 0),
            ),
        )
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.recent_dropped_cargo_targets(10, 64), {(3, 0)})

    def test_worker_explores_left_when_no_resource_is_visible(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_scout_target_stays_sticky_while_worker_advances(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        memory = TacticMemory()
        turn = make_turn(worker=worker, core=core)

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        first_target = memory.scout_progress["worker"].target
        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

        worker.actions.clear()
        worker.position = (-1, 0)
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.scout_progress["worker"].target, first_target)
        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_scout_physical_progress_survives_a_temporary_cost_increase(self):
        memory = TacticMemory()
        target = (-10, 0)

        self.assertFalse(
            memory.note_scout_progress("worker", target, 10, (0, 0))
        )
        self.assertFalse(
            memory.note_scout_progress("worker", target, 11, (0, -1))
        )

        progress = memory.scout_progress["worker"]
        self.assertEqual(progress.stalled_turns, 0)
        self.assertEqual(progress.path_stalled_turns, 1)

    def test_scout_current_route_progress_resets_after_a_cost_jump(self):
        memory = TacticMemory()
        target = (-10, 0)

        self.assertFalse(memory.note_scout_progress("worker", target, 10, (0, 0)))
        self.assertFalse(memory.note_scout_progress("worker", target, 14, (0, -1)))
        self.assertFalse(memory.note_scout_progress("worker", target, 13, (0, -2)))

        progress = memory.scout_progress["worker"]
        self.assertEqual(progress.best_cost, 10)
        self.assertEqual(progress.last_cost, 13)
        self.assertEqual(progress.path_stalled_turns, 0)

    def test_scout_switches_after_three_non_improving_path_costs(self):
        memory = TacticMemory()
        target = (-10, 0)

        self.assertFalse(memory.note_scout_progress("worker", target, 10, (0, 0)))
        self.assertFalse(memory.note_scout_progress("worker", target, 11, (0, -1)))
        self.assertFalse(memory.note_scout_progress("worker", target, 12, (0, -2)))
        self.assertTrue(memory.note_scout_progress("worker", target, 13, (0, -3)))
        self.assertNotIn("worker", memory.scout_progress)

    def test_scout_switches_after_four_ticks_without_physical_movement(self):
        memory = TacticMemory()
        target = (-10, 0)

        self.assertFalse(
            memory.note_scout_progress("worker", target, 10, (0, 0))
        )
        self.assertFalse(
            memory.note_scout_progress("worker", target, 9, (-1, 0))
        )
        for cost in (8, 7, 6):
            self.assertFalse(
                memory.note_scout_progress("worker", target, cost, (-1, 0))
            )

        self.assertTrue(
            memory.note_scout_progress("worker", target, 5, (-1, 0))
        )
        self.assertNotIn("worker", memory.scout_progress)

    def test_duplicate_scout_targets_are_split_into_distinct_routes(self):
        first = FakeActor("a-worker", (0, 0), cargo=0)
        second = FakeActor("b-worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=first, core=core)
        turn.workers = (first, second)
        turn.units = (first, second)
        memory = TacticMemory(
            scout_progress={
                "a-worker": ScoutProgress((-10, 0), 1),
                "b-worker": ScoutProgress((-10, 0), 1),
            }
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotEqual(
            memory.scout_progress["a-worker"].target,
            memory.scout_progress["b-worker"].target,
        )
        self.assertEqual(first.actions, [("MOVE", Direction.LEFT)])
        self.assertNotEqual(second.actions[0][1], Direction.LEFT)

    def test_workers_explore_in_different_directions(self):
        first = FakeActor("a-worker", (0, 0), cargo=0)
        second = FakeActor("b-worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=first, core=core)
        turn.workers = (first, second)
        turn.units = (first, second)
        turn.state.population = 2

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(first.actions, [("MOVE", Direction.LEFT)])
        self.assertEqual(second.actions, [("MOVE", Direction.RIGHT)])

    def test_workers_receive_distinct_resource_intents(self):
        first = FakeActor("a-worker", (0, 0), cargo=0)
        second = FakeActor("b-worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=first, core=core, resources=frozenset({(-2, 0), (2, 0)}))
        turn.workers = (first, second)
        turn.units = (first, second)
        turn.state.population = 2
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(set(memory.resource_intents.values()), {(-2, 0), (2, 0)})
        self.assertEqual(first.actions, [("MOVE", Direction.LEFT)])
        self.assertEqual(second.actions, [("MOVE", Direction.RIGHT)])

    def test_stalled_resource_is_released_and_worker_explores(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        memory = TacticMemory(resource_intents={"worker": (4, 0)})

        for tick in range(10, 17):
            turn = make_turn(worker=worker, core=core, resources=frozenset({(4, 0)}))
            turn.tick = tick
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("worker", memory.resource_intents)
        self.assertGreater(memory.resource_cooldowns[(4, 0)], 16)
        self.assertEqual(worker.actions[-1], ("MOVE", Direction.LEFT))

    def test_unreachable_hidden_resource_is_forgotten_immediately(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (10, 10))
        target = (4, 0)
        turn = make_turn(
            worker=worker,
            core=core,
            obstacles=frozenset({target}),
        )
        memory = TacticMemory(
            resource_observation_memory={target: turn.tick},
            resource_intents={"worker": target},
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("worker", memory.resource_intents)
        self.assertNotIn(target, memory.resource_observation_memory)
        self.assertGreater(memory.resource_cooldowns[target], turn.tick)

    def test_scout_changes_route_after_three_non_improving_path_ticks(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        memory = TacticMemory()

        for tick in range(10, 15):
            turn = make_turn(worker=worker, core=core)
            turn.tick = tick
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions[0], ("MOVE", Direction.LEFT))
        self.assertEqual(worker.actions[2], ("MOVE", Direction.LEFT))
        self.assertEqual(memory.scout_progress["worker"].target, (32, 0))

    def test_scout_sweeps_outward_across_frontier_chunks_before_rotating(self):
        memory = TacticMemory()
        targets = []

        for tick in range(10, 14):
            target = memory.exploration_target(
                "worker",
                (0, 0),
                0,
                (0, 0),
                tick,
            )
            targets.append(target)
            memory.scout_chunk_last_seen[
                (target[0] // 32, target[1] // 32)
            ] = tick
            memory.advance_exploration("worker", 0)

        self.assertEqual(targets, [(-32, 0), (-64, 0), (-96, 0), (-128, 0)])

    def test_scout_restart_continues_from_persisted_frontier(self):
        memory = TacticMemory(
            scout_chunk_last_seen={
                (0, 0): 20,
                (-1, 0): 21,
                (-2, 0): 22,
            },
            exploration_anchor=(0, 0),
        )

        target = memory.exploration_target(
            "new-worker",
            (0, 0),
            0,
            (0, 0),
            23,
        )

        self.assertEqual(target, (-96, 0))

    def test_core_spawns_only_after_resource_and_population_checks(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        turn.resources = 5
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(max_population=2))

        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_unit_cost_uses_new_population_price_tiers(self):
        self.assertEqual(_unit_cost(UnitType.WORKER, 19), 5)
        for population in (20, 24):
            self.assertEqual(_unit_cost(UnitType.WORKER, population), 7)
            self.assertEqual(_unit_cost(UnitType.VANGUARD, population), 13)
            self.assertEqual(_unit_cost(UnitType.RANGER, population), 16)
        for population in (25, 29):
            self.assertEqual(_unit_cost(UnitType.WORKER, population), 8)
            self.assertEqual(_unit_cost(UnitType.VANGUARD, population), 17)
            self.assertEqual(_unit_cost(UnitType.RANGER, population), 20)

    def test_worker_first_expansion_costs_one_hundred_forty_nine_resources(self):
        expansion = (
            (19, UnitType.WORKER),
            (20, UnitType.WORKER),
            (21, UnitType.WORKER),
            (22, UnitType.WORKER),
            (23, UnitType.VANGUARD),
            (24, UnitType.VANGUARD),
            (25, UnitType.VANGUARD),
            (26, UnitType.RANGER),
            (27, UnitType.RANGER),
            (28, UnitType.RANGER),
            (29, UnitType.RANGER),
        )

        self.assertEqual(
            sum(_unit_cost(unit_type, population) for population, unit_type in expansion),
            149,
        )

    def test_core_uses_same_tick_worker_delivery_for_production(self):
        worker = FakeActor("worker", (0, 0), cargo=5)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        turn.resources = 0
        turn.resource_capacity = 10
        turn.resource_space = 10
        turn.state.population = 1

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(max_population=2),
        )

        self.assertEqual(worker.actions, [("DEPOSIT",)])
        self.assertEqual(core.actions, [("WAIT",)])
        self.assertEqual(report.pending_delivery, 5)
        self.assertEqual(report.available_resources, 5)
        self.assertEqual(report.production_cost, 5)

    def test_core_uses_dynamic_cost_after_twenty_living_units(self):
        workers = tuple(
            FakeActor(f"worker-{index}", (0, 0)) for index in range(20)
        )
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.units = workers
        turn.state.population = 20
        turn.resources = 7

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(
                max_population=21,
                spawn_unit_type=UnitType.WORKER,
                auto_production=False,
                worker_target=21,
            ),
        )

        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_default_roster_targets_are_sixteen_six_eight(self):
        config = AgentConfig()

        self.assertEqual(config.max_population, 0)
        self.assertFalse(config.population_expansion_enabled)
        self.assertEqual(config.expansion_resource_threshold, 150)
        self.assertEqual(config.expansion_casualty_buffer_units, 6)
        self.assertEqual(config.worker_target, 16)
        self.assertEqual(config.vanguard_target, 6)
        self.assertEqual(config.ranger_target, 8)
        self.assertEqual(config.beacon_policy, "RETREAT")

    def test_core_retreats_from_nearby_ground_beacon(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=None, core=core)
        turn.beacon = SimpleNamespace(position=(4, 0), status=BeaconStatus.GROUND)

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.lifecycle, "ACTIVE")
        self.assertEqual(report.mission, "SCOUT")
        self.assertEqual(core.actions, [("START_MOVE", Direction.LEFT)])

    def test_core_holds_position_when_beacon_policy_is_hold(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=None, core=core)
        turn.beacon = SimpleNamespace(position=(4, 0), status=BeaconStatus.GROUND)

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None, beacon_policy="HOLD"),
        )

        self.assertEqual(core.actions, [("WAIT",)])

    def test_retreat_policy_does_not_pull_workers_toward_ground_beacon(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        turn.beacon = SimpleNamespace(position=(4, 0), status=BeaconStatus.GROUND)

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None, beacon_policy="RETREAT"),
        )

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_missing_core_reports_respawning_recovery_lifecycle(self):
        turn = make_turn(worker=None, core=None)

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.lifecycle, "RESPAWNING")
        self.assertEqual(report.mission, "RECOVERY")

    def test_core_loss_event_reports_recovery_lifecycle(self):
        for event_type in ("CORE_LOST", "CORE_DESTROYED"):
            with self.subTest(event_type=event_type):
                core = FakeActor("core", (0, 0))
                turn = make_turn(worker=None, core=core)
                turn.events = (
                    SimpleNamespace(
                        event_type=event_type,
                        reason_code=None,
                        actor_id="enemy",
                        target_id="core",
                        position=(0, 0),
                    ),
                )

                report = plan_turn(
                    turn,
                    TacticMemory(),
                    AgentConfig(spawn_unit_type=None),
                )

                self.assertEqual(report.lifecycle, "RECOVERY")
                self.assertEqual(report.mission, "RECOVERY")

    def test_enemy_core_destroyed_event_does_not_report_recovery(self):
        core = FakeActor("core", (0, 0))
        ranger = FakeActor("ranger", (8, 4), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core)
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        turn.events = (
            SimpleNamespace(
                event_type="CORE_DESTROYED",
                reason_code=None,
                actor_id="ranger",
                target_id="enemy-core",
                position=(8, 4),
            ),
        )

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.lifecycle, "ACTIVE")
        self.assertNotEqual(report.mission, "RECOVERY")

    def test_enemy_core_resource_capture_does_not_report_recovery(self):
        core = FakeActor("core", (0, 0))
        ranger = FakeActor("ranger", (8, 4), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core)
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        turn.events = (
            SimpleNamespace(
                event_type="CORE_RESOURCES_CAPTURED",
                reason_code=None,
                actor_id="ranger",
                target_id="enemy-core",
                position=(8, 4),
                values={
                    "amount": 7,
                    "available": 10,
                    "destroyed": 3,
                    "capacity": 7,
                },
            ),
        )
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    id="enemy-core",
                    position=(8, 4),
                    hp=0,
                    shield=0,
                    state="NORMAL",
                    last_seen_tick=turn.tick - 1,
                )
            }
        )

        report = plan_turn(
            turn,
            memory,
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.lifecycle, "ACTIVE")
        self.assertNotEqual(report.mission, "RECOVERY")
        self.assertEqual(memory.recovery_until_tick, 0)
        self.assertNotIn("enemy-core", memory.enemy_core_memory)

    def test_automatic_production_moves_to_vanguard_after_worker_target(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(12))
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.units = workers
        turn.state.population = 12
        turn.resources = 10

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.VANGUARD)])

    def test_automatic_production_starts_defense_after_four_workers(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(4))
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.units = workers
        turn.state.population = 4
        turn.resources = 10

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.VANGUARD)])

    def test_automatic_production_adds_ranger_before_more_workers(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(8))
        vanguard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.vanguards = (vanguard,)
        turn.units = workers + (vanguard,)
        turn.state.population = 9
        turn.resources = 12

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.RANGER)])

    def test_automatic_production_moves_to_ranger_after_defense_target(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(12))
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (0, 0), unit_type=UnitType.VANGUARD)
            for index in range(3)
        )
        for vanguard in vanguards:
            vanguard.hp = 4
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.vanguards = vanguards
        turn.units = workers + vanguards
        turn.state.population = 15
        turn.resources = 12

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.RANGER)])

    def test_expansion_starts_with_workers_after_baseline_roster(self):
        turn, core = make_roster_turn(12, 3, 4, resources=20)

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_expansion_moves_to_vanguard_after_sixteen_workers(self):
        turn, core = make_roster_turn(16, 3, 4, resources=28)

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.VANGUARD)])

    def test_expansion_moves_to_ranger_after_six_vanguards(self):
        turn, core = make_roster_turn(16, 6, 4, resources=35)

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.RANGER)])

    def test_expansion_keeps_fifteen_resource_reserve(self):
        waiting_turn, waiting_core = make_roster_turn(12, 3, 4, resources=19)
        spawning_turn, spawning_core = make_roster_turn(12, 3, 4, resources=20)

        plan_turn(waiting_turn, TacticMemory(), AgentConfig())
        plan_turn(spawning_turn, TacticMemory(), AgentConfig())

        self.assertEqual(waiting_core.actions, [("WAIT",)])
        self.assertEqual(spawning_core.actions, [("SPAWN", UnitType.WORKER)])

    def test_baseline_loss_is_restored_before_worker_expansion_with_ten_reserve(self):
        turn, core = make_roster_turn(13, 2, 4, resources=20)

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.VANGUARD)])

    def test_twenty_ninth_unit_can_expand_to_strategy_cap(self):
        turn, core = make_roster_turn(16, 6, 7, resources=35)

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("SPAWN", UnitType.RANGER)])

    def test_custom_population_cap_stops_expansion(self):
        turn, core = make_roster_turn(13, 3, 4, resources=100)

        plan_turn(turn, TacticMemory(), AgentConfig(max_population=20))

        self.assertEqual(core.actions, [("WAIT",)])

    def test_custom_targets_below_baseline_stop_without_repeating(self):
        turn, core = make_roster_turn(8, 1, 1, resources=100)
        config = AgentConfig(
            max_population=30,
            worker_target=8,
            vanguard_target=1,
            ranger_target=1,
        )

        plan_turn(turn, TacticMemory(), config)

        self.assertEqual(core.actions, [("WAIT",)])

    def test_automatic_production_stops_at_full_roster(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(16))
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (0, 0), unit_type=UnitType.VANGUARD)
            for index in range(6)
        )
        rangers = tuple(
            FakeActor(f"ranger-{index}", (0, 0), unit_type=UnitType.RANGER)
            for index in range(8)
        )
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = workers + vanguards + rangers
        turn.state.population = 30
        turn.resources = 100

        plan_turn(turn, TacticMemory(), AgentConfig())

        self.assertEqual(core.actions, [("WAIT",)])

    def test_optional_population_expansion_waits_for_resource_threshold(self):
        turn, _ = make_roster_turn(16, 6, 8, resources=149)
        config = AgentConfig(population_expansion_enabled=True)

        self.assertIsNone(_choose_spawn_unit(turn, config))

        turn.resources = 150
        self.assertEqual(_choose_spawn_unit(turn, config), UnitType.WORKER)

    def test_optional_population_expansion_uses_same_tick_delivery(self):
        turn, _ = make_roster_turn(16, 6, 8, resources=149)
        config = AgentConfig(population_expansion_enabled=True)

        self.assertEqual(
            _choose_spawn_unit(turn, config, pending_delivery=1),
            UnitType.WORKER,
        )

    def test_optional_population_expansion_respects_finite_hard_cap(self):
        turn, _ = make_roster_turn(16, 6, 9, resources=153)
        config = AgentConfig(
            max_population=31,
            population_expansion_enabled=True,
        )

        self.assertIsNone(_choose_spawn_unit(turn, config))

    def test_optional_population_expansion_follows_eight_three_four_ratio(self):
        counts = [16, 6, 9]
        expected = [
            UnitType.WORKER,
            UnitType.VANGUARD,
            UnitType.WORKER,
            UnitType.WORKER,
            UnitType.RANGER,
            UnitType.WORKER,
            UnitType.VANGUARD,
            UnitType.WORKER,
            UnitType.RANGER,
        ]
        config = AgentConfig(population_expansion_enabled=True)
        actual = []
        for _ in expected:
            turn, _ = make_roster_turn(*counts, resources=1000)
            unit_type = _choose_spawn_unit(turn, config)
            actual.append(unit_type)
            counts[
                (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER).index(
                    unit_type
                )
            ] += 1

        self.assertEqual(actual, expected)

    def test_core_evades_a_close_enemy_before_production_or_repair(self):
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (3, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.resources = 5
        turn.state.population = 0

        plan_turn(turn, TacticMemory(), AgentConfig(max_population=2))

        self.assertEqual(len(core.actions), 1)
        self.assertEqual(core.actions[0][0], "START_MOVE")
        self.assertNotEqual(core.actions[0][1], Direction.RIGHT)

    def test_core_pauses_production_when_enemy_enters_alert_range(self):
        worker = FakeActor("worker", (0, 0))
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (10, 0), unit_type=UnitType.RANGER)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        core.hp = 5
        turn.resources = 5
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(max_population=2))

        self.assertEqual(core.actions, [("WAIT",)])

    def test_worker_evades_close_enemy_before_collecting(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (1, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(
            worker=worker,
            core=core,
            resources=frozenset({(0, -1)}),
            enemies=(enemy,),
        )

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.UP)])

    def test_worker_crosses_one_ranger_danger_cell_to_escape_firing_lane(self):
        worker = FakeActor("worker", (0, 1), cargo=0)
        core = FakeActor("core", (-10, -1))
        enemy = FakeActor("enemy", (0, 0), unit_type=UnitType.RANGER)
        turn = make_turn(
            worker=worker,
            core=core,
            enemies=(enemy,),
            obstacles=frozenset({(0, -1), (1, 1)}),
        )
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

        worker.actions.clear()
        worker.position = (-1, 1)
        turn.tick += 1
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_evading_scout_returns_to_core_before_resuming_exploration(self):
        worker = FakeActor("worker", (6, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (7, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        memory = TacticMemory(
            scout_progress={
                "worker": ScoutProgress(
                    (32, 0),
                    26,
                    last_position=(6, 0),
                    last_cost=26,
                )
            }
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        self.assertEqual(memory.scout_return_targets["worker"], (0, 0))
        self.assertEqual(memory.scout_progress["worker"].path_stalled_turns, 0)

        worker.actions.clear()
        worker.position = (5, 0)
        turn.visible_enemies = ()
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertNotIn("worker", memory.scout_return_targets)

        worker.actions.clear()
        worker.position = (2, 0)
        turn.tick = 12
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("worker", memory.scout_return_targets)
        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertEqual(memory.scout_progress["worker"].path_stalled_turns, 0)

    def test_injured_distant_scout_abandons_target_for_safe_return(self):
        worker = FakeActor("worker", (69, 0), cargo=0)
        worker.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory(
            scout_progress={
                "worker": ScoutProgress(
                    (96, 0),
                    27,
                    last_position=(69, 0),
                    last_cost=27,
                )
            }
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.scout_return_targets["worker"], (0, 0))
        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_dead_scout_return_entries_are_pruned_from_live_roster(self):
        worker = FakeActor("alive", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory(
            scout_return_targets={"alive": (0, 0), "dead": (0, 0)},
            scout_return_progress={
                "alive": ScoutReturnProgress((0, 0), 0, 10),
                "dead": ScoutReturnProgress((0, 0), 0, 10),
            },
        )

        memory.observe(turn)

        self.assertIn("alive", memory.scout_return_targets)
        self.assertNotIn("dead", memory.scout_return_targets)
        self.assertNotIn("dead", memory.scout_return_progress)

    def test_scout_return_releases_after_eight_ticks_without_route_improvement(self):
        memory = TacticMemory()
        target = (0, 0)

        self.assertFalse(memory.note_scout_return_progress("worker", target, 12, 10))
        for tick in range(11, 17):
            self.assertFalse(
                memory.note_scout_return_progress("worker", target, 12, tick)
            )
        self.assertTrue(
            memory.note_scout_return_progress("worker", target, 12, 18)
        )

    def test_returning_scout_can_take_local_resource_assignment(self):
        worker = FakeActor("worker", (4, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core, resources=frozenset({(4, 1)}))
        memory = TacticMemory(scout_return_targets={"worker": (0, 0)})

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.resource_intents.get("worker"), (4, 1))
        self.assertEqual(worker.actions, [("MOVE", Direction.DOWN)])

    def test_one_hp_worker_is_kept_in_return_behavior(self):
        worker = FakeActor("worker", (50, 0), cargo=0)
        worker.hp = 1
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.scout_return_targets.get("worker"), (0, 0))
        self.assertNotIn("worker", memory.scout_progress)

    def test_enemy_chunk_is_marked_as_scout_risk(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (32, 0), unit_type=UnitType.RANGER)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        memory = TacticMemory()

        memory.observe(turn)

        self.assertIn((1, 0), memory.scout_risk_chunks)

    def test_scout_return_avoids_recent_combat_danger_after_enemy_disappears(self):
        worker = FakeActor("worker", (6, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (5, 1), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        plan_turn(turn, memory, config)

        self.assertEqual(memory.scout_return_targets["worker"], (0, 0))
        self.assertIn((5, 0), memory.recent_worker_return_danger(turn.tick))

        worker.actions.clear()
        turn.tick += 1
        turn.visible_enemies = ()
        plan_turn(turn, memory, config)

        self.assertEqual(worker.actions[0][0], "MOVE")
        self.assertNotEqual(worker.actions[0][1], Direction.LEFT)

        worker.actions.clear()
        turn.tick = 10 + 6 + 1
        memory.worker_routes.clear()
        plan_turn(turn, memory, config)

        self.assertEqual(memory.recent_worker_return_danger(turn.tick), set())
        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])

    def test_enemy_worker_alone_does_not_create_return_danger(self):
        worker = FakeActor("worker", (6, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy-worker", (5, 1), unit_type=UnitType.WORKER)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("worker", memory.scout_return_targets)
        self.assertEqual(memory.recent_worker_return_danger(turn.tick), set())

    def test_own_attack_does_not_enter_engaged_threat_state(self):
        worker = FakeActor("worker", (0, 0))
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0))
        event = SimpleNamespace(
            event_type="ATTACK_SUCCEEDED",
            reason_code=None,
            actor_id="ranger",
            target_id="enemy",
            position=(0, 0),
        )
        turn = make_turn(worker=worker, core=core)
        turn.rangers = (ranger,)
        turn.units = (worker, ranger)
        turn.events = (event,)
        core.hp = 5
        turn.resources = 5
        turn.state.population = 2

        report = plan_turn(turn, TacticMemory(), AgentConfig(max_population=3))

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_attack_targeting_core_enters_engaged_threat_state(self):
        core = FakeActor("core", (0, 0))
        event = SimpleNamespace(
            event_type="DAMAGE_APPLIED",
            reason_code=None,
            actor_id="enemy",
            target_id="core",
            position=(0, 0),
        )
        turn = make_turn(worker=None, core=core)
        turn.events = (event,)

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "ENGAGED")
        self.assertEqual(core.actions, [("WAIT",)])

    def test_distant_worker_attack_does_not_escalate_core_or_clear_cargo_lane(self):
        cargo = FakeActor("cargo", (2, 0), cargo=1)
        scout = FakeActor("scout", (45, 0), cargo=0)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        enemies = (
            FakeActor("enemy-1", (45, 1), unit_type=UnitType.RANGER),
            FakeActor("enemy-2", (46, 0), unit_type=UnitType.RANGER),
            FakeActor("enemy-3", (46, 1), unit_type=UnitType.RANGER),
        )
        event = SimpleNamespace(
            event_type="UNIT_DAMAGED",
            reason_code="ATTACK",
            actor_id=None,
            target_id="scout",
            position=(45, 0),
        )
        turn = make_turn(worker=cargo, core=core, enemies=enemies)
        turn.workers = (cargo, scout)
        turn.units = (cargo, scout)
        turn.events = (event,)
        turn.state.population = 2
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="cargo",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick - 1,
                last_planned_tick=turn.tick - 1,
            )
        )

        report = plan_turn(
            turn,
            memory,
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertEqual(report.threat_reason, "NONE")
        self.assertEqual(report.mission, "ECONOMY")
        self.assertTrue(memory.cargo_lane.active)
        self.assertEqual(memory.cargo_lane.owner_id, "cargo")
        self.assertFalse(any(action[0] == "START_MOVE" for action in core.actions))
        self.assertIn((45, 0), memory.recent_worker_return_danger(turn.tick))
        self.assertEqual(memory.scout_return_targets["scout"], (0, 0))

    def test_recent_attack_inside_alert_range_does_not_move_core(self):
        worker = FakeActor("worker", (10, 0), cargo=0)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        enemy = FakeActor("enemy", (11, 0), unit_type=UnitType.RANGER)
        event = SimpleNamespace(
            event_type="UNIT_DAMAGED",
            reason_code="ATTACK",
            actor_id=None,
            target_id="worker",
            position=(10, 0),
        )
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        turn.events = (event,)

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "ALERT")
        self.assertEqual(report.threat_reason, "RECENT_ATTACK")
        self.assertFalse(any(action[0] == "START_MOVE" for action in core.actions))

    def test_recent_attack_inside_evade_range_still_moves_core(self):
        worker = FakeActor("worker", (7, 0), cargo=0)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        enemy = FakeActor("enemy", (8, 0), unit_type=UnitType.RANGER)
        event = SimpleNamespace(
            event_type="UNIT_DAMAGED",
            reason_code="ATTACK",
            actor_id=None,
            target_id="worker",
            position=(7, 0),
        )
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        turn.events = (event,)

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "ENGAGED")
        self.assertEqual(report.threat_reason, "RECENT_ATTACK")
        self.assertEqual(core.actions[0][0], "START_MOVE")
        self.assertNotEqual(core.actions[0][1], Direction.RIGHT)

    def test_recent_attack_distance_drives_emergency_spawn_with_only_far_enemy(self):
        worker = FakeActor("worker", (5, 0), cargo=0)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        enemy = FakeActor("enemy", (20, 0), unit_type=UnitType.RANGER)
        event = SimpleNamespace(
            event_type="UNIT_DAMAGED",
            reason_code="ATTACK",
            actor_id=None,
            target_id="worker",
            position=(5, 0),
        )
        turn = make_turn(
            worker=worker,
            core=core,
            enemies=(enemy,),
            obstacles=frozenset({(0, -1), (1, 0), (0, 1), (-1, 0)}),
        )
        turn.events = (event,)
        turn.resources = 12
        turn.resource_capacity = 20
        turn.state.population = 1

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(max_population=2),
        )

        self.assertEqual(report.threat_level, "ENGAGED")
        self.assertEqual(report.threat_reason, "RECENT_ATTACK")
        self.assertEqual(core.actions, [("SPAWN", UnitType.RANGER)])

    def test_enemy_approach_inside_sixteen_tick_horizon_preemptively_evades(self):
        worker = FakeActor("worker", (0, 0))
        core = FakeActor("core", (0, 0))
        enemy = FakeActor("enemy", (20, 0), unit_type=UnitType.RANGER)
        memory = TacticMemory()
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))
        core.hp = 5
        turn.resources = 5
        turn.state.population = 1
        plan_turn(turn, memory, AgentConfig(max_population=2))

        core.actions.clear()
        enemy.position = (19, 0)
        turn.tick = 11
        report = plan_turn(turn, memory, AgentConfig(max_population=2))

        self.assertEqual(report.threat_level, "PRE_EVADE")
        self.assertEqual(report.threat_reason, "TIME_TO_RANGE")
        self.assertEqual(core.actions[0][0], "START_MOVE")

        core.actions.clear()
        turn.visible_enemies = ()
        turn.tick = 12
        report = plan_turn(turn, memory, AgentConfig(max_population=2))

        self.assertEqual(report.threat_level, "PRE_EVADE")
        self.assertEqual(core.actions[0][0], "START_MOVE")

    def test_distant_pursuer_outside_horizon_does_not_move_core(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy = FakeActor("enemy", (50, 0), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.resources = 5
        turn.state.population = 0
        memory = TacticMemory()
        config = AgentConfig(max_population=1)

        for tick, enemy_x in ((10, 50), (11, 49), (12, 48)):
            turn.tick = tick
            enemy.position = (enemy_x, 0)
            core.actions.clear()
            report = plan_turn(turn, memory, config)

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertEqual(report.mission, "SCOUT")
        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_core_escape_does_not_step_toward_threat_in_dead_end(self):
        enemy = FakeActor("enemy", (-10, 0), unit_type=UnitType.VANGUARD)

        direction = _core_escape_direction(
            (0, 0),
            [enemy],
            {(0, -1), (1, 0), (0, 1)},
        )

        self.assertIsNone(direction)

    def test_worker_escape_prefers_core_when_safe_directions_tie(self):
        worker_position = (-178, 649)
        core_position = (-217, 664)
        enemy = FakeActor("enemy", (-175, 649), unit_type=UnitType.RANGER)
        obstacles = {(-178, 648)}

        direction = _worker_escape_direction(
            worker_position,
            [enemy],
            obstacles,
            obstacles,
            core_position,
            2,
        )

        self.assertEqual(direction, Direction.LEFT)

    def test_core_move_start_suppresses_same_tick_worker_deposit(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory(
            enemy_motion_memory={
                "enemy": EnemyMotion(
                    position=(3, 0),
                    last_tick=10,
                    core_distance=3,
                    unit_type=UnitType.VANGUARD,
                    pursuit_score=4,
                    activity_until_tick=10,
                    ticks_to_attack_range=2,
                )
            }
        )

        report = plan_turn(
            turn,
            memory,
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertEqual(core.actions[0][0], "START_MOVE")
        self.assertEqual(report.pending_delivery, 0)

    def test_enemy_worker_does_not_trigger_worker_or_core_retreat(self):
        worker = FakeActor("worker", (6, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy_worker = FakeActor(
            "enemy-worker",
            (7, 0),
            unit_type=UnitType.WORKER,
        )
        turn = make_turn(worker=worker, core=core, enemies=(enemy_worker,))
        memory = TacticMemory()

        report = plan_turn(
            turn,
            memory,
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertNotIn("worker", memory.scout_return_targets)
        self.assertFalse(any(action[0] == "START_MOVE" for action in core.actions))

        worker.actions.clear()
        core.actions.clear()
        enemy_worker.position = (6, 0)
        turn.tick = 11
        report = plan_turn(
            turn,
            memory,
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertNotIn("worker", memory.scout_return_targets)
        self.assertFalse(any(action[0] == "START_MOVE" for action in core.actions))

    def test_enemy_core_does_not_trigger_defensive_retreat(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(1, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "NORMAL")
        self.assertEqual(core.actions, [("WAIT",)])

    def test_core_keeps_eight_tick_caution_after_combat_enemy_disappears(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy = FakeActor("enemy", (10, 0), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.resources = 5
        turn.state.population = 0
        memory = TacticMemory()
        config = AgentConfig(max_population=1)

        plan_turn(turn, memory, config)
        core.actions.clear()
        turn.visible_enemies = ()
        turn.tick = 11
        plan_turn(turn, memory, config)

        self.assertEqual(core.actions, [("WAIT",)])

        core.actions.clear()
        turn.tick = 19
        plan_turn(turn, memory, config)

        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])

    def test_worker_evade_step_avoids_ranger_attack_line(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (10, 0))
        core.hp = 5
        enemy = FakeActor("enemy", (0, 3), unit_type=UnitType.RANGER)
        turn = make_turn(worker=worker, core=core, enemies=(enemy,))

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_core_emergency_spawns_vanguard_when_escape_is_blocked(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy = FakeActor("enemy", (2, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(
            worker=None,
            core=core,
            enemies=(enemy,),
            obstacles=frozenset({(0, -1), (1, 0), (0, 1), (-1, 0)}),
        )
        turn.resources = 10
        turn.state.population = 0

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(max_population=2),
        )

        self.assertEqual(core.actions, [("SPAWN", UnitType.VANGUARD)])

    def test_moving_core_cancels_destination_with_worse_projected_damage(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        core.view.state = CoreState.MOVING
        core.view.destination = (0, 1)
        enemy = FakeActor("enemy", (0, 4), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(core.actions, [("CANCEL_MOVE",)])

    def test_multi_axis_enemy_pressure_enters_breakout_state(self):
        core = FakeActor("core", (0, 0))
        first_enemy = FakeActor("enemy-a", (6, 0), unit_type=UnitType.RANGER)
        second_enemy = FakeActor("enemy-b", (0, 6), unit_type=UnitType.RANGER)
        turn = make_turn(
            worker=None,
            core=core,
            enemies=(first_enemy, second_enemy),
        )
        core.hp = 5
        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(report.threat_level, "BREAKOUT")
        self.assertEqual(core.actions[0][0], "START_MOVE")

    def test_vanguard_sweeps_adjacent_enemy_instead_of_waiting(self):
        core = FakeActor("core", (0, 0))
        vanguard = FakeActor("vanguard", (1, 0), unit_type=UnitType.VANGUARD)
        vanguard.hp = 4
        enemy = FakeActor("enemy", (2, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.vanguards = (vanguard,)
        turn.units = (vanguard,)
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(vanguard.actions, [("SWEEP", Direction.RIGHT)])

    def test_ranger_shoots_legal_enemy_without_chasing(self):
        core = FakeActor("core", (0, 0))
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy = FakeActor("enemy", (0, 2), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(ranger.actions, [("SHOOT", "enemy")])

    def test_ranger_prioritizes_ranger_threat_over_nearer_vanguard(self):
        core = FakeActor("core", (0, 0))
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy_vanguard = FakeActor(
            "enemy-vanguard",
            (0, 1),
            unit_type=UnitType.VANGUARD,
        )
        enemy_ranger = FakeActor(
            "enemy-ranger",
            (0, 3),
            unit_type=UnitType.RANGER,
        )
        turn = make_turn(
            worker=None,
            core=core,
            enemies=(enemy_vanguard, enemy_ranger),
        )
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(ranger.actions, [("SHOOT", "enemy-ranger")])

    def test_one_hp_ranger_uses_safer_exit_instead_of_shooting(self):
        core = FakeActor("core", (-5, 0))
        core.hp = 5
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        ranger.hp = 1
        enemy = FakeActor("enemy-ranger", (0, 3), unit_type=UnitType.RANGER)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(ranger.actions, [("MOVE", Direction.LEFT)])
        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))

    def test_two_hp_vanguard_uses_safer_exit_instead_of_sweeping(self):
        core = FakeActor("core", (-5, 0))
        core.hp = 5
        vanguard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        vanguard.hp = 2
        enemy = FakeActor("enemy-vanguard", (1, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.vanguards = (vanguard,)
        turn.units = (vanguard,)
        turn.state.population = 1

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(vanguard.actions, [("MOVE", Direction.LEFT)])
        self.assertFalse(any(action[0] == "SWEEP" for action in vanguard.actions))

    def test_critical_units_last_attack_when_no_safer_exit_exists(self):
        for unit_type, action_name in (
            (UnitType.VANGUARD, "SWEEP"),
            (UnitType.RANGER, "SHOOT"),
        ):
            with self.subTest(unit_type=unit_type):
                core = FakeActor("core", (-5, 0))
                core.hp = 5
                defender = FakeActor("defender", (0, 0), unit_type=unit_type)
                defender.hp = 2 if unit_type == UnitType.VANGUARD else 1
                enemy = FakeActor(
                    "enemy-vanguard",
                    (1, 0),
                    unit_type=UnitType.VANGUARD,
                )
                turn = make_turn(
                    worker=None,
                    core=core,
                    enemies=(enemy,),
                    obstacles=frozenset({(-1, 0), (0, -1), (0, 1)}),
                )
                if unit_type == UnitType.VANGUARD:
                    turn.vanguards = (defender,)
                else:
                    turn.rangers = (defender,)
                turn.units = (defender,)
                turn.state.population = 1

                plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

                self.assertEqual(defender.actions[0][0], action_name)
                self.assertFalse(any(action[0] == "MOVE" for action in defender.actions))

    def test_damaged_defender_returns_to_core_and_heals_with_backup(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        damaged = FakeActor("a-damaged", (2, 0), unit_type=UnitType.VANGUARD)
        damaged.hp = 1
        healthy = FakeActor("b-healthy", (0, 3), unit_type=UnitType.VANGUARD)
        healthy.hp = 4
        turn = make_turn(worker=None, core=core)
        turn.vanguards = (damaged, healthy)
        turn.units = (damaged, healthy)
        turn.state.population = 2
        turn.resources = 13
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(damaged.actions, [("MOVE", Direction.LEFT)])

        damaged.actions.clear()
        healthy.actions.clear()
        damaged.position = (0, 0)
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(damaged.actions, [("HEAL",)])

    def test_ranger_clears_a_confirmed_stationary_target(self):
        core = FakeActor("core", (10, 10))
        core.hp = 5
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy = FakeActor("enemy", (0, 2), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        ranger.actions.clear()
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(ranger.actions, [("SHOOT", "enemy")])

    def test_stationary_enemy_core_requires_specialized_raid(self):
        core = FakeActor("core", (10, 10))
        core.hp = 5
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(0, 2),
            hp=5,
            shield=5,
            owner_username="enemy",
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        ranger.actions.clear()
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))
        self.assertEqual(memory.raid.state, "CORE_TARGET_MEMORY")

    def test_vanguard_stationary_clear_moves_to_empty_attack_cell(self):
        core = FakeActor("core", (10, 10), shield=5)
        core.hp = 5
        guard = FakeActor("vanguard-a", (10, 9), unit_type=UnitType.VANGUARD)
        clearer = FakeActor("vanguard-b", (0, 0), unit_type=UnitType.VANGUARD)
        guard.hp = clearer.hp = 4
        enemy = FakeActor("enemy-worker", (2, 0), unit_type=UnitType.WORKER)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.vanguards = (guard, clearer)
        turn.units = (guard, clearer)
        turn.state.population = 2
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        plan_turn(turn, memory, config)
        for unit in turn.units:
            unit.actions.clear()
        turn.tick = 11
        plan_turn(turn, memory, config)

        self.assertEqual(clearer.actions, [("MOVE", Direction.RIGHT)])
        self.assertEqual(memory.planned_unit_destinations["vanguard-b"], (1, 0))
        self.assertEqual(memory.planned_unit_targets["vanguard-b"], (1, 0))
        self.assertEqual(memory.planned_stationary_targets["vanguard-b"], (2, 0))

    def test_stationary_worker_on_enemy_core_is_not_cleared_as_normal_target(self):
        core = FakeActor("core", (10, 10), shield=5)
        core.hp = 5
        guard = FakeActor("vanguard-a", (10, 9), unit_type=UnitType.VANGUARD)
        clearer = FakeActor("vanguard-b", (1, 0), unit_type=UnitType.VANGUARD)
        guard.hp = clearer.hp = 4
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(2, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        enemy_worker = FakeActor(
            "enemy-worker",
            (2, 0),
            unit_type=UnitType.WORKER,
        )
        turn = make_turn(
            worker=None,
            core=core,
            enemies=(enemy_core, enemy_worker),
        )
        turn.vanguards = (guard, clearer)
        turn.units = (guard, clearer)
        turn.state.population = 2
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        plan_turn(turn, memory, config)
        for unit in turn.units:
            unit.actions.clear()
        turn.tick = 11
        plan_turn(turn, memory, config)

        self.assertFalse(any(action[0] == "SWEEP" for action in clearer.actions))
        self.assertNotIn("vanguard-b", memory.planned_stationary_targets)

    def test_moving_enemy_cancels_stationary_clearance(self):
        core = FakeActor("core", (10, 10))
        core.hp = 5
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy = FakeActor("enemy", (0, 2), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        ranger.actions.clear()
        enemy.position = (1, 2)
        turn.tick = 11
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))

    def test_stationary_confirmation_requires_consecutive_ticks(self):
        core = FakeActor("core", (10, 10))
        core.hp = 5
        ranger = FakeActor("ranger", (0, 0), unit_type=UnitType.RANGER)
        enemy = FakeActor("enemy", (0, 2), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        ranger.actions.clear()
        turn.tick = 20
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))

    def test_complete_route_avoids_real_concave_pocket(self):
        obstacles = {
            (-263, 662),
            (-262, 663),
            (-263, 664),
        }

        route = _complete_route(
            (-264, 663),
            (-258, 663),
            obstacles,
            max_expansions=ROUTE_MAX_EXPANSIONS,
        )

        self.assertEqual(route.status, "SUCCESS")
        self.assertNotEqual(route.path[1], (-263, 663))
        self.assertLess(len(route.explored), ROUTE_MAX_EXPANSIONS)

    def test_complete_route_distinguishes_budget_and_unreachable(self):
        budget = _complete_route((0, 0), (10, 0), set(), max_expansions=1)
        unreachable = _complete_route((0, 0), (1, 0), {(1, 0)})

        self.assertEqual(budget.status, "BUDGET_EXCEEDED")
        self.assertEqual(unreachable.status, "UNREACHABLE")

    def test_tick_89304_closed_pocket_has_admitted_cargo_and_does_not_trigger(self):
        obstacles = {
            (-225, 667), (-223, 669), (-221, 660), (-221, 662),
            (-221, 670), (-220, 664), (-220, 666), (-219, 659),
            (-219, 665), (-219, 669), (-217, 670), (-216, 665),
            (-216, 671), (-215, 659), (-215, 665), (-215, 670),
            (-214, 672), (-213, 660), (-213, 663), (-212, 666),
            (-212, 670), (-211, 661), (-211, 668), (-211, 669),
            (-211, 673), (-210, 670),
        }
        workers = (
            ("0f13adab", (-179, 658), 0),
            ("626fe5a3", (-156, 661), 1),
            ("6553c3ed", (-240, 630), 0),
            ("656d3944", (-189, 668), 0),
            ("8bb2ffda", (-225, 656), 0),
            ("9a2a356b", (-228, 650), 0),
            ("a975bdda", (-220, 541), 0),
            ("b722cb72", (-217, 664), 1),
            ("b865048a", (-202, 663), 0),
            ("c3ca0560", (-217, 659), 1),
            ("ca727ec5", (-216, 662), 0),
            ("cee8bd78", (-224, 687), 0),
            ("d3139ba8", (-225, 659), 1),
            ("dd996335", (-211, 672), 0),
            ("f0c09f3e", (-215, 664), 0),
        )
        turn = make_pocket_snapshot(
            89304,
            obstacles,
            workers,
            self.POCKET_GUARDS,
        )
        memory = TacticMemory()

        pocket = _update_core_pocket(
            turn,
            (-217, 665),
            obstacles,
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
        )

        self.assertTrue(pocket.closed)
        self.assertEqual(len(pocket.component), 7)
        self.assertEqual(pocket.admitted_cargo_ids, frozenset({"b722cb72"}))
        self.assertEqual(pocket.consecutive_ticks, 0)
        self.assertFalse(pocket.blocked)

    def test_ticks_89474_89475_activate_exact_nine_cell_pocket(self):
        turn_89474, turn_89475, obstacles_89474, obstacles_89475 = (
            make_blocked_pocket_snapshots(self.POCKET_GUARDS)
        )
        memory = TacticMemory()
        first = _update_core_pocket(
            turn_89474,
            (-217, 665),
            obstacles_89474,
            _friendly_cell_occupancy(turn_89474),
            memory,
            core_accepts_delivery=True,
        )
        second = _update_core_pocket(
            turn_89475,
            (-217, 665),
            obstacles_89475,
            _friendly_cell_occupancy(turn_89475),
            memory,
            core_accepts_delivery=True,
        )

        self.assertEqual(first.consecutive_ticks, 1)
        self.assertFalse(first.blocked)
        self.assertEqual(
            second.component,
            frozenset(
                {
                    (-219, 663), (-219, 664), (-219, 666),
                    (-218, 663), (-218, 665), (-218, 666),
                    (-217, 663), (-217, 664), (-217, 665),
                }
            ),
        )
        self.assertEqual(second.admitted_cargo_ids, frozenset())
        self.assertEqual(len(second.static_reachable_cargo_ids), 7)
        self.assertEqual(second.consecutive_ticks, 2)
        self.assertTrue(second.blocked)

    def test_active_cargo_lane_suppresses_controller_induced_pocket(self):
        first_turn, second_turn, first_obstacles, second_obstacles = (
            make_blocked_pocket_snapshots(self.POCKET_GUARDS)
        )
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                core_position=(-217, 665),
                path=((-217, 665), (-217, 664), (-216, 664), (-215, 664)),
                gateway=(-215, 664),
            )
        )

        first = _update_core_pocket(
            first_turn,
            (-217, 665),
            first_obstacles,
            _friendly_cell_occupancy(first_turn),
            memory,
            core_accepts_delivery=True,
        )
        second = _update_core_pocket(
            second_turn,
            (-217, 665),
            second_obstacles,
            _friendly_cell_occupancy(second_turn),
            memory,
            core_accepts_delivery=True,
        )

        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(second.consecutive_ticks, 0)
        self.assertFalse(second.blocked)

    def test_real_pocket_lane_delivers_all_cargo_without_move_contention(self):
        turn_89474, turn, obstacles_89474, _ = make_blocked_pocket_snapshots(
            self.POCKET_GUARDS
        )
        memory = TacticMemory()
        first = _update_core_pocket(
            turn_89474,
            (-217, 665),
            obstacles_89474,
            _friendly_cell_occupancy(turn_89474),
            memory,
            core_accepts_delivery=True,
        )
        self.assertEqual(first.consecutive_ticks, 1)

        deposit_ticks = []
        deposit_ids = []
        deposit_egress_ids = []
        lane_history = []
        config = AgentConfig(spawn_unit_type=None)
        for _ in range(96):
            action_tick = turn.tick
            plan_turn(turn, memory, config)
            workers_by_id = {str(worker.id): worker for worker in turn.workers}
            lane_history.append(
                (
                    action_tick,
                    memory.cargo_lane.phase,
                    memory.cargo_lane.owner_id,
                    memory.cargo_lane.queued_owner_id,
                    memory.cargo_lane.departing_worker_id,
                    (
                        workers_by_id[memory.cargo_lane.owner_id].position
                        if memory.cargo_lane.owner_id in workers_by_id
                        else None
                    ),
                    tuple(sorted(memory.cargo_lane.yield_worker_ids)),
                    {
                        worker_id: workers_by_id[worker_id].position
                        for worker_id in memory.cargo_lane.yield_worker_ids
                        if worker_id in workers_by_id
                    },
                    dict(memory.cargo_lane.stage_targets),
                    dict(memory.route_diagnostics),
                )
            )
            if action_tick == 89475:
                self.assertTrue(memory.cargo_lane.active)
                self.assertEqual(memory.cargo_lane.owner_id, "cee8bd78")
                self.assertEqual(
                    memory.cargo_lane.path,
                    (
                        (-217, 665),
                        (-217, 664),
                        (-216, 664),
                        (-215, 664),
                    ),
                )
                self.assertEqual(
                    memory.cargo_lane.gateway,
                    memory.cargo_lane.path[-1],
                )
                self.assertEqual(
                    memory.cargo_lane.yield_worker_ids,
                    {"656d3944", "b722cb72"},
                )
                blocker = workers_by_id["656d3944"]
                self.assertEqual(blocker.actions[0][0], "MOVE")
            _, deposited = apply_synchronous_actions(turn)
            if deposited:
                deposit_ticks.extend([action_tick] * len(deposited))
                deposit_ids.extend(deposited)
                deposit_egress_ids.extend(deposited)
            if len(deposit_ids) == 7:
                break

        remaining = {
            str(worker.id): worker.position
            for worker in turn.workers
            if worker.cargo > 0
        }
        self.assertEqual(
            len(set(deposit_ids)),
            7,
            f"deposit_ids={deposit_ids}, deposit_ticks={deposit_ticks}, "
            f"remaining={remaining}, lane={memory.cargo_lane}, "
            f"history={lane_history}",
        )
        self.assertLessEqual(
            deposit_ticks[0],
            89483,
            f"deposit_ticks={deposit_ticks}, history={lane_history[:20]}",
        )
        intervals = [
            current - previous
            for previous, current in zip(deposit_ticks, deposit_ticks[1:])
        ]
        self.assertTrue(
            all(7 <= interval <= 14 for interval in intervals),
            f"deposit_ticks={deposit_ticks}, intervals={intervals}, "
            f"history={lane_history}",
        )
        for deposited_id, deposit_tick in zip(deposit_egress_ids[:-1], deposit_ticks):
            following = [
                row
                for row in lane_history
                if row[0] > deposit_tick and row[4] == deposited_id
            ]
            self.assertTrue(following, f"missing egress for {deposited_id}")
            self.assertTrue(
                all(row[2] is None for row in following),
                f"new owner admitted before {deposited_id} egressed: {following}",
            )

    def test_stalled_admitted_cargo_stops_suppressing_pocket(self):
        cargo = FakeActor("cargo", (0, -1), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        obstacles = {(1, 0), (0, 1), (-1, 0)}
        turn = make_turn(
            worker=cargo,
            core=core,
            obstacles=frozenset(obstacles),
        )
        memory = TacticMemory()

        observations = []
        for tick in range(10, 14):
            turn.tick = tick
            observations.append(
                _update_core_pocket(
                    turn,
                    (0, 0),
                    obstacles,
                    _friendly_cell_occupancy(turn),
                    memory,
                    core_accepts_delivery=True,
                )
            )

        self.assertEqual(observations[1].admitted_cargo_ids, frozenset({"cargo"}))
        self.assertEqual(
            observations[2].stalled_admitted_cargo_ids,
            frozenset({"cargo"}),
        )
        self.assertEqual(observations[2].consecutive_ticks, 1)
        self.assertTrue(observations[3].blocked)

    def test_worker_route_cache_reuses_valid_remaining_path(self):
        memory = TacticMemory()
        first_direction, first_result = _cached_worker_route_step(
            memory,
            "worker",
            "CARGO_RETURN",
            (0, 0),
            (4, 0),
            set(),
        )
        second_direction, second_result = _cached_worker_route_step(
            memory,
            "worker",
            "CARGO_RETURN",
            (1, 0),
            (4, 0),
            set(),
        )

        self.assertEqual(first_direction, Direction.RIGHT)
        self.assertIsNotNone(first_result)
        self.assertEqual(second_direction, Direction.RIGHT)
        self.assertIsNone(second_result)

        third_direction, third_result = _cached_worker_route_step(
            memory,
            "worker",
            "CARGO_RETURN",
            (2, 0),
            (4, 0),
            {(3, 0)},
        )

        self.assertNotEqual(third_direction, Direction.RIGHT)
        self.assertIsNotNone(third_result)

    def test_move_contested_temporarily_routes_worker_around_destination(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (3, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        plan_turn(turn, memory, config)

        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])
        self.assertEqual(memory.planned_worker_destinations["worker"], (1, 0))

        worker.actions.clear()
        turn.tick += 1
        turn.events = (
            SimpleNamespace(
                event_type="UNIT_MOVE_FAILED",
                reason_code="MOVE_CONTESTED",
                actor_id="worker",
                target_id="enemy-worker",
                position=None,
            ),
        )
        plan_turn(turn, memory, config)

        self.assertNotEqual(worker.actions, [("MOVE", Direction.RIGHT)])
        self.assertEqual(
            memory.contested_worker_positions("worker", turn.tick),
            {(1, 0)},
        )

        worker.actions.clear()
        turn.tick += MOVE_CONTESTED_AVOID_TICKS
        turn.events = ()
        memory.worker_routes.clear()
        plan_turn(turn, memory, config)

        self.assertEqual(memory.contested_worker_positions("worker", turn.tick), set())
        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_queue_move_keeps_enemy_target_as_a_hard_blocker(self):
        vanguard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        moved = _queue_move(
            vanguard,
            (1, 0),
            {(1, 0), (-1, 0), (0, -1), (0, 1)},
            {},
            friendly_occupancy={(0, 0): 1},
        )

        self.assertFalse(moved)
        self.assertEqual(vanguard.actions, [])

    def test_historical_stationary_scene_never_moves_into_enemy_occupied_cell(self):
        vanguard = FakeActor(
            "vanguard",
            (-213, 633),
            unit_type=UnitType.VANGUARD,
        )
        enemy_positions = {
            (-213, 634),
            (-212, 633),
            (-212, 634),
        }

        moved = _queue_move(
            vanguard,
            (-212, 634),
            enemy_positions,
            {},
            friendly_occupancy={(-213, 633): 1},
        )

        if moved:
            destination = _next_position(vanguard.position, vanguard.actions[0][1])
            self.assertNotIn(destination, enemy_positions)
        else:
            self.assertEqual(vanguard.actions, [])

    def test_combat_move_failure_avoids_destination_and_cools_stationary_target(self):
        vanguard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        memory = TacticMemory()
        self.assertTrue(
            _queue_move(
                vanguard,
                (1, 0),
                set(),
                {},
                friendly_occupancy={(0, 0): 1},
                memory=memory,
                tick=10,
                stationary_target=(2, 0),
            )
        )
        turn = make_turn(worker=None)
        turn.tick = 11
        turn.units = (vanguard,)
        turn.vanguards = (vanguard,)
        turn.events = (
            SimpleNamespace(
                event_type="UNIT_MOVE_FAILED",
                reason_code="MOVE_DESTINATION_OCCUPIED",
                actor_id="vanguard",
                target_id="enemy-worker",
                position=None,
            ),
        )

        memory.observe(turn)

        self.assertEqual(
            memory.contested_unit_positions("vanguard", turn.tick),
            {(1, 0)},
        )
        self.assertEqual(
            memory.stationary_clear_cooldowns[(2, 0)],
            turn.tick + MOVE_OCCUPIED_AVOID_TICKS,
        )
        vanguard.actions.clear()
        self.assertTrue(
            _queue_move(
                vanguard,
                (1, 1),
                set(),
                {},
                friendly_occupancy={(0, 0): 1},
                memory=memory,
                tick=turn.tick,
            )
        )
        self.assertNotEqual(vanguard.actions, [("MOVE", Direction.RIGHT)])

    def test_cell_unit_limit_avoids_worker_destination_for_eight_ticks(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (3, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        plan_turn(turn, memory, config)
        self.assertEqual(memory.planned_worker_destinations["worker"], (1, 0))

        worker.actions.clear()
        turn.tick = 11
        turn.events = (
            SimpleNamespace(
                event_type="UNIT_MOVE_FAILED",
                reason_code="CELL_UNIT_LIMIT",
                actor_id="worker",
                target_id=None,
                position=None,
            ),
        )
        plan_turn(turn, memory, config)

        self.assertEqual(
            memory.contested_worker_positions("worker", turn.tick),
            {(1, 0)},
        )
        self.assertNotEqual(worker.actions, [("MOVE", Direction.RIGHT)])

        worker.actions.clear()
        turn.tick += MOVE_OCCUPIED_AVOID_TICKS
        turn.events = ()
        memory.worker_routes.clear()
        plan_turn(turn, memory, config)

        self.assertEqual(memory.contested_worker_positions("worker", turn.tick), set())
        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_cell_unit_limit_cools_combat_stationary_target(self):
        vanguard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        memory = TacticMemory()
        self.assertTrue(
            _queue_move(
                vanguard,
                (1, 0),
                set(),
                {},
                friendly_occupancy={(0, 0): 1},
                memory=memory,
                tick=10,
                stationary_target=(2, 0),
            )
        )
        turn = make_turn(worker=None)
        turn.tick = 11
        turn.units = (vanguard,)
        turn.vanguards = (vanguard,)
        turn.events = (
            SimpleNamespace(
                event_type="UNIT_MOVE_FAILED",
                reason_code="CELL_UNIT_LIMIT",
                actor_id="vanguard",
                target_id="enemy-worker",
                position=None,
            ),
        )

        memory.observe(turn)

        self.assertEqual(
            memory.contested_unit_positions("vanguard", turn.tick),
            {(1, 0)},
        )
        self.assertEqual(
            memory.stationary_clear_cooldowns[(2, 0)],
            turn.tick + MOVE_OCCUPIED_AVOID_TICKS,
        )

    def test_two_contested_stationary_moves_apply_global_target_cooldown(self):
        vanguard = FakeActor("vanguard-a", (0, 0), unit_type=UnitType.VANGUARD)
        other = FakeActor("vanguard-b", (5, 0), unit_type=UnitType.VANGUARD)
        memory = TacticMemory()
        event = SimpleNamespace(
            event_type="UNIT_MOVE_FAILED",
            reason_code="MOVE_CONTESTED",
            actor_id="vanguard-a",
            target_id="enemy-worker",
            position=None,
        )
        turn = make_turn(worker=None)
        turn.units = (vanguard, other)
        turn.vanguards = (vanguard, other)
        turn.events = (event,)

        memory.note_unit_move(
            "vanguard-a",
            (1, 0),
            (1, 0),
            stationary_target=(2, 0),
        )
        turn.tick = 11
        memory.observe(turn)
        self.assertNotIn((2, 0), memory.stationary_clear_cooldowns)

        memory.note_unit_move(
            "vanguard-a",
            (1, 0),
            (1, 0),
            stationary_target=(2, 0),
        )
        turn.tick = 15
        memory.observe(turn)

        self.assertEqual(
            memory.stationary_clear_cooldowns[(2, 0)],
            turn.tick + MOVE_OCCUPIED_AVOID_TICKS,
        )
        self.assertNotIn("vanguard-a", memory.stationary_move_failures)

        enemy = FakeActor("enemy-worker", (2, 0), unit_type=UnitType.WORKER)
        enemy.hp = 1
        core = FakeActor("core", (10, 10))
        core.hp = 5
        turn.core = core
        turn.visible_enemies = (enemy,)
        turn.events = ()
        turn.tick = 16
        memory.stationary_enemy_observation_memory["enemy-worker"] = (
            15,
            (2, 0),
            2,
            "UNIT",
        )
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("vanguard-b", memory.planned_stationary_targets)

    def test_visible_enemy_core_and_worker_cells_block_cargo_return(self):
        for enemy in (
            SimpleNamespace(
                id="enemy-core",
                position=(0, 0),
                hp=5,
                shield=5,
                owner_username="enemy",
            ),
            FakeActor("enemy-worker", (0, 0), unit_type=UnitType.WORKER),
        ):
            with self.subTest(enemy=getattr(enemy, "id", "")):
                worker = FakeActor("worker", (0, 1), cargo=1)
                core = FakeActor("core", (0, -2))
                core.hp = 5
                turn = make_turn(worker=worker, core=core, enemies=(enemy,))

                plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

                self.assertTrue(worker.actions)
                self.assertEqual(worker.actions[0][0], "MOVE")
                self.assertNotEqual(worker.actions[0][1], Direction.UP)

    def test_capacity_blocked_overwrites_route_budget_diagnostic(self):
        worker = FakeActor("worker", (0, 1), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        guard = FakeActor("guard", (0, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = (guard,)
        turn.units = (worker, guard)
        turn.state.population = 2
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertEqual(memory.route_diagnostics["worker"], "CAPACITY_BLOCKED")

    def test_critical_cargo_return_avoids_recent_combat_danger(self):
        worker = FakeActor("worker", (2, 0), cargo=1)
        worker.hp = 1
        core = FakeActor("core", (-1, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)
        memory = TacticMemory(recent_combat_danger_positions={(1, 0): turn.tick})

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions[0][0], "MOVE")
        self.assertNotEqual(worker.actions[0][1], Direction.LEFT)

    def test_critical_cargo_return_waits_when_only_exit_is_recent_danger(self):
        worker = FakeActor("worker", (1, 0), cargo=1)
        worker.hp = 1
        core = FakeActor("core", (-1, 0))
        core.hp = 5
        turn = make_turn(
            worker=worker,
            core=core,
            obstacles=frozenset({(1, -1), (2, 0), (1, 1)}),
        )
        memory = TacticMemory(recent_combat_danger_positions={(0, 0): turn.tick})

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])

    def test_real_enemy_occupied_cells_are_never_selected_for_return(self):
        scenarios = (
            (
                (-223, 682),
                SimpleNamespace(
                    id="6a3bd583",
                    position=(-223, 681),
                    hp=5,
                    shield=5,
                    owner_username="enemy",
                ),
            ),
            (
                (-254, 667),
                SimpleNamespace(
                    id="75cf3673",
                    position=(-253, 667),
                    hp=5,
                    shield=5,
                    owner_username="enemy",
                ),
            ),
            (
                (-171, 659),
                FakeActor(
                    "c7583c54",
                    (-172, 659),
                    unit_type=UnitType.WORKER,
                ),
            ),
        )
        deltas = {
            Direction.UP: (0, -1),
            Direction.RIGHT: (1, 0),
            Direction.DOWN: (0, 1),
            Direction.LEFT: (-1, 0),
        }

        for worker_position, enemy in scenarios:
            with self.subTest(enemy=getattr(enemy, "id", "")):
                worker = FakeActor("worker", worker_position, cargo=1)
                core = FakeActor("core", (-235, 661))
                core.hp = 5
                turn = make_turn(worker=worker, core=core, enemies=(enemy,))

                plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

                self.assertEqual(worker.actions[0][0], "MOVE")
                dx, dy = deltas[worker.actions[0][1]]
                destination = worker.position[0] + dx, worker.position[1] + dy
                self.assertNotEqual(destination, enemy.position)

    def test_core_cell_is_not_capacity_exempt_for_through_traffic(self):
        worker = FakeActor("worker", (0, 1), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        guard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(
            worker=worker,
            core=core,
            resources=frozenset({(0, -2)}),
        )
        turn.vanguards = (guard,)
        turn.units = (worker, guard)
        turn.state.population = 2

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertNotEqual(worker.actions, [("MOVE", Direction.UP)])

    def test_worker_route_avoids_a_normal_cell_with_one_unit(self):
        worker = FakeActor("worker", (0, 1), cargo=1)
        core = FakeActor("core", (0, -2))
        core.hp = 5
        blocker = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = (blocker,)
        turn.units = (worker, blocker)
        turn.state.population = 2

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions[0][0], "MOVE")
        self.assertNotEqual(worker.actions[0][1], Direction.UP)

    def test_enemy_core_memory_expires_moves_and_is_not_persisted(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(3, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        memory = TacticMemory()
        memory.observe(turn)
        turn.tick = 11
        memory.observe(turn)
        self.assertIn("enemy-core", memory.enemy_core_memory)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            _save_tactic_memory(memory, path, current_tick=11)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("enemy_core_memory", payload)

        enemy_core.state = CoreState.MOVING
        turn.tick = 12
        memory.observe(turn)
        self.assertNotIn("enemy-core", memory.enemy_core_memory)

        enemy_core.state = CoreState.NORMAL
        turn.tick = 20
        memory.observe(turn)
        turn.tick = 21
        memory.observe(turn)
        turn.visible_enemies = ()
        turn.tick = 21 + CORE_TARGET_MEMORY_TTL + 1
        memory.observe(turn)
        self.assertNotIn("enemy-core", memory.enemy_core_memory)

    def test_cargo_recovery_uses_reachable_boundary_after_sixteen_ticks(self):
        worker = FakeActor("worker", (0, 0), cargo=1)
        core = FakeActor("core", (3, 0))
        core.hp = 5
        obstacles = frozenset(
            {(-1, 0), (0, -1), (0, 1), (1, -1), (1, 1), (2, 0)}
        )
        turn = make_turn(worker=worker, core=core, obstacles=obstacles)
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        for tick in range(10, 26):
            turn.tick = tick
            worker.actions.clear()
            plan_turn(turn, memory, config)

        recovery = memory.cargo_recovery["worker"]
        self.assertEqual(recovery.target, (1, 0))
        turn.tick = 26
        worker.actions.clear()
        plan_turn(turn, memory, config)
        self.assertEqual(worker.actions, [("MOVE", Direction.RIGHT)])

    def test_concave_cargo_route_deposits_within_one_hundred_ticks(self):
        worker = FakeActor("worker", (-264, 663), cargo=1)
        core = FakeActor("core", (-258, 663))
        core.hp = 5
        obstacles = frozenset({(-263, 662), (-262, 663), (-263, 664)})
        turn = make_turn(worker=worker, core=core, obstacles=obstacles)
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)
        deltas = {
            Direction.UP: (0, -1),
            Direction.RIGHT: (1, 0),
            Direction.DOWN: (0, 1),
            Direction.LEFT: (-1, 0),
        }
        deposited = False

        for tick in range(10, 110):
            turn.tick = tick
            worker.actions.clear()
            plan_turn(turn, memory, config)
            action = worker.actions[0]
            if action[0] == "DEPOSIT":
                deposited = True
                break
            if action[0] == "MOVE":
                dx, dy = deltas[action[1]]
                worker.position = worker.position[0] + dx, worker.position[1] + dy

        self.assertTrue(deposited)

    def test_cargo_worker_can_enter_an_empty_core_cell(self):
        worker = FakeActor("worker", (0, 1), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=worker, core=core)

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.UP)])

    def test_cargo_waits_when_another_unit_occupies_the_core_cell(self):
        worker = FakeActor("worker", (0, 1), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        guard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = (guard,)
        turn.units = (worker, guard)
        turn.state.population = 2

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])

    def test_single_entrance_core_lane_clears_for_waiting_cargo(self):
        cargo = FakeActor("a-cargo", (-1, 0), cargo=1)
        empty = FakeActor("b-empty", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(
            worker=empty,
            core=core,
            obstacles=frozenset({(0, -1), (1, 0), (0, 1)}),
        )
        turn.workers = (cargo, empty)
        turn.units = (cargo, empty)
        turn.state.population = 2
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)
        history = []
        deposit_tick = None
        for _ in range(24):
            action_tick = turn.tick
            plan_turn(turn, memory, config)
            history.append(
                (
                    action_tick,
                    memory.cargo_lane.phase,
                    memory.cargo_lane.owner_id,
                    memory.cargo_lane.departing_worker_id,
                    cargo.position,
                    empty.position,
                    _worker_mode(cargo, memory, action_tick),
                    _worker_mode(empty, memory, action_tick),
                )
            )
            if memory.cargo_lane.phase == "STARTUP_EVACUATION":
                self.assertNotEqual(cargo.position, core.position)
            _, deposited = apply_synchronous_actions(turn)
            if deposited:
                deposit_tick = action_tick
            if deposit_tick is not None and not memory.cargo_lane.active:
                break

        self.assertIsNotNone(deposit_tick, f"history={history}")
        startup_rows = [row for row in history if row[1] == "STARTUP_EVACUATION"]
        self.assertTrue(startup_rows, f"history={history}")
        self.assertTrue(
            any(row[7] == "CARGO_LANE_EGRESS" for row in startup_rows),
            f"history={history}",
        )
        first_inbound = next(row for row in history if row[1] == "INBOUND")
        self.assertLessEqual(first_inbound[5][0], -4, f"history={history}")
        self.assertTrue(
            all(row[4] != core.position for row in history if row[0] < first_inbound[0]),
            f"history={history}",
        )
        self.assertTrue(
            any(
                row[0] > deposit_tick and row[6] == "CARGO_LANE_EGRESS"
                for row in history
            ),
            f"history={history}",
        )

    def test_cargo_lane_owner_admission_rejects_distant_wait_credit(self):
        near = FakeActor("near", (4, 0), cargo=1)
        far = FakeActor("far", (58, 0), cargo=1)
        memory = TacticMemory(cargo_lane_wait_ticks={"near": 65, "far": 90})

        owner, route = _select_cargo_lane_owner(
            (far, near),
            (0, 0),
            set(),
            memory,
        )

        self.assertIs(owner, near)
        self.assertEqual(len(route.path) - 1, 4)

    def test_cargo_lane_wait_credit_is_capped_before_route_tiebreak(self):
        near = FakeActor("near", (1, 0), cargo=1)
        farther = FakeActor("farther", (8, 0), cargo=1)
        memory = TacticMemory(
            cargo_lane_wait_ticks={"near": 1, "farther": 100}
        )

        owner, route = _select_cargo_lane_owner(
            (farther, near),
            (0, 0),
            set(),
            memory,
        )

        self.assertIs(owner, near)
        self.assertEqual(len(route.path) - 1, 1)

    def test_cargo_lane_owner_admission_falls_back_to_nearest(self):
        nearer = FakeActor("nearer", (9, 0), cargo=1)
        farther = FakeActor("farther", (20, 0), cargo=1)
        memory = TacticMemory(cargo_lane_wait_ticks={"farther": 100})

        owner, route = _select_cargo_lane_owner(
            (farther, nearer),
            (0, 0),
            set(),
            memory,
        )

        self.assertIs(owner, nearer)
        self.assertEqual(len(route.path) - 1, 9)

    def test_cargo_lane_wait_credit_counts_only_staged_rejections(self):
        owner = FakeActor("owner", (1, 0), cargo=1)
        staged = FakeActor("staged", (3, 1), cargo=1)
        approaching = FakeActor("approaching", (20, 0), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(
            worker=owner,
            core=core,
            obstacles=frozenset({(0, -1), (-1, 0), (0, 1)}),
        )
        turn.workers = (owner, staged, approaching)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                stage_targets={
                    "staged": (3, 1),
                    "approaching": (4, 1),
                },
            ),
            cargo_lane_wait_ticks={
                "owner": 5,
                "staged": 5,
                "approaching": 5,
            },
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            {(0, -1), (-1, 0), (0, 1)},
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertNotIn("owner", memory.cargo_lane_wait_ticks)
        self.assertEqual(memory.cargo_lane_wait_ticks["staged"], 6)
        self.assertNotIn("approaching", memory.cargo_lane_wait_ticks)

    def test_queued_cargo_survives_egress_and_is_promoted_directly(self):
        departing = FakeActor("departing", (2, 0), cargo=0)
        queued = FakeActor("queued", (3, 1), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(
            worker=departing,
            core=core,
            obstacles=frozenset(blocked),
        )
        turn.workers = (departing, queued)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                departing_worker_id="departing",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                stage_targets={"queued": (3, 1)},
            ),
            cargo_lane_wait_ticks={"queued": 5},
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "EGRESS")
        self.assertEqual(memory.cargo_lane.queued_owner_id, "queued")
        self.assertIsNone(memory.cargo_lane.owner_id)

        departing.position = (4, 0)
        turn.tick += 1
        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "INBOUND")
        self.assertEqual(memory.cargo_lane.owner_id, "queued")
        self.assertIsNone(memory.cargo_lane.queued_owner_id)
        self.assertIsNone(memory.cargo_lane.departing_worker_id)
        self.assertNotIn("queued", memory.cargo_lane_wait_ticks)

    def test_remote_heal_intent_does_not_block_queued_cargo_handoff(self):
        queued = FakeActor("queued", (3, 1), cargo=1)
        healer = FakeActor("healer", (0, 120), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=queued, core=core, obstacles=frozenset(blocked))
        turn.workers = (queued, healer)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            heal_intent_best_distance=120,
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick - 20,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            ),
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "INBOUND")
        self.assertEqual(memory.cargo_lane.owner_id, "queued")
        self.assertEqual(memory.heal_intent_id, "healer")
        self.assertIsNone(memory.heal_priority_intent_id)
        self.assertEqual(memory.core_visit_deposit_streak, 0)
        self.assertIsNone(memory.core_visit_forced_purpose)

    def test_staged_reachable_healer_blocks_new_lane_after_three_deposits(self):
        cargo = FakeActor("cargo", (3, 1), cargo=1)
        healer = FakeActor("healer", (4, 1), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=cargo, core=core, obstacles=frozenset(blocked))
        turn.workers = (cargo, healer)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            heal_intent_best_distance=5,
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertFalse(memory.cargo_lane.active)
        self.assertEqual(memory.heal_priority_intent_id, "healer")
        self.assertEqual(memory.core_visit_deposit_streak, 3)
        self.assertEqual(memory.core_visit_forced_purpose, "HEAL")

    def test_staged_heal_priority_activates_after_third_completed_deposit(self):
        healer = FakeActor("healer", (4, 1), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=healer, core=core)
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            heal_intent_best_distance=5,
            heal_priority_intent_id="healer",
        )

        for index in range(3):
            depositor = FakeActor(f"depositor-{index}", (0, 0), cargo=0)
            turn.workers = (depositor, healer)
            turn.units = turn.workers
            turn.state.population = len(turn.units)
            memory.core_visit = CoreVisit(
                depositor.id,
                "DEPOSIT",
                turn.tick - 1,
            )
            memory.observe(turn)
            self.assertEqual(memory.core_visit_deposit_streak, index + 1)
            self.assertIsNone(memory.core_visit_forced_purpose)
            turn.tick += 1

        cargo = FakeActor("cargo", (3, 1), cargo=1)
        turn.workers = (cargo, healer)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        blocked = {(0, -1), (-1, 0), (0, 1)}

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertFalse(memory.cargo_lane.active)
        self.assertEqual(memory.core_visit_forced_purpose, "HEAL")

    def test_unreachable_staged_healer_releases_cargo_priority(self):
        queued = FakeActor("queued", (3, 1), cargo=1)
        healer = FakeActor("healer", (4, 1), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {
            (0, -1),
            (-1, 0),
            (0, 1),
            (4, 0),
            (5, 1),
            (4, 2),
            (3, 1),
        }
        turn = make_turn(worker=queued, core=core, obstacles=frozenset(blocked))
        turn.workers = (queued, healer)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            heal_intent_best_distance=5,
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick - 20,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            ),
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "INBOUND")
        self.assertEqual(memory.cargo_lane.owner_id, "queued")
        self.assertIsNone(memory.heal_priority_intent_id)
        self.assertIsNone(memory.core_visit_forced_purpose)

    def test_heal_priority_bound_interleaves_one_cargo_without_losing_quota(self):
        queued = FakeActor("queued", (3, 1), cargo=1)
        healer = FakeActor("healer", (4, 1), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=queued, core=core, obstacles=frozenset(blocked))
        turn.tick = 33
        turn.workers = (queued, healer)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            heal_intent_best_distance=5,
            heal_priority_intent_id="healer",
            heal_priority_started_tick=(
                1
            ),
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=1,
                phase_started_tick=14,
                geometry_source="SINGLE_OPEN",
            ),
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "INBOUND")
        self.assertEqual(memory.cargo_lane.phase_started_tick, turn.tick)
        self.assertEqual(memory.cargo_lane.owner_id, "queued")
        self.assertEqual(memory.cargo_lane.watchdog_tick, turn.tick)
        self.assertEqual(
            memory.cargo_lane.watchdog_reason,
            "HEAL_PRIORITY_INTERLEAVE",
        )
        self.assertEqual(memory.core_visit_deposit_streak, 3)
        self.assertEqual(memory.heal_priority_intent_id, "healer")
        self.assertTrue(memory.heal_priority_interleave_pending)
        self.assertIsNone(memory.core_visit_forced_purpose)

    def test_cargo_lane_phase_start_tick_changes_at_each_handoff(self):
        departing = FakeActor("departing", (2, 0), cargo=0)
        queued = FakeActor("queued", (3, 1), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=departing, core=core, obstacles=frozenset(blocked))
        turn.tick = 20
        turn.workers = (departing, queued)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                departing_worker_id="departing",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                phase_started_tick=18,
                geometry_source="SINGLE_OPEN",
            )
        )

        departing.position = (4, 0)
        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "INBOUND")
        self.assertEqual(memory.cargo_lane.phase_started_tick, turn.tick)

    def test_cargo_lane_dead_pocket_is_not_egress_complete(self):
        lane = CargoLanePlan(
            active=True,
            core_position=(0, 0),
            path=((0, 0), (1, 0), (2, 0), (3, 0)),
            gateway=(3, 0),
        )
        blocked = {(0, -1), (-1, 0), (0, 1), (1, -1), (1, 1), (2, 0)}

        self.assertFalse(
            _cargo_lane_egress_complete(
                (1, 0),
                lane,
                blocked,
                {(1, 0): 1},
            )
        )
        self.assertTrue(
            _cargo_lane_egress_complete(
                (4, 0),
                lane,
                blocked,
                {(4, 0): 1},
            )
        )

    def test_cargo_lane_egress_route_survives_temporary_friendly_blockers(self):
        lane = CargoLanePlan(
            active=True,
            phase="EGRESS",
            core_position=(0, 0),
            departing_worker_id="departing",
            path=((0, 0), (1, 0), (2, 0), (3, 0)),
            gateway=(3, 0),
        )
        blocked = {(0, -1), (-1, 0), (0, 1)}
        friendly_occupancy = {
            (0, 0): 1,
            (1, 0): 1,
            (2, 0): 1,
            (3, 0): 1,
            (4, 0): 1,
        }

        route = _cargo_lane_egress_route(
            (0, 0),
            lane,
            blocked,
            friendly_occupancy,
        )

        self.assertIsNotNone(route)
        self.assertEqual(route[:4], lane.path)
        self.assertNotIn(route[-1], friendly_occupancy)
        self.assertTrue(
            _cargo_lane_egress_complete(
                route[-1],
                lane,
                blocked,
                friendly_occupancy,
            )
        )

    def test_cargo_lane_extends_a_dynamically_closed_egress_endpoint(self):
        lane = CargoLanePlan(
            active=True,
            phase="EGRESS",
            core_position=(0, 0),
            departing_worker_id="departing",
            path=((0, 0), (1, 0), (2, 0), (3, 0)),
            gateway=(3, 0),
            egress_path=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0)),
            egress_target=(4, 0),
        )
        blocked = {(4, -1), (4, 1), (5, -1), (5, 1)}
        friendly_occupancy = {(3, 0): 1, (4, 0): 1, (5, 0): 1}

        self.assertFalse(
            _cargo_lane_egress_complete(
                (4, 0),
                lane,
                blocked,
                friendly_occupancy,
            )
        )

        route = _cargo_lane_egress_route(
            (4, 0),
            lane,
            blocked,
            friendly_occupancy,
        )

        self.assertEqual(route, ((4, 0), (5, 0), (6, 0)))
        self.assertTrue(
            _cargo_lane_egress_complete(
                route[-1],
                lane,
                blocked,
                friendly_occupancy,
            )
        )

    def test_cargo_lane_egress_clears_friendly_occupied_corridor(self):
        departing = FakeActor("departing", (0, 0), cargo=0)
        blockers = (
            FakeActor("blocker-1", (1, 0), cargo=0),
            FakeActor("blocker-2", (2, 0), cargo=1),
            FakeActor("blocker-3", (3, 0), cargo=1),
            FakeActor("blocker-4", (4, 0), cargo=1),
        )
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(
            worker=departing,
            core=core,
            obstacles=frozenset({(0, -1), (-1, 0), (0, 1)}),
        )
        turn.workers = (departing,) + blockers
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                departing_worker_id="departing",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick - 1,
                last_planned_tick=turn.tick - 1,
            )
        )
        history = []

        for _ in range(16):
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
            history.append(
                (
                    turn.tick,
                    memory.cargo_lane.phase,
                    memory.cargo_lane.departing_worker_id,
                    departing.position,
                    tuple(sorted(memory.cargo_lane.yield_worker_ids)),
                    memory.cargo_lane.owner_id,
                )
            )
            apply_synchronous_actions(turn)
            if memory.cargo_lane.departing_worker_id is None:
                break

        self.assertIsNone(memory.cargo_lane.departing_worker_id, f"history={history}")
        self.assertGreaterEqual(departing.position[0], 4, f"history={history}")
        self.assertTrue(
            any(row[4] for row in history),
            f"no blocker entered CARGO_LANE_YIELD: history={history}",
        )
        self.assertIsNotNone(memory.cargo_lane.owner_id, f"history={history}")

    def test_cargo_lane_egress_forces_ranger_off_gateway_before_handoff(self):
        departing = FakeActor("departing", (2, 0), cargo=0)
        queued = FakeActor("queued", (4, 1), cargo=1)
        ranger = FakeActor("ranger", (3, 0), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
        turn = make_turn(worker=departing, core=core, obstacles=blocked)
        turn.workers = (departing, queued)
        turn.rangers = (ranger,)
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                departing_worker_id="departing",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                egress_path=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0)),
                egress_target=(4, 0),
                started_tick=turn.tick - 1,
                phase_started_tick=turn.tick - 1,
                geometry_source="SINGLE_OPEN",
            )
        )
        history = []

        for _ in range(8):
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
            history.append(
                (
                    turn.tick,
                    departing.position,
                    ranger.position,
                    tuple(sorted(memory.cargo_lane.yield_unit_ids)),
                    memory.cargo_lane.departing_worker_id,
                    memory.cargo_lane.owner_id,
                )
            )
            apply_synchronous_actions(turn)
            if memory.cargo_lane.departing_worker_id is None:
                break

        self.assertNotEqual(ranger.position, (3, 0), f"history={history}")
        self.assertTrue(
            any("ranger" in row[3] for row in history),
            f"ranger never received lane-yield priority: history={history}",
        )
        self.assertIsNone(memory.cargo_lane.departing_worker_id, f"history={history}")
        self.assertEqual(memory.cargo_lane.owner_id, "queued", f"history={history}")

    def test_inbound_owner_approach_marks_vanguard_and_ranger_for_yield(self):
        owner = FakeActor("owner", (6, 0), cargo=1)
        vanguard = FakeActor("vanguard", (5, 0), unit_type=UnitType.VANGUARD)
        vanguard.hp = 4
        ranger = FakeActor("ranger", (4, 0), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=owner, core=core, obstacles=frozenset(blocked))
        turn.vanguards = (vanguard,)
        turn.rangers = (ranger,)
        turn.units = (owner, vanguard, ranger)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            )
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(
            memory.cargo_lane.owner_approach_path,
            ((6, 0), (5, 0), (4, 0), (3, 0)),
        )
        self.assertEqual(
            memory.cargo_lane.yield_unit_ids,
            {"vanguard", "ranger"},
        )

    def test_inbound_owner_approach_starts_at_owner_not_gateway_suffix(self):
        owner = FakeActor("owner", (10, 0), cargo=1)
        early_blocker = FakeActor(
            "early-blocker",
            (9, 0),
            unit_type=UnitType.VANGUARD,
        )
        early_blocker.hp = 4
        late_blocker = FakeActor(
            "late-blocker",
            (5, 0),
            unit_type=UnitType.RANGER,
        )
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=owner, core=core, obstacles=frozenset(blocked))
        turn.vanguards = (early_blocker,)
        turn.rangers = (late_blocker,)
        turn.units = (owner, early_blocker, late_blocker)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            )
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(
            memory.cargo_lane.owner_approach_path,
            tuple((x, 0) for x in range(10, 2, -1)),
        )
        self.assertEqual(
            memory.cargo_lane.yield_unit_ids,
            {"early-blocker", "late-blocker"},
        )

    def test_inbound_approach_defenders_yield_then_owner_progresses(self):
        owner = FakeActor("owner", (6, 0), cargo=1)
        vanguard = FakeActor("vanguard", (5, 0), unit_type=UnitType.VANGUARD)
        vanguard.hp = 4
        ranger = FakeActor("ranger", (4, 0), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
        turn = make_turn(worker=owner, core=core, obstacles=blocked)
        turn.vanguards = (vanguard,)
        turn.rangers = (ranger,)
        turn.units = (owner, vanguard, ranger)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            )
        )
        history = []

        for _ in range(5):
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
            history.append(
                (
                    turn.tick,
                    owner.position,
                    vanguard.position,
                    ranger.position,
                    tuple(sorted(memory.cargo_lane.yield_unit_ids)),
                )
            )
            apply_synchronous_actions(turn)
            if owner.position != (6, 0):
                break

        self.assertTrue(
            any({"vanguard", "ranger"}.issubset(set(row[4])) for row in history),
            f"approach blockers never yielded: history={history}",
        )
        self.assertNotEqual(owner.position, (6, 0), f"history={history}")
        self.assertNotIn(vanguard.position, memory.cargo_lane.owner_approach_path)
        self.assertNotIn(ranger.position, memory.cargo_lane.owner_approach_path)

    def test_inbound_watchdog_replaces_owner_outside_physical_lane(self):
        owner = FakeActor("owner", (6, 0), cargo=1)
        replacement = FakeActor("replacement", (4, 1), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=owner, core=core, obstacles=frozenset(blocked))
        turn.tick = 40
        turn.workers = (owner, replacement)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                queued_owner_id="replacement",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=1,
                phase_started_tick=1,
                geometry_source="SINGLE_OPEN",
                owner_progress_id="owner",
                owner_best_remaining_steps=6,
                owner_last_progress_tick=(
                    turn.tick - CARGO_LANE_INBOUND_WATCHDOG_TICKS
                ),
            )
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.owner_id, "replacement")
        self.assertEqual(memory.cargo_lane.owner_progress_id, "replacement")
        self.assertEqual(memory.cargo_lane.phase_started_tick, turn.tick)
        self.assertEqual(memory.cargo_lane.watchdog_tick, turn.tick)
        self.assertEqual(
            memory.cargo_lane.watchdog_reason,
            "INBOUND_OWNER_STALLED",
        )
        self.assertEqual(
            memory.cargo_lane_stalled_owners["owner"][0],
            owner.position,
        )

    def test_stalled_owner_cooldown_prefers_alternative_until_owner_moves(self):
        stalled = FakeActor("stalled", (4, 0), cargo=1)
        alternative = FakeActor("alternative", (7, 0), cargo=1)
        memory = TacticMemory(
            last_observed_tick=10,
            cargo_lane_stalled_owners={"stalled": ((4, 0), 26)},
        )

        owner, _ = _select_cargo_lane_owner(
            (stalled, alternative),
            (3, 0),
            set(),
            memory,
        )
        self.assertEqual(owner.id, "alternative")

        stalled.position = (5, 0)
        owner, _ = _select_cargo_lane_owner(
            (stalled, alternative),
            (3, 0),
            set(),
            memory,
        )
        self.assertEqual(owner.id, "stalled")

    def test_inbound_watchdog_never_replaces_owner_inside_physical_lane(self):
        owner = FakeActor("owner", (2, 0), cargo=1)
        replacement = FakeActor("replacement", (4, 1), cargo=1)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=owner, core=core, obstacles=frozenset(blocked))
        turn.tick = 40
        turn.workers = (owner, replacement)
        turn.units = turn.workers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                queued_owner_id="replacement",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=1,
                phase_started_tick=1,
                geometry_source="SINGLE_OPEN",
                owner_progress_id="owner",
                owner_best_remaining_steps=2,
                owner_last_progress_tick=(
                    turn.tick - CARGO_LANE_INBOUND_WATCHDOG_TICKS
                ),
            )
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.owner_id, "owner")
        self.assertEqual(memory.cargo_lane.queued_owner_id, "replacement")
        self.assertEqual(memory.cargo_lane.owner_progress_id, "owner")
        self.assertEqual(memory.cargo_lane.watchdog_tick, turn.tick)
        self.assertEqual(
            memory.cargo_lane.watchdog_reason,
            "INBOUND_OWNER_STALLED_IN_LANE",
        )

    def test_core_occupant_releases_closed_cargo_pocket_for_stalled_owner(self):
        owner = FakeActor("owner", (-1, 0), cargo=1)
        defender = FakeActor(
            "defender",
            (0, 0),
            unit_type=UnitType.VANGUARD,
        )
        defender.hp = 4
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        # The only temporary release cell is immediately above the Core. It
        # cannot become a normal yield target because it does not lead beyond
        # the closed clearance ring.
        blocked = frozenset({(1, 0), (0, 1), (-1, -1), (1, -1), (0, -2)})
        turn = make_turn(worker=owner, core=core, obstacles=blocked)
        turn.vanguards = (defender,)
        turn.units = (owner, defender)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (-1, 0)),
                gateway=(-1, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="CORE_POCKET",
            )
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.cargo_lane.yield_targets["defender"], (0, -1))
        self.assertEqual(defender.actions, [("MOVE", Direction.UP)])
        self.assertEqual(owner.actions, [("WAIT",)])
        apply_synchronous_actions(turn)

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(owner.actions, [("MOVE", Direction.RIGHT)])
        self.assertEqual(defender.actions, [("WAIT",)])
        self.assertIn(
            "defender",
            memory.cargo_lane.core_release_hold_unit_ids,
        )
        apply_synchronous_actions(turn)

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(owner.actions, [("DEPOSIT",)])
        self.assertNotIn(
            "defender",
            memory.cargo_lane.core_release_hold_unit_ids,
        )

    def test_healed_ranger_stays_released_until_cargo_enters_core(self):
        owner = FakeActor("owner", (0, -1), cargo=1)
        healer = FakeActor("healer", (0, 0), unit_type=UnitType.RANGER)
        healer.hp = 2
        reserve = FakeActor("reserve", (5, 5), unit_type=UnitType.RANGER)
        reserve.hp = 2
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=owner, core=core)
        turn.resources = 10
        turn.resource_capacity = 30
        turn.rangers = (healer, reserve)
        turn.units = (owner, healer, reserve)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="owner",
                path=((0, 0), (0, -1)),
                gateway=(0, -1),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            )
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(owner.actions, [("WAIT",)])
        self.assertTrue(any(action[0] == "MOVE" for action in healer.actions))
        self.assertIn("healer", memory.cargo_lane.core_release_hold_unit_ids)
        apply_synchronous_actions(turn)

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(owner.actions, [("MOVE", Direction.DOWN)])
        self.assertTrue(any(action[0] == "MOVE" for action in healer.actions))
        self.assertIn("healer", memory.cargo_lane.core_release_hold_unit_ids)
        apply_synchronous_actions(turn)

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(owner.actions, [("DEPOSIT",)])
        self.assertNotIn("healer", memory.cargo_lane.core_release_hold_unit_ids)

    def test_held_core_release_defender_can_continue_across_diagonal_pocket(self):
        lane = CargoLanePlan(
            active=True,
            core_position=(-191, 680),
            path=((-191, 680), (-191, 681), (-191, 682), (-191, 683)),
            core_release_hold_unit_ids={"defender"},
        )
        route = _cargo_lane_temporary_release_route(
            (-192, 681),
            lane,
            set(),
            {
                (-193, 681),
                (-192, 679),
                (-192, 682),
                (-190, 679),
                (-190, 680),
                (-190, 681),
            },
            set(),
            set(),
            held=True,
        )
        self.assertEqual(route, ((-192, 681), (-192, 680)))

    def test_unadmitted_wounded_vanguard_yields_then_stages_outside_lane(self):
        cargo = FakeActor("cargo", (4, 0), cargo=1)
        healer = FakeActor("healer", (2, 0), unit_type=UnitType.VANGUARD)
        healer.hp = 2
        reserve = FakeActor("reserve", (6, 2), unit_type=UnitType.VANGUARD)
        reserve.hp = 4
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
        turn = make_turn(worker=cargo, core=core, obstacles=blocked)
        turn.resources = 20
        turn.vanguards = (healer, reserve)
        turn.units = (cargo, healer, reserve)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="INBOUND",
                core_position=(0, 0),
                owner_id="cargo",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            )
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIn("healer", memory.cargo_lane.yield_unit_ids)
        self.assertNotEqual(memory.healing_defender_admitted_id, "healer")
        first_move = next(action for action in healer.actions if action[0] == "MOVE")
        healer.position = _next_position(healer.position, first_move[1])
        self.assertNotIn(healer.position, memory.cargo_lane.path)
        for actor in (*turn.units, turn.core):
            actor.actions.clear()
        turn.tick += 1

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        second_move = next(action for action in healer.actions if action[0] == "MOVE")
        second_destination = _next_position(healer.position, second_move[1])
        self.assertNotIn(second_destination, memory.cargo_lane.path)
        self.assertNotIn(
            second_destination,
            {
                _next_position(core.position, direction)
                for direction in (
                    Direction.UP,
                    Direction.DOWN,
                    Direction.LEFT,
                    Direction.RIGHT,
                )
            },
        )

    def test_defender_heal_admission_exempts_vanguard_and_ranger_from_yield(self):
        for unit_type, wounded_hp in (
            (UnitType.VANGUARD, 2),
            (UnitType.RANGER, 1),
        ):
            with self.subTest(unit_type=unit_type):
                queued = FakeActor("queued", (4, 1), cargo=1)
                healer = FakeActor("healer", (5, 0), unit_type=unit_type)
                healer.hp = wounded_hp
                reserve = FakeActor("reserve", (6, 2), unit_type=unit_type)
                reserve.hp = 4 if unit_type == UnitType.VANGUARD else 2
                core = FakeActor("core", (0, 0), shield=5)
                core.hp = 5
                blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
                turn = make_turn(worker=queued, core=core, obstacles=blocked)
                turn.resources = 12
                turn.resource_capacity = 30
                turn.workers = (queued,)
                if unit_type == UnitType.VANGUARD:
                    turn.vanguards = (healer, reserve)
                else:
                    turn.rangers = (healer, reserve)
                turn.units = turn.workers + (healer, reserve)
                turn.state.population = len(turn.units)
                memory = TacticMemory(
                    healing_defender_ids={"healer"},
                    healing_defender_intent_id="healer",
                    healing_defender_intent_tick=turn.tick,
                    healing_defender_stage_target=(5, 0),
                    heal_priority_intent_id="healer",
                    core_visit_deposit_streak=3,
                    cargo_lane=CargoLanePlan(
                        active=True,
                        phase="EGRESS",
                        core_position=(0, 0),
                        queued_owner_id="queued",
                        path=((0, 0), (1, 0), (2, 0), (3, 0)),
                        gateway=(3, 0),
                        started_tick=turn.tick - 5,
                        phase_started_tick=turn.tick - 1,
                        geometry_source="SINGLE_OPEN",
                    ),
                )
                yield_history = []

                for _ in range(7):
                    plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
                    yield_history.append(set(memory.cargo_lane.yield_unit_ids))
                    if healer.actions == [("HEAL",)]:
                        break
                    move_actions = [
                        action for action in healer.actions if action[0] == "MOVE"
                    ]
                    self.assertTrue(
                        move_actions,
                        f"actions={healer.actions} lane={memory.cargo_lane} "
                        f"stage={memory.healing_defender_stage_target}",
                    )
                    move = move_actions[0]
                    healer.position = _next_position(healer.position, move[1])
                    for actor in (*turn.units, turn.core):
                        actor.actions.clear()
                    turn.tick += 1

                self.assertEqual(memory.healing_defender_admitted_id, "healer")
                self.assertTrue(all("healer" not in ids for ids in yield_history))
                self.assertEqual(healer.position, core.position)
                self.assertEqual(healer.actions, [("HEAL",)])
                self.assertEqual(
                    memory.core_visit,
                    CoreVisit("healer", "HEAL", turn.tick, reached_core=True),
                )
                self.assertIsNone(memory.cargo_lane.owner_id)

    def test_defender_heal_progress_uses_complete_route_around_long_wall(self):
        healer = FakeActor("healer", (0, 0), unit_type=UnitType.RANGER)
        healer.hp = 1
        reserve = FakeActor("reserve", (0, 3), unit_type=UnitType.RANGER)
        reserve.hp = 2
        core = FakeActor("core", (2, 0), shield=5)
        core.hp = 5
        wall = {(1, y) for y in range(-10, 11)}
        turn = make_turn(worker=None, core=core, obstacles=frozenset(wall))
        turn.resources = 20
        turn.rangers = (healer, reserve)
        turn.units = turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_intent_tick=1,
            healing_defender_admitted_id="healer",
            healing_defender_admitted_tick=1,
            healing_defender_best_distance=24,
            healing_defender_last_progress_tick=1,
            healing_defender_last_position=(0, 0),
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
        )
        route = _complete_route(healer.position, core.position, wall)
        self.assertEqual(route.status, "SUCCESS")
        self.assertGreater(len(route.path) - 1, 16)

        for tick, position in enumerate(route.path[1:18], start=2):
            healer.position = position
            turn.tick = tick
            _refresh_healing_defenders(turn, memory, core.position, wall)

        self.assertEqual(memory.healing_defender_admitted_id, "healer")
        self.assertEqual(memory.healing_defender_intent_id, "healer")
        self.assertEqual(memory.healing_defender_last_progress_tick, turn.tick)
        self.assertNotIn("healer", memory.healing_defender_cooldowns)

    def test_admitted_healer_route_forces_worker_and_ranger_to_yield(self):
        owner = FakeActor("owner", (4, 1), cargo=1)
        worker_blocker = FakeActor("worker-blocker", (-1, 0), cargo=1)
        healer = FakeActor("healer", (-3, 0), unit_type=UnitType.RANGER)
        healer.hp = 1
        ranger_blocker = FakeActor(
            "ranger-blocker",
            (-2, 0),
            unit_type=UnitType.RANGER,
        )
        ranger_blocker.hp = 2
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (0, 1), (1, 0)}
        turn = make_turn(worker=owner, core=core, obstacles=frozenset(blocked))
        turn.workers = (owner, worker_blocker)
        turn.rangers = (healer, ranger_blocker)
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_admitted_id="healer",
            heal_priority_intent_id="healer",
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="owner",
                path=((0, 0), (-1, 0), (-2, 0)),
                gateway=(-2, 0),
                started_tick=turn.tick - 4,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            ),
        )

        _refresh_cargo_lane_occupants(
            turn,
            memory,
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
        )

        self.assertEqual(
            memory.cargo_lane.heal_approach_path,
            ((-3, 0), (-2, 0), (-1, 0), (0, 0)),
        )
        self.assertIn("worker-blocker", memory.cargo_lane.yield_worker_ids)
        self.assertIn("ranger-blocker", memory.cargo_lane.yield_unit_ids)
        self.assertNotIn("healer", memory.cargo_lane.yield_unit_ids)

    def test_admitted_healer_waits_for_published_route_blocker_then_advances(self):
        queued = FakeActor("queued", (2, 0), cargo=1)
        healer = FakeActor("healer", (-3, 0), unit_type=UnitType.RANGER)
        healer.hp = 1
        blocker = FakeActor("blocker", (-2, 0), unit_type=UnitType.RANGER)
        blocker.hp = 2
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=queued, core=core)
        turn.resources = 20
        turn.workers = (queued,)
        turn.rangers = (healer, blocker)
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_admitted_id="healer",
            heal_priority_intent_id="healer",
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (-1, 0), (-2, 0)),
                gateway=(-2, 0),
                heal_approach_path=((-3, 0), (-2, 0), (-1, 0), (0, 0)),
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(healer.actions, [("WAIT",)])
        self.assertTrue(any(action[0] == "MOVE" for action in blocker.actions))

        healer.actions.clear()
        blocker.actions.clear()
        blocker.position = (-2, 1)
        turn.tick += 1
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(healer.actions, [("MOVE", Direction.RIGHT)])

    def test_defender_yield_falls_back_to_one_cell_release_without_full_route(self):
        blocker = FakeActor("blocker", (-187, 650), unit_type=UnitType.VANGUARD)
        blocker.hp = 4
        lane = CargoLanePlan(
            active=True,
            phase="EGRESS",
            core_position=(-187, 647),
            path=((-187, 647), (-187, 646), (-186, 646)),
            gateway=(-186, 646),
            heal_approach_path=(
                (-186, 650),
                (-187, 650),
                (-187, 649),
                (-187, 648),
                (-187, 647),
            ),
            yield_unit_ids={"blocker"},
        )
        memory = TacticMemory(cargo_lane=lane)

        moved = _queue_cargo_lane_defender_yield(
            blocker,
            lane,
            blocked=set(),
            reservations={},
            friendly_occupancy={blocker.position: 1},
            memory=memory,
            tick=10,
        )

        self.assertTrue(moved)
        self.assertEqual(blocker.actions, [("MOVE", Direction.DOWN)])

    def test_admitted_heal_publishes_yield_for_adjacent_route_blocker(self):
        queued = FakeActor("queued", (3, 0), cargo=1)
        healer = FakeActor("healer", (0, 1), unit_type=UnitType.RANGER)
        healer.hp = 1
        blocker = FakeActor("blocker", (1, 1), unit_type=UnitType.VANGUARD)
        blocker.hp = 4
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=queued, core=core)
        turn.resources = 20
        turn.workers = (queued,)
        turn.rangers = (healer,)
        turn.vanguards = (blocker,)
        turn.units = turn.workers + turn.rangers + turn.vanguards
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_admitted_id="healer",
            heal_priority_intent_id="healer",
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (0, -1), (1, -1)),
                gateway=(1, -1),
            ),
        )

        _refresh_cargo_lane_occupants(
            turn,
            memory,
            blocked=set(),
            danger_cells=set(),
            friendly_occupancy=_friendly_cell_occupancy(turn.units),
        )

        self.assertIn("blocker", memory.cargo_lane.yield_unit_ids)

    def test_dense_healing_blocker_chain_clears_before_admission_timeout(self):
        queued = FakeActor("queued", (2, 0), cargo=1)
        healer = FakeActor("healer", (-5, 0), unit_type=UnitType.RANGER)
        healer.hp = 1
        blockers = tuple(
            FakeActor(
                f"blocker-{index}",
                (-index, 0),
                unit_type=UnitType.RANGER,
            )
            for index in range(1, 5)
        )
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=queued, core=core)
        turn.resources = 20
        turn.resource_capacity = 30
        turn.workers = (queued,)
        turn.rangers = (healer,) + blockers
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_intent_tick=turn.tick,
            healing_defender_stage_target=healer.position,
            healing_defender_admitted_id="healer",
            healing_defender_admitted_tick=turn.tick,
            healing_defender_best_distance=5,
            healing_defender_last_progress_tick=turn.tick,
            healing_defender_last_position=healer.position,
            heal_priority_intent_id="healer",
            heal_priority_started_tick=turn.tick,
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (-1, 0), (-2, 0)),
                gateway=(-2, 0),
                started_tick=turn.tick,
                phase_started_tick=turn.tick,
                geometry_source="SINGLE_OPEN",
            ),
        )
        history = []

        for _ in range(12):
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
            history.append(
                (
                    turn.tick,
                    healer.position,
                    tuple(blocker.position for blocker in blockers),
                    dict(memory.cargo_lane.yield_targets),
                    tuple(healer.actions),
                )
            )
            if healer.actions == [("HEAL",)]:
                break
            apply_synchronous_actions(turn)

        self.assertEqual(healer.actions, [("HEAL",)], f"history={history}")
        self.assertLess(
            turn.tick - memory.healing_defender_admitted_tick,
            16,
            f"history={history}",
        )
        self.assertNotIn("healer", memory.healing_defender_cooldowns)
        self.assertTrue(
            any(len(row[3]) == len(blockers) for row in history),
            f"blockers lacked deterministic targets: history={history}",
        )

    def test_defender_heal_waits_for_three_deposits_before_admission(self):
        queued = FakeActor("queued", (4, 1), cargo=1)
        healer = FakeActor("healer", (5, 0), unit_type=UnitType.VANGUARD)
        healer.hp = 2
        reserve = FakeActor("reserve", (6, 2), unit_type=UnitType.VANGUARD)
        reserve.hp = 4
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
        turn = make_turn(worker=queued, core=core, obstacles=blocked)
        turn.resources = 12
        turn.resource_capacity = 30
        turn.vanguards = (healer, reserve)
        turn.units = (queued, healer, reserve)
        turn.state.population = len(turn.units)

        def memory_with_streak(streak):
            return TacticMemory(
                healing_defender_ids={"healer"},
                healing_defender_intent_id="healer",
                healing_defender_intent_tick=turn.tick,
                healing_defender_stage_target=(5, 0),
                heal_priority_intent_id="healer",
                core_visit_deposit_streak=streak,
                cargo_lane=CargoLanePlan(
                    active=True,
                    phase="EGRESS",
                    core_position=(0, 0),
                    queued_owner_id="queued",
                    path=((0, 0), (1, 0), (2, 0), (3, 0)),
                    gateway=(3, 0),
                    started_tick=turn.tick - 5,
                    phase_started_tick=turn.tick - 1,
                    geometry_source="SINGLE_OPEN",
                ),
            )

        before_quota = memory_with_streak(2)
        plan_turn(turn, before_quota, AgentConfig(spawn_unit_type=None))
        self.assertIsNone(before_quota.healing_defender_admitted_id)
        self.assertEqual(before_quota.cargo_lane.owner_id, "queued")

        for actor in (*turn.units, turn.core):
            actor.actions.clear()
        at_quota = memory_with_streak(3)
        plan_turn(turn, at_quota, AgentConfig(spawn_unit_type=None))
        self.assertEqual(at_quota.healing_defender_admitted_id, "healer")
        self.assertIsNone(at_quota.cargo_lane.owner_id)
        self.assertEqual(at_quota.core_visit_forced_purpose, "HEAL")

    def test_critical_defender_wait_limit_preserves_one_cargo_delivery(self):
        def run_case(unit_type, hp, deposit_streak):
            queued = FakeActor("queued", (5, 0), cargo=1)
            healer = FakeActor("healer", (4, 1), unit_type=unit_type)
            healer.hp = hp
            reserve = FakeActor("reserve", (6, 2), unit_type=unit_type)
            reserve.hp = 4 if unit_type == UnitType.VANGUARD else 2
            core = FakeActor("core", (0, 0), shield=5)
            core.hp = 5
            turn = make_turn(worker=queued, core=core)
            turn.tick = 30
            turn.resources = 20
            turn.resource_capacity = 30
            turn.workers = (queued,)
            if unit_type == UnitType.VANGUARD:
                turn.vanguards = (healer, reserve)
            else:
                turn.rangers = (healer, reserve)
            turn.units = turn.workers + (healer, reserve)
            turn.state.population = len(turn.units)
            memory = TacticMemory(
                healing_defender_ids={"healer"},
                healing_defender_intent_id="healer",
                healing_defender_intent_tick=(
                    turn.tick - CRITICAL_DEFENDER_HEAL_MAX_WAIT_TICKS
                ),
                healing_defender_stage_target=healer.position,
                heal_priority_intent_id="healer",
                core_visit_deposit_streak=deposit_streak,
                cargo_lane=CargoLanePlan(
                    active=True,
                    phase="EGRESS",
                    core_position=(0, 0),
                    queued_owner_id="queued",
                    path=((0, 0), (1, 0), (2, 0), (3, 0)),
                    gateway=(3, 0),
                    started_tick=turn.tick - 5,
                    phase_started_tick=turn.tick - 1,
                    geometry_source="SINGLE_OPEN",
                ),
            )
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
            return memory

        before_delivery = run_case(UnitType.RANGER, 1, 0)
        self.assertIsNone(before_delivery.healing_defender_admitted_id)
        self.assertEqual(before_delivery.cargo_lane.owner_id, "queued")

        after_delivery = run_case(UnitType.RANGER, 1, 1)
        self.assertEqual(after_delivery.healing_defender_admitted_id, "healer")
        self.assertIsNone(after_delivery.cargo_lane.owner_id)
        self.assertEqual(after_delivery.core_visit_forced_purpose, "HEAL")

        noncritical = run_case(UnitType.VANGUARD, 3, 1)
        self.assertIsNone(noncritical.healing_defender_admitted_id)
        self.assertEqual(noncritical.cargo_lane.owner_id, "queued")

    def test_near_core_wounded_ranger_admits_without_reaching_outer_stage(self):
        queued = FakeActor("queued", (4, 1), cargo=1)
        healer = FakeActor("healer", (3, 0), unit_type=UnitType.RANGER)
        healer.hp = 1
        reserve = FakeActor("reserve", (6, 2), unit_type=UnitType.RANGER)
        reserve.hp = 2
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        turn = make_turn(worker=queued, core=core)
        turn.resources = 20
        turn.resource_capacity = 30
        turn.workers = (queued,)
        turn.rangers = (healer, reserve)
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_intent_tick=turn.tick,
            healing_defender_stage_target=(5, 0),
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=turn.tick - 5,
                phase_started_tick=turn.tick - 1,
                geometry_source="SINGLE_OPEN",
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.healing_defender_admitted_id, "healer")
        self.assertNotEqual(healer.position, memory.healing_defender_stage_target)
        self.assertTrue(any(action[0] in {"MOVE", "HEAL"} for action in healer.actions))

    def test_stalled_defender_heal_admission_rotates_without_losing_quota(self):
        queued = FakeActor("queued", (4, 1), cargo=1)
        healer = FakeActor("healer", (5, 0), unit_type=UnitType.VANGUARD)
        healer.hp = 2
        reserve = FakeActor("reserve", (6, 2), unit_type=UnitType.VANGUARD)
        reserve.hp = 2
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = frozenset({(0, -1), (-1, 0), (0, 1)})
        turn = make_turn(worker=queued, core=core, obstacles=blocked)
        turn.tick = 40
        turn.resources = 12
        turn.resource_capacity = 30
        turn.vanguards = (healer, reserve)
        turn.units = (queued, healer, reserve)
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            healing_defender_ids={"healer"},
            healing_defender_intent_id="healer",
            healing_defender_intent_tick=10,
            healing_defender_stage_target=(5, 0),
            healing_defender_admitted_id="healer",
            healing_defender_admitted_tick=20,
            healing_defender_best_distance=5,
            healing_defender_last_progress_tick=24,
            heal_priority_intent_id="healer",
            core_visit_deposit_streak=3,
            core_visit_forced_purpose="HEAL",
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                started_tick=10,
                phase_started_tick=39,
                geometry_source="SINGLE_OPEN",
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIsNone(memory.healing_defender_admitted_id)
        self.assertEqual(memory.healing_defender_intent_id, "reserve")
        self.assertGreater(memory.healing_defender_cooldowns["healer"], turn.tick)
        self.assertEqual(memory.heal_priority_intent_id, "reserve")
        self.assertEqual(memory.core_visit_deposit_streak, 3)
        self.assertEqual(memory.core_visit_forced_purpose, "HEAL")
        self.assertIsNone(memory.cargo_lane.owner_id)

    def test_egress_watchdog_reports_stalled_departing_without_unsafe_handoff(self):
        departing = FakeActor("departing", (2, 0), cargo=0)
        queued = FakeActor("queued", (4, 1), cargo=1)
        ranger = FakeActor("ranger", (3, 0), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocked = {(0, -1), (-1, 0), (0, 1)}
        turn = make_turn(worker=departing, core=core, obstacles=frozenset(blocked))
        turn.tick = 30
        turn.workers = (departing, queued)
        turn.rangers = (ranger,)
        turn.units = turn.workers + turn.rangers
        turn.state.population = len(turn.units)
        memory = TacticMemory(
            cargo_lane=CargoLanePlan(
                active=True,
                phase="EGRESS",
                core_position=(0, 0),
                queued_owner_id="queued",
                departing_worker_id="departing",
                path=((0, 0), (1, 0), (2, 0), (3, 0)),
                gateway=(3, 0),
                egress_path=((0, 0), (1, 0), (2, 0), (3, 0), (4, 0)),
                egress_target=(4, 0),
                started_tick=1,
                phase_started_tick=14,
                geometry_source="SINGLE_OPEN",
                departing_progress_id="departing",
                departing_best_remaining_steps=2,
                departing_last_progress_tick=14,
            )
        )

        _update_cargo_lane(
            turn,
            (0, 0),
            blocked,
            set(),
            _friendly_cell_occupancy(turn),
            memory,
            core_accepts_delivery=True,
            core_stable=True,
            threat_level="NORMAL",
        )

        self.assertEqual(memory.cargo_lane.phase, "EGRESS")
        self.assertEqual(memory.cargo_lane.departing_worker_id, "departing")
        self.assertEqual(memory.cargo_lane.queued_owner_id, "queued")
        self.assertIsNone(memory.cargo_lane.owner_id)
        self.assertEqual(memory.cargo_lane.watchdog_tick, turn.tick)
        self.assertEqual(
            memory.cargo_lane.watchdog_reason,
            "EGRESS_DEPARTING_STALLED",
        )
        self.assertIn("ranger", memory.cargo_lane.yield_unit_ids)

    def test_tick_93715_detects_right_lane_and_seven_startup_workers(self):
        core_position = (-217, 666)
        obstacles = {
            (-223, 669), (-221, 662), (-221, 670), (-220, 664),
            (-220, 666), (-219, 659), (-219, 665), (-219, 669),
            (-217, 670), (-216, 665), (-216, 671), (-215, 659),
            (-215, 665), (-215, 670), (-214, 672), (-213, 659),
            (-213, 660), (-213, 663), (-212, 666), (-212, 670),
            (-211, 661), (-211, 668), (-211, 669),
        }
        worker_specs = (
            ("0f13adab", (-213, 662), 1),
            ("57606495", (-212, 658), 0),
            ("626fe5a3", (-217, 664), 0),
            ("656d3944", (-219, 666), 0),
            ("6e8fe8f1", (-217, 665), 0),
            ("9a2a356b", (-216, 667), 0),
            ("cee8bd78", (-218, 665), 0),
            ("dd996335", (-217, 667), 0),
            ("f0c09f3e", (-218, 667), 0),
        )
        guard_specs = (
            ("3df43662", (-216, 668)), ("3f0bd5b9", (-218, 664)),
            ("4da07eb7", (-215, 667)), ("6a24bb5a", (-218, 662)),
            ("70f06c55", (-215, 662)), ("fb8b4510", (-215, 664)),
            ("23f6dad8", (-217, 668)), ("3059ebff", (-214, 667)),
            ("3f55a749", (-218, 668)), ("4a49109c", (-216, 664)),
            ("5ee3e17e", (-217, 663)), ("7e94b747", (-215, 663)),
            ("839247ac", (-216, 663)), ("c1c37a0a", (-219, 667)),
            ("e2790ccf", (-219, 668)),
        )
        turn = make_pocket_snapshot(93715, obstacles, worker_specs, guard_specs)
        turn.core.position = core_position
        occupancy = _friendly_cell_occupancy(turn)

        path = _single_open_cargo_lane_path(
            core_position,
            obstacles,
            occupancy,
        )
        startup_ids = _cargo_lane_startup_workers(turn.workers, core_position)

        self.assertEqual(
            path,
            ((-217, 666), (-216, 666), (-215, 666), (-214, 666)),
        )
        self.assertEqual(
            startup_ids,
            {
                "626fe5a3", "656d3944", "6e8fe8f1", "9a2a356b",
                "cee8bd78", "dd996335", "f0c09f3e",
            },
        )

        for worker in turn.workers:
            worker.cargo = 0
        memory = TacticMemory()
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertTrue(memory.cargo_lane.active)
        self.assertEqual(memory.cargo_lane.phase, "STARTUP_EVACUATION")
        self.assertIsNone(memory.cargo_lane.owner_id)
        self.assertEqual(memory.cargo_lane.startup_pending_ids, startup_ids)

    def test_occupied_core_clears_even_when_another_exit_is_free(self):
        cargo = FakeActor("a-cargo", (0, 1), cargo=1)
        empty = FakeActor("b-empty", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=empty, core=core)
        turn.workers = (cargo, empty)
        turn.units = (cargo, empty)
        turn.state.population = 2
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.core_lane_departing_worker_id, "b-empty")
        self.assertEqual(empty.actions, [("MOVE", Direction.UP)])

    def test_heal_intent_on_core_is_not_selected_for_cargo_clearance(self):
        cargo = FakeActor("cargo", (0, 1), cargo=1)
        healer = FakeActor("healer", (0, 0), cargo=0)
        healer.hp = 1
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=healer, core=core)
        turn.workers = (cargo, healer)
        turn.units = (cargo, healer)
        turn.state.population = 2
        turn.resources = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            core_visit_forced_purpose="HEAL",
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertIsNone(memory.core_lane_departing_worker_id)
        self.assertEqual(healer.actions, [("HEAL",)])
        self.assertEqual(memory.core_visit.purpose, "HEAL")

    def test_idle_core_worker_egresses_as_scout_when_frontier_is_unreachable(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        obstacles = frozenset(
            {(0, -1), (1, 0), (0, 1), (-1, -1), (-2, 0), (-1, 1)}
        )
        turn = make_turn(worker=worker, core=core, obstacles=obstacles)
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])
        self.assertEqual(memory.scout_progress["worker"].target, (-1, 0))

    def test_capacity_blocked_scout_keeps_its_assignment(self):
        worker = FakeActor("worker", (0, 0), cargo=0)
        core = FakeActor("core", (10, 10))
        core.hp = 5
        blockers = tuple(
            FakeActor(f"guard-{index}", position, unit_type=UnitType.VANGUARD)
            for index, position in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0)))
        )
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = blockers
        turn.units = (worker,) + blockers
        turn.state.population = len(turn.units)
        memory = TacticMemory()

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertIn("worker", memory.scout_progress)
        self.assertEqual(memory.route_diagnostics["worker"], "CAPACITY_BLOCKED")

    def test_distant_cargo_approaches_an_occupied_core_before_queueing(self):
        worker = FakeActor("worker", (0, 2), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        guard = FakeActor("vanguard", (0, 0), unit_type=UnitType.VANGUARD)
        turn = make_turn(worker=worker, core=core)
        turn.vanguards = (guard,)
        turn.units = (worker, guard)
        turn.state.population = 2

        plan_turn(turn, TacticMemory(), AgentConfig(spawn_unit_type=None))

        self.assertEqual(worker.actions, [("MOVE", Direction.UP)])

    def test_chebyshev_guard_ring_is_complete_and_deterministic(self):
        first = _chebyshev_ring_positions((0, 0), 3)
        second = _chebyshev_ring_positions((0, 0), 3)

        self.assertEqual(len(first), 24)
        self.assertEqual(first, second)
        self.assertNotIn((0, 0), first)

    def test_guard_assignment_uses_dummy_when_posts_are_insufficient(self):
        units = [
            FakeActor(f"vanguard-{index}", (0, 0), unit_type=UnitType.VANGUARD)
            for index in range(3)
        ]
        ring = set(_chebyshev_ring_positions((0, 0), 1))
        legal = {(-1, 0), (1, 0)}

        assignments, _, idle_count = _assign_guard_posts(
            units,
            (0, 0),
            (1,),
            ring - legal,
            set(),
            set(),
            set(),
            set(),
            {(0, 0): 3},
        )

        self.assertEqual(idle_count, 1)
        self.assertEqual(len(assignments), 2)
        self.assertEqual(set(assignments.values()), legal)

    def test_guard_can_keep_own_post_but_not_take_an_occupied_post(self):
        first = FakeActor("vanguard-a", (1, 0), unit_type=UnitType.VANGUARD)
        second = FakeActor("vanguard-b", (5, 5), unit_type=UnitType.VANGUARD)
        ring = set(_chebyshev_ring_positions((0, 0), 1))
        candidates = {(1, 0), (0, 1)}

        assignments, _, idle_count = _assign_guard_posts(
            (first, second),
            (0, 0),
            (1,),
            ring - candidates,
            set(),
            set(),
            set(),
            set(),
            {(1, 0): 1, (0, 1): 1, (5, 5): 1},
        )

        self.assertEqual(assignments["vanguard-a"], (1, 0))
        self.assertEqual(assignments["vanguard-b"], (5, 5))
        self.assertNotIn((0, 1), assignments.values())
        self.assertEqual(idle_count, 1)

    def test_guard_cannot_idle_on_a_reserved_core_neighbor(self):
        guard = FakeActor("ranger", (0, -1), unit_type=UnitType.RANGER)
        reserved = {(0, -1)}

        assignments, _, idle_count = _assign_guard_posts(
            (guard,),
            (0, 0),
            (1, 2),
            set(),
            set(),
            set(),
            set(),
            reserved,
            {(0, -1): 1},
        )

        self.assertNotEqual(assignments["ranger"], (0, -1))
        self.assertNotIn(assignments["ranger"], reserved)
        self.assertEqual(idle_count, 0)

    def test_raid_assignment_pads_three_rows_for_two_real_positions(self):
        units = [
            FakeActor(f"vanguard-{index}", (0, index + 1), unit_type=UnitType.VANGUARD)
            for index in range(3)
        ]

        assignments, idle_count = _assign_raid_positions(
            units,
            ((1, 0), (-1, 0)),
            set(),
            {},
        )

        self.assertEqual(len(assignments), 2)
        self.assertEqual(idle_count, 1)
        self.assertGreater(PATH_COST_UNREACHABLE, IDLE_ASSIGNMENT_COST)

    def test_raid_assignment_keeps_own_cell_and_rejects_another_occupied_cell(self):
        first = FakeActor("vanguard-a", (1, 0), unit_type=UnitType.VANGUARD)
        second = FakeActor("vanguard-b", (2, 0), unit_type=UnitType.VANGUARD)

        assignments, idle_count = _assign_raid_positions(
            (first, second),
            ((1, 0), (0, 1)),
            set(),
            {(1, 0): 1, (0, 1): 1, (2, 0): 1},
        )

        self.assertEqual(assignments, {"vanguard-a": (1, 0)})
        self.assertEqual(idle_count, 1)

    def test_core_and_worker_same_cell_have_twelve_durability(self):
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(2, 2),
            hp=5,
            shield=5,
            owner_username="enemy",
        )
        enemy_worker = FakeActor(
            "enemy-worker",
            (2, 2),
            unit_type=UnitType.WORKER,
        )

        self.assertEqual(
            _raid_target_durability(enemy_core, (enemy_core, enemy_worker)),
            12,
        )

    def test_established_defense_keeps_ten_resource_production_reserve(self):
        workers = tuple(FakeActor(f"worker-{index}", (0, 0)) for index in range(4))
        vanguard = FakeActor("vanguard", (1, 0), unit_type=UnitType.VANGUARD)
        ranger = FakeActor("ranger", (0, 1), unit_type=UnitType.RANGER)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.vanguards = (vanguard,)
        turn.rangers = (ranger,)
        turn.units = workers + (vanguard, ranger)
        turn.state.population = 6
        turn.resources = 14

        plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(worker_target=5, vanguard_target=1, ranger_target=1),
        )

        self.assertEqual(core.actions, [("WAIT",)])

    def test_core_raid_observer_handoff_and_visible_attacks(self):
        observer = FakeActor("observer", (8, 1))
        core = FakeActor("core", (0, 0))
        core.hp = 5
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (-3, index), unit_type=UnitType.VANGUARD)
            for index in range(3)
        )
        for unit in vanguards:
            unit.hp = 4
        rangers = tuple(
            FakeActor(f"ranger-{index}", (-2, index), unit_type=UnitType.RANGER)
            for index in range(3)
        )
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        enemy_worker = FakeActor(
            "enemy-worker",
            (10, 0),
            unit_type=UnitType.WORKER,
        )
        turn = make_turn(
            worker=observer,
            core=core,
            enemies=(enemy_core, enemy_worker),
        )
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = (observer,) + vanguards + rangers
        turn.state.population = len(turn.units)
        turn.resources = 22
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        for tick in range(10, 15):
            turn.tick = tick
            for unit in turn.units:
                unit.actions.clear()
            plan_turn(turn, memory, config)

        self.assertEqual(memory.raid.state, "CORE_STAGING")
        self.assertEqual(memory.raid.observer_id, "observer")
        self.assertEqual(len(memory.raid.raid_member_ids), 4)
        member_types = {
            unit_id: unit.unit_type
            for unit_id, unit in {
                str(unit.id): unit for unit in vanguards + rangers
            }.items()
            if unit_id in memory.raid.raid_member_ids
        }
        self.assertEqual(sum(value == UnitType.VANGUARD for value in member_types.values()), 2)
        self.assertEqual(sum(value == UnitType.RANGER for value in member_types.values()), 2)

        combat_by_id = {str(unit.id): unit for unit in vanguards + rangers}
        for member_id, position in memory.raid.assignments.items():
            combat_by_id[member_id].position = position
            combat_by_id[member_id].actions.clear()
        observer.actions.clear()
        turn.tick = 15
        plan_turn(turn, memory, config)

        self.assertEqual(memory.raid.state, "CORE_RAID")
        self.assertIsNone(memory.raid.observer_id)
        self.assertTrue(
            any(
                action[0] in {"SWEEP", "SHOOT"}
                for member_id in memory.raid.raid_member_ids
                for action in combat_by_id[member_id].actions
            )
        )
        self.assertEqual(memory.raid.last_durability, 12)

        enemy_worker.hp = 1
        for unit in turn.units:
            unit.actions.clear()
        turn.tick = 16
        plan_turn(turn, memory, config)
        self.assertEqual(memory.raid.stalled_ticks, 0)
        self.assertEqual(memory.raid.last_durability, 11)

    def test_lost_observer_uses_eight_tick_replacement_and_safe_ring(self):
        observer = FakeActor("observer", (0, 0))
        replacement = FakeActor("replacement", (8, 2))
        core = FakeActor("core", (0, 0))
        core.hp = 5
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (index + 1, 0), unit_type=UnitType.VANGUARD)
            for index in range(3)
        )
        for unit in vanguards:
            unit.hp = 4
        rangers = tuple(
            FakeActor(f"ranger-{index}", (index + 1, 1), unit_type=UnitType.RANGER)
            for index in range(3)
        )
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(
            worker=observer,
            core=core,
            enemies=(enemy_core,),
        )
        turn.workers = (observer, replacement)
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = (observer, replacement) + vanguards + rangers
        turn.state.population = len(turn.units)
        turn.resources = 22
        member_ids = {
            str(vanguards[1].id),
            str(vanguards[2].id),
            str(rangers[1].id),
            str(rangers[2].id),
        }
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_STAGING",
                target_id="enemy-core",
                target_position=(10, 0),
                observer_id="observer",
                observer_position=(8, 1),
                observer_confirmed=True,
                raid_member_ids=set(member_ids),
                assignments={member_id: (9, 0) for member_id in member_ids},
            ),
        )
        turn.tick = 20

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.observer_id, "replacement")
        self.assertEqual(memory.raid.replacement_deadline, 28)
        self.assertTrue(memory.raid.assignments)
        self.assertTrue(
            all(
                max(abs(position[0] - 10), abs(position[1])) > 5
                for position in memory.raid.assignments.values()
            )
        )

        replacement.position = memory.raid.observer_position
        for unit in turn.units:
            unit.actions.clear()
        turn.tick = 21
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        self.assertEqual(memory.raid.replacement_deadline, 0)
        self.assertTrue(memory.raid.raid_member_ids)

    def test_lost_observer_without_replacement_recalls_immediately(self):
        observer = FakeActor("observer", (4, 0))
        core = FakeActor("core", (0, 0))
        core.hp = 5
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=observer, core=core, enemies=(enemy_core,))
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_STAGING",
                target_id="enemy-core",
                target_position=(10, 0),
                observer_id="observer",
                observer_position=(8, 1),
                observer_confirmed=True,
            ),
        )
        turn.tick = 20

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.state, "CORE_RECALL")
        self.assertIn("observer", memory.core_observer_return_ids)

    def test_damaged_raid_vanguard_recalls_without_attacking(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        vanguard = FakeActor("vanguard", (9, 0), unit_type=UnitType.VANGUARD)
        vanguard.hp = 2
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        turn.vanguards = (vanguard,)
        turn.units = (vanguard,)
        turn.state.population = 1
        turn.tick = 20
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_RAID",
                target_id="enemy-core",
                target_position=(10, 0),
                raid_member_ids={"vanguard"},
                assignments={"vanguard": (9, 0)},
                last_durability=10,
                last_formation_cost=0,
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.state, "CORE_RECALL")
        self.assertFalse(any(action[0] == "SWEEP" for action in vanguard.actions))

    def test_one_hp_raid_ranger_recalls_immediately_without_attacking(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        ranger = FakeActor("ranger", (8, 0), unit_type=UnitType.RANGER)
        ranger.hp = 1
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        turn.tick = 20
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_RAID",
                target_id="enemy-core",
                target_position=(10, 0),
                raid_member_ids={"ranger"},
                assignments={"ranger": (8, 0)},
                last_durability=10,
                last_formation_cost=0,
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        self.assertEqual(memory.raid.state, "CORE_RECALL")
        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))
        self.assertEqual(ranger.actions, [("MOVE", Direction.LEFT)])

    def test_core_raid_recalls_after_six_ticks_without_progress(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        ranger = FakeActor("ranger", (8, 0), unit_type=UnitType.RANGER)
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        turn.rangers = (ranger,)
        turn.units = (ranger,)
        turn.state.population = 1
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_RAID",
                target_id="enemy-core",
                target_position=(10, 0),
                raid_member_ids={"ranger"},
                assignments={"ranger": (8, 0)},
                last_durability=10,
                last_formation_cost=0,
            ),
        )

        for tick in range(20, 26):
            turn.tick = tick
            ranger.actions.clear()
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.state, "CORE_RECALL")

    def test_core_staging_replans_an_occupied_assignment(self):
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        blocker = FakeActor("blocker", (9, 0))
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (-index - 1, 0), UnitType.VANGUARD)
            for index in range(3)
        )
        for unit in vanguards:
            unit.hp = 4
        rangers = tuple(
            FakeActor(f"ranger-{index}", (-index - 1, 1), UnitType.RANGER)
            for index in range(3)
        )
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=blocker, core=core, enemies=(enemy_core,))
        turn.tick = 20
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = (blocker,) + vanguards + rangers
        turn.state.population = len(turn.units)
        turn.resources = 22
        member_ids = {"vanguard-1", "vanguard-2", "ranger-1", "ranger-2"}
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_STAGING",
                target_id="enemy-core",
                target_position=(10, 0),
                raid_member_ids=set(member_ids),
                assignments={
                    "vanguard-1": (9, 0),
                    "vanguard-2": (10, 1),
                    "ranger-1": (8, 0),
                    "ranger-2": (10, 2),
                },
            ),
        )

        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.state, "CORE_STAGING")
        self.assertEqual(memory.raid.last_replan_tick, 20)
        self.assertEqual(memory.raid.last_replan_reason, "FRIENDLY_OCCUPIED")
        self.assertNotIn((9, 0), memory.raid.assignments.values())

    def test_core_staging_recalls_after_six_unchanged_route_ticks(self):
        core = FakeActor("core", (0, 0), shield=5)
        core.hp = 5
        vanguards = tuple(
            FakeActor(f"vanguard-{index}", (-index - 1, 0), UnitType.VANGUARD)
            for index in range(3)
        )
        for unit in vanguards:
            unit.hp = 4
        rangers = tuple(
            FakeActor(f"ranger-{index}", (-index - 1, 1), UnitType.RANGER)
            for index in range(3)
        )
        enemy_core = SimpleNamespace(
            id="enemy-core",
            position=(10, 0),
            hp=5,
            shield=5,
            owner_username="enemy",
            state=CoreState.NORMAL,
        )
        turn = make_turn(worker=None, core=core, enemies=(enemy_core,))
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = vanguards + rangers
        turn.state.population = len(turn.units)
        turn.resources = 22
        memory = TacticMemory(
            enemy_core_memory={
                "enemy-core": EnemyCoreObservation(
                    "enemy-core", (10, 0), 5, 5, "NORMAL", 20
                )
            },
            raid=CoreRaidPlan(
                state="CORE_STAGING",
                target_id="enemy-core",
                target_position=(10, 0),
                raid_member_ids={
                    "vanguard-1",
                    "vanguard-2",
                    "ranger-1",
                    "ranger-2",
                },
                assignments={
                    "vanguard-1": (9, 0),
                    "vanguard-2": (10, 1),
                    "ranger-1": (8, 0),
                    "ranger-2": (10, 2),
                },
            ),
        )

        for tick in range(20, 27):
            turn.tick = tick
            for unit in turn.units:
                unit.actions.clear()
            plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertEqual(memory.raid.state, "CORE_RECALL")

    def test_status_panel_uses_localized_labels(self):
        workers = (
            FakeActor("worker-1", (3, 4), cargo=1),
            FakeActor("worker-2", (4, 4), cargo=0),
            FakeActor("worker-3", (5, 4), cargo=2),
        )
        workers[2].hp = 1
        vanguards = (
            FakeActor("vanguard-1", (6, 4), unit_type=UnitType.VANGUARD),
            FakeActor("vanguard-2", (7, 4), unit_type=UnitType.VANGUARD),
        )
        vanguards[0].hp = 4
        vanguards[1].hp = 2
        rangers = (
            FakeActor("ranger-1", (8, 4), unit_type=UnitType.RANGER),
            FakeActor("ranger-2", (9, 4), unit_type=UnitType.RANGER),
        )
        rangers[1].hp = 1
        core = FakeActor("core", (1, 2))
        turn = make_turn(worker=workers[0], core=core)
        turn.workers = workers
        turn.vanguards = vanguards
        turn.rangers = rangers
        turn.units = workers + vanguards + rangers
        turn.resource_capacity = 20
        turn.state.status = CoreState.NORMAL
        report = PlanReport(
            12,
            7,
            7,
            3,
            2,
            2,
            0,
            2,
            1,
            vanguard_defenders=1,
            vanguard_raid=1,
            vanguard_attacks=1,
            ranger_defenders=2,
            ranger_attacks=1,
        )
        output = StringIO()

        with redirect_stdout(output):
            _render_turn(turn, report, SimpleNamespace(accepted=True))

        rendered = output.getvalue()
        self.assertIn("控制台", rendered)
        self.assertIn("回合:12", rendered)
        self.assertIn(
            "工人状态: 总数:3   携货:2   空手:1   货物合计:3   血量:5/6   未满血:1",
            rendered,
        )
        self.assertIn(
            "先锋状态: 防守:1   出征:1   本轮攻击:1   血量:6/8   未满血:1   危急:1",
            rendered,
        )
        self.assertIn(
            "游侠状态: 防守:2   出征:0   本轮攻击:1   血量:3/4   未满血:1   危急:1",
            rendered,
        )
        self.assertIn("物流通道: IDLE", rendered)
        self.assertIn("治疗通行: 无", rendered)
        self.assertNotIn("工人1", rendered)
        self.assertNotIn("(3, 4)", rendered)
        self.assertIn("生产报价", rendered)
        self.assertNotIn("维护", rendered)
        self.assertNotIn("Tick", rendered)

    def test_plan_report_counts_combat_attacks_and_defenders(self):
        core = FakeActor("core", (0, 0))
        core.hp = 5
        vanguard = FakeActor(
            "vanguard",
            (0, 1),
            unit_type=UnitType.VANGUARD,
        )
        vanguard.hp = 4
        ranger = FakeActor("ranger", (0, 2), unit_type=UnitType.RANGER)
        enemy = FakeActor("enemy", (1, 1), unit_type=UnitType.VANGUARD)
        enemy.hp = 4
        turn = make_turn(worker=None, core=core, enemies=(enemy,))
        turn.vanguards = (vanguard,)
        turn.rangers = (ranger,)
        turn.units = (vanguard, ranger)
        turn.state.population = 2

        report = plan_turn(
            turn,
            TacticMemory(),
            AgentConfig(spawn_unit_type=None),
        )

        self.assertEqual(vanguard.actions, [("SWEEP", Direction.RIGHT)])
        self.assertEqual(ranger.actions, [("SHOOT", "enemy")])
        self.assertEqual(report.vanguard_defenders, 1)
        self.assertEqual(report.vanguard_raid, 0)
        self.assertEqual(report.vanguard_attacks, 1)
        self.assertEqual(report.ranger_defenders, 1)
        self.assertEqual(report.ranger_raid, 0)
        self.assertEqual(report.ranger_attacks, 1)

    def test_remote_healing_worker_stages_outside_active_cargo_lane(self):
        healer = FakeActor("healer", (10, 10))
        healer.hp = 1
        owner = FakeActor("owner", (1, 0), cargo=1)
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=healer, core=core)
        turn.tick = 20
        turn.resources = 20
        turn.resource_capacity = 100
        turn.resource_space = 80
        turn.workers = (healer, owner)
        turn.units = turn.workers
        turn.state.population = 2
        lane = CargoLanePlan(
            active=True,
            phase="INBOUND",
            core_position=core.position,
            owner_id="owner",
            path=((0, 0), (1, 0)),
            gateway=(1, 0),
        )
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=turn.tick,
            cargo_lane=lane,
        )
        occupancy = _friendly_cell_occupancy(turn)
        target = _refresh_healing_worker_stage_target(
            turn,
            core.position,
            set(),
            set(),
            occupancy,
            memory,
        )

        self.assertIsNotNone(target)
        self.assertNotIn(target, lane.path)
        self.assertEqual(_worker_mode(healer, memory, turn.tick), "HEAL_STAGE")

        _plan_workers(
            turn,
            core,
            set(),
            None,
            memory,
            resource_memory_ttl=32,
            combat_enemies=[],
            visible_enemies=[],
            danger_cells=set(),
            friendly_occupancy=occupancy,
        )
        self.assertTrue(healer.actions)
        self.assertEqual(healer.actions[0][0], "MOVE")
        self.assertNotEqual(
            _next_position(healer.position, healer.actions[0][1]),
            (0, 0),
        )

    def test_healing_intent_survives_active_cargo_lane_wait(self):
        healer = FakeActor("healer", (5, 5))
        healer.hp = 1
        core = FakeActor("core", (0, 0))
        core.hp = 5
        turn = make_turn(worker=healer, core=core)
        turn.tick = 20
        memory = TacticMemory(
            healing_worker_ids={"healer"},
            heal_intent_id="healer",
            heal_intent_tick=1,
            heal_intent_best_distance=10,
            cargo_lane=CargoLanePlan(
                active=True,
                core_position=core.position,
                owner_id="owner",
                path=((0, 0), (1, 0)),
                gateway=(1, 0),
            ),
        )

        memory.observe(turn)

        self.assertEqual(memory.heal_intent_id, "healer")


if __name__ == "__main__":
    unittest.main()
