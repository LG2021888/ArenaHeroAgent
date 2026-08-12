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
    ScoutProgress,
    SessionRecorder,
    TacticMemory,
    UnitType,
    CORE_TARGET_MEMORY_TTL,
    IDLE_ASSIGNMENT_COST,
    PATH_COST_UNREACHABLE,
    ROUTE_MAX_EXPANSIONS,
    MOVE_CONTESTED_AVOID_TICKS,
    MOVE_OCCUPIED_AVOID_TICKS,
    CoreRaidPlan,
    EnemyCoreObservation,
    _assign_guard_posts,
    _assign_raid_positions,
    _chebyshev_ring_positions,
    _cached_worker_route_step,
    _choose_spawn_unit,
    _complete_route,
    _core_escape_direction,
    _friendly_cell_occupancy,
    _raid_target_durability,
    _render_turn,
    _estimated_path_cost,
    _is_retryable_protocol_error,
    _is_retryable_api_error,
    _load_tactic_memory,
    _next_position,
    _parse_stream_message_with_compatibility,
    _parse_args,
    _queue_move,
    _reconnect_delay,
    _save_tactic_memory,
    _submit_turn_with_retry,
    _trace_event,
    _update_core_pocket,
    _unit_cost,
    _worker_escape_direction,
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

        result = _submit_turn_with_retry(
            turn,
            attempts=2,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(result, "accepted")
        self.assertEqual(turn.calls[0], turn.calls[1])
        self.assertTrue(turn.calls[0].startswith("arena-agent-"))
        self.assertTrue(turn.calls[0].endswith("-42"))

    def test_retryable_api_error_reuses_idempotency_key(self):
        turn = RetryableApiTurn()

        result = _submit_turn_with_retry(
            turn,
            attempts=2,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(result, "accepted")
        self.assertEqual(turn.calls[0], turn.calls[1])
        self.assertTrue(turn.calls[0].startswith("arena-agent-"))
        self.assertTrue(turn.calls[0].endswith("-43"))

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
        self.assertEqual(core.actions, [("SPAWN", UnitType.WORKER)])
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

        self.assertEqual(worker.actions, [("MOVE", Direction.LEFT)])
        self.assertEqual(memory.scout_progress["worker"].path_stalled_turns, 0)

        worker.actions.clear()
        worker.position = (2, 0)
        turn.tick = 12
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))

        self.assertNotIn("worker", memory.scout_return_targets)
        self.assertEqual(worker.actions, [("WAIT",)])
        self.assertEqual(memory.scout_progress["worker"].path_stalled_turns, 0)

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
        memory = TacticMemory()
        config = AgentConfig(spawn_unit_type=None)

        for tick, enemy_x in ((10, 50), (11, 49), (12, 48)):
            turn.tick = tick
            enemy.position = (enemy_x, 0)
            core.actions.clear()
            report = plan_turn(turn, memory, config)

        self.assertEqual(report.threat_level, "ALERT")
        self.assertEqual(core.actions, [("WAIT",)])

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
        enemy = FakeActor("enemy", (20, 0), unit_type=UnitType.RANGER)
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
        queued_ready_at_deposit = []
        lane_history = []
        config = AgentConfig(spawn_unit_type=None)
        for _ in range(96):
            action_tick = turn.tick
            plan_turn(turn, memory, config)
            workers_by_id = {str(worker.id): worker for worker in turn.workers}
            lane_history.append(
                (
                    action_tick,
                    memory.cargo_lane.owner_id,
                    memory.cargo_lane.queued_owner_id,
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
                queued_id = memory.cargo_lane.queued_owner_id
                workers_after_actions = {
                    str(worker.id): worker for worker in turn.workers
                }
                queued_ready_at_deposit.extend(
                    [
                        bool(
                            queued_id in workers_after_actions
                            and workers_after_actions[queued_id].position
                            == memory.cargo_lane.gateway
                        )
                    ]
                    * len(deposited)
                )
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
        steady_intervals = [
            interval
            for interval, queued_ready in zip(
                intervals,
                queued_ready_at_deposit,
            )
            if queued_ready
        ]
        self.assertGreaterEqual(
            len(steady_intervals),
            4,
            f"deposit_ticks={deposit_ticks}, queued={queued_ready_at_deposit}",
        )
        self.assertTrue(
            all(interval <= 4 for interval in steady_intervals),
            f"deposit_ticks={deposit_ticks}, intervals={intervals}, "
            f"queued={queued_ready_at_deposit}, "
            f"history={lane_history}",
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

        plan_turn(turn, memory, config)

        self.assertEqual(cargo.actions, [("MOVE", Direction.UP)])
        self.assertEqual(empty.actions, [("WAIT",)])

        cargo.position = (-1, -1)
        cargo.actions.clear()
        empty.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, config)

        self.assertEqual(cargo.actions, [("WAIT",)])
        self.assertEqual(empty.actions, [("MOVE", Direction.LEFT)])

        empty.position = (-1, 0)
        cargo.actions.clear()
        empty.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, config)

        self.assertEqual(cargo.actions, [("WAIT",)])
        self.assertEqual(empty.actions, [("MOVE", Direction.DOWN)])

        empty.position = (-1, 1)
        cargo.actions.clear()
        empty.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, config)
        self.assertEqual(cargo.actions, [("MOVE", Direction.DOWN)])

        cargo.position = (-1, 0)
        cargo.actions.clear()
        empty.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, config)
        self.assertEqual(cargo.actions, [("MOVE", Direction.RIGHT)])

        cargo.position = (0, 0)
        cargo.actions.clear()
        empty.actions.clear()
        turn.tick += 1
        plan_turn(turn, memory, config)
        self.assertEqual(cargo.actions, [("DEPOSIT",)])

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
        worker = FakeActor("worker", (3, 4), cargo=1)
        core = FakeActor("core", (1, 2))
        turn = make_turn(worker=worker, core=core)
        turn.resource_capacity = 20
        turn.state.status = CoreState.NORMAL
        report = PlanReport(12, 7, 1, 1, 0, 0, 0, 2, 1)
        output = StringIO()

        with redirect_stdout(output):
            _render_turn(turn, report, SimpleNamespace(accepted=True))

        rendered = output.getvalue()
        self.assertIn("控制台", rendered)
        self.assertIn("回合:12", rendered)
        self.assertIn("工人1", rendered)
        self.assertIn("生产报价", rendered)
        self.assertNotIn("维护", rendered)
        self.assertNotIn("Tick", rendered)


if __name__ == "__main__":
    unittest.main()
