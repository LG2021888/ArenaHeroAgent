from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from arena_agent import (
    AgentConfig,
    PlanReport,
    ScoutProgress,
    SessionRecorder,
    TacticMemory,
    UnitType,
    CORE_TARGET_MEMORY_TTL,
    IDLE_ASSIGNMENT_COST,
    PATH_COST_UNREACHABLE,
    ROUTE_MAX_EXPANSIONS,
    CoreRaidPlan,
    EnemyCoreObservation,
    _assign_guard_posts,
    _assign_raid_positions,
    _chebyshev_ring_positions,
    _cached_worker_route_step,
    _complete_route,
    _raid_target_durability,
    _render_turn,
    _estimated_path_cost,
    _is_retryable_protocol_error,
    _is_retryable_api_error,
    _load_tactic_memory,
    _parse_stream_message_with_compatibility,
    _parse_args,
    _reconnect_delay,
    _save_tactic_memory,
    _submit_turn_with_retry,
    _unit_cost,
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

        self.assertEqual(config.max_population, 30)
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
        core = FakeActor("core", (0, 0))
        turn = make_turn(worker=None, core=core)
        turn.events = (
            SimpleNamespace(
                event_type="CORE_RESOURCES_CAPTURED",
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

        self.assertEqual(worker.actions, [("MOVE", Direction.UP)])

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

    def test_one_hp_raid_ranger_attacks_once_then_recalls(self):
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
        self.assertEqual(ranger.actions, [("SHOOT", "enemy-core")])
        self.assertEqual(memory.raid.state, "CORE_RAID")

        ranger.actions.clear()
        turn.tick = 21
        plan_turn(turn, memory, AgentConfig(spawn_unit_type=None))
        self.assertEqual(memory.raid.state, "CORE_RECALL")
        self.assertFalse(any(action[0] == "SHOOT" for action in ranger.actions))

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
