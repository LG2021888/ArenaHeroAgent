"""A continuously running Arena Hero tactic built on the official Python SDK."""

from __future__ import annotations

import argparse
import atexit
from collections import deque
from datetime import datetime, timezone
import heapq
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sys
import time
import uuid
import arena_hero as arena_hero_package
import arena_hero.client as arena_hero_client_module
import arena_hero._protocol as arena_hero_protocol_module
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Iterable

from arena_hero import (
    ArenaHeroClient,
    ArenaHeroError,
    APIError,
    AuthenticationError,
    BeaconStatus,
    CoreState,
    Direction,
    PolicyViolationError,
    ProtocolError,
    TransportError,
    UnitType,
)

try:
    from arena_hero import unit_cost as _sdk_unit_cost
except ImportError:
    # Keep local tests and an older installed SDK usable during upgrades.
    _sdk_unit_cost = None


LOGGER = logging.getLogger("arena_agent")
ARENA_HERO_SDK_VERSION = getattr(arena_hero_package, "__version__", "unknown")
EXPECTED_ARENA_HERO_SDK_VERSION = "0.2.9"
_AGENT_SESSION_ID = uuid.uuid4().hex
_protocol_compatibility_warned = False
Position = tuple[int, int]
SCOUT_VECTORS: tuple[Position, ...] = (
    (-1, 0),
    (1, 0),
    (0, -1),
    (0, 1),
    (-1, -1),
    (1, -1),
    (-1, 1),
    (1, 1),
)
SCOUT_RING_STEP = 10
SCOUT_SECTOR_SWEEP_CHUNKS = 4
SCOUT_CHUNK_SIZE = 32
SCOUT_MAX_TARGET_ATTEMPTS = len(SCOUT_VECTORS) * SCOUT_SECTOR_SWEEP_CHUNKS
RESOURCE_STALL_TICKS = 6
RESOURCE_COOLDOWN_TICKS = 8
# A Worker that has not changed cells is genuinely blocked; release it
# quickly. Route progress compares consecutive estimates so a newly discovered
# detour can keep moving as long as its remaining cost is falling.
SCOUT_NO_MOVE_TICKS = 4
SCOUT_PATH_STALL_TICKS = 3
SCOUT_RETURN_STALL_TICKS = 8
SCOUT_INJURED_MAX_DISTANCE = 40
SCOUT_RISK_MEMORY_TICKS = 96
HEAL_VISIT_TIMEOUT_TICKS = 12
HEAL_INTENT_TIMEOUT_TICKS = 12
HEAL_INTENT_COOLDOWN_TICKS = 12
HEAL_PRIORITY_DEPOSIT_LIMIT = 3
CRITICAL_DEFENDER_HEAL_MAX_WAIT_TICKS = 8
CRITICAL_DEFENDER_HEAL_MIN_DEPOSITS = 1
DEFENDER_HEAL_ADMISSION_TIMEOUT_TICKS = 16
DEFENDER_HEAL_COOLDOWN_TICKS = 12
HEAL_PRIORITY_MAX_ADMISSION_TIMEOUTS = 2
HEAL_PRIORITY_MAX_HOLD_TICKS = (
    DEFENDER_HEAL_ADMISSION_TIMEOUT_TICKS
    * HEAL_PRIORITY_MAX_ADMISSION_TIMEOUTS
)
CARGO_LANE_INBOUND_WATCHDOG_TICKS = 16
CARGO_LANE_EGRESS_WATCHDOG_TICKS = 16
CARGO_LANE_STALLED_OWNER_COOLDOWN_TICKS = 16
SCOUT_POSITION_HISTORY_SIZE = 6
SCOUT_RECENT_POSITION_PENALTY = 4
STATIONARY_CONFIRMATION_TICKS = 2
STATIONARY_CLEAR_RADIUS = 6
DROPPED_CARGO_PRIORITY_BONUS = 4
RESOURCE_ASSIGNMENT_STICKY_BONUS = 2
RESOURCE_MEMORY_PENALTY = 2
PATH_COST_MAX_EXPANSIONS = 512
PATH_COST_UNREACHABLE = 1_000_000
IDLE_ASSIGNMENT_COST = 100_000
ROUTE_MAX_EXPANSIONS = 4096
CORE_TARGET_MEMORY_TTL = 96
CARGO_UNREACHABLE_TICKS = 16
CARGO_RECOVERY_RETARGET_TICKS = 8
CORE_OBSERVER_REPLACEMENT_TICKS = 8
CORE_RAID_COOLDOWN_TICKS = 8
CORE_RAID_MAX_PATH_COST = 48
CORE_RAID_STALL_TICKS = 6
assert PATH_COST_UNREACHABLE > IDLE_ASSIGNMENT_COST
CORE_ALERT_DISTANCE = 12
CORE_EVADE_DISTANCE = 8
WORKER_EVADE_DISTANCE = 5
WORKER_ESCAPE_SEARCH_DEPTH = 4
POST_THREAT_CAUTION_TICKS = 8
PURSUIT_MEMORY_TTL = 2
PURSUIT_SCORE_MAX = 4
DISTANT_PURSUIT_SCORE_THRESHOLD = 3
ACTIVE_ENEMY_ALERT_TICKS = 2
CORE_PREEMPTIVE_EVADE_HORIZON_TICKS = 16
CORE_BEACON_RETREAT_DISTANCE = 8
VANGUARD_GUARD_RADIUS = 3
RANGER_GUARD_RADIUS = 2
UNIT_HEAL_RESOURCE_RESERVE = 10
EXPANSION_PRODUCTION_RESERVE = 15
CARDINAL_DIRECTIONS = (
    Direction.UP,
    Direction.RIGHT,
    Direction.DOWN,
    Direction.LEFT,
)
SUBMIT_RETRY_ATTEMPTS = 3
SUBMIT_RETRY_BASE_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
MAX_RECONNECT_ATTEMPTS = 8
DEFAULT_LOG_FILE = "arena_agent.log"
DEFAULT_STATE_FILE = "arena_agent_state.json"
DEFAULT_TRACE_FILE = "arena_agent_trace.jsonl"
DEFAULT_STATS_FILE = "arena_agent_stats.json"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
TRACE_MAX_BYTES = 10 * 1024 * 1024
TRACE_BACKUP_COUNT = 2
STATE_SCHEMA_VERSION = 1
STATE_SAVE_INTERVAL_TICKS = 5
STATS_SAVE_INTERVAL_TICKS = 10
RETRYABLE_API_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
NON_RETRYABLE_API_ERRORS = frozenset({"IDEMPOTENCY_CONFLICT"})
RETRYABLE_PROTOCOL_MESSAGE = "state arrived before tick"
# Establish a minimum combat screen before the economy grows beyond the
# starting Workers; otherwise an early attack can freeze production first.
EARLY_DEFENSE_WORKER_GOAL = 4
EARLY_DEFENSE_VANGUARD_TARGET = 1
EARLY_DEFENSE_RANGER_TARGET = 1
RECENT_ATTACK_MEMORY_TICKS = 6
MOVE_CONTESTED_AVOID_TICKS = 3
MOVE_OCCUPIED_AVOID_TICKS = 8
STATIONARY_CLEAR_CONTESTED_FAILURES = 2
CORE_POCKET_MAX_CELLS = 128
CORE_POCKET_DEBOUNCE_TICKS = 2
POPULATION_EXPANSION_RESOURCE_FLOOR = 150
POPULATION_EXPANSION_CASUALTY_BUFFER_UNITS = 6
ADMITTED_CARGO_STALL_TICKS = 3
CARGO_LANE_RADIUS = 8
CARGO_LANE_STAGE_RADII = (3, 4, 5)
CARGO_LANE_GATEWAY_STEPS = 3
CARGO_LANE_OWNER_ADMISSION_STEPS = (
    max(CARGO_LANE_STAGE_RADII) + CARGO_LANE_GATEWAY_STEPS
)
CARGO_LANE_WAIT_CREDIT_TICKS = 8
CARGO_LANE_STARTUP_RADIUS = 3


class _Ansi:
    RESET = "\033[0m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    DIM = "\033[90m"


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{_Ansi.RESET}"


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def _format_position(value: Any) -> str:
    position = _position(value)
    return f"({position[0]}, {position[1]})" if position is not None else "-"


def _unit_health_summary(
    units: Iterable[Any],
    unit_type: UnitType,
) -> tuple[int, int, int, int]:
    unit_list = tuple(units)
    max_hp_per_unit = _unit_max_hp(unit_type)
    critical_hp = _critical_combat_hp(unit_type)
    hit_points = [
        max(
            0,
            min(
                max_hp_per_unit,
                int(getattr(unit, "hp", max_hp_per_unit)),
            ),
        )
        for unit in unit_list
    ]
    return (
        sum(hit_points),
        len(unit_list) * max_hp_per_unit,
        sum(hp < max_hp_per_unit for hp in hit_points),
        sum(critical_hp > 0 and hp <= critical_hp for hp in hit_points),
    )


def _planned_combat_attack_counts(turn: Any) -> tuple[int, int]:
    plan = getattr(turn, "plan", None)
    planned_actions = getattr(plan, "unit_actions", {}) if plan is not None else {}

    def action_type(unit: Any) -> str | None:
        action = planned_actions.get(getattr(unit, "id", None))
        if action is not None:
            return str(getattr(action, "type", "")).upper()
        fake_actions = getattr(unit, "actions", ())
        if fake_actions:
            return str(fake_actions[-1][0]).upper()
        return None

    return (
        sum(
            action_type(unit) == "SWEEP"
            for unit in getattr(turn, "vanguards", ())
        ),
        sum(
            action_type(unit) == "SHOOT"
            for unit in getattr(turn, "rangers", ())
        ),
    )


def _state_label(value: Any) -> str:
    raw_name = getattr(value, "name", None) or str(value).rsplit(".", 1)[-1]
    state_name = str(raw_name).upper()
    labels = {
        "ACTIVE": "运行中",
        "RESPAWNING": "重生中",
        "RECOVERY": "恢复中",
        "COMPATIBILITY_HOLD": "兼容保护",
        "NORMAL": "\u6b63\u5e38",
        "MOVING": "\u79fb\u52a8\u4e2d",
        "DEAD": "\u5df2\u6467\u6bc1",
        "DESTROYED": "\u5df2\u6467\u6bc1",
        "SPAWNING": "\u751f\u4ea7\u4e2d",
    }
    if state_name in {"", "?", "NONE"}:
        return "\u672a\u77e5"
    return labels.get(state_name, state_name)


def _render_turn(turn: Any, report: "PlanReport", accepted: Any) -> None:
    """Render a localized status panel without changing gameplay behavior."""

    enabled = _color_enabled()
    accepted_value = (
        accepted
        if isinstance(accepted, bool)
        else bool(getattr(accepted, "accepted", True))
    )
    status_text = "\u5df2\u63a5\u53d7" if accepted_value else "\u672a\u63a5\u53d7"
    status = _paint(status_text, _Ansi.GREEN if accepted_value else _Ansi.RED, enabled)
    core = getattr(turn, "core", None)
    core_position = getattr(core, "position", None) if core is not None else None
    capacity = _resource_capacity(turn)
    resource_space = _resource_space(turn)
    available_resources = (
        report.available_resources
        if report.available_resources is not None
        else report.resources
    )
    production_label = UNIT_LABELS.get(report.production_unit, "\u65e0")
    production_cost = (
        str(report.production_cost)
        if report.production_cost is not None
        else "-"
    )
    workers = getattr(turn, "workers", ())
    worker_cargo = [max(0, int(getattr(worker, "cargo", 0) or 0)) for worker in workers]
    carrying_workers = sum(cargo > 0 for cargo in worker_cargo)
    empty_workers = len(worker_cargo) - carrying_workers
    total_cargo = sum(worker_cargo)
    worker_hp, worker_max_hp, injured_workers, _ = _unit_health_summary(
        workers,
        UnitType.WORKER,
    )
    vanguards = tuple(getattr(turn, "vanguards", ()))
    rangers = tuple(getattr(turn, "rangers", ()))
    (
        vanguard_hp,
        vanguard_max_hp,
        injured_vanguards,
        critical_vanguards,
    ) = _unit_health_summary(vanguards, UnitType.VANGUARD)
    ranger_hp, ranger_max_hp, injured_rangers, critical_rangers = (
        _unit_health_summary(rangers, UnitType.RANGER)
    )
    lifecycle_labels = {
        "ACTIVE": "运行中",
        "RESPAWNING": "重生中",
        "RECOVERY": "恢复中",
        "COMPATIBILITY_HOLD": "兼容保护",
    }
    lifecycle = lifecycle_labels.get(report.lifecycle, report.lifecycle)
    mission_labels = {
        "ECONOMY": "经济",
        "SCOUT": "探索",
        "GUARD": "防卫",
        "RECOVERY": "恢复",
    }
    mission = mission_labels.get(report.mission, report.mission)
    threat_labels = {
        "NORMAL": "正常",
        "ALERT": "警戒",
        "PRE_EVADE": "撤离",
        "ENGAGED": "交战",
        "BREAKOUT": "突围",
    }
    threat = threat_labels.get(report.threat_level, report.threat_level)
    obstacle_count = len(getattr(turn, "obstacle_cells", ()))
    border = "\u2500" * 68

    print()
    print("\u256d\u2500 " + _paint("Arena Hero Agent \u63a7\u5236\u53f0", _Ansi.CYAN, enabled) + " " + border + "\u256e")
    print(f"\u2502 \u56de\u5408:{report.tick}   \u8ba1\u5212:{status}   \u751f\u547d:{lifecycle}   \u4efb\u52a1:{mission}")
    print(f"\u2502 \u6838\u5fc3\u4f4d\u7f6e:{_format_position(core_position)}   \u8d44\u6e90:{report.resources}/{capacity}   \u53ef\u63a5\u6536:{resource_space}   \u4eba\u53e3:{report.population}")
    print(f"\u2502 \u751f\u4ea7\u62a5\u4ef7:{production_label}={production_cost}   \u53ef\u7528\u8d44\u6e90:{available_resources}   \u672c轮交付:+{report.pending_delivery}")
    print(f"\u2502 \u5355\u4f4d\u7edf\u8ba1: \u5de5\u4eba:{report.workers}   \u5148\u950b:{report.vanguards}   \u6e38\u4fa0:{report.rangers}")
    print(
        f"\u2502 \u5de5\u4eba\u72b6\u6001: \u603b\u6570:{len(worker_cargo)}   "
        f"\u643a\u8d27:{carrying_workers}   \u7a7a\u624b:{empty_workers}   "
        f"\u8d27\u7269\u5408\u8ba1:{total_cargo}   "
        f"\u8840\u91cf:{worker_hp}/{worker_max_hp}   \u672a\u6ee1\u8840:{injured_workers}"
    )
    print(
        f"\u2502 \u5148\u950b\u72b6\u6001: \u9632\u5b88:{report.vanguard_defenders}   "
        f"\u51fa\u5f81:{report.vanguard_raid}   \u672c\u8f6e\u653b\u51fb:{report.vanguard_attacks}   "
        f"\u8840\u91cf:{vanguard_hp}/{vanguard_max_hp}   "
        f"\u672a\u6ee1\u8840:{injured_vanguards}   \u5371\u6025:{critical_vanguards}"
    )
    print(
        f"\u2502 \u6e38\u4fa0\u72b6\u6001: \u9632\u5b88:{report.ranger_defenders}   "
        f"\u51fa\u5f81:{report.ranger_raid}   \u672c\u8f6e\u653b\u51fb:{report.ranger_attacks}   "
        f"\u8840\u91cf:{ranger_hp}/{ranger_max_hp}   "
        f"\u672a\u6ee1\u8840:{injured_rangers}   \u5371\u6025:{critical_rangers}"
    )
    watchdog = report.cargo_watchdog_reason or "\u65e0"
    print(
        f"\u2502 \u7269\u6d41\u901a\u9053: {report.cargo_lane_phase}   "
        f"\u8ba9\u8def \u5de5\u4eba:{report.cargo_lane_yield_workers} \u6218\u6597:{report.cargo_lane_yield_units}   "
        f"\u770b\u95e8\u72d7:{watchdog}"
    )
    print(
        f"\u2502 \u6cbb\u7597\u901a\u884c: {'\u8fdb\u884c\u4e2d' if report.heal_priority_active else '\u65e0'}   "
        f"\u5df2\u51c6\u5165:{'\u662f' if report.healing_defender_admitted else '\u5426'}   "
        f"\u7b49\u5f85:{report.heal_priority_wait_ticks}\u56de\u5408   "
        f"Cargo\u7a7f\u63d2:{'\u662f' if report.heal_priority_interleave_pending else '\u5426'}"
    )
    print(f"\u2502 \u89c6\u91ce\u8d44\u6e90:{report.visible_resources}   \u8bb0\u5fc6\u8d44\u6e90:{report.remembered_resources}   \u654c\u4eba:{report.visible_enemies}   \u6218\u6597\u654c\u4eba:{report.visible_combat_enemies}   \u5371\u9669\u683c:{report.danger_cells}   \u5a01\u80c1:{threat}   \u969c\u788d:{obstacle_count}")
    print("\u2570" + border + "\u256f")


def _log_turn_summary(report: "PlanReport", accepted: Any) -> None:
    """Persist the compact per-Tick facts needed to diagnose a match later."""

    accepted_value = (
        accepted
        if isinstance(accepted, bool)
        else bool(getattr(accepted, "accepted", True))
    )
    LOGGER.debug(
        "tick=%s accepted=%s resources=%s population=%s workers=%s "
        "vanguards=%s rangers=%s enemies=%s combat_enemies=%s "
        "danger_cells=%s pursuing=%s preemptive=%s threat=%s reason=%s "
        "lifecycle=%s mission=%s production=%s cost=%s available=%s "
        "pending_delivery=%s cargo_lane=%s cargo_yield_workers=%s "
        "cargo_yield_units=%s cargo_watchdog=%s heal_priority=%s "
        "heal_admitted=%s heal_wait_ticks=%s heal_interleave=%s",
        report.tick,
        accepted_value,
        report.resources,
        report.population,
        report.workers,
        report.vanguards,
        report.rangers,
        report.visible_enemies,
        report.visible_combat_enemies,
        report.danger_cells,
        report.pursuing_enemies,
        report.preemptive_enemies,
        report.threat_level,
        report.threat_reason,
        report.lifecycle,
        report.mission,
        _enum_label(report.production_unit) if report.production_unit else "NONE",
        report.production_cost,
        report.available_resources,
        report.pending_delivery,
        report.cargo_lane_phase,
        report.cargo_lane_yield_workers,
        report.cargo_lane_yield_units,
        report.cargo_watchdog_reason,
        report.heal_priority_active,
        report.healing_defender_admitted,
        report.heal_priority_wait_ticks,
        report.heal_priority_interleave_pending,
    )


BASE_SPAWN_COSTS = {
    UnitType.WORKER: 5,
    UnitType.VANGUARD: 10,
    UnitType.RANGER: 12,
}

BASELINE_WORKER_TARGET = 12
BASELINE_VANGUARD_TARGET = 3
BASELINE_RANGER_TARGET = 4
DEFAULT_WORKER_TARGET = 16
DEFAULT_VANGUARD_TARGET = 6
DEFAULT_RANGER_TARGET = 8
DEFAULT_MAX_POPULATION = 0

UNIT_LABELS = {
    UnitType.WORKER: "\u5de5\u4eba",
    UnitType.VANGUARD: "\u5148\u950b",
    UnitType.RANGER: "\u6e38\u4fa0",
}


@dataclass
class AgentConfig:
    """Tunable decisions that do not depend on the current world snapshot."""

    max_population: int = DEFAULT_MAX_POPULATION
    spawn_unit_type: UnitType | None = UnitType.WORKER
    auto_production: bool = True
    population_expansion_enabled: bool = False
    expansion_resource_threshold: int = POPULATION_EXPANSION_RESOURCE_FLOOR
    expansion_casualty_buffer_units: int = POPULATION_EXPANSION_CASUALTY_BUFFER_UNITS
    worker_target: int = DEFAULT_WORKER_TARGET
    vanguard_target: int = DEFAULT_VANGUARD_TARGET
    ranger_target: int = DEFAULT_RANGER_TARGET
    enable_combat: bool = True
    resource_memory_ttl: int = 64
    beacon_policy: str = "RETREAT"

    def target_for(self, unit_type: UnitType) -> int:
        return {
            UnitType.WORKER: self.worker_target,
            UnitType.VANGUARD: self.vanguard_target,
            UnitType.RANGER: self.ranger_target,
        }[unit_type]


@dataclass
class ResourceProgress:
    target: Position
    best_cost: int
    stalled_turns: int = 0


@dataclass
class ScoutProgress:
    target: Position
    best_cost: int
    stalled_turns: int = 0
    last_position: Position | None = None
    path_stalled_turns: int = 0
    last_cost: int | None = None


@dataclass
class ScoutReturnProgress:
    target: Position
    best_cost: int
    last_improved_tick: int


@dataclass(frozen=True)
class ThreatAssessment:
    level: str = "NORMAL"
    reason: str = "NONE"
    nearest_distance: int | None = None
    recent_attack: bool = False


@dataclass(frozen=True)
class CoreMoveIntent:
    direction: Direction
    remember_escape_direction: bool = False


@dataclass(frozen=True)
class EnemyMotion:
    position: Position
    last_tick: int
    core_distance: int
    unit_type: UnitType
    pursuit_score: int = 0
    activity_until_tick: int = 0
    preemptive_until_tick: int = 0
    ticks_to_attack_range: int | None = None


@dataclass(frozen=True)
class RememberedCombatEnemy:
    id: str
    position: Position
    unit_type: UnitType
    hp: int = 5


@dataclass(frozen=True)
class EnemyCoreObservation:
    id: str
    position: Position
    hp: int
    shield: int
    state: str
    last_seen_tick: int


@dataclass(frozen=True)
class RouteSearchResult:
    status: str
    path: tuple[Position, ...] = ()
    explored: frozenset[Position] = frozenset()


@dataclass
class WorkerRoute:
    role: str
    target: Position
    path: tuple[Position, ...]
    core_memory_revision: int
    index: int = 0


@dataclass
class CargoRecovery:
    unreachable_ticks: int = 0
    target: Position | None = None
    selected_at_tick: int = 0


@dataclass
class StationaryMoveFailure:
    destination: Position
    target: Position
    consecutive_failures: int
    last_tick: int


@dataclass
class AdmittedCargoProgress:
    position: Position
    stationary_ticks: int
    last_tick: int


@dataclass(frozen=True)
class CorePocketObservation:
    component: frozenset[Position] = frozenset()
    closed: bool = False
    raw_admitted_cargo_ids: frozenset[str] = frozenset()
    admitted_cargo_ids: frozenset[str] = frozenset()
    stalled_admitted_cargo_ids: frozenset[str] = frozenset()
    outside_cargo_ids: frozenset[str] = frozenset()
    static_reachable_cargo_ids: frozenset[str] = frozenset()
    consecutive_ticks: int = 0
    blocked: bool = False


@dataclass(frozen=True)
class CargoLaneBridge:
    path: tuple[Position, ...]
    gateway: Position
    blocker_ids: frozenset[str]


@dataclass
class CargoLanePlan:
    active: bool = False
    phase: str = "INBOUND"
    core_position: Position | None = None
    owner_id: str | None = None
    queued_owner_id: str | None = None
    departing_worker_id: str | None = None
    path: tuple[Position, ...] = ()
    gateway: Position | None = None
    owner_approach_path: tuple[Position, ...] = ()
    heal_approach_path: tuple[Position, ...] = ()
    egress_path: tuple[Position, ...] = ()
    egress_target: Position | None = None
    startup_pending_ids: set[str] = field(default_factory=set)
    yield_worker_ids: set[str] = field(default_factory=set)
    yield_unit_ids: set[str] = field(default_factory=set)
    core_release_hold_unit_ids: set[str] = field(default_factory=set)
    yield_targets: dict[str, Position] = field(default_factory=dict)
    yield_paths: dict[str, tuple[Position, ...]] = field(default_factory=dict)
    stage_targets: dict[str, Position] = field(default_factory=dict)
    started_tick: int = 0
    phase_started_tick: int = 0
    last_planned_tick: int = -1
    geometry_source: str = "UNKNOWN"
    watchdog_tick: int = -1
    watchdog_reason: str | None = None
    owner_progress_id: str | None = None
    owner_best_remaining_steps: int | None = None
    owner_last_progress_tick: int = -1
    departing_progress_id: str | None = None
    departing_best_remaining_steps: int | None = None
    departing_last_progress_tick: int = -1


@dataclass
class CoreVisit:
    unit_id: str | None = None
    purpose: str | None = None
    granted_tick: int = -1
    reached_core: bool = field(default=False, compare=False)


@dataclass
class CoreRaidPlan:
    state: str = "IDLE"
    target_id: str | None = None
    target_position: Position | None = None
    observer_id: str | None = None
    observer_position: Position | None = None
    observer_confirmed: bool = False
    replacement_deadline: int = 0
    cooldown_until: int = 0
    raid_member_ids: set[str] = field(default_factory=set)
    assignments: dict[str, Position] = field(default_factory=dict)
    last_durability: int | None = None
    last_formation_cost: int | None = None
    stalled_ticks: int = 0
    last_replan_tick: int = -1
    last_replan_reason: str | None = None
    ranger_retreat_after_tick: dict[str, int] = field(default_factory=dict)


@dataclass
class TacticMemory:
    """Persistent observations kept separate from the current authoritative Turn."""

    obstacle_memory: set[Position] = field(default_factory=set)
    last_observed_tick: int | None = None
    resource_observation_memory: dict[Position, int] = field(default_factory=dict)
    resource_intents: dict[str, Position] = field(default_factory=dict)
    resource_progress: dict[str, ResourceProgress] = field(default_factory=dict)
    resource_cooldowns: dict[Position, int] = field(default_factory=dict)
    exploration_anchor: Position | None = None
    scout_chunk_last_seen: dict[Position, int] = field(default_factory=dict)
    scout_risk_chunks: dict[Position, int] = field(default_factory=dict)
    scout_stages: dict[str, int] = field(default_factory=dict)
    scout_progress: dict[str, ScoutProgress] = field(default_factory=dict)
    enemy_observation_memory: dict[str, tuple[int, Position]] = field(default_factory=dict)
    enemy_motion_memory: dict[str, EnemyMotion] = field(default_factory=dict)
    active_enemy_ids: set[str] = field(default_factory=set)
    pursuing_enemy_ids: set[str] = field(default_factory=set)
    preemptive_enemy_ids: set[str] = field(default_factory=set)
    threat_caution_until_tick: int = 0
    recent_attack_until_tick: int = 0
    recent_attack_positions: dict[Position, int] = field(default_factory=dict)
    recent_combat_danger_positions: dict[Position, int] = field(default_factory=dict)
    recovery_until_tick: int = 0
    dropped_cargo_observation_memory: dict[Position, int] = field(default_factory=dict)
    stationary_enemy_observation_memory: dict[
        str, tuple[int, Position, int, str]
    ] = field(default_factory=dict)
    scout_return_targets: dict[str, Position] = field(default_factory=dict)
    scout_return_progress: dict[str, ScoutReturnProgress] = field(default_factory=dict)
    scout_return_hold_ids: set[str] = field(default_factory=set)
    scout_cooldowns: dict[str, int] = field(default_factory=dict)
    worker_position_history: dict[str, list[Position]] = field(default_factory=dict)
    healing_defender_ids: set[str] = field(default_factory=set)
    healing_defender_intent_id: str | None = None
    healing_defender_intent_tick: int = -1
    healing_defender_stage_target: Position | None = None
    healing_defender_admitted_id: str | None = None
    healing_defender_admitted_tick: int = -1
    healing_defender_best_distance: int | None = None
    healing_defender_last_progress_tick: int = -1
    healing_defender_last_position: Position | None = None
    healing_defender_timeout_tick: int = -1
    healing_defender_cooldowns: dict[str, int] = field(default_factory=dict)
    healing_worker_ids: set[str] = field(default_factory=set)
    healing_worker_stage_target: Position | None = None
    # A healing intent is a queue reservation, not physical Core ownership.
    # Only a worker standing on the Core cell may populate core_visit.
    heal_intent_id: str | None = None
    heal_intent_tick: int = -1
    heal_intent_best_distance: int | None = None
    heal_intent_cooldowns: dict[str, int] = field(default_factory=dict)
    last_core_escape_direction: Direction | None = None
    enemy_core_memory: dict[str, EnemyCoreObservation] = field(default_factory=dict)
    enemy_core_revision: int = 0
    worker_routes: dict[str, WorkerRoute] = field(default_factory=dict)
    planned_worker_destinations: dict[str, Position] = field(default_factory=dict)
    worker_contested_positions: dict[str, dict[Position, int]] = field(
        default_factory=dict
    )
    unit_contested_positions: dict[str, dict[Position, int]] = field(
        default_factory=dict
    )
    planned_unit_destinations: dict[str, Position] = field(default_factory=dict)
    planned_unit_targets: dict[str, Position] = field(default_factory=dict)
    planned_stationary_targets: dict[str, Position] = field(default_factory=dict)
    stationary_clear_cooldowns: dict[Position, int] = field(default_factory=dict)
    stationary_move_failures: dict[str, StationaryMoveFailure] = field(
        default_factory=dict
    )
    cargo_recovery: dict[str, CargoRecovery] = field(default_factory=dict)
    route_diagnostics: dict[str, str] = field(default_factory=dict)
    admitted_cargo_progress: dict[str, AdmittedCargoProgress] = field(
        default_factory=dict
    )
    core_pocket: CorePocketObservation = field(default_factory=CorePocketObservation)
    core_pocket_candidate_tick: int = -1
    core_pocket_candidate_core: Position | None = None
    core_pocket_candidate_cargo_ids: set[str] = field(default_factory=set)
    core_pocket_candidate_streak: int = 0
    cargo_lane: CargoLanePlan = field(default_factory=CargoLanePlan)
    core_visit: CoreVisit = field(default_factory=CoreVisit)
    core_visit_deposit_streak: int = 0
    core_visit_forced_purpose: str | None = None
    heal_priority_intent_id: str | None = None
    heal_priority_started_tick: int = -1
    heal_priority_admission_timeouts: int = 0
    heal_priority_interleave_pending: bool = False
    cargo_lane_wait_ticks: dict[str, int] = field(default_factory=dict)
    cargo_lane_stalled_owners: dict[str, tuple[Position, int]] = field(
        default_factory=dict
    )
    core_lane_departing_worker_id: str | None = None
    core_lane_yield_worker_ids: set[str] = field(default_factory=set)
    core_observer_return_ids: set[str] = field(default_factory=set)
    raid: CoreRaidPlan = field(default_factory=CoreRaidPlan)
    guard_idle_assignments: int = 0
    raid_state_changed_tick: int = -1

    def observe(self, turn: Any) -> None:
        """Remember permanent obstacles and timestamped, potentially stale resources."""

        self.obstacle_memory.update(_positions(getattr(turn, "obstacle_cells", ())))
        tick = int(getattr(turn, "tick", 0))
        previous_worker_destinations = self.planned_worker_destinations
        previous_unit_destinations = self.planned_unit_destinations
        previous_stationary_targets = self.planned_stationary_targets
        self.planned_worker_destinations = {}
        self.planned_unit_destinations = {}
        self.planned_unit_targets = {}
        self.planned_stationary_targets = {}
        self.last_observed_tick = tick
        self.route_diagnostics.clear()
        self.guard_idle_assignments = 0
        living_unit_ids = {
            str(getattr(unit, "id", ""))
            for unit in getattr(turn, "units", ())
            if getattr(unit, "id", None) is not None
        }
        living_defender_ids = {
            str(getattr(unit, "id", ""))
            for unit in tuple(getattr(turn, "vanguards", ()))
            + tuple(getattr(turn, "rangers", ()))
            if getattr(unit, "id", None) is not None
        }
        if self.healing_defender_admitted_id not in living_defender_ids:
            self.healing_defender_admitted_id = None
            self.healing_defender_admitted_tick = -1
            self.healing_defender_best_distance = None
            self.healing_defender_last_progress_tick = -1
            self.healing_defender_last_position = None
        self.healing_defender_ids.intersection_update(living_defender_ids)
        self.healing_defender_cooldowns = {
            defender_id: expires_at
            for defender_id, expires_at in self.healing_defender_cooldowns.items()
            if defender_id in living_defender_ids and expires_at > tick
        }
        if self.core_visit.unit_id not in living_unit_ids:
            if self.core_visit.unit_id == self.healing_defender_admitted_id:
                self.healing_defender_admitted_id = None
                self.healing_defender_admitted_tick = -1
                self.healing_defender_best_distance = None
                self.healing_defender_last_progress_tick = -1
                self.healing_defender_last_position = None
            self.core_visit = CoreVisit()
        else:
            visit_unit = next(
                (
                    unit
                    for unit in getattr(turn, "units", ())
                    if str(getattr(unit, "id", "")) == self.core_visit.unit_id
                ),
                None,
            )
            core_position = _position(
                getattr(getattr(turn, "core", None), "position", None)
            )
            unit_position = _position(getattr(visit_unit, "position", None))
            deposit_finished = self.core_visit.purpose == "DEPOSIT" and (
                core_position is None
                or unit_position != core_position
                or int(getattr(visit_unit, "cargo", 0)) <= 0
            )
            heal_finished = self.core_visit.purpose == "HEAL" and (
                int(getattr(visit_unit, "hp", 0))
                >= _unit_max_hp(getattr(visit_unit, "unit_type", UnitType.WORKER))
            )
            if self.core_visit.purpose == "HEAL" and unit_position == core_position:
                self.core_visit.reached_core = True
            heal_left_core = (
                self.core_visit.purpose == "HEAL"
                and self.core_visit.reached_core
                and unit_position != core_position
            )
            heal_timed_out = (
                self.core_visit.purpose == "HEAL"
                and not self.core_visit.reached_core
                and tick - self.core_visit.granted_tick >= HEAL_VISIT_TIMEOUT_TICKS
            )
            if visit_unit is None or deposit_finished or heal_finished or heal_left_core or heal_timed_out:
                if self.core_visit.purpose == "DEPOSIT":
                    self.core_visit_deposit_streak += 1
                    if self.heal_priority_interleave_pending:
                        self.heal_priority_interleave_pending = False
                        self.heal_priority_admission_timeouts = 0
                        self.heal_priority_started_tick = tick
                elif self.core_visit.purpose == "HEAL":
                    healing_defender_visit = (
                        self.core_visit.unit_id
                        == self.healing_defender_admitted_id
                        or self.core_visit.unit_id in self.healing_defender_ids
                    )
                    self.core_visit_deposit_streak = 0
                    self.core_visit_forced_purpose = None
                    self.heal_priority_intent_id = None
                    self.heal_priority_started_tick = -1
                    self.heal_priority_admission_timeouts = 0
                    self.heal_priority_interleave_pending = False
                    if heal_timed_out and self.heal_intent_id == self.core_visit.unit_id:
                        self.heal_intent_cooldowns[self.heal_intent_id] = (
                            tick + HEAL_INTENT_COOLDOWN_TICKS
                        )
                        self.heal_intent_id = None
                        self.heal_intent_tick = -1
                        self.heal_intent_best_distance = None
                    if healing_defender_visit:
                        defender_id = self.core_visit.unit_id
                        if heal_finished:
                            self.healing_defender_ids.discard(defender_id)
                            self.healing_defender_intent_id = None
                            self.healing_defender_intent_tick = -1
                            self.healing_defender_stage_target = None
                        elif defender_id == self.healing_defender_admitted_id:
                            self.healing_defender_cooldowns[defender_id] = (
                                tick + DEFENDER_HEAL_COOLDOWN_TICKS
                            )
                            self.healing_defender_intent_id = None
                            self.healing_defender_intent_tick = -1
                            self.healing_defender_stage_target = None
                        self.healing_defender_admitted_id = None
                        self.healing_defender_admitted_tick = -1
                        self.healing_defender_best_distance = None
                        self.healing_defender_last_progress_tick = -1
                        self.healing_defender_last_position = None
                self.core_visit = CoreVisit()
        living_worker_ids: set[str] = set()
        living_worker_positions: dict[str, Position] = {}
        for worker in getattr(turn, "workers", ()):
            worker_key = str(getattr(worker, "id", ""))
            position = _position(getattr(worker, "position", None))
            if not worker_key or position is None:
                continue
            living_worker_ids.add(worker_key)
            living_worker_positions[worker_key] = position
            history = self.worker_position_history.setdefault(worker_key, [])
            if not history or history[-1] != position:
                history.append(position)
                del history[:-SCOUT_POSITION_HISTORY_SIZE]
        self.cargo_lane_stalled_owners = {
            worker_key: (stalled_position, expires_at)
            for worker_key, (stalled_position, expires_at) in (
                self.cargo_lane_stalled_owners.items()
            )
            if expires_at > tick
            and living_worker_positions.get(worker_key) == stalled_position
        }
        for worker_key in set(self.worker_position_history) - living_worker_ids:
            self.worker_position_history.pop(worker_key, None)
            self.worker_routes.pop(worker_key, None)
            self.worker_contested_positions.pop(worker_key, None)
            self.cargo_recovery.pop(worker_key, None)
            self.admitted_cargo_progress.pop(worker_key, None)
            self.core_observer_return_ids.discard(worker_key)
        self.scout_return_targets = {
            worker_key: target
            for worker_key, target in self.scout_return_targets.items()
            if worker_key in living_worker_ids
        }
        self.scout_return_progress = {
            worker_key: progress
            for worker_key, progress in self.scout_return_progress.items()
            if worker_key in living_worker_ids
        }
        self.scout_return_hold_ids.intersection_update(living_worker_ids)
        if self.heal_intent_id not in living_worker_ids:
            self.heal_intent_id = None
            self.heal_intent_tick = -1
            self.heal_intent_best_distance = None
        intent_worker = None
        if self.heal_intent_id is not None:
            intent_worker = next(
                (
                    worker
                    for worker in getattr(turn, "workers", ())
                    if str(getattr(worker, "id", "")) == self.heal_intent_id
                ),
                None,
            )
            intent_position = _position(getattr(intent_worker, "position", None))
            intent_core_position = _position(
                getattr(getattr(turn, "core", None), "position", None)
            )
            if intent_position is not None and intent_core_position is not None:
                intent_distance = _manhattan(intent_position, intent_core_position)
                if (
                    self.heal_intent_best_distance is None
                    or intent_distance < self.heal_intent_best_distance
                ):
                    self.heal_intent_best_distance = intent_distance
                    self.heal_intent_tick = tick
        waiting_for_cargo_lane = bool(
            self.cargo_lane.active
            and intent_worker is not None
            and int(getattr(intent_worker, "cargo", 0)) <= 0
        )
        if (
            self.heal_intent_id is not None
            and self.heal_intent_tick >= 0
            and tick - self.heal_intent_tick >= HEAL_INTENT_TIMEOUT_TICKS
            and not waiting_for_cargo_lane
        ):
            self.heal_intent_cooldowns[self.heal_intent_id] = (
                tick + HEAL_INTENT_COOLDOWN_TICKS
            )
            self.heal_intent_id = None
            self.heal_intent_tick = -1
            self.heal_intent_best_distance = None
        self.heal_intent_cooldowns = {
            worker_id: expires_at
            for worker_id, expires_at in self.heal_intent_cooldowns.items()
            if worker_id in living_worker_ids and expires_at > tick
        }
        for unit_key in set(self.unit_contested_positions) - living_unit_ids:
            self.unit_contested_positions.pop(unit_key, None)
            self.stationary_move_failures.pop(unit_key, None)
        if self.core_lane_departing_worker_id not in living_worker_ids:
            self.core_lane_departing_worker_id = None
            self.core_lane_yield_worker_ids.clear()
        else:
            self.core_lane_yield_worker_ids.intersection_update(living_worker_ids)
        friendly_units = tuple(getattr(turn, "units", ()))
        friendly_ids = {
            str(getattr(unit, "id", ""))
            for unit in friendly_units
            if getattr(unit, "id", None) is not None
        }
        friendly_positions = {
            str(getattr(unit, "id", "")): position
            for unit in friendly_units
            if getattr(unit, "id", None) is not None
            and (position := _position(getattr(unit, "position", None))) is not None
        }
        core = getattr(turn, "core", None)
        core_id = str(getattr(core, "id", "") or "")
        core_position = _position(getattr(core, "position", None))
        if core_id:
            friendly_ids.add(core_id)
            if core_position is not None:
                friendly_positions[core_id] = core_position
        for event in getattr(turn, "events", ()):
            event_type = str(getattr(event, "event_type", "")).upper().rsplit(".", 1)[-1]
            reason_code = str(getattr(event, "reason_code", "")).upper().rsplit(".", 1)[-1]
            if event_type == "HARVEST_SUCCEEDED" or (
                event_type == "HARVEST_FAILED" and reason_code == "RESOURCE_DEPLETED"
            ):
                position = _position(getattr(event, "position", None))
                if position is not None:
                    self.resource_observation_memory.pop(position, None)
                    if event_type in {"HARVEST_SUCCEEDED", "HARVEST_FAILED"}:
                        self.dropped_cargo_observation_memory.pop(position, None)
            if event_type == "WORKER_CARGO_DROPPED":
                position = _position(getattr(event, "position", None))
                if position is not None:
                    self.dropped_cargo_observation_memory[position] = tick
            combat_event = any(
                token in event_type for token in ("ATTACK", "DAMAGE", "DAMAGED", "HIT")
            )
            actor_id = str(getattr(event, "actor_id", "") or "")
            target_id = str(getattr(event, "target_id", "") or "")
            own_attack = actor_id in friendly_ids and target_id not in friendly_ids
            if combat_event and not own_attack:
                position = _position(getattr(event, "position", None))
                if target_id == core_id and core_position is not None:
                    position = core_position
                if position is None:
                    position = friendly_positions.get(target_id)
                if position is not None:
                    self.recent_attack_positions[position] = tick
                core_relevant = target_id == core_id or bool(
                    core_position is not None
                    and position is not None
                    and _manhattan(core_position, position) <= CORE_ALERT_DISTANCE
                )
                if core_relevant:
                    self.recent_attack_until_tick = max(
                        self.recent_attack_until_tick,
                        tick + RECENT_ATTACK_MEMORY_TICKS,
                    )
            own_core_destroyed = (
                event_type == "CORE_DESTROYED"
                and target_id in friendly_ids
            )
            if event_type == "CORE_LOST" or own_core_destroyed:
                self.recovery_until_tick = max(self.recovery_until_tick, tick + 8)
            if event_type == "UNIT_MOVE_SUCCEEDED" and actor_id:
                self.stationary_move_failures.pop(actor_id, None)
            if event_type == "UNIT_MOVE_FAILED" and actor_id:
                self.worker_routes.pop(actor_id, None)
                self.route_diagnostics[actor_id] = reason_code or "MOVE_FAILED"
                occupied_failure = reason_code in {
                    "MOVE_DESTINATION_OCCUPIED",
                    "CELL_UNIT_LIMIT",
                }
                avoid_ticks = (
                    MOVE_CONTESTED_AVOID_TICKS
                    if reason_code == "MOVE_CONTESTED"
                    else MOVE_OCCUPIED_AVOID_TICKS
                    if occupied_failure
                    else 0
                )
                failed_destination = previous_unit_destinations.get(actor_id)
                if failed_destination is None:
                    failed_destination = previous_worker_destinations.get(actor_id)
                if failed_destination is None:
                    failed_destination = _position(getattr(event, "position", None))
                if (
                    avoid_ticks > 0
                    and actor_id in living_unit_ids
                    and failed_destination is not None
                ):
                    contested = self.unit_contested_positions.setdefault(actor_id, {})
                    contested[failed_destination] = max(
                        contested.get(failed_destination, 0),
                        tick + avoid_ticks,
                    )
                    if actor_id in living_worker_ids:
                        worker_contested = self.worker_contested_positions.setdefault(
                            actor_id,
                            {},
                        )
                        worker_contested[failed_destination] = max(
                            worker_contested.get(failed_destination, 0),
                            tick + avoid_ticks,
                        )
                    elif occupied_failure:
                        failed_target = previous_stationary_targets.get(actor_id)
                        if failed_target is not None:
                            self.stationary_clear_cooldowns[failed_target] = max(
                                self.stationary_clear_cooldowns.get(failed_target, 0),
                                tick + MOVE_OCCUPIED_AVOID_TICKS,
                            )
                            self.stationary_move_failures.pop(actor_id, None)
                    elif reason_code == "MOVE_CONTESTED":
                        failed_target = previous_stationary_targets.get(actor_id)
                        if failed_target is not None:
                            previous_failure = self.stationary_move_failures.get(actor_id)
                            consecutive_failures = (
                                previous_failure.consecutive_failures + 1
                                if previous_failure is not None
                                and previous_failure.destination == failed_destination
                                and previous_failure.target == failed_target
                                and 0 < tick - previous_failure.last_tick
                                <= MOVE_OCCUPIED_AVOID_TICKS
                                else 1
                            )
                            if (
                                consecutive_failures
                                >= STATIONARY_CLEAR_CONTESTED_FAILURES
                            ):
                                self.stationary_clear_cooldowns[failed_target] = max(
                                    self.stationary_clear_cooldowns.get(failed_target, 0),
                                    tick + MOVE_OCCUPIED_AVOID_TICKS,
                                )
                                self.stationary_move_failures.pop(actor_id, None)
                            else:
                                self.stationary_move_failures[actor_id] = (
                                    StationaryMoveFailure(
                                        destination=failed_destination,
                                        target=failed_target,
                                        consecutive_failures=consecutive_failures,
                                        last_tick=tick,
                                    )
                                )
            if event_type in {
                "CORE_DESTROYED",
                "CORE_LOST",
                "CORE_RESOURCES_CAPTURED",
            } and target_id:
                if target_id in self.enemy_core_memory:
                    self.enemy_core_memory.pop(target_id, None)
                    self.enemy_core_revision += 1
        for position in _positions(getattr(turn, "resource_cells", ())):
            self.resource_observation_memory[position] = tick

        core_position = _position(getattr(core, "position", None))
        visible_combat_ids: set[str] = set()
        for enemy in getattr(turn, "visible_enemies", ()):
            enemy_position = _position(getattr(enemy, "position", None))
            if enemy_position is None:
                continue
            enemy_key = str(getattr(enemy, "id", ""))
            previous_stationary = self.stationary_enemy_observation_memory.get(enemy_key)
            stable_ticks = (
                previous_stationary[2] + 1
                if previous_stationary is not None
                and previous_stationary[0] == tick - 1
                and previous_stationary[1] == enemy_position
                else 1
            )
            self.stationary_enemy_observation_memory[enemy_key] = (
                tick,
                enemy_position,
                stable_ticks,
                "CORE" if _is_enemy_core(enemy) else "UNIT",
            )

            if _is_enemy_core(enemy):
                state = _enum_label(
                    getattr(
                        enemy,
                        "state",
                        getattr(getattr(enemy, "view", None), "state", "NORMAL"),
                    )
                )
                previous_core = self.enemy_core_memory.get(enemy_key)
                if state == "MOVING":
                    self.stationary_enemy_observation_memory.pop(enemy_key, None)
                    if previous_core is not None:
                        self.enemy_core_memory.pop(enemy_key, None)
                        self.enemy_core_revision += 1
                elif stable_ticks >= STATIONARY_CONFIRMATION_TICKS:
                    observation = EnemyCoreObservation(
                        id=enemy_key,
                        position=enemy_position,
                        hp=int(getattr(enemy, "hp", 5)),
                        shield=int(getattr(enemy, "shield", 0) or 0),
                        state=state,
                        last_seen_tick=tick,
                    )
                    if previous_core != observation:
                        if previous_core is None or previous_core.position != enemy_position:
                            self.enemy_core_revision += 1
                        self.enemy_core_memory[enemy_key] = observation
                elif previous_core is not None and previous_core.position != enemy_position:
                    self.enemy_core_memory.pop(enemy_key, None)
                    self.enemy_core_revision += 1

            if not _is_mobile_combat_enemy(enemy):
                self.enemy_observation_memory.pop(enemy_key, None)
                self.enemy_motion_memory.pop(enemy_key, None)
                continue

            visible_combat_ids.add(enemy_key)
            remembered_danger = _enemy_danger_cells((enemy,), self.obstacle_memory)
            remembered_danger.add(enemy_position)
            for position in remembered_danger:
                self.recent_combat_danger_positions[position] = tick
            unit_type = getattr(enemy, "unit_type")
            core_distance = (
                _manhattan(core_position, enemy_position)
                if core_position is not None
                else sys.maxsize
            )
            if core_distance <= CORE_ALERT_DISTANCE:
                self.threat_caution_until_tick = max(
                    self.threat_caution_until_tick,
                    tick + POST_THREAT_CAUTION_TICKS,
                )
            self.enemy_observation_memory[enemy_key] = (tick, enemy_position)

            previous_motion = self.enemy_motion_memory.get(enemy_key)
            pursuit_score = 0
            activity_until_tick = 0
            preemptive_until_tick = 0
            ticks_to_attack_range: int | None = None
            if (
                previous_motion is not None
                and 0 < tick - previous_motion.last_tick <= PURSUIT_MEMORY_TTL
            ):
                observation_gap = tick - previous_motion.last_tick
                missed_ticks = max(0, observation_gap - 1)
                pursuit_score = max(0, previous_motion.pursuit_score - missed_ticks)
                activity_until_tick = previous_motion.activity_until_tick
                preemptive_until_tick = previous_motion.preemptive_until_tick
                if previous_motion.position == enemy_position:
                    pursuit_score = 0
                else:
                    activity_until_tick = tick + ACTIVE_ENEMY_ALERT_TICKS
                    closed_distance = previous_motion.core_distance - core_distance
                    if closed_distance > 0 and core_position is not None:
                        pursuit_score = min(
                            PURSUIT_SCORE_MAX,
                            pursuit_score + 2,
                        )
                        remaining_distance = max(
                            0,
                            core_distance - _enemy_attack_range(unit_type),
                        )
                        ticks_to_attack_range = math.ceil(
                            remaining_distance * observation_gap / closed_distance
                        )
                        if (
                            ticks_to_attack_range
                            <= CORE_PREEMPTIVE_EVADE_HORIZON_TICKS
                        ):
                            preemptive_until_tick = tick + ACTIVE_ENEMY_ALERT_TICKS
                    elif closed_distance == 0:
                        pursuit_score = min(
                            PURSUIT_SCORE_MAX,
                            pursuit_score + 1,
                        )
                    else:
                        pursuit_score = max(0, pursuit_score - 1)
            self.enemy_motion_memory[enemy_key] = EnemyMotion(
                position=enemy_position,
                last_tick=tick,
                core_distance=core_distance,
                unit_type=unit_type,
                pursuit_score=pursuit_score,
                activity_until_tick=activity_until_tick,
                preemptive_until_tick=preemptive_until_tick,
                ticks_to_attack_range=ticks_to_attack_range,
            )

        for enemy_key, motion in tuple(self.enemy_motion_memory.items()):
            if enemy_key in visible_combat_ids:
                continue
            hidden_ticks = tick - motion.last_tick
            if (
                hidden_ticks >= PURSUIT_MEMORY_TTL
                and tick > motion.activity_until_tick
                and tick > motion.preemptive_until_tick
            ):
                self.enemy_motion_memory.pop(enemy_key, None)
                self.enemy_observation_memory.pop(enemy_key, None)

        self.active_enemy_ids = {
            enemy_key
            for enemy_key, motion in self.enemy_motion_memory.items()
            if tick <= motion.activity_until_tick
        }
        self.preemptive_enemy_ids = {
            enemy_key
            for enemy_key, motion in self.enemy_motion_memory.items()
            if tick <= motion.preemptive_until_tick
        }
        self.pursuing_enemy_ids = {
            enemy_key
            for enemy_key, motion in self.enemy_motion_memory.items()
            if motion.pursuit_score > 0
            and (
                motion.core_distance <= CORE_EVADE_DISTANCE
                or (
                    motion.pursuit_score >= DISTANT_PURSUIT_SCORE_THRESHOLD
                    and motion.ticks_to_attack_range is not None
                    and motion.ticks_to_attack_range
                    <= CORE_PREEMPTIVE_EVADE_HORIZON_TICKS
                )
            )
            and tick - motion.last_tick < PURSUIT_MEMORY_TTL
        }

        observed_positions = set(_positions(getattr(turn, "resource_cells", ())))
        observed_positions.update(_positions(getattr(turn, "obstacle_cells", ())))
        observed_positions.update(
            _positions(
                getattr(unit, "position", None)
                for unit in getattr(turn, "workers", ())
            )
        )
        observed_positions.update(
            _positions(
                getattr(enemy, "position", None)
                for enemy in getattr(turn, "visible_enemies", ())
            )
        )
        core_position = _position(getattr(getattr(turn, "core", None), "position", None))
        if core_position is not None:
            observed_positions.add(core_position)
        for position in observed_positions:
            self.scout_chunk_last_seen[_chunk_coordinates(position)] = tick
        for enemy in getattr(turn, "visible_enemies", ()):
            enemy_position = _position(getattr(enemy, "position", None))
            if enemy_position is None or not _is_mobile_combat_enemy(enemy):
                continue
            self.scout_risk_chunks[_chunk_coordinates(enemy_position)] = tick
            for danger_position in _enemy_danger_cells(
                (enemy,),
                self.obstacle_memory,
            ):
                self.scout_risk_chunks[_chunk_coordinates(danger_position)] = tick
        for position, expires_at in tuple(self.resource_cooldowns.items()):
            if expires_at <= tick:
                self.resource_cooldowns.pop(position, None)
        for position, observed_at in tuple(self.recent_attack_positions.items()):
            if tick - observed_at > RECENT_ATTACK_MEMORY_TICKS:
                self.recent_attack_positions.pop(position, None)
        for position, observed_at in tuple(
            self.recent_combat_danger_positions.items()
        ):
            if tick - observed_at > RECENT_ATTACK_MEMORY_TICKS:
                self.recent_combat_danger_positions.pop(position, None)
        for chunk, observed_at in tuple(self.scout_risk_chunks.items()):
            if tick - observed_at > SCOUT_RISK_MEMORY_TICKS:
                self.scout_risk_chunks.pop(chunk, None)
        for worker_key, positions in tuple(self.worker_contested_positions.items()):
            for position, expires_at in tuple(positions.items()):
                if expires_at <= tick:
                    positions.pop(position, None)
            if not positions:
                self.worker_contested_positions.pop(worker_key, None)
        for unit_key, positions in tuple(self.unit_contested_positions.items()):
            for position, expires_at in tuple(positions.items()):
                if expires_at <= tick:
                    positions.pop(position, None)
            if not positions:
                self.unit_contested_positions.pop(unit_key, None)
        for position, expires_at in tuple(self.stationary_clear_cooldowns.items()):
            if expires_at <= tick:
                self.stationary_clear_cooldowns.pop(position, None)
        for unit_key, failure in tuple(self.stationary_move_failures.items()):
            if (
                unit_key not in living_unit_ids
                or tick - failure.last_tick > MOVE_OCCUPIED_AVOID_TICKS
            ):
                self.stationary_move_failures.pop(unit_key, None)
        for enemy_key, observation in tuple(
            self.stationary_enemy_observation_memory.items()
        ):
            if tick - observation[0] > RECENT_ATTACK_MEMORY_TICKS:
                self.stationary_enemy_observation_memory.pop(enemy_key, None)
        for enemy_key, observation in tuple(self.enemy_core_memory.items()):
            if tick - observation.last_seen_tick > CORE_TARGET_MEMORY_TTL:
                self.enemy_core_memory.pop(enemy_key, None)
                self.enemy_core_revision += 1
        for position, observed_at in tuple(
            self.dropped_cargo_observation_memory.items()
        ):
            if tick - observed_at > 64:
                self.dropped_cargo_observation_memory.pop(position, None)
        for worker_key, expires_at in tuple(self.scout_cooldowns.items()):
            if expires_at <= tick:
                self.scout_cooldowns.pop(worker_key, None)

    def recent_resource_targets(self, tick: int, ttl: int) -> set[Position]:
        return {
            position
            for position, last_seen in self.resource_observation_memory.items()
            if 0 <= tick - last_seen <= ttl
        }

    def forget_resource(self, position: Position) -> None:
        self.resource_observation_memory.pop(position, None)
        self.dropped_cargo_observation_memory.pop(position, None)

    def recent_dropped_cargo_targets(self, tick: int, ttl: int) -> set[Position]:
        return {
            position
            for position, observed_at in self.dropped_cargo_observation_memory.items()
            if 0 <= tick - observed_at <= ttl
        }

    def begin_scout_return(self, actor_id: Any, core_position: Position) -> None:
        worker_key = str(actor_id)
        self.scout_return_targets[worker_key] = core_position
        self.scout_return_progress.pop(worker_key, None)
        self.scout_return_hold_ids.discard(worker_key)

    def clear_scout_return(self, actor_id: Any, tick: int) -> None:
        worker_key = str(actor_id)
        self.scout_return_targets.pop(worker_key, None)
        self.scout_return_progress.pop(worker_key, None)
        self.scout_return_hold_ids.discard(worker_key)
        self.scout_cooldowns[worker_key] = tick + 3

    def release_scout_return(self, actor_id: Any, tick: int, *, hold: bool) -> None:
        worker_key = str(actor_id)
        self.scout_return_targets.pop(worker_key, None)
        self.scout_return_progress.pop(worker_key, None)
        if hold:
            self.scout_return_hold_ids.add(worker_key)
        else:
            self.scout_return_hold_ids.discard(worker_key)
            self.scout_cooldowns[worker_key] = tick + 3

    def note_scout_return_progress(
        self,
        actor_id: Any,
        target: Position,
        route_cost: int,
        tick: int,
    ) -> bool:
        worker_key = str(actor_id)
        progress = self.scout_return_progress.get(worker_key)
        if progress is None or progress.target != target:
            self.scout_return_progress[worker_key] = ScoutReturnProgress(
                target=target,
                best_cost=route_cost,
                last_improved_tick=tick,
            )
            return False
        if route_cost < progress.best_cost:
            progress.best_cost = route_cost
            progress.last_improved_tick = tick
            return False
        return tick - progress.last_improved_tick >= SCOUT_RETURN_STALL_TICKS

    def scout_is_cooling_down(self, actor_id: Any, tick: int) -> bool:
        return self.scout_cooldowns.get(str(actor_id), 0) > tick

    def recent_worker_positions(self, actor_id: Any) -> set[Position]:
        return set(self.worker_position_history.get(str(actor_id), ()))

    def recent_worker_return_danger(self, tick: int) -> set[Position]:
        danger = {
            position
            for position, observed_at in self.recent_combat_danger_positions.items()
            if 0 <= tick - observed_at <= RECENT_ATTACK_MEMORY_TICKS
        }
        danger.update(
            position
            for position, observed_at in self.recent_attack_positions.items()
            if 0 <= tick - observed_at <= RECENT_ATTACK_MEMORY_TICKS
        )
        return danger

    def contested_worker_positions(self, actor_id: Any, tick: int) -> set[Position]:
        positions = {
            position
            for position, expires_at in self.worker_contested_positions.get(
                str(actor_id),
                {},
            ).items()
            if expires_at > tick
        }
        positions.update(self.contested_unit_positions(actor_id, tick))
        return positions

    def contested_unit_positions(self, actor_id: Any, tick: int) -> set[Position]:
        return {
            position
            for position, expires_at in self.unit_contested_positions.get(
                str(actor_id),
                {},
            ).items()
            if expires_at > tick
        }

    def note_worker_move(self, actor_id: Any, destination: Position) -> None:
        actor_key = str(actor_id)
        self.planned_worker_destinations[actor_key] = destination
        self.planned_unit_destinations[actor_key] = destination

    def note_unit_move(
        self,
        actor_id: Any,
        destination: Position,
        target: Position,
        stationary_target: Position | None = None,
    ) -> None:
        actor_key = str(actor_id)
        self.planned_unit_destinations[actor_key] = destination
        self.planned_unit_targets[actor_key] = target
        if stationary_target is not None:
            self.planned_stationary_targets[actor_key] = stationary_target

    def clear_resource_intent(self, actor_id: Any) -> None:
        key = str(actor_id)
        self.resource_intents.pop(key, None)
        self.resource_progress.pop(key, None)

    def set_resource_intent(
        self,
        actor_id: Any,
        target: Position,
        path_cost: int,
    ) -> None:
        key = str(actor_id)
        if self.resource_intents.get(key) != target:
            self.resource_progress[key] = ResourceProgress(target, path_cost)
        self.resource_intents[key] = target

    def note_resource_progress(
        self,
        actor_id: Any,
        target: Position,
        path_cost: int,
        tick: int,
    ) -> bool:
        key = str(actor_id)
        progress = self.resource_progress.get(key)
        if progress is None or progress.target != target:
            self.resource_progress[key] = ResourceProgress(target, path_cost)
            return False
        if path_cost < progress.best_cost:
            progress.best_cost = path_cost
            progress.stalled_turns = 0
            return False
        progress.stalled_turns += 1
        if progress.stalled_turns < RESOURCE_STALL_TICKS:
            return False
        self.resource_cooldowns[target] = max(
            self.resource_cooldowns.get(target, 0),
            tick + RESOURCE_COOLDOWN_TICKS,
        )
        self.clear_resource_intent(actor_id)
        return True

    def resource_is_cooling_down(self, target: Position, tick: int) -> bool:
        return self.resource_cooldowns.get(target, 0) > tick

    def exploration_target(
        self,
        actor_id: Any,
        anchor: Position,
        slot: int,
        origin: Position | None,
        tick: int,
        excluded_targets: set[Position] | None = None,
        excluded_vectors: set[int] | None = None,
    ) -> Position:
        """Return a deterministic waypoint on the persisted exploration frontier."""

        if self.exploration_anchor != anchor:
            self.exploration_anchor = anchor

        key = str(actor_id)
        vector_count = len(SCOUT_VECTORS)
        stage = self.scout_stages.setdefault(key, slot % vector_count)
        preferred_vector = stage % vector_count
        excluded_targets = excluded_targets or set()
        excluded_vectors = excluded_vectors or set()
        anchor_chunk = _chunk_coordinates(anchor)
        observed_chunks = set(self.scout_chunk_last_seen)
        observed_chunks.add(anchor_chunk)
        frontier_chunks = {
            (chunk[0] + vector_x, chunk[1] + vector_y)
            for chunk in observed_chunks
            for vector_x, vector_y in SCOUT_VECTORS
            if (chunk[0] + vector_x, chunk[1] + vector_y)
            not in observed_chunks
        }
        excluded_chunks = {
            _chunk_coordinates(target) for target in excluded_targets
        }

        def choose_target(
            chunks: set[Position],
            *,
            oldest_first: bool,
            avoid_reserved_vectors: bool,
        ) -> Position | None:
            candidates: list[tuple[tuple[int, ...], Position]] = []
            for chunk in chunks:
                if chunk in excluded_chunks:
                    continue
                if (
                    chunk in self.scout_risk_chunks
                    and 0 <= tick - self.scout_risk_chunks[chunk] <= SCOUT_RISK_MEMORY_TICKS
                ):
                    continue
                target = _scout_chunk_waypoint(anchor, chunk)
                if target == origin or target in excluded_targets:
                    continue
                vector_index = _scout_vector_index(anchor, target)
                if vector_index is None:
                    continue
                if avoid_reserved_vectors and vector_index in excluded_vectors:
                    continue
                rotation = (vector_index - preferred_vector) % vector_count
                distance = _manhattan(origin, target) if origin is not None else 0
                radial_distance = max(
                    abs(chunk[0] - anchor_chunk[0]),
                    abs(chunk[1] - anchor_chunk[1]),
                )
                score_prefix = (
                    (self.scout_chunk_last_seen.get(chunk, tick),)
                    if oldest_first
                    else ()
                )
                score = score_prefix + (
                    rotation,
                    distance,
                    radial_distance,
                    chunk[1],
                    chunk[0],
                )
                candidates.append((score, target))
            if not candidates:
                return None
            candidates.sort(key=lambda candidate: candidate[0])
            return candidates[0][1]

        # Unseen neighboring chunks are the real frontier. Only fall back to
        # the oldest observed chunk when every frontier target is reserved.
        for chunks, oldest_first in (
            (frontier_chunks, False),
            (observed_chunks, True),
        ):
            target = choose_target(
                chunks,
                oldest_first=oldest_first,
                avoid_reserved_vectors=True,
            )
            if target is not None:
                return target
            target = choose_target(
                chunks,
                oldest_first=oldest_first,
                avoid_reserved_vectors=False,
            )
            if target is not None:
                return target

        fallback_vector = SCOUT_VECTORS[preferred_vector]
        return _offset_position(
            anchor,
            (
                fallback_vector[0] * SCOUT_RING_STEP,
                fallback_vector[1] * SCOUT_RING_STEP,
            ),
        )

    def advance_exploration(
        self,
        actor_id: Any,
        slot: int,
        *,
        rotate: bool = False,
    ) -> None:
        key = str(actor_id)
        vector_count = len(SCOUT_VECTORS)
        current = self.scout_stages.get(key, slot % vector_count)
        current_sweep = current // vector_count
        if not rotate and current_sweep + 1 < SCOUT_SECTOR_SWEEP_CHUNKS:
            # Keep expanding through adjacent frontier chunks in one sector.
            next_stage = current + vector_count
        else:
            # Rotate after a completed sweep or a blocked/stalled target.
            next_stage = (current % vector_count + 1) % vector_count
        self.scout_stages[key] = next_stage
        self.scout_progress.pop(key, None)

    def note_scout_progress(
        self,
        actor_id: Any,
        target: Position,
        path_cost: int,
        position: Position | None = None,
    ) -> bool:
        key = str(actor_id)
        progress = self.scout_progress.get(key)
        if progress is None or progress.target != target:
            self.scout_progress[key] = ScoutProgress(
                target,
                path_cost,
                last_position=position,
                last_cost=path_cost,
            )
            return False

        if progress.last_position is None:
            progress.last_position = position
            progress.stalled_turns = 0
        elif position is not None and position != progress.last_position:
            # Physical movement is progress even when a detour temporarily
            # increases the estimated route cost.
            progress.stalled_turns = 0
            progress.last_position = position
        else:
            progress.stalled_turns += 1

        previous_cost = progress.last_cost
        if path_cost < progress.best_cost:
            progress.best_cost = path_cost
        if previous_cost is None or path_cost < previous_cost:
            progress.path_stalled_turns = 0
        else:
            progress.path_stalled_turns += 1
        progress.last_cost = path_cost

        if (
            progress.stalled_turns < SCOUT_NO_MOVE_TICKS
            and progress.path_stalled_turns < SCOUT_PATH_STALL_TICKS
        ):
            return False
        self.scout_progress.pop(key, None)
        return True


@dataclass(frozen=True)
class PlanReport:
    tick: int
    resources: int
    population: int
    workers: int
    vanguards: int
    rangers: int
    visible_enemies: int
    visible_resources: int
    remembered_resources: int
    threat_level: str = "NORMAL"
    threat_reason: str = "NONE"
    lifecycle: str = "ACTIVE"
    mission: str = "ECONOMY"
    production_unit: UnitType | None = None
    production_cost: int | None = None
    available_resources: int | None = None
    pending_delivery: int = 0
    visible_combat_enemies: int = 0
    danger_cells: int = 0
    pursuing_enemies: int = 0
    preemptive_enemies: int = 0
    vanguard_defenders: int = 0
    vanguard_raid: int = 0
    vanguard_attacks: int = 0
    ranger_defenders: int = 0
    ranger_raid: int = 0
    ranger_attacks: int = 0
    cargo_lane_phase: str = "IDLE"
    cargo_lane_yield_workers: int = 0
    cargo_lane_yield_units: int = 0
    cargo_watchdog_reason: str | None = None
    heal_priority_active: bool = False
    healing_defender_admitted: bool = False
    heal_priority_wait_ticks: int = 0
    heal_priority_interleave_pending: bool = False


def _position(value: Any) -> Position | None:
    if value is None:
        return None
    try:
        x, y = value
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def _offset_position(origin: Position, offset: Position) -> Position:
    return origin[0] + offset[0], origin[1] + offset[1]


def _chunk_coordinates(position: Position) -> Position:
    return position[0] // SCOUT_CHUNK_SIZE, position[1] // SCOUT_CHUNK_SIZE


def _scout_chunk_waypoint(anchor: Position, chunk: Position) -> Position:
    """Keep the anchor's local offset while moving to another map chunk."""

    anchor_chunk = _chunk_coordinates(anchor)
    return (
        anchor[0] + (chunk[0] - anchor_chunk[0]) * SCOUT_CHUNK_SIZE,
        anchor[1] + (chunk[1] - anchor_chunk[1]) * SCOUT_CHUNK_SIZE,
    )


def _scout_vector_index(anchor: Position, target: Position) -> int | None:
    dx = target[0] - anchor[0]
    dy = target[1] - anchor[1]
    vector = (
        0 if dx == 0 else 1 if dx > 0 else -1,
        0 if dy == 0 else 1 if dy > 0 else -1,
    )
    try:
        return SCOUT_VECTORS.index(vector)
    except ValueError:
        return None


def _json_position(position: Position | None) -> list[int] | None:
    return list(position) if position is not None else None


def _decode_position(raw: Any) -> Position | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        if isinstance(raw[0], bool) or isinstance(raw[1], bool):
            return None
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _decode_position_set(raw: Any) -> set[Position]:
    if not isinstance(raw, list):
        return set()
    result: set[Position] = set()
    for item in raw:
        position = _decode_position(item)
        if position is not None:
            result.add(position)
    return result


def _decode_position_records(raw: Any) -> dict[Position, int]:
    if not isinstance(raw, list):
        return {}
    result: dict[Position, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        position = _decode_position(item.get("position"))
        observed_at = item.get("tick")
        if position is None or isinstance(observed_at, bool):
            continue
        try:
            result[position] = int(observed_at)
        except (TypeError, ValueError):
            continue
    return result


def _encode_position_records(
    records: dict[Position, int],
    *,
    current_tick: int | None = None,
    ttl: int | None = None,
) -> list[dict[str, Any]]:
    encoded: list[dict[str, Any]] = []
    for position, observed_at in sorted(records.items()):
        if (
            current_tick is not None
            and ttl is not None
            and not 0 <= current_tick - observed_at <= ttl
        ):
            continue
        encoded.append({"position": _json_position(position), "tick": observed_at})
    return encoded


def _load_tactic_memory(path: Path) -> TacticMemory:
    """Load only safe map memory; unit-specific tactical state is rebuilt live."""

    if not path.is_file():
        return TacticMemory()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("持久化记忆读取失败，将使用空记忆: %s", exc)
        return TacticMemory()
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        LOGGER.warning("持久化记忆版本不兼容，将使用空记忆: %s", path)
        return TacticMemory()

    memory = TacticMemory(
        obstacle_memory=_decode_position_set(payload.get("obstacles")),
        last_observed_tick=(
            int(payload["last_tick"])
            if isinstance(payload.get("last_tick"), int)
            and not isinstance(payload.get("last_tick"), bool)
            else None
        ),
        resource_observation_memory=_decode_position_records(
            payload.get("resources")
        ),
        dropped_cargo_observation_memory=_decode_position_records(
            payload.get("dropped_cargo")
        ),
        scout_chunk_last_seen=_decode_position_records(
            payload.get("scout_chunks")
        ),
        exploration_anchor=_decode_position(payload.get("exploration_anchor")),
    )
    LOGGER.info(
        "已恢复地图记忆 | 障碍=%s 资源=%s 掉落货物=%s",
        len(memory.obstacle_memory),
        len(memory.resource_observation_memory),
        len(memory.dropped_cargo_observation_memory),
    )
    return memory


def _save_tactic_memory(
    memory: TacticMemory,
    path: Path,
    current_tick: int | None = None,
) -> None:
    """Atomically persist map observations without credentials or stale unit IDs."""

    if current_tick is None:
        current_tick = memory.last_observed_tick
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_tick": current_tick,
        "obstacles": [
            _json_position(position)
            for position in sorted(memory.obstacle_memory)
        ],
        "resources": _encode_position_records(
            memory.resource_observation_memory,
            current_tick=current_tick,
            ttl=64,
        ),
        "dropped_cargo": _encode_position_records(
            memory.dropped_cargo_observation_memory,
            current_tick=current_tick,
            ttl=64,
        ),
        "scout_chunks": _encode_position_records(memory.scout_chunk_last_seen),
        "exploration_anchor": _json_position(memory.exploration_anchor),
    }
    temporary_path = Path(f"{path}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    except OSError as exc:
        LOGGER.warning("持久化记忆保存失败: %s", exc)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _enum_label(value: Any) -> str:
    raw = getattr(value, "name", value)
    return str(raw).upper().rsplit(".", 1)[-1]


def _trace_actor(actor: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(getattr(actor, "id", "")),
        "position": _json_position(_position(getattr(actor, "position", None))),
        "unit_type": _enum_label(getattr(actor, "unit_type", "")),
    }
    for field_name in ("cargo", "hp", "shield"):
        value = getattr(actor, field_name, None)
        if value is not None:
            payload[field_name] = value
    return payload


def _worker_mode(worker: Any, memory: TacticMemory, tick: int) -> str:
    worker_key = str(getattr(worker, "id", ""))
    lane = memory.cargo_lane
    if lane.active and worker_key == lane.departing_worker_id:
        return "CARGO_LANE_EGRESS"
    if lane.active and (
        worker_key in lane.startup_pending_ids
        or worker_key in lane.yield_worker_ids
    ):
        return "CARGO_LANE_YIELD"
    if worker_key == memory.core_lane_departing_worker_id:
        return "CORE_CLEAR"
    if worker_key in memory.core_lane_yield_worker_ids:
        return "CARGO_YIELD"
    if int(getattr(worker, "cargo", 0)) > 0:
        return "CARGO_RETURN"
    if worker_key in memory.core_observer_return_ids:
        return "CORE_RECALL"
    if memory.raid.observer_id == worker_key:
        return "CORE_OBSERVER"
    if (
        worker_key in memory.healing_worker_ids
        and memory.heal_intent_id == worker_key
    ):
        if memory.healing_worker_stage_target is not None:
            return "HEAL_STAGE"
        return "HEAL_RETURN"
    return_target = memory.scout_return_targets.get(worker_key)
    if (
        return_target is not None
        and worker_key in memory.resource_intents
        and (worker_position := _position(getattr(worker, "position", None))) is not None
        and _manhattan(worker_position, return_target) <= CARGO_LANE_STAGE_RADII[-1]
    ):
        return "RESOURCE"
    if return_target is not None:
        return "SCOUT_RETURN"
    if worker_key in memory.scout_return_hold_ids:
        return "HEAL_WAIT"
    if worker_key in memory.resource_intents:
        return "RESOURCE"
    if memory.scout_is_cooling_down(worker_key, tick):
        return "SCOUT_COOLDOWN"
    if worker_key in memory.scout_progress:
        return "SCOUT"
    return "IDLE"


def _event_label(event: Any) -> str:
    return _enum_label(getattr(event, "event_type", "UNKNOWN"))


def _trace_event(event: Any) -> dict[str, Any]:
    """Keep selected authoritative values while avoiding opaque event payloads."""

    event_name = _event_label(event)
    payload: dict[str, Any] = {"type": event_name}
    reason_code = getattr(event, "reason_code", None)
    if reason_code is not None:
        payload["reason_code"] = _enum_label(reason_code)
    diagnostic_event = event_name in {
        "UNIT_MOVE_FAILED",
        "CORE_RESOURCES_CAPTURED",
    } or any(
        token in event_name
        for token in ("DAMAGE", "DAMAGED", "DESTROY", "DROPPED", "HIT", "LOST")
    )
    if diagnostic_event:
        actor_id = getattr(event, "actor_id", None)
        target_id = getattr(event, "target_id", None)
        position = _position(getattr(event, "position", None))
        if actor_id is not None:
            payload["actor_id"] = str(actor_id)
        if target_id is not None:
            payload["target_id"] = str(target_id)
        if position is not None:
            payload["position"] = _json_position(position)
    selected_fields = {
        "CORE_RESOURCES_CAPTURED": (
            "amount",
            "available",
            "destroyed",
            "capacity",
        ),
        "CORE_SPAWN_SUCCEEDED": ("unit_type", "cost", "required"),
        "CORE_SPAWN_FAILED": ("unit_type", "cost", "required"),
    }.get(event_name)
    if selected_fields is None:
        return payload

    values = getattr(event, "values", None)
    if not isinstance(values, dict):
        return payload
    selected_values = {
        field_name: values[field_name]
        for field_name in selected_fields
        if field_name in values
    }
    if selected_values:
        payload["values"] = selected_values
    return payload


def _log_spawn_resolution(turn: Any) -> None:
    """Log server-authoritative spawn prices for post-match diagnosis."""

    for event in getattr(turn, "events", ()):
        event_name = _event_label(event)
        if event_name not in {"CORE_SPAWN_SUCCEEDED", "CORE_SPAWN_FAILED"}:
            continue
        details = _trace_event(event)
        LOGGER.debug(
            "tick=%s server_spawn_resolution=%s",
            getattr(turn, "tick", "?"),
            details,
        )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionStats:
    session_id: str
    started_at: str = field(default_factory=_utc_timestamp)
    last_tick: int | None = None
    ticks: int = 0
    accepted_ticks: int = 0
    rejected_ticks: int = 0
    max_population: int = 0
    max_workers: int = 0
    max_vanguards: int = 0
    max_rangers: int = 0
    visible_resource_ticks: int = 0
    enemy_ticks: int = 0
    threat_counts: dict[str, int] = field(default_factory=dict)
    mission_counts: dict[str, int] = field(default_factory=dict)
    event_counts: dict[str, int] = field(default_factory=dict)
    last_deposit_tick: int | None = None
    max_deposit_gap_ticks: int = 0
    current_injured_units: int = 0
    max_injured_units: int = 0
    current_critical_combat_units: int = 0
    max_critical_combat_units: int = 0
    max_heal_priority_wait_ticks: int = 0
    healing_admission_timeouts: int = 0
    cargo_watchdog_counts: dict[str, int] = field(default_factory=dict)
    runtime_error_counts: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        turn: Any,
        report: PlanReport,
        accepted: Any,
        memory: TacticMemory | None = None,
    ) -> None:
        accepted_value = (
            accepted
            if isinstance(accepted, bool)
            else bool(getattr(accepted, "accepted", True))
        )
        self.last_tick = report.tick
        self.ticks += 1
        if accepted_value:
            self.accepted_ticks += 1
        else:
            self.rejected_ticks += 1
        self.max_population = max(self.max_population, report.population)
        self.max_workers = max(self.max_workers, report.workers)
        self.max_vanguards = max(self.max_vanguards, report.vanguards)
        self.max_rangers = max(self.max_rangers, report.rangers)
        if report.visible_resources or report.remembered_resources:
            self.visible_resource_ticks += 1
        if report.visible_enemies:
            self.enemy_ticks += 1
        self.threat_counts[report.threat_level] = (
            self.threat_counts.get(report.threat_level, 0) + 1
        )
        self.mission_counts[report.mission] = (
            self.mission_counts.get(report.mission, 0) + 1
        )
        event_names = []
        for event in getattr(turn, "events", ()):
            event_name = _event_label(event)
            event_names.append(event_name)
            self.event_counts[event_name] = self.event_counts.get(event_name, 0) + 1
        if "DEPOSIT_SUCCEEDED" in event_names:
            if self.last_deposit_tick is not None:
                self.max_deposit_gap_ticks = max(
                    self.max_deposit_gap_ticks,
                    report.tick - self.last_deposit_tick,
                )
            self.last_deposit_tick = report.tick
        workers = tuple(getattr(turn, "workers", ()))
        vanguards = tuple(getattr(turn, "vanguards", ()))
        rangers = tuple(getattr(turn, "rangers", ()))
        injured_counts = (
            _unit_health_summary(workers, UnitType.WORKER)[2],
            _unit_health_summary(vanguards, UnitType.VANGUARD)[2],
            _unit_health_summary(rangers, UnitType.RANGER)[2],
        )
        critical_counts = (
            _unit_health_summary(vanguards, UnitType.VANGUARD)[3],
            _unit_health_summary(rangers, UnitType.RANGER)[3],
        )
        self.current_injured_units = sum(injured_counts)
        self.max_injured_units = max(
            self.max_injured_units,
            self.current_injured_units,
        )
        self.current_critical_combat_units = sum(critical_counts)
        self.max_critical_combat_units = max(
            self.max_critical_combat_units,
            self.current_critical_combat_units,
        )
        if memory is not None and memory.heal_priority_started_tick >= 0:
            self.max_heal_priority_wait_ticks = max(
                self.max_heal_priority_wait_ticks,
                report.tick - memory.heal_priority_started_tick,
            )
        if (
            memory is not None
            and memory.healing_defender_timeout_tick == report.tick
        ):
            self.healing_admission_timeouts += 1
        lane = memory.cargo_lane if memory is not None else None
        if lane is not None and lane.watchdog_tick == report.tick and lane.watchdog_reason:
            self.cargo_watchdog_counts[lane.watchdog_reason] = (
                self.cargo_watchdog_counts.get(lane.watchdog_reason, 0) + 1
            )

    def record_runtime_error(self, category: str) -> None:
        self.runtime_error_counts[category] = (
            self.runtime_error_counts.get(category, 0) + 1
        )

    def as_dict(self, finished_at: str | None = None) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "last_tick": self.last_tick,
            "ticks": self.ticks,
            "accepted_ticks": self.accepted_ticks,
            "rejected_ticks": self.rejected_ticks,
            "max_population": self.max_population,
            "max_workers": self.max_workers,
            "max_vanguards": self.max_vanguards,
            "max_rangers": self.max_rangers,
            "visible_resource_ticks": self.visible_resource_ticks,
            "enemy_ticks": self.enemy_ticks,
            "threat_counts": dict(sorted(self.threat_counts.items())),
            "mission_counts": dict(sorted(self.mission_counts.items())),
            "event_counts": dict(sorted(self.event_counts.items())),
            "last_deposit_tick": self.last_deposit_tick,
            "max_deposit_gap_ticks": self.max_deposit_gap_ticks,
            "current_injured_units": self.current_injured_units,
            "max_injured_units": self.max_injured_units,
            "current_critical_combat_units": (
                self.current_critical_combat_units
            ),
            "max_critical_combat_units": self.max_critical_combat_units,
            "max_heal_priority_wait_ticks": self.max_heal_priority_wait_ticks,
            "healing_admission_timeouts": self.healing_admission_timeouts,
            "cargo_watchdog_counts": dict(
                sorted(self.cargo_watchdog_counts.items())
            ),
            "runtime_error_counts": dict(
                sorted(self.runtime_error_counts.items())
            ),
        }


class SessionRecorder:
    """Write replay-friendly JSONL turns and a compact session summary."""

    def __init__(self, trace_file: str, stats_file: str):
        self.trace_path = _resolve_runtime_path(trace_file)
        self.stats_path = _resolve_runtime_path(stats_file)
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        self.stats = SessionStats(_AGENT_SESSION_ID)
        self._closed = False
        self.trace_logger = logging.getLogger(
            f"arena_agent.trace.{_AGENT_SESSION_ID}"
        )
        self.trace_logger.propagate = False
        self.trace_logger.setLevel(logging.INFO)
        trace_handler = RotatingFileHandler(
            self.trace_path,
            maxBytes=TRACE_MAX_BYTES,
            backupCount=TRACE_BACKUP_COUNT,
            encoding="utf-8",
        )
        trace_handler.setFormatter(logging.Formatter("%(message)s"))
        self.trace_logger.addHandler(trace_handler)
        self._trace_handler = trace_handler

    def record_turn(
        self,
        turn: Any,
        report: PlanReport,
        accepted: Any,
        memory: TacticMemory,
    ) -> None:
        if self._closed:
            return
        events = [_event_label(event) for event in getattr(turn, "events", ())]
        workers = list(getattr(turn, "workers", ()))
        healing_defender = next(
            (
                unit
                for unit in tuple(getattr(turn, "vanguards", ()))
                + tuple(getattr(turn, "rangers", ()))
                if str(getattr(unit, "id", ""))
                == memory.healing_defender_intent_id
            ),
            None,
        )
        critical_heal_wait_exhausted = _critical_defender_heal_wait_exhausted(
            turn,
            healing_defender,
            memory,
            memory.heal_priority_intent_id,
            report.tick,
        )
        heal_force_reason = None
        if memory.core_visit_forced_purpose == "HEAL":
            if critical_heal_wait_exhausted:
                heal_force_reason = "CRITICAL_WAIT_LIMIT"
            elif memory.core_visit_deposit_streak >= HEAL_PRIORITY_DEPOSIT_LIMIT:
                heal_force_reason = "DEPOSIT_QUOTA"
            elif memory.healing_defender_admitted_id is not None:
                heal_force_reason = "ADMITTED"
        payload = {
            "session_id": _AGENT_SESSION_ID,
            "tick": report.tick,
            "accepted": (
                accepted
                if isinstance(accepted, bool)
                else bool(getattr(accepted, "accepted", True))
            ),
            "core": _trace_actor(getattr(turn, "core", None))
            if getattr(turn, "core", None) is not None
            else None,
            "resources": report.resources,
            "resource_cells": [
                _json_position(position)
                for position in sorted(_positions(getattr(turn, "resource_cells", ())))
            ],
            "population": report.population,
            "workers": [_trace_actor(worker) for worker in workers],
            "vanguards": [
                _trace_actor(unit) for unit in getattr(turn, "vanguards", ())
            ],
            "rangers": [
                _trace_actor(unit) for unit in getattr(turn, "rangers", ())
            ],
            "enemies": [
                _trace_actor(enemy)
                for enemy in getattr(turn, "visible_enemies", ())
            ],
            "obstacles": [
                _json_position(position)
                for position in sorted(_positions(getattr(turn, "obstacle_cells", ())))
            ],
            "events": events,
            "event_details": [_trace_event(event) for event in getattr(turn, "events", ())],
            "threat": report.threat_level,
            "threat_reason": report.threat_reason,
            "combat_enemy_count": report.visible_combat_enemies,
            "danger_cell_count": report.danger_cells,
            "active_enemy_ids": sorted(memory.active_enemy_ids),
            "pursuing_enemy_ids": sorted(memory.pursuing_enemy_ids),
            "preemptive_enemy_ids": sorted(memory.preemptive_enemy_ids),
            "lifecycle": report.lifecycle,
            "mission": report.mission,
            "production_quote": {
                "unit_type": _enum_label(report.production_unit)
                if report.production_unit is not None
                else None,
                "cost": report.production_cost,
            },
            "available_resources": report.available_resources,
            "pending_delivery": report.pending_delivery,
            "resource_targets": {
                worker_id: _json_position(target)
                for worker_id, target in sorted(memory.resource_intents.items())
            },
            "scout_targets": {
                worker_id: _json_position(progress.target)
                for worker_id, progress in sorted(memory.scout_progress.items())
            },
            "worker_modes": {
                str(getattr(worker, "id", "")): _worker_mode(
                    worker,
                    memory,
                    report.tick,
                )
                for worker in workers
            },
            "scout_return_targets": {
                worker_id: _json_position(target)
                for worker_id, target in sorted(memory.scout_return_targets.items())
            },
            "scout_return_progress": {
                worker_id: {
                    "target": _json_position(progress.target),
                    "best_cost": progress.best_cost,
                    "last_improved_tick": progress.last_improved_tick,
                }
                for worker_id, progress in sorted(memory.scout_return_progress.items())
            },
            "healing_worker_ids": sorted(memory.healing_worker_ids),
            "healing_worker_stage_target": _json_position(
                memory.healing_worker_stage_target
            ),
            "healing_defenders": {
                "candidate_ids": sorted(memory.healing_defender_ids),
                "intent_id": memory.healing_defender_intent_id,
                "intent_tick": memory.healing_defender_intent_tick,
                "intent_wait_ticks": (
                    max(0, report.tick - memory.healing_defender_intent_tick)
                    if memory.healing_defender_intent_tick >= 0
                    else 0
                ),
                "critical_wait_exhausted": critical_heal_wait_exhausted,
                "stage_target": _json_position(
                    memory.healing_defender_stage_target
                ),
                "admitted_id": memory.healing_defender_admitted_id,
                "admitted_tick": memory.healing_defender_admitted_tick,
                "last_progress_tick": (
                    memory.healing_defender_last_progress_tick
                ),
                "last_position": _json_position(
                    memory.healing_defender_last_position
                ),
                "last_timeout_tick": memory.healing_defender_timeout_tick,
                "approach_path": [
                    _json_position(position)
                    for position in memory.cargo_lane.heal_approach_path
                ],
            },
            "heal_intent": {
                "unit_id": memory.heal_intent_id,
                "queued_tick": memory.heal_intent_tick,
            },
            "core_visit": {
                "unit_id": memory.core_visit.unit_id,
                "purpose": memory.core_visit.purpose,
                "granted_tick": memory.core_visit.granted_tick,
                "deposit_streak": memory.core_visit_deposit_streak,
                "forced_purpose": memory.core_visit_forced_purpose,
                "heal_force_reason": heal_force_reason,
                "heal_priority_intent_id": memory.heal_priority_intent_id,
                "heal_priority_started_tick": memory.heal_priority_started_tick,
                "heal_priority_admission_timeouts": (
                    memory.heal_priority_admission_timeouts
                ),
                "heal_priority_interleave_pending": (
                    memory.heal_priority_interleave_pending
                ),
            },
            "scout_progress": {
                worker_id: {
                    "target": _json_position(progress.target),
                    "route_cost": progress.last_cost,
                    "best_cost": progress.best_cost,
                    "no_move_ticks": progress.stalled_turns,
                    "path_stalled_ticks": progress.path_stalled_turns,
                }
                for worker_id, progress in sorted(memory.scout_progress.items())
            },
            "worker_recent_positions": {
                worker_id: [_json_position(position) for position in positions]
                for worker_id, positions in sorted(
                    memory.worker_position_history.items()
                )
            },
        }
        if memory.route_diagnostics:
            payload["route_anomalies"] = dict(sorted(memory.route_diagnostics.items()))
        pocket = memory.core_pocket
        if pocket.closed or pocket.consecutive_ticks:
            payload["core_pocket"] = {
                "status": "BLOCKED" if pocket.blocked else "CLOSED",
                "size": len(pocket.component),
                "admitted_cargo_ids": sorted(pocket.admitted_cargo_ids),
                "stalled_admitted_cargo_ids": sorted(
                    pocket.stalled_admitted_cargo_ids
                ),
                "outside_cargo_ids": sorted(pocket.outside_cargo_ids),
                "static_reachable_cargo_ids": sorted(
                    pocket.static_reachable_cargo_ids
                ),
                "consecutive_ticks": pocket.consecutive_ticks,
            }
        lane = memory.cargo_lane
        if lane.active:
            payload["cargo_lane"] = {
                "phase": lane.phase,
                "owner_id": lane.owner_id,
                "queued_owner_id": lane.queued_owner_id,
                "departing_worker_id": lane.departing_worker_id,
                "path": [_json_position(position) for position in lane.path],
                "gateway": _json_position(lane.gateway),
                "owner_approach_path": [
                    _json_position(position)
                    for position in lane.owner_approach_path
                ],
                "heal_approach_path": [
                    _json_position(position)
                    for position in lane.heal_approach_path
                ],
                "egress_path": [
                    _json_position(position) for position in lane.egress_path
                ],
                "egress_target": _json_position(lane.egress_target),
                "startup_pending_ids": sorted(lane.startup_pending_ids),
                "yield_worker_ids": sorted(lane.yield_worker_ids),
                "yield_unit_ids": sorted(lane.yield_unit_ids),
                "core_release_hold_unit_ids": sorted(
                    lane.core_release_hold_unit_ids
                ),
                "yield_targets": {
                    unit_id: _json_position(target)
                    for unit_id, target in sorted(lane.yield_targets.items())
                },
                "yield_paths": {
                    unit_id: [_json_position(position) for position in path]
                    for unit_id, path in sorted(lane.yield_paths.items())
                },
                "stage_targets": {
                    worker_id: _json_position(target)
                    for worker_id, target in sorted(lane.stage_targets.items())
                },
                "started_tick": lane.started_tick,
                "phase_started_tick": lane.phase_started_tick,
                "last_planned_tick": lane.last_planned_tick,
                "geometry_source": lane.geometry_source,
                "watchdog_tick": lane.watchdog_tick,
                "watchdog_reason": lane.watchdog_reason,
                "owner_progress_id": lane.owner_progress_id,
                "owner_best_remaining_steps": (
                    lane.owner_best_remaining_steps
                ),
                "owner_last_progress_tick": lane.owner_last_progress_tick,
                "departing_progress_id": lane.departing_progress_id,
                "departing_best_remaining_steps": (
                    lane.departing_best_remaining_steps
                ),
                "departing_last_progress_tick": lane.departing_last_progress_tick,
                "stalled_owner_cooldowns": {
                    worker_id: {
                        "position": _json_position(position),
                        "expires_at": expires_at,
                    }
                    for worker_id, (position, expires_at) in sorted(
                        memory.cargo_lane_stalled_owners.items()
                    )
                },
            }
        active_contested = {
            worker_id: [
                _json_position(position)
                for position in sorted(
                    memory.contested_worker_positions(worker_id, report.tick)
                )
            ]
            for worker_id in sorted(memory.worker_contested_positions)
            if memory.contested_worker_positions(worker_id, report.tick)
        }
        if active_contested:
            payload["worker_contested_positions"] = active_contested
        remembered_return_danger = memory.recent_worker_return_danger(report.tick)
        if remembered_return_danger:
            payload["worker_return_danger_count"] = len(remembered_return_danger)
        if memory.guard_idle_assignments:
            payload["guard_idle_assignments"] = memory.guard_idle_assignments
        if memory.raid_state_changed_tick == report.tick:
            payload["core_raid_state"] = {
                "state": memory.raid.state,
                "target_id": memory.raid.target_id,
                "target_position": _json_position(memory.raid.target_position),
                "observer_id": memory.raid.observer_id,
                "member_count": len(memory.raid.raid_member_ids),
            }
        if memory.raid.last_replan_tick == report.tick:
            payload["core_raid_replan"] = {
                "reason": memory.raid.last_replan_reason,
                "member_count": len(memory.raid.raid_member_ids),
                "formation_cost": memory.raid.last_formation_cost,
                "stalled_ticks": memory.raid.stalled_ticks,
            }
        self.trace_logger.info(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        self.stats.record(turn, report, accepted, memory)
        if self.stats.ticks % STATS_SAVE_INTERVAL_TICKS == 0:
            self.save_stats()

    def save_stats(self, finished_at: str | None = None) -> None:
        payload = self.stats.as_dict(finished_at=finished_at)
        temporary_path = Path(f"{self.stats_path}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.stats_path)
        except OSError as exc:
            LOGGER.warning("战局统计保存失败: %s", exc)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def record_runtime_error(self, category: str) -> None:
        self.stats.record_runtime_error(category)
        self.save_stats()

    def close(self) -> None:
        if self._closed:
            return
        self.save_stats(finished_at=_utc_timestamp())
        self._trace_handler.flush()
        self._trace_handler.close()
        self.trace_logger.removeHandler(self._trace_handler)
        self._closed = True


def _positions(values: Iterable[Any]) -> set[Position]:
    result: set[Position] = set()
    for value in values:
        position = _position(value)
        if position is not None:
            result.add(position)
    return result


def _same_cell(left: Any, right: Any) -> bool:
    left_position = _position(left)
    right_position = _position(right)
    return left_position is not None and left_position == right_position


def _manhattan(left: Position, right: Position) -> int:
    return abs(left[0] - right[0]) + abs(left[1] - right[1])


def _chebyshev(left: Position, right: Position) -> int:
    return max(abs(left[0] - right[0]), abs(left[1] - right[1]))


def _chebyshev_ring_positions(origin: Position, radius: int) -> tuple[Position, ...]:
    """Enumerate a complete ring in a deterministic (dx, dy) order."""

    if radius <= 0:
        return ()
    return tuple(
        (origin[0] + dx, origin[1] + dy)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        if max(abs(dx), abs(dy)) == radius
    )


def _minimum_cost_assignment(costs: list[list[int]]) -> tuple[int, ...]:
    """Return one deterministic minimum-cost column for each matrix row."""

    if not costs:
        return ()
    row_count = len(costs)
    column_count = len(costs[0])
    if column_count < row_count or any(len(row) != column_count for row in costs):
        raise ValueError("assignment matrix must be rectangular with rows <= columns")

    row_potential = [0] * (row_count + 1)
    column_potential = [0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)

    for row_index in range(1, row_count + 1):
        matched_row[0] = row_index
        current_column = 0
        minimum_slack = [sys.maxsize] * (column_count + 1)
        visited = [False] * (column_count + 1)
        while True:
            visited[current_column] = True
            current_row = matched_row[current_column]
            delta = sys.maxsize
            next_column = 0
            for column_index in range(1, column_count + 1):
                if visited[column_index]:
                    continue
                reduced_cost = (
                    costs[current_row - 1][column_index - 1]
                    - row_potential[current_row]
                    - column_potential[column_index]
                )
                if reduced_cost < minimum_slack[column_index]:
                    minimum_slack[column_index] = reduced_cost
                    previous_column[column_index] = current_column
                if minimum_slack[column_index] < delta:
                    delta = minimum_slack[column_index]
                    next_column = column_index
            for column_index in range(column_count + 1):
                if visited[column_index]:
                    row_potential[matched_row[column_index]] += delta
                    column_potential[column_index] -= delta
                else:
                    minimum_slack[column_index] -= delta
            current_column = next_column
            if matched_row[current_column] == 0:
                break
        while True:
            next_column = previous_column[current_column]
            matched_row[current_column] = matched_row[next_column]
            current_column = next_column
            if current_column == 0:
                break

    assignment = [-1] * row_count
    for column_index in range(1, column_count + 1):
        row_index = matched_row[column_index]
        if row_index:
            assignment[row_index - 1] = column_index - 1
    return tuple(assignment)


def _direction_for_delta(dx: int, dy: int) -> Direction:
    if dx > 0:
        return Direction.RIGHT
    if dx < 0:
        return Direction.LEFT
    if dy > 0:
        return Direction.DOWN
    return Direction.UP


def _candidate_steps(origin: Position, target: Position) -> list[tuple[Direction, Position]]:
    dx = target[0] - origin[0]
    dy = target[1] - origin[1]
    deltas = [(dx, 0), (0, dy)] if abs(dx) >= abs(dy) else [(0, dy), (dx, 0)]
    candidates: list[tuple[Direction, Position]] = []
    for delta_x, delta_y in deltas:
        if delta_x == 0 and delta_y == 0:
            continue
        direction = _direction_for_delta(delta_x, delta_y)
        candidates.append(
            (
                direction,
                (origin[0] + (1 if delta_x > 0 else -1 if delta_x < 0 else 0),
                 origin[1] + (1 if delta_y > 0 else -1 if delta_y < 0 else 0)),
            )
        )
    return candidates


def _estimated_path_cost(
    start: Position,
    target: Position,
    blocked: set[Position],
    *,
    discouraged: set[Position] | None = None,
    max_expansions: int = PATH_COST_MAX_EXPANSIONS,
) -> int:
    """Estimate route length without letting a blocked target stall a Worker forever."""

    if start == target:
        return 0
    blocked = set(blocked)
    blocked.discard(start)
    discouraged = set(discouraged or ())
    discouraged.discard(start)
    if target in blocked:
        return PATH_COST_UNREACHABLE

    sequence = count()
    start_distance = _manhattan(start, target)
    frontier: list[tuple[int, int, int, Position]] = [
        (start_distance, 0, next(sequence), start)
    ]
    costs = {start: 0}
    expansions = 0
    directions = ((0, -1), (1, 0), (0, 1), (-1, 0))

    while frontier and expansions < max_expansions:
        _, current_cost, _, current = heapq.heappop(frontier)
        if current_cost != costs.get(current):
            continue
        if current == target:
            return current_cost
        expansions += 1
        for delta_x, delta_y in directions:
            destination = current[0] + delta_x, current[1] + delta_y
            if destination in blocked:
                continue
            new_cost = current_cost + 1
            if destination in discouraged:
                new_cost += SCOUT_RECENT_POSITION_PENALTY
            if new_cost >= costs.get(destination, sys.maxsize):
                continue
            costs[destination] = new_cost
            remaining = _manhattan(destination, target)
            heapq.heappush(
                frontier,
                (new_cost + remaining, new_cost, next(sequence), destination),
            )
    if not frontier:
        return PATH_COST_UNREACHABLE
    return min(estimated_cost for estimated_cost, *_ in frontier)


def _complete_route(
    start: Position,
    target: Position,
    blocked: set[Position],
    *,
    discouraged: set[Position] | None = None,
    max_expansions: int = ROUTE_MAX_EXPANSIONS,
) -> RouteSearchResult:
    """Find an exact cardinal path, distinguishing exhaustion from budget use."""

    if start == target:
        return RouteSearchResult("SUCCESS", (start,), frozenset({start}))
    blocked = set(blocked)
    blocked.discard(start)
    if target in blocked:
        return RouteSearchResult("UNREACHABLE", explored=frozenset({start}))
    discouraged = set(discouraged or ())
    discouraged.discard(start)
    sequence = count()
    frontier: list[tuple[int, int, int, Position]] = [
        (_manhattan(start, target), 0, next(sequence), start)
    ]
    costs = {start: 0}
    parents: dict[Position, Position] = {}
    expanded: set[Position] = set()
    directions = ((0, -1), (1, 0), (0, 1), (-1, 0))

    while frontier and len(expanded) < max_expansions:
        _, current_cost, _, current = heapq.heappop(frontier)
        if current_cost != costs.get(current) or current in expanded:
            continue
        if current == target:
            path = [current]
            while path[-1] != start:
                path.append(parents[path[-1]])
            path.reverse()
            return RouteSearchResult(
                "SUCCESS",
                tuple(path),
                frozenset(expanded | {current}),
            )
        expanded.add(current)
        for delta_x, delta_y in directions:
            destination = current[0] + delta_x, current[1] + delta_y
            if destination in blocked:
                continue
            new_cost = current_cost + 1
            if destination in discouraged:
                new_cost += SCOUT_RECENT_POSITION_PENALTY
            if new_cost >= costs.get(destination, sys.maxsize):
                continue
            costs[destination] = new_cost
            parents[destination] = current
            heapq.heappush(
                frontier,
                (
                    new_cost + _manhattan(destination, target),
                    new_cost,
                    next(sequence),
                    destination,
                ),
            )
    status = "BUDGET_EXCEEDED" if frontier else "UNREACHABLE"
    return RouteSearchResult(status, explored=frozenset(expanded))


def _direction_between(origin: Position, destination: Position) -> Direction | None:
    return _adjacent_direction(origin, destination)


def _cached_worker_route_step(
    memory: TacticMemory,
    worker_id: str,
    role: str,
    origin: Position,
    target: Position,
    blocked: set[Position],
    *,
    discouraged: set[Position] | None = None,
) -> tuple[Direction | None, RouteSearchResult | None]:
    cached = memory.worker_routes.get(worker_id)
    if (
        cached is not None
        and cached.role == role
        and cached.target == target
        and cached.core_memory_revision == memory.enemy_core_revision
        and origin in cached.path[cached.index :]
    ):
        path_index = cached.path.index(origin, cached.index)
        cached.index = path_index
        if path_index + 1 < len(cached.path):
            next_cell = cached.path[path_index + 1]
            if next_cell not in blocked:
                return _direction_between(origin, next_cell), None
        elif origin == target:
            return None, None

    result = _complete_route(
        origin,
        target,
        blocked,
        discouraged=discouraged,
    )
    if result.status == "SUCCESS":
        memory.worker_routes[worker_id] = WorkerRoute(
            role=role,
            target=target,
            path=result.path,
            core_memory_revision=memory.enemy_core_revision,
            index=0,
        )
        if len(result.path) >= 2:
            return _direction_between(origin, result.path[1]), result
        return None, result
    memory.worker_routes.pop(worker_id, None)
    return None, result


def _next_position(origin: Position, direction: Direction) -> Position:
    if direction == Direction.UP:
        return origin[0], origin[1] - 1
    if direction == Direction.DOWN:
        return origin[0], origin[1] + 1
    if direction == Direction.LEFT:
        return origin[0] - 1, origin[1]
    return origin[0] + 1, origin[1]


def _position_in_direction(
    origin: Position,
    direction: Direction,
    distance: int,
) -> Position:
    position = origin
    for _ in range(distance):
        position = _next_position(position, direction)
    return position


def _primary_enemy_direction(origin: Position, enemy: Any) -> Direction | None:
    enemy_position = _position(getattr(enemy, "position", None))
    if enemy_position is None:
        return None
    return _direction_for_delta(
        enemy_position[0] - origin[0],
        enemy_position[1] - origin[1],
    )


def _guard_position(
    origin: Position,
    core_position: Position,
    radius: int,
    slot: int,
    enemies: list[Any],
    blocked: set[Position],
    danger_cells: set[Position],
    resource_cells: set[Position],
    priority_enemy_ids: set[str],
) -> Position:
    """Choose a safe Core-facing post without starting a chase."""

    axes: list[Direction] = []
    for enemy in sorted(
        enemies,
        key=lambda item: (
            int(str(getattr(item, "id", "")) not in priority_enemy_ids),
            _manhattan(core_position, _position(item.position)),
            str(getattr(item, "id", "")),
        ),
    ):
        direction = _primary_enemy_direction(core_position, enemy)
        if direction is not None and direction not in axes:
            axes.append(direction)
    axes.extend(direction for direction in CARDINAL_DIRECTIONS if direction not in axes)
    rotated_axes = axes[slot % len(axes) :] + axes[: slot % len(axes)]
    enemy_positions = {
        position
        for position in (_position(getattr(enemy, "position", None)) for enemy in enemies)
        if position is not None
    }
    for direction in rotated_axes:
        destination = _position_in_direction(core_position, direction, radius)
        if destination == core_position:
            continue
        if destination in blocked or destination in danger_cells:
            continue
        if destination in resource_cells or destination in enemy_positions:
            continue
        return destination
    if origin != core_position:
        return origin
    return _next_position(core_position, CARDINAL_DIRECTIONS[slot % 4])


def _guard_candidates(
    core_position: Position,
    radii: tuple[int, ...],
    blocked: set[Position],
    danger_cells: set[Position],
    resource_cells: set[Position],
    enemy_positions: set[Position],
    reserved_positions: set[Position],
) -> list[Position]:
    candidates: list[Position] = []
    for radius in radii:
        for position in _chebyshev_ring_positions(core_position, radius):
            if position == core_position or position in candidates:
                continue
            if position in blocked or position in danger_cells:
                continue
            if position in resource_cells or position in enemy_positions:
                continue
            if position in reserved_positions:
                continue
            candidates.append(position)
    return candidates


def _assign_guard_posts(
    units: Iterable[Any],
    core_position: Position | None,
    radii: tuple[int, ...],
    blocked: set[Position],
    danger_cells: set[Position],
    resource_cells: set[Position],
    enemy_positions: set[Position],
    reserved_positions: set[Position],
    friendly_occupancy: dict[Position, int],
) -> tuple[dict[str, Position], set[Position], int]:
    ordered_units = [
        unit
        for unit in sorted(units, key=lambda item: str(getattr(item, "id", "")))
        if _position(getattr(unit, "position", None)) is not None
    ]
    if core_position is None or not ordered_units:
        return {}, set(), 0
    candidates = _guard_candidates(
        core_position,
        radii,
        blocked,
        danger_cells,
        resource_cells,
        enemy_positions,
        reserved_positions,
    )
    costs: list[list[int]] = []
    route_costs: list[list[int]] = []
    for unit in ordered_units:
        origin = _position(getattr(unit, "position", None))
        assert origin is not None
        occupied_by_others = _unit_capacity_blocked(
            friendly_occupancy,
            actor_origin=origin,
        )
        row_blocked = blocked | danger_cells | occupied_by_others | reserved_positions
        row_routes = [
            PATH_COST_UNREACHABLE
            if candidate in occupied_by_others
            else _estimated_path_cost(origin, candidate, row_blocked)
            for candidate in candidates
        ]
        route_costs.append(row_routes)
        row = list(row_routes)
        row.extend([IDLE_ASSIGNMENT_COST] * len(ordered_units))
        costs.append(row)
    assignment = _minimum_cost_assignment(costs)
    result: dict[str, Position] = {}
    planned_occupancy: dict[Position, int] = {}
    idle_count = 0
    for row_index, column_index in enumerate(assignment):
        unit = ordered_units[row_index]
        unit_id = str(getattr(unit, "id", ""))
        origin = _position(getattr(unit, "position", None))
        if (
            0 <= column_index < len(candidates)
            and route_costs[row_index][column_index] < PATH_COST_UNREACHABLE
        ):
            destination = candidates[column_index]
            result[unit_id] = destination
            planned_occupancy[destination] = planned_occupancy.get(destination, 0) + 1
            continue

        idle_count += 1
        if origin is None:
            continue
        origin_is_legal = (
            origin != core_position
            and origin not in blocked
            and origin not in danger_cells
            and origin not in resource_cells
            and origin not in enemy_positions
            and origin not in reserved_positions
            and friendly_occupancy.get(origin, 0) <= 1
        )
        if origin_is_legal:
            result[unit_id] = origin
            planned_occupancy[origin] = planned_occupancy.get(origin, 0) + 1
            continue
        fallback = min(
            (
                position
                for position in candidates
                if friendly_occupancy.get(position, 0)
                + planned_occupancy.get(position, 0)
                < 1
            ),
            key=lambda position: (
                _manhattan(origin, position),
                position[0],
                position[1],
            ),
            default=None,
        )
        if fallback is not None:
            result[unit_id] = fallback
            planned_occupancy[fallback] = planned_occupancy.get(fallback, 0) + 1
    return result, set(planned_occupancy), idle_count


def _core_escape_direction(
    core_position: Position,
    enemies: list[Any],
    blocked: set[Position],
    reserved_destinations: set[Position] | None = None,
    previous_direction: Direction | None = None,
) -> Direction | None:
    enemy_positions = {
        position
        for position in (_position(getattr(enemy, "position", None)) for enemy in enemies)
        if position is not None
    }
    candidates: list[tuple[tuple[int, tuple[int, ...], int, int], Direction]] = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(core_position, direction)
        if (
            destination in blocked
            or destination in enemy_positions
            or (
                reserved_destinations is not None
                and destination in reserved_destinations
            )
        ):
            continue
        distances = tuple(
            sorted(
                _manhattan(destination, enemy_position)
                for enemy_position in enemy_positions
            )
        )
        projected_damage = _projected_incoming_damage(
            destination,
            enemies,
            blocked,
        )
        candidates.append(
            (
                (
                    -projected_damage,
                    distances,
                    int(direction == previous_direction),
                    -index,
                ),
                direction,
            )
        )
    if not candidates:
        return None
    best_score, best_direction = max(candidates, key=lambda candidate: candidate[0])
    current_damage = _projected_incoming_damage(
        core_position,
        enemies,
        blocked,
    )
    best_damage = -best_score[0]
    if best_damage > current_damage:
        return None
    if best_damage == current_damage:
        current_distances = tuple(
            sorted(
                _manhattan(core_position, enemy_position)
                for enemy_position in enemy_positions
            )
        )
        if best_score[1] < current_distances:
            return None
        if (
            best_score[1] == current_distances
            and best_direction != previous_direction
        ):
            return None
    return best_direction


def _worker_escape_direction(
    worker_position: Position,
    enemies: list[Any],
    obstacles: set[Position],
    hard_blocked: set[Position],
    core_position: Position | None,
    hp: int,
) -> Direction | None:
    """Choose a short escape path, allowing bounded fire-lane exposure."""

    enemy_positions = {
        position
        for position in (_position(getattr(enemy, "position", None)) for enemy in enemies)
        if position is not None
    }
    blocked = set(hard_blocked) | enemy_positions
    blocked.discard(worker_position)
    # Each frontier entry keeps one simple path; the search is intentionally
    # tiny because only its first move is submitted this Tick.
    frontier: list[tuple[Position, tuple[Direction, ...], frozenset[Position], int]] = [
        (worker_position, (), frozenset({worker_position}), 0)
    ]
    safe_paths: list[
        tuple[tuple[int, int, int, int, int, int, int, int], Direction]
    ] = []
    fallback_steps: list[tuple[tuple[int, int, int, int, int], Direction]] = []
    health = max(1, hp)

    for _ in range(WORKER_ESCAPE_SEARCH_DEPTH):
        next_frontier = []
        for position, path, visited, accumulated_damage in frontier:
            for direction_index, direction in enumerate(CARDINAL_DIRECTIONS):
                destination = _next_position(position, direction)
                if destination in blocked or destination in visited:
                    continue
                projected_damage = _projected_incoming_damage(
                    destination,
                    enemies,
                    obstacles,
                )
                new_path = path + (direction,)
                new_damage = accumulated_damage + projected_damage
                distances = [
                    _manhattan(destination, enemy_position)
                    for enemy_position in enemy_positions
                ]
                nearest_enemy = min(distances, default=PATH_COST_UNREACHABLE)
                core_distance = (
                    _manhattan(destination, core_position)
                    if core_position is not None
                    else 0
                )
                # Manhattan ties are common when both axes lead home. Reduce
                # the dominant Core separation before using direction order.
                core_axis_distance = (
                    _chebyshev(destination, core_position)
                    if core_position is not None
                    else 0
                )
                first_direction = new_path[0]
                first_index = CARDINAL_DIRECTIONS.index(first_direction)
                first_damage = _projected_incoming_damage(
                    _next_position(worker_position, first_direction),
                    enemies,
                    obstacles,
                )

                if len(new_path) == 1:
                    fallback_steps.append(
                        (
                            (
                                -projected_damage,
                                nearest_enemy,
                                -core_distance,
                                -core_axis_distance,
                                -direction_index,
                            ),
                            first_direction,
                        )
                    )
                if projected_damage == 0:
                    safe_paths.append(
                        (
                            (
                                int(new_damage < health),
                                -first_damage,
                                -new_damage,
                                -len(new_path),
                                -core_distance,
                                -core_axis_distance,
                                nearest_enemy,
                                -first_index,
                            ),
                            first_direction,
                        )
                    )
                    continue
                next_frontier.append(
                    (
                        destination,
                        new_path,
                        visited | {destination},
                        new_damage,
                    )
                )
        frontier = next_frontier
        if not frontier:
            break

    if safe_paths:
        return max(safe_paths, key=lambda candidate: candidate[0])[1]
    if not fallback_steps:
        return None
    best_score, best_direction = max(
        fallback_steps,
        key=lambda candidate: candidate[0],
    )
    current_damage = _projected_incoming_damage(
        worker_position,
        enemies,
        obstacles,
    )
    if -best_score[0] > current_damage:
        return None
    return best_direction


def _beacon_escape_direction(
    core_position: Position,
    beacon_position: Position,
    blocked: set[Position],
) -> Direction | None:
    """Choose a legal step that increases Core distance from a ground Beacon."""

    current_distance = _manhattan(core_position, beacon_position)
    if current_distance >= CORE_BEACON_RETREAT_DISTANCE:
        return None
    away_direction = _direction_for_delta(
        core_position[0] - beacon_position[0],
        core_position[1] - beacon_position[1],
    )
    candidates: list[tuple[tuple[int, int, int], Direction]] = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(core_position, direction)
        if destination in blocked:
            continue
        candidates.append(
            (
                (
                    _manhattan(destination, beacon_position),
                    int(direction == away_direction),
                    -index,
                ),
                direction,
            )
        )
    if not candidates:
        return None
    best_distance = max(score[0][0] for score in candidates)
    if best_distance <= current_distance:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _lifecycle_state(turn: Any, core: Any, memory: TacticMemory) -> str:
    """Resolve lifecycle independently from tactical threat pressure."""

    tick = int(getattr(turn, "tick", 0))
    raw_status = getattr(getattr(turn, "state", None), "status", "ACTIVE")
    status = str(getattr(raw_status, "name", raw_status)).upper().rsplit(".", 1)[-1]
    if status == "RESPAWNING" or core is None:
        return "RESPAWNING"
    if memory.recovery_until_tick >= tick:
        return "RECOVERY"
    return "ACTIVE"


def _mission_state(
    lifecycle: str,
    threat: ThreatAssessment,
    visible_resource_count: int,
    remembered_resource_count: int,
    cargo_worker_count: int = 0,
) -> str:
    """Summarize the current subordinate mission without replacing facts."""

    if lifecycle != "ACTIVE":
        return "RECOVERY"
    if threat.level in {"ALERT", "PRE_EVADE", "ENGAGED", "BREAKOUT"}:
        return "GUARD"
    if visible_resource_count or remembered_resource_count or cargo_worker_count:
        return "ECONOMY"
    return "SCOUT"


def _assess_threat(
    turn: Any,
    memory: TacticMemory,
    core: Any,
    enemies: list[Any],
) -> ThreatAssessment:
    """Classify current combat pressure without treating stale memory as truth."""

    tick = int(getattr(turn, "tick", 0))
    core_position = _position(getattr(core, "position", None))
    enemy_positions = [
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in enemies
        )
        if position is not None
    ]
    if core_position is None:
        return ThreatAssessment()

    nearest_distance = (
        min(_manhattan(core_position, position) for position in enemy_positions)
        if enemy_positions
        else None
    )
    recent_attack = memory.recent_attack_until_tick >= tick
    recent_attack_distances = [
        _manhattan(core_position, position)
        for position, observed_at in memory.recent_attack_positions.items()
        if 0 <= tick - observed_at <= RECENT_ATTACK_MEMORY_TICKS
    ]
    nearest_recent_attack = (
        min(recent_attack_distances) if recent_attack_distances else None
    )
    pressure_distances = [
        distance
        for distance in (nearest_distance, nearest_recent_attack)
        if distance is not None
    ]
    nearest_pressure_distance = min(pressure_distances) if pressure_distances else None
    if recent_attack and (
        nearest_recent_attack is None
        or nearest_recent_attack <= CORE_EVADE_DISTANCE
    ):
        return ThreatAssessment(
            level="ENGAGED",
            reason="RECENT_ATTACK",
            nearest_distance=nearest_pressure_distance,
            recent_attack=True,
        )
    if (
        recent_attack
        and nearest_recent_attack is not None
        and nearest_recent_attack <= CORE_ALERT_DISTANCE
    ):
        return ThreatAssessment(
            level="ALERT",
            reason="RECENT_ATTACK",
            nearest_distance=nearest_pressure_distance,
            recent_attack=True,
        )

    active_enemy_near_core = any(
        enemy_id in memory.enemy_motion_memory
        and _manhattan(
            core_position,
            memory.enemy_motion_memory[enemy_id].position,
        )
        <= CORE_ALERT_DISTANCE
        for enemy_id in memory.active_enemy_ids
    )

    if not enemy_positions:
        if memory.pursuing_enemy_ids:
            return ThreatAssessment(level="PRE_EVADE", reason="CONFIRMED_PURSUIT")
        if memory.preemptive_enemy_ids:
            return ThreatAssessment(level="PRE_EVADE", reason="TIME_TO_RANGE")
        if active_enemy_near_core:
            return ThreatAssessment(level="ALERT", reason="ENEMY_ACTIVITY")
        return ThreatAssessment()

    assert nearest_distance is not None
    if _projected_incoming_damage(
        core_position,
        enemies,
        memory.obstacle_memory,
    ):
        return ThreatAssessment(
            level="ENGAGED",
            reason="CURRENT_CORE_ATTACK",
            nearest_distance=nearest_distance,
        )
    axes = {
        direction
        for direction in (
            _direction_for_delta(
                position[0] - core_position[0],
                position[1] - core_position[1],
            )
            for position in enemy_positions
        )
    }
    if len(axes) >= 2 and nearest_distance <= CORE_ALERT_DISTANCE:
        return ThreatAssessment(
            level="BREAKOUT",
            reason="MULTI_AXIS_PRESSURE",
            nearest_distance=nearest_distance,
        )
    if memory.pursuing_enemy_ids:
        return ThreatAssessment(
            level="PRE_EVADE",
            reason="CONFIRMED_PURSUIT",
            nearest_distance=nearest_distance,
        )
    if memory.preemptive_enemy_ids:
        return ThreatAssessment(
            level="PRE_EVADE",
            reason="TIME_TO_RANGE",
            nearest_distance=nearest_distance,
        )
    if nearest_distance <= CORE_EVADE_DISTANCE:
        return ThreatAssessment(
            level="PRE_EVADE",
            reason="ENEMY_CLOSE",
            nearest_distance=nearest_distance,
        )
    if nearest_distance <= CORE_ALERT_DISTANCE:
        return ThreatAssessment(
            level="ALERT",
            reason=("ENEMY_ACTIVITY" if active_enemy_near_core else "ENEMY_NEAR"),
            nearest_distance=nearest_distance,
        )
    return ThreatAssessment(nearest_distance=nearest_distance)


def _threat_level(core: Any, enemies: list[Any]) -> str:
    core_position = _position(getattr(core, "position", None))
    if core_position is None or not enemies:
        return "NORMAL"
    nearest_distance = min(
        _manhattan(core_position, enemy_position)
        for enemy_position in (
            _position(getattr(enemy, "position", None)) for enemy in enemies
        )
        if enemy_position is not None
    )
    if nearest_distance <= CORE_EVADE_DISTANCE:
        return "PRE_EVADE"
    if nearest_distance <= CORE_ALERT_DISTANCE:
        return "ALERT"
    return "NORMAL"


def _population_expansion_threshold(turn: Any, config: AgentConfig) -> int:
    casualty_buffer = max(0, config.expansion_casualty_buffer_units) * 5
    return max(
        0,
        config.expansion_resource_threshold,
        _resource_capacity(turn) - casualty_buffer,
    )


def _proportional_expansion_unit(counts: dict[UnitType, int]) -> UnitType:
    weights = {
        UnitType.WORKER: 8,
        UnitType.VANGUARD: 3,
        UnitType.RANGER: 4,
    }
    population = sum(counts.values())
    weight_total = sum(weights.values())
    return min(
        (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER),
        key=lambda unit_type: (
            counts[unit_type] * weight_total - population * weights[unit_type],
            (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER).index(unit_type),
        ),
    )


def _choose_spawn_unit(
    turn: Any,
    config: AgentConfig,
    pending_delivery: int = 0,
) -> UnitType | None:
    """Restore configured targets, then optionally grow at a resource floor."""

    if config.spawn_unit_type is None:
        return None

    counts = {
        UnitType.WORKER: len(tuple(getattr(turn, "workers", ()))),
        UnitType.VANGUARD: len(tuple(getattr(turn, "vanguards", ()))),
        UnitType.RANGER: len(tuple(getattr(turn, "rangers", ()))),
    }
    population = _population(turn)
    if config.max_population > 0 and population >= config.max_population:
        return None
    if config.auto_production:
        final_targets = {
            unit_type: max(0, config.target_for(unit_type))
            for unit_type in (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)
        }
        early_worker_goal = min(
            final_targets[UnitType.WORKER],
            EARLY_DEFENSE_WORKER_GOAL,
        )
        if counts[UnitType.WORKER] < early_worker_goal:
            return UnitType.WORKER
        if counts[UnitType.VANGUARD] < min(
            final_targets[UnitType.VANGUARD],
            EARLY_DEFENSE_VANGUARD_TARGET,
        ):
            return UnitType.VANGUARD
        if counts[UnitType.RANGER] < min(
            final_targets[UnitType.RANGER],
            EARLY_DEFENSE_RANGER_TARGET,
        ):
            return UnitType.RANGER

        baseline_targets = {
            UnitType.WORKER: min(
                final_targets[UnitType.WORKER], BASELINE_WORKER_TARGET
            ),
            UnitType.VANGUARD: min(
                final_targets[UnitType.VANGUARD], BASELINE_VANGUARD_TARGET
            ),
            UnitType.RANGER: min(
                final_targets[UnitType.RANGER], BASELINE_RANGER_TARGET
            ),
        }
        for unit_type in (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER):
            if counts[unit_type] < baseline_targets[unit_type]:
                return unit_type
        for unit_type in (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER):
            if counts[unit_type] < final_targets[unit_type]:
                return unit_type
        if (
            config.population_expansion_enabled
            and _available_resources(turn, pending_delivery)
            >= _population_expansion_threshold(turn, config)
        ):
            return _proportional_expansion_unit(counts)
        return None

    if counts[config.spawn_unit_type] < max(0, config.target_for(config.spawn_unit_type)):
        return config.spawn_unit_type
    return None


def _production_reserve(turn: Any, config: AgentConfig) -> int:
    """Keep larger reserves only after the configured baseline is intact."""

    counts = {
        UnitType.WORKER: len(tuple(getattr(turn, "workers", ()))),
        UnitType.VANGUARD: len(tuple(getattr(turn, "vanguards", ()))),
        UnitType.RANGER: len(tuple(getattr(turn, "rangers", ()))),
    }
    final_targets = {
        unit_type: max(0, config.target_for(unit_type))
        for unit_type in (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)
    }
    early_targets = {
        UnitType.WORKER: min(
            final_targets[UnitType.WORKER], EARLY_DEFENSE_WORKER_GOAL
        ),
        UnitType.VANGUARD: min(
            final_targets[UnitType.VANGUARD], EARLY_DEFENSE_VANGUARD_TARGET
        ),
        UnitType.RANGER: min(
            final_targets[UnitType.RANGER], EARLY_DEFENSE_RANGER_TARGET
        ),
    }
    if any(counts[unit_type] < early_targets[unit_type] for unit_type in counts):
        return 0

    baseline_targets = {
        UnitType.WORKER: min(
            final_targets[UnitType.WORKER], BASELINE_WORKER_TARGET
        ),
        UnitType.VANGUARD: min(
            final_targets[UnitType.VANGUARD], BASELINE_VANGUARD_TARGET
        ),
        UnitType.RANGER: min(
            final_targets[UnitType.RANGER], BASELINE_RANGER_TARGET
        ),
    }
    if any(counts[unit_type] < baseline_targets[unit_type] for unit_type in counts):
        return UNIT_HEAL_RESOURCE_RESERVE
    return EXPANSION_PRODUCTION_RESERVE


def choose_step_direction(
    origin: Position,
    target: Position,
    blocked: set[Position],
    *,
    discouraged: set[Position] | None = None,
) -> Direction | None:
    """Choose a cardinal step using route cost before falling back to a detour."""

    if target in blocked:
        return None

    candidates = [
        (direction, next_cell)
        for direction, next_cell in _candidate_steps(origin, target)
        if next_cell not in blocked
    ]
    if discouraged is not None:
        candidate_directions = {direction for direction, _ in candidates}
        for direction in CARDINAL_DIRECTIONS:
            if direction in candidate_directions:
                continue
            next_cell = _next_position(origin, direction)
            if next_cell not in blocked:
                candidates.append((direction, next_cell))
    if candidates:
        # A free greedy step can lead into a cul-de-sac. Compare the remaining
        # route costs first so cargo returns do not oscillate at obstacle edges.
        best_direction: Direction | None = None
        best_route_cost = PATH_COST_UNREACHABLE
        for direction, next_cell in candidates:
            route_cost = _estimated_path_cost(
                next_cell,
                target,
                blocked,
                discouraged=discouraged,
            )
            if discouraged and next_cell in discouraged:
                route_cost += SCOUT_RECENT_POSITION_PENALTY
            if route_cost < best_route_cost:
                best_direction = direction
                best_route_cost = route_cost
        if best_direction is not None:
            return best_direction
        # Preserve forward progress when no bounded route estimate is available.
        return candidates[0][0]

    # Direct candidates are not enough when a remembered obstacle forms a wall.
    # Search only the rectangle around this route so exploration cannot wander
    # across the unbounded world while looking for a path.
    margin = 12
    min_x = min(origin[0], target[0]) - margin
    max_x = max(origin[0], target[0]) + margin
    min_y = min(origin[1], target[1]) - margin
    max_y = max(origin[1], target[1]) + margin
    directions = (
        (Direction.UP, (0, -1)),
        (Direction.DOWN, (0, 1)),
        (Direction.LEFT, (-1, 0)),
        (Direction.RIGHT, (1, 0)),
    )
    queue: list[Position] = [origin]
    first_direction: dict[Position, Direction] = {}
    visited = {origin}
    for current in queue:
        if current == target:
            break
        for direction, (dx, dy) in directions:
            next_cell = current[0] + dx, current[1] + dy
            if next_cell in visited or next_cell in blocked:
                continue
            if not (min_x <= next_cell[0] <= max_x and min_y <= next_cell[1] <= max_y):
                continue
            visited.add(next_cell)
            first_direction[next_cell] = first_direction.get(
                current, direction
            )
            queue.append(next_cell)
            if next_cell == target:
                return first_direction[next_cell]
    return None


def _queue_move(
    actor: Any,
    target: Position | None,
    blocked: set[Position],
    reserved_destinations: Any = None,
    *,
    discouraged: set[Position] | None = None,
    friendly_occupancy: dict[Position, int] | None = None,
    memory: TacticMemory | None = None,
    tick: int = 0,
    stationary_target: Position | None = None,
) -> bool:
    origin = _position(getattr(actor, "position", None))
    if origin is None or target is None or origin == target:
        return False
    hard_blocked = set(blocked)
    movement_blocked = set(hard_blocked)
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy or {},
        reserved_destinations,
        actor_origin=origin,
    )
    movement_blocked.update(capacity_blocked)
    contested_blocked: set[Position] = set()
    if memory is not None:
        contested_blocked = memory.contested_unit_positions(
            getattr(actor, "id", ""),
            tick,
        )
        movement_blocked.update(contested_blocked)
    # A friendly Unit can temporarily occupy an approach target, but obstacles,
    # danger cells, enemy occupants, and recently rejected destinations remain
    # hard blockers.
    if target not in hard_blocked and target not in contested_blocked:
        movement_blocked.discard(target)
    soft_discouraged = set(discouraged or ())
    direction = choose_step_direction(
        origin,
        target,
        movement_blocked,
        discouraged=soft_discouraged,
    )
    if direction is None:
        return False
    destination = _next_position(origin, direction)
    if destination in capacity_blocked:
        return False
    if reserved_destinations is not None:
        _reserve_destination(reserved_destinations, destination)
    if memory is not None:
        memory.note_unit_move(
            getattr(actor, "id", ""),
            destination,
            target,
            stationary_target,
        )
    actor.move(direction)
    return True


def _reservation_cells(reservations: Any) -> set[Position]:
    if not reservations:
        return set()
    return set(reservations)


def _reservation_count(reservations: Any, position: Position) -> int:
    if not reservations:
        return 0
    if isinstance(reservations, dict):
        return int(reservations.get(position, 0))
    return int(position in reservations)


def _reserve_destination(reservations: Any, position: Position) -> None:
    if isinstance(reservations, dict):
        reservations[position] = reservations.get(position, 0) + 1
    else:
        reservations.add(position)


def _friendly_cell_occupancy(turn: Any) -> dict[Position, int]:
    occupancy: dict[Position, int] = {}
    for unit in getattr(turn, "units", ()):
        position = _position(getattr(unit, "position", None))
        if position is not None:
            occupancy[position] = occupancy.get(position, 0) + 1
    return occupancy


def _unit_capacity_blocked(
    friendly_occupancy: dict[Position, int],
    reservations: Any = None,
    *,
    actor_origin: Position | None = None,
) -> set[Position]:
    """Return cells that cannot accept another Unit this Tick."""

    positions = set(friendly_occupancy) | _reservation_cells(reservations)
    return {
        position
        for position in positions
        if friendly_occupancy.get(position, 0)
        - int(position == actor_origin)
        + _reservation_count(reservations, position)
        >= 1
    }


def _bounded_reachable_component(
    start: Position,
    blocked: set[Position],
    max_cells: int = CORE_POCKET_MAX_CELLS,
) -> tuple[frozenset[Position], bool]:
    """Return a bounded four-neighbor component and whether it was exhausted."""

    if max_cells <= 0:
        return frozenset(), False
    blocked = set(blocked)
    blocked.discard(start)
    visited = {start}
    frontier = deque((start,))
    while frontier and len(visited) < max_cells:
        current = frontier.popleft()
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if destination in blocked or destination in visited:
                continue
            visited.add(destination)
            frontier.append(destination)
            if len(visited) >= max_cells:
                break
    return frozenset(visited), not frontier


def _cargo_touches_component(
    cargo_position: Position,
    core_position: Position,
    component: frozenset[Position],
) -> bool:
    if cargo_position == core_position:
        return True
    return any(
        _next_position(cargo_position, direction) in component
        for direction in CARDINAL_DIRECTIONS
    )


def _update_core_pocket(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    *,
    core_accepts_delivery: bool,
) -> CorePocketObservation:
    """Classify a persistent closed Core pocket without disrupting normal deposits."""

    tick = int(getattr(turn, "tick", 0))
    cargo_workers = {
        str(getattr(worker, "id", "")): worker
        for worker in getattr(turn, "workers", ())
        if str(getattr(worker, "id", ""))
        and int(getattr(worker, "cargo", 0)) > 0
        and _position(getattr(worker, "position", None)) is not None
    }
    if core_position is None:
        memory.admitted_cargo_progress.clear()
        memory.core_pocket_candidate_streak = 0
        memory.core_pocket_candidate_cargo_ids.clear()
        memory.core_pocket = CorePocketObservation()
        return memory.core_pocket

    dynamic_blocked = set(blocked) | set(friendly_occupancy)
    dynamic_blocked.discard(core_position)
    component, closed = _bounded_reachable_component(
        core_position,
        dynamic_blocked,
    )
    raw_admitted_ids = {
        worker_id
        for worker_id, worker in cargo_workers.items()
        if _cargo_touches_component(
            _position(getattr(worker, "position", None)),
            core_position,
            component,
        )
    }

    deposit_succeeded = any(
        _event_label(event) == "DEPOSIT_SUCCEEDED"
        for event in getattr(turn, "events", ())
    )
    if deposit_succeeded:
        memory.admitted_cargo_progress.clear()

    stalled_admitted_ids: set[str] = set()
    for worker_id in raw_admitted_ids:
        worker = cargo_workers[worker_id]
        position = _position(getattr(worker, "position", None))
        assert position is not None
        if position == core_position:
            memory.admitted_cargo_progress.pop(worker_id, None)
            continue
        previous = memory.admitted_cargo_progress.get(worker_id)
        stationary_ticks = (
            previous.stationary_ticks + 1
            if previous is not None
            and previous.last_tick == tick - 1
            and previous.position == position
            else 1
        )
        memory.admitted_cargo_progress[worker_id] = AdmittedCargoProgress(
            position=position,
            stationary_ticks=stationary_ticks,
            last_tick=tick,
        )
        if stationary_ticks >= ADMITTED_CARGO_STALL_TICKS:
            stalled_admitted_ids.add(worker_id)
    for worker_id in set(memory.admitted_cargo_progress) - raw_admitted_ids:
        memory.admitted_cargo_progress.pop(worker_id, None)

    admitted_ids = raw_admitted_ids - stalled_admitted_ids
    outside_ids = set(cargo_workers) - admitted_ids
    static_reachable_ids: set[str] = set()
    if closed and not admitted_ids and core_accepts_delivery:
        for worker_id in sorted(outside_ids):
            worker_position = _position(
                getattr(cargo_workers[worker_id], "position", None)
            )
            if worker_position is None:
                continue
            static_route = _complete_route(worker_position, core_position, blocked)
            if static_route.status == "SUCCESS":
                static_reachable_ids.add(worker_id)

    candidate = bool(
        closed
        and not admitted_ids
        and static_reachable_ids
        and core_accepts_delivery
        and not deposit_succeeded
        and not memory.cargo_lane.active
    )
    same_sequence = bool(
        candidate
        and memory.core_pocket_candidate_tick == tick - 1
        and memory.core_pocket_candidate_core == core_position
        and memory.core_pocket_candidate_cargo_ids & static_reachable_ids
    )
    if candidate:
        memory.core_pocket_candidate_streak = (
            memory.core_pocket_candidate_streak + 1 if same_sequence else 1
        )
        memory.core_pocket_candidate_tick = tick
        memory.core_pocket_candidate_core = core_position
        memory.core_pocket_candidate_cargo_ids = set(static_reachable_ids)
    else:
        memory.core_pocket_candidate_streak = 0
        memory.core_pocket_candidate_tick = -1
        memory.core_pocket_candidate_core = None
        memory.core_pocket_candidate_cargo_ids.clear()

    observation = CorePocketObservation(
        component=component,
        closed=closed,
        raw_admitted_cargo_ids=frozenset(raw_admitted_ids),
        admitted_cargo_ids=frozenset(admitted_ids),
        stalled_admitted_cargo_ids=frozenset(stalled_admitted_ids),
        outside_cargo_ids=frozenset(outside_ids),
        static_reachable_cargo_ids=frozenset(static_reachable_ids),
        consecutive_ticks=memory.core_pocket_candidate_streak,
        blocked=memory.core_pocket_candidate_streak >= CORE_POCKET_DEBOUNCE_TICKS,
    )
    memory.core_pocket = observation
    return observation


def _clear_cargo_lane(memory: TacticMemory) -> None:
    memory.cargo_lane = CargoLanePlan()


def _cargo_lane_gateway(
    core_position: Position,
    owner_position: Position,
    static_path: tuple[Position, ...],
) -> Position | None:
    if not static_path:
        return None
    core_to_owner = tuple(reversed(static_path))
    gateway = core_position
    for position in core_to_owner[1:]:
        if max(
            abs(position[0] - core_position[0]),
            abs(position[1] - core_position[1]),
        ) > CARGO_LANE_RADIUS:
            break
        gateway = position
        if position == owner_position:
            break
    return gateway


def _local_cargo_lane_bridge(
    core_position: Position,
    owner: Any,
    static_path: tuple[Position, ...],
    blocked: set[Position],
    danger_cells: set[Position],
    units: Iterable[Any],
) -> CargoLaneBridge | None:
    """Find a low-disruption path through the bounded Core logistics window."""

    owner_id = str(getattr(owner, "id", ""))
    owner_position = _position(getattr(owner, "position", None))
    if not owner_id or owner_position is None:
        return None
    gateway = _cargo_lane_gateway(core_position, owner_position, static_path)
    if gateway is None:
        return None
    core_to_owner = tuple(reversed(static_path))
    try:
        static_gateway_steps = core_to_owner.index(gateway)
    except ValueError:
        return None
    static_total_steps = max(0, len(static_path) - 1)
    max_total_steps = static_total_steps + 2

    min_x = core_position[0] - CARGO_LANE_RADIUS
    max_x = core_position[0] + CARGO_LANE_RADIUS
    min_y = core_position[1] - CARGO_LANE_RADIUS
    max_y = core_position[1] + CARGO_LANE_RADIUS
    hard_blocked = set(blocked) | set(danger_cells)
    hard_blocked.discard(core_position)
    units_by_position: dict[Position, list[Any]] = {}
    worker_ids: set[str] = set()
    cargo_ids: set[str] = set()
    for unit in units:
        unit_id = str(getattr(unit, "id", ""))
        position = _position(getattr(unit, "position", None))
        if not unit_id or position is None:
            continue
        units_by_position.setdefault(position, []).append(unit)
        if _enum_label(getattr(unit, "unit_type", "")) == "WORKER":
            worker_ids.add(unit_id)
            if int(getattr(unit, "cargo", 0)) > 0:
                cargo_ids.add(unit_id)

    owner_side_start = (
        owner_position
        if min_x <= owner_position[0] <= max_x
        and min_y <= owner_position[1] <= max_y
        else gateway
    )
    owner_blocked = hard_blocked | set(units_by_position)
    owner_blocked.discard(owner_side_start)
    owner_distance_offset = (
        0
        if owner_side_start == owner_position
        else static_path.index(owner_side_start)
    )
    owner_distances: dict[Position, int] = {
        owner_side_start: owner_distance_offset
    }
    owner_frontier = deque((owner_side_start,))
    while owner_frontier:
        current = owner_frontier.popleft()
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if not (
                min_x <= destination[0] <= max_x
                and min_y <= destination[1] <= max_y
            ):
                continue
            if destination in owner_blocked or destination in owner_distances:
                continue
            owner_distances[destination] = owner_distances[current] + 1
            owner_frontier.append(destination)

    def occupancy_cost(position: Position) -> tuple[int, int, int, int]:
        occupants = [
            unit
            for unit in units_by_position.get(position, ())
            if str(getattr(unit, "id", "")) != owner_id
        ]
        occupant_ids = {str(getattr(unit, "id", "")) for unit in occupants}
        return (
            len(occupant_ids),
            len(occupant_ids & cargo_ids),
            1,
            len(occupant_ids & (worker_ids - cargo_ids)),
        )

    start_cost = (0, 0, 0, 0)
    costs: dict[Position, tuple[int, int, int, int]] = {core_position: start_cost}
    parents: dict[Position, Position] = {}
    sequence = count()
    frontier: list[tuple[tuple[int, int, int, int], int, Position]] = [
        (start_cost, next(sequence), core_position)
    ]
    best_target: Position | None = None
    best_target_score: tuple[int, int, int, int] | None = None
    while frontier:
        current_cost, _, current = heapq.heappop(frontier)
        if current_cost != costs.get(current):
            continue
        owner_distance = owner_distances.get(current)
        if owner_distance is not None:
            total_steps = current_cost[2] + owner_distance
            target_score = (
                current_cost[0],
                current_cost[1],
                total_steps,
                current_cost[3],
            )
            if total_steps <= max_total_steps and (
                best_target_score is None or target_score < best_target_score
            ):
                best_target = current
                best_target_score = target_score
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if not (
                min_x <= destination[0] <= max_x
                and min_y <= destination[1] <= max_y
            ):
                continue
            if destination in hard_blocked:
                continue
            step_cost = occupancy_cost(destination)
            new_cost = tuple(
                current_cost[index] + step_cost[index] for index in range(4)
            )
            if new_cost[2] > max_total_steps:
                continue
            if new_cost >= costs.get(
                destination,
                (sys.maxsize, sys.maxsize, sys.maxsize, sys.maxsize),
            ):
                continue
            costs[destination] = new_cost
            parents[destination] = current
            heapq.heappush(
                frontier,
                (new_cost, next(sequence), destination),
            )
    if best_target is None:
        return None
    path = [best_target]
    while path[-1] != core_position:
        path.append(parents[path[-1]])
    path.reverse()
    blocker_ids = {
        str(getattr(unit, "id", ""))
        for position in path
        for unit in units_by_position.get(position, ())
        if str(getattr(unit, "id", "")) != owner_id
    }
    return CargoLaneBridge(
        path=tuple(path),
        gateway=best_target,
        blocker_ids=frozenset(blocker_ids),
    )


def _cargo_stage_targets(
    cargo_workers: Iterable[Any],
    core_position: Position,
    gateway: Position,
    lane_path: set[Position],
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    *,
    queued_owner_id: str | None = None,
    admit_queued_to_gateway: bool = False,
) -> dict[str, Position]:
    cargo_workers = tuple(cargo_workers)
    candidates = [
        position
        for radius in CARGO_LANE_STAGE_RADII
        for position in _chebyshev_ring_positions(core_position, radius)
        if position not in lane_path
        and position not in blocked
        and position not in danger_cells
    ]
    available = set(candidates)
    assignments: dict[str, Position] = {}
    if admit_queued_to_gateway and any(
        str(getattr(worker, "id", "")) == queued_owner_id
        for worker in cargo_workers
    ):
        assignments[str(queued_owner_id)] = gateway
    for worker in sorted(
        cargo_workers,
        key=lambda item: str(getattr(item, "id", "")),
    ):
        worker_id = str(getattr(worker, "id", ""))
        origin = _position(getattr(worker, "position", None))
        if not worker_id or origin is None:
            continue
        if worker_id in assignments:
            continue
        target = min(
            (
                position
                for position in available
                if friendly_occupancy.get(position, 0) == 0
                or position == origin
            ),
            key=lambda position: (
                _estimated_path_cost(origin, position, blocked | danger_cells)
                + _manhattan(position, gateway),
                _manhattan(position, gateway),
                position[0],
                position[1],
            ),
            default=None,
        )
        if target is None:
            continue
        assignments[worker_id] = target
        available.remove(target)
    return assignments


def _select_cargo_lane_owner(
    cargo_workers: Iterable[Any],
    target_position: Position,
    blocked: set[Position],
    memory: TacticMemory,
    *,
    onward_steps: int = 0,
) -> tuple[Any | None, RouteSearchResult | None]:
    candidates: list[tuple[int, int, int, str, Any, RouteSearchResult]] = []
    for worker in cargo_workers:
        worker_id = str(getattr(worker, "id", ""))
        position = _position(getattr(worker, "position", None))
        if not worker_id or position is None:
            continue
        route = _complete_route(position, target_position, blocked)
        if route.status != "SUCCESS":
            continue
        approach_length = max(0, len(route.path) - 1)
        route_length = approach_length + max(0, onward_steps)
        wait_ticks = memory.cargo_lane_wait_ticks.get(worker_id, 0)
        candidates.append(
            (
                approach_length,
                route_length,
                wait_ticks,
                worker_id,
                worker,
                route,
            )
        )
    if not candidates:
        return None, None
    tick = int(memory.last_observed_tick or 0)
    unpenalized = [
        candidate
        for candidate in candidates
        if not (
            (penalty := memory.cargo_lane_stalled_owners.get(candidate[3]))
            and penalty[1] > tick
            and _position(getattr(candidate[4], "position", None)) == penalty[0]
        )
    ]
    if unpenalized:
        candidates = unpenalized
    admitted = [
        candidate
        for candidate in candidates
        if candidate[0] <= CARGO_LANE_OWNER_ADMISSION_STEPS
    ]
    if admitted:
        _, _, _, _, owner, route = min(
            admitted,
            key=lambda candidate: (
                candidate[1]
                - min(candidate[2], CARGO_LANE_WAIT_CREDIT_TICKS),
                candidate[1],
                -candidate[2],
                candidate[3],
            ),
        )
    else:
        _, _, _, _, owner, route = min(
            candidates,
            key=lambda candidate: (candidate[1], -candidate[2], candidate[3]),
        )
    return owner, route


def _cargo_lane_open_route(
    start: Position,
    core_position: Position,
    lane_path: set[Position],
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
    *,
    block_core: bool = False,
) -> tuple[Position, ...] | None:
    """Find a complete route from a local Core pocket into open space."""

    dynamic_blocked = set(blocked) | set(friendly_occupancy)
    dynamic_blocked.discard(start)
    if block_core:
        dynamic_blocked.add(core_position)
    visited = {start}
    parents: dict[Position, Position] = {}
    frontier = deque((start,))
    exit_candidates: list[Position] = []
    while frontier and len(visited) < CORE_POCKET_MAX_CELLS:
        current = frontier.popleft()
        if (
            current not in lane_path
            and max(
                abs(current[0] - core_position[0]),
                abs(current[1] - core_position[1]),
            ) > CARGO_LANE_STARTUP_RADIUS
        ):
            exit_candidates.append(current)
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if destination in dynamic_blocked or destination in visited:
                continue
            parents[destination] = current
            visited.add(destination)
            frontier.append(destination)
            if len(visited) >= CORE_POCKET_MAX_CELLS:
                break
    if not frontier or not exit_candidates:
        return None
    target = min(
        exit_candidates,
        key=lambda position: (
            _manhattan(start, position),
            position[0],
            position[1],
        ),
    )
    path = [target]
    while path[-1] != start:
        path.append(parents[path[-1]])
    path.reverse()
    return tuple(path)


def _single_open_cargo_lane_path(
    core_position: Position,
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
) -> tuple[Position, ...] | None:
    """Return the sole outward Core gateway, if local geometry has one."""

    paths: list[tuple[Position, ...]] = []
    for direction in CARDINAL_DIRECTIONS:
        neighbor = _next_position(core_position, direction)
        if neighbor in blocked:
            continue
        route = _cargo_lane_open_route(
            neighbor,
            core_position,
            {core_position},
            blocked,
            friendly_occupancy,
            block_core=True,
        )
        if route is not None:
            paths.append((core_position,) + route)
    if len(paths) != 1:
        return None
    path = paths[0]
    if len(path) <= CARGO_LANE_GATEWAY_STEPS:
        return None
    return path[: CARGO_LANE_GATEWAY_STEPS + 1]


def _cargo_lane_egress_complete(
    position: Position,
    lane: CargoLanePlan,
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
) -> bool:
    if lane.core_position is None or position in set(lane.path):
        return False
    if max(
        abs(position[0] - lane.core_position[0]),
        abs(position[1] - lane.core_position[1]),
    ) <= CARGO_LANE_STARTUP_RADIUS:
        return False
    dynamic_blocked = set(blocked) | set(friendly_occupancy)
    dynamic_blocked.discard(position)
    _, closed = _bounded_reachable_component(position, dynamic_blocked)
    return not closed


def _cargo_lane_egress_route(
    start: Position,
    lane: CargoLanePlan,
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
) -> tuple[Position, ...] | None:
    if lane.core_position is None or lane.gateway is None:
        return None
    if start in lane.path:
        prefix = lane.path[lane.path.index(start) :]
        search_start = lane.gateway
    else:
        prefix = (start,)
        search_start = start

    # Search through temporary friendly occupancy so those Units can be told
    # to yield, but only finish at a currently open, unoccupied component.
    route_blocked = set(blocked) | set(lane.path)
    route_blocked.discard(search_start)
    visited = {search_start}
    parents: dict[Position, Position] = {}
    frontier = deque((search_start,))
    target = None
    while frontier and len(visited) < CORE_POCKET_MAX_CELLS:
        current = frontier.popleft()
        current_occupancy = friendly_occupancy.get(current, 0) - int(
            current == start
        )
        if (
            current_occupancy <= 0
            and _cargo_lane_egress_complete(
                current,
                lane,
                blocked,
                friendly_occupancy,
            )
        ):
            target = current
            break
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if destination in route_blocked or destination in visited:
                continue
            parents[destination] = current
            visited.add(destination)
            frontier.append(destination)
            if len(visited) >= CORE_POCKET_MAX_CELLS:
                break
    if target is None:
        return None
    outward = [target]
    while outward[-1] != search_start:
        outward.append(parents[outward[-1]])
    outward.reverse()
    return prefix + tuple(outward[1:])


def _cargo_lane_startup_workers(
    workers: Iterable[Any],
    core_position: Position,
) -> set[str]:
    return {
        str(getattr(worker, "id", ""))
        for worker in workers
        if str(getattr(worker, "id", ""))
        and int(getattr(worker, "cargo", 0)) == 0
        and (
            (position := _position(getattr(worker, "position", None))) is not None
        )
        and max(
            abs(position[0] - core_position[0]),
            abs(position[1] - core_position[1]),
        ) <= CARGO_LANE_STARTUP_RADIUS
    }


def _select_cargo_lane_departing_worker(
    lane: CargoLanePlan,
    workers_by_id: dict[str, Any],
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
    tick: int,
) -> None:
    if lane.core_position is None:
        return
    candidates: list[tuple[int, str, tuple[Position, ...]]] = []
    for worker_id in sorted(lane.startup_pending_ids):
        worker = workers_by_id.get(worker_id)
        position = _position(getattr(worker, "position", None))
        if worker is None or position is None or int(getattr(worker, "cargo", 0)) > 0:
            continue
        route = _cargo_lane_egress_route(
            position,
            lane,
            blocked,
            friendly_occupancy,
        )
        if route is not None:
            candidates.append((len(route), worker_id, route))
    if not candidates:
        lane.departing_worker_id = None
        lane.egress_path = ()
        lane.egress_target = None
        return
    _, worker_id, route = min(candidates)
    lane.departing_worker_id = worker_id
    lane.egress_path = route
    lane.egress_target = route[-1]
    _set_cargo_lane_phase(lane, "STARTUP_EVACUATION", tick)


def _set_cargo_lane_phase(lane: CargoLanePlan, phase: str, tick: int) -> None:
    if lane.phase != phase:
        lane.phase = phase
        lane.phase_started_tick = tick


def _reset_cargo_lane_owner_progress(
    lane: CargoLanePlan,
    tick: int = -1,
) -> None:
    lane.owner_progress_id = lane.owner_id
    lane.owner_best_remaining_steps = None
    lane.owner_last_progress_tick = tick


def _cargo_lane_owner_stalled(
    lane: CargoLanePlan,
    position: Position | None,
    blocked: set[Position],
    tick: int,
) -> bool:
    owner_id = lane.owner_id
    if (
        lane.phase != "INBOUND"
        or owner_id is None
        or position is None
        or lane.gateway is None
    ):
        _reset_cargo_lane_owner_progress(lane)
        return False
    if lane.owner_progress_id != owner_id:
        _reset_cargo_lane_owner_progress(lane, tick)
    if position in lane.path:
        remaining_steps = lane.path.index(position)
    else:
        route = _complete_route(position, lane.gateway, blocked)
        remaining_steps = (
            len(route.path) - 1 + max(0, len(lane.path) - 1)
            if route.status == "SUCCESS"
            else _manhattan(position, lane.gateway) + max(0, len(lane.path) - 1)
        )
    if (
        lane.owner_best_remaining_steps is None
        or remaining_steps < lane.owner_best_remaining_steps
    ):
        lane.owner_best_remaining_steps = remaining_steps
        lane.owner_last_progress_tick = tick
        return False
    return (
        lane.owner_last_progress_tick >= 0
        and tick - lane.owner_last_progress_tick
        >= CARGO_LANE_INBOUND_WATCHDOG_TICKS
    )


def _reset_cargo_lane_departing_progress(
    lane: CargoLanePlan,
    tick: int = -1,
) -> None:
    lane.departing_progress_id = lane.departing_worker_id
    lane.departing_best_remaining_steps = None
    lane.departing_last_progress_tick = tick


def _cargo_lane_departing_stalled(
    lane: CargoLanePlan,
    position: Position | None,
    tick: int,
) -> bool:
    departing_id = lane.departing_worker_id
    if departing_id is None or position is None or not lane.egress_path:
        _reset_cargo_lane_departing_progress(lane)
        return False
    if lane.departing_progress_id != departing_id:
        _reset_cargo_lane_departing_progress(lane, tick)
    try:
        path_index = lane.egress_path.index(position)
    except ValueError:
        remaining_steps = (
            _manhattan(position, lane.egress_target)
            if lane.egress_target is not None
            else None
        )
    else:
        remaining_steps = len(lane.egress_path) - path_index - 1
    if remaining_steps is None:
        return False
    if (
        lane.departing_best_remaining_steps is None
        or remaining_steps < lane.departing_best_remaining_steps
    ):
        lane.departing_best_remaining_steps = remaining_steps
        lane.departing_last_progress_tick = tick
        return False
    return (
        lane.phase == "EGRESS"
        and lane.departing_last_progress_tick >= 0
        and tick - lane.departing_last_progress_tick
        >= CARGO_LANE_EGRESS_WATCHDOG_TICKS
    )


def _healing_priority_worker(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    memory: TacticMemory,
) -> Any | None:
    if core_position is None or memory.heal_intent_id is None:
        return None
    worker = next(
        (
            candidate
            for candidate in getattr(turn, "workers", ())
            if str(getattr(candidate, "id", "")) == memory.heal_intent_id
        ),
        None,
    )
    position = _position(getattr(worker, "position", None))
    missing_hp = _unit_max_hp(UnitType.WORKER) - int(getattr(worker, "hp", 0))
    if (
        worker is None
        or position is None
        or missing_hp <= 0
        or int(getattr(worker, "cargo", 0)) > 0
        or int(getattr(turn, "resources", 0)) < UNIT_HEAL_RESOURCE_RESERVE + missing_hp
        or _chebyshev(position, core_position) > max(CARGO_LANE_STAGE_RADII)
    ):
        return None
    route = _complete_route(position, core_position, blocked | danger_cells)
    return worker if route.status == "SUCCESS" else None


def _defender_heal_staging_exclusions(
    memory: TacticMemory,
    core_position: Position,
) -> set[Position]:
    exclusions = {
        _next_position(core_position, direction)
        for direction in CARDINAL_DIRECTIONS
    }
    lane = memory.cargo_lane
    if lane.active:
        exclusions.update(lane.path)
        exclusions.update(lane.owner_approach_path)
        exclusions.update(lane.heal_approach_path)
        exclusions.update(lane.egress_path)
        exclusions.update(lane.stage_targets.values())
    return exclusions


def _refresh_defender_heal_staging_target(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> Position | None:
    intent_id = memory.healing_defender_intent_id
    if core_position is None or intent_id is None:
        memory.healing_defender_stage_target = None
        return None
    defender = next(
        (
            unit
            for unit in tuple(getattr(turn, "vanguards", ()))
            + tuple(getattr(turn, "rangers", ()))
            if str(getattr(unit, "id", "")) == intent_id
        ),
        None,
    )
    origin = _position(getattr(defender, "position", None))
    if origin is None:
        memory.healing_defender_stage_target = None
        return None
    exclusions = _defender_heal_staging_exclusions(memory, core_position)
    route_blocked = set(blocked) | set(danger_cells) | exclusions
    route_blocked.discard(origin)

    def valid(target: Position | None) -> bool:
        if target is None or target in route_blocked:
            return False
        if friendly_occupancy.get(target, 0) - int(target == origin) > 0:
            return False
        return _complete_route(origin, target, route_blocked).status == "SUCCESS"

    if valid(memory.healing_defender_stage_target):
        return memory.healing_defender_stage_target
    candidates: list[tuple[int, int, int, Position]] = []
    for target in _chebyshev_ring_positions(
        core_position,
        max(CARGO_LANE_STAGE_RADII),
    ):
        if not valid(target):
            continue
        route = _complete_route(origin, target, route_blocked)
        candidates.append((len(route.path), target[0], target[1], target))
    memory.healing_defender_stage_target = (
        min(candidates)[3] if candidates else None
    )
    return memory.healing_defender_stage_target


def _healing_priority_defender(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    memory: TacticMemory,
) -> Any | None:
    intent_id = memory.healing_defender_intent_id
    if core_position is None or intent_id is None:
        return None
    defender = next(
        (
            unit
            for unit in tuple(getattr(turn, "vanguards", ()))
            + tuple(getattr(turn, "rangers", ()))
            if str(getattr(unit, "id", "")) == intent_id
        ),
        None,
    )
    position = _position(getattr(defender, "position", None))
    if defender is None or position is None:
        return None
    missing_hp = _unit_max_hp(defender.unit_type) - int(
        getattr(defender, "hp", 0)
    )
    if (
        missing_hp <= 0
        or int(getattr(turn, "resources", 0))
        < UNIT_HEAL_RESOURCE_RESERVE + missing_hp
    ):
        return None
    if memory.healing_defender_admitted_id == intent_id:
        return defender
    near_idle_core = bool(
        not memory.cargo_lane.active
        and not any(
            int(getattr(worker, "cargo", 0)) > 0
            for worker in getattr(turn, "workers", ())
        )
        and _chebyshev(position, core_position)
        <= max(CARGO_LANE_STAGE_RADII)
    )
    within_staging_ring = (
        _chebyshev(position, core_position) <= max(CARGO_LANE_STAGE_RADII)
    )
    if not near_idle_core and not within_staging_ring and (
        memory.healing_defender_stage_target is None
        or position != memory.healing_defender_stage_target
    ):
        return None
    route = _complete_route(position, core_position, blocked | danger_cells)
    return defender if route.status == "SUCCESS" else None


def _critical_defender_heal_wait_exhausted(
    turn: Any,
    defender: Any | None,
    memory: TacticMemory,
    intent_id: str | None,
    tick: int,
) -> bool:
    if (
        defender is None
        or intent_id is None
        or str(getattr(defender, "id", "")) != intent_id
        or memory.healing_defender_intent_tick < 0
    ):
        return False
    max_hp = _unit_max_hp(getattr(defender, "unit_type", UnitType.WORKER))
    critical = int(getattr(defender, "hp", max_hp)) * 2 <= max_hp
    waited_long_enough = (
        tick - memory.healing_defender_intent_tick
        >= CRITICAL_DEFENDER_HEAL_MAX_WAIT_TICKS
    )
    cargo_waiting = any(
        int(getattr(worker, "cargo", 0)) > 0
        for worker in getattr(turn, "workers", ())
    )
    cargo_turn_preserved = (
        memory.core_visit_deposit_streak
        >= CRITICAL_DEFENDER_HEAL_MIN_DEPOSITS
        or not cargo_waiting
    )
    return critical and waited_long_enough and cargo_turn_preserved


def _refresh_healing_priority(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> bool:
    _refresh_defender_heal_staging_target(
        turn,
        core_position,
        blocked,
        danger_cells,
        friendly_occupancy,
        memory,
    )
    worker = _healing_priority_worker(
        turn,
        core_position,
        blocked,
        danger_cells,
        memory,
    )
    defender = _healing_priority_defender(
        turn,
        core_position,
        blocked,
        danger_cells,
        memory,
    )
    priority_candidates: list[tuple[int, int, str]] = []
    if worker is not None:
        priority_candidates.append(
            (
                memory.heal_intent_tick,
                0,
                str(getattr(worker, "id", "")),
            )
        )
    if defender is not None:
        priority_candidates.append(
            (
                memory.healing_defender_intent_tick,
                1,
                str(getattr(defender, "id", "")),
            )
        )
    ready_candidate_ids = {candidate[2] for candidate in priority_candidates}
    rotated_priority_ids = {
        memory.healing_defender_intent_id
    } if (
        memory.heal_priority_admission_timeouts > 0
        and memory.healing_defender_intent_id is not None
        and memory.healing_defender_intent_id in memory.healing_defender_ids
    ) else set()
    intent_id = (
        memory.heal_priority_intent_id
        if memory.heal_priority_intent_id
        in ready_candidate_ids | rotated_priority_ids
        else min(priority_candidates)[2]
        if priority_candidates
        else None
    )
    if intent_id is None:
        memory.heal_priority_intent_id = None
        memory.core_visit_deposit_streak = 0
        memory.core_visit_forced_purpose = None
        memory.heal_priority_started_tick = -1
        memory.heal_priority_admission_timeouts = 0
        memory.heal_priority_interleave_pending = False
        return False
    if memory.heal_priority_intent_id != intent_id:
        memory.heal_priority_intent_id = intent_id
        memory.core_visit_deposit_streak = 0
        memory.core_visit_forced_purpose = None
        memory.heal_priority_started_tick = -1
        memory.heal_priority_admission_timeouts = 0
        memory.heal_priority_interleave_pending = False
    tick = int(getattr(turn, "tick", 0))
    force_heal = (
        memory.healing_defender_admitted_id == intent_id
        or memory.core_visit_deposit_streak >= HEAL_PRIORITY_DEPOSIT_LIMIT
        or _critical_defender_heal_wait_exhausted(
            turn,
            defender,
            memory,
            intent_id,
            tick,
        )
    )
    if force_heal and memory.heal_priority_started_tick < 0:
        memory.heal_priority_started_tick = tick
    if (
        force_heal
        and memory.healing_defender_admitted_id is None
        and memory.heal_priority_started_tick >= 0
        and tick - memory.heal_priority_started_tick
        >= HEAL_PRIORITY_MAX_HOLD_TICKS
    ):
        memory.heal_priority_interleave_pending = True
    if force_heal and not memory.heal_priority_interleave_pending:
        memory.core_visit_forced_purpose = "HEAL"
    elif memory.heal_priority_interleave_pending:
        memory.core_visit_forced_purpose = None
    return intent_id in ready_candidate_ids


def _admit_healing_defender(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    memory: TacticMemory,
    tick: int,
) -> bool:
    defender_id = memory.healing_defender_intent_id
    if (
        defender_id is None
        or defender_id != memory.heal_priority_intent_id
        or core_position is None
        or memory.core_visit.unit_id is not None
    ):
        return False
    defender = next(
        (
            unit
            for unit in tuple(getattr(turn, "vanguards", ()))
            + tuple(getattr(turn, "rangers", ()))
            if str(getattr(unit, "id", "")) == defender_id
        ),
        None,
    )
    position = _position(getattr(defender, "position", None))
    if defender is None or position is None:
        return False
    if memory.healing_defender_admitted_id != defender_id:
        memory.healing_defender_admitted_id = defender_id
        memory.healing_defender_admitted_tick = tick
        route = _complete_route(position, core_position, blocked)
        memory.healing_defender_best_distance = (
            max(0, len(route.path) - 1)
            if route.status == "SUCCESS"
            else PATH_COST_UNREACHABLE
        )
        memory.healing_defender_last_progress_tick = tick
        memory.healing_defender_last_position = position
    memory.core_visit_forced_purpose = "HEAL"
    return True


def _healing_worker_lane_access_ready(
    turn: Any,
    memory: TacticMemory,
    worker_id: str,
) -> bool:
    """Return whether a queued healing Worker may cross a safe EGRESS lane."""
    lane = memory.cargo_lane
    if not (
        lane.active
        and lane.phase == "EGRESS"
        and memory.heal_intent_id == worker_id
        and memory.heal_priority_intent_id == worker_id
        and memory.core_visit_forced_purpose == "HEAL"
        and lane.owner_id is None
        and lane.departing_worker_id is None
        and not lane.startup_pending_ids
        and memory.core_visit.unit_id in {None, worker_id}
    ):
        return False
    worker = next(
        (
            candidate
            for candidate in getattr(turn, "workers", ())
            if str(getattr(candidate, "id", "")) == worker_id
        ),
        None,
    )
    return bool(
        worker is not None
        and int(getattr(worker, "cargo", 0)) == 0
        and int(getattr(worker, "hp", 0)) < _unit_max_hp(UnitType.WORKER)
    )


def _refresh_cargo_lane_occupants(
    turn: Any,
    memory: TacticMemory,
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
) -> None:
    lane = memory.cargo_lane
    if not lane.active or lane.core_position is None or lane.gateway is None:
        return
    workers = tuple(getattr(turn, "workers", ()))
    lane_positions = set(lane.path) | set(lane.egress_path)
    workers_by_id = {
        str(getattr(worker, "id", "")): worker
        for worker in workers
        if str(getattr(worker, "id", ""))
    }
    living_defender_ids = {
        str(getattr(unit, "id", ""))
        for unit in getattr(turn, "units", ())
        if _enum_label(getattr(unit, "unit_type", ""))
        in {"VANGUARD", "RANGER"}
        and str(getattr(unit, "id", ""))
    }
    lane.core_release_hold_unit_ids.intersection_update(living_defender_ids)
    core_neighbors = {
        _next_position(lane.core_position, direction)
        for direction in CARDINAL_DIRECTIONS
    }
    owner = workers_by_id.get(lane.owner_id or "")
    owner_position = _position(getattr(owner, "position", None))
    if owner_position == lane.core_position:
        lane.core_release_hold_unit_ids.clear()
    owner_approach_path: tuple[Position, ...] = ()
    if (
        lane.phase in {"INBOUND", "DEPOSIT"}
        and owner_position is not None
        and lane.gateway is not None
        and owner_position not in lane_positions
    ):
        owner_route = _complete_route(
            owner_position,
            lane.gateway,
            blocked | danger_cells,
        )
        if owner_route.status == "SUCCESS":
            owner_approach_path = tuple(
                owner_route.path[: CARGO_LANE_OWNER_ADMISSION_STEPS + 1]
            )
    lane.owner_approach_path = owner_approach_path
    owner_approach_positions = set(owner_approach_path)
    admitted_defender = next(
        (
            unit
            for unit in tuple(getattr(turn, "vanguards", ()))
            + tuple(getattr(turn, "rangers", ()))
            if str(getattr(unit, "id", ""))
            == memory.healing_defender_admitted_id
        ),
        None,
    )
    admitted_position = _position(getattr(admitted_defender, "position", None))
    heal_approach_path: tuple[Position, ...] = ()
    if admitted_position is not None:
        heal_route = _complete_route(
            admitted_position,
            lane.core_position,
            blocked | danger_cells,
        )
        if heal_route.status == "SUCCESS":
            heal_approach_path = tuple(heal_route.path)
    admitted_worker_id = (
        memory.heal_intent_id
        if _healing_worker_lane_access_ready(
            turn,
            memory,
            memory.heal_intent_id or "",
        )
        else None
    )
    admitted_healing_worker = workers_by_id.get(admitted_worker_id or "")
    admitted_worker_position = _position(
        getattr(admitted_healing_worker, "position", None)
    )
    if admitted_worker_position is not None:
        worker_heal_route = _complete_route(
            admitted_worker_position,
            lane.core_position,
            blocked | danger_cells,
        )
        if worker_heal_route.status == "SUCCESS":
            heal_approach_path = tuple(worker_heal_route.path)
    lane.heal_approach_path = heal_approach_path
    staging_exclusions = (
        lane_positions
        | owner_approach_positions
        | set(heal_approach_path)
    )
    if (
        memory.healing_defender_admitted_id is not None
        or admitted_worker_id is not None
    ):
        # A blocker beside the healing route can pin the next blocker on the
        # route itself. Include the route's one-cell neighborhood so the
        # clearance planner can form a deterministic yield chain.
        for position in heal_approach_path:
            staging_exclusions.update(
                _next_position(position, direction)
                for direction in CARDINAL_DIRECTIONS
            )
    queued_owner = workers_by_id.get(lane.queued_owner_id or "")
    queued_owner_has_cargo = bool(
        queued_owner is not None and int(getattr(queued_owner, "cargo", 0)) > 0
    )
    owner_ready_for_queue = bool(
        lane.phase in {"INBOUND", "DEPOSIT"}
        and (
            owner_position in lane_positions
            or (
                owner_position is not None
                and lane.gateway is not None
                and _manhattan(owner_position, lane.gateway) == 1
            )
        )
    )
    # A queued Cargo must not occupy the shared gateway until the previous
    # depositor has reached open space.
    admit_queued_to_gateway = False
    lane.yield_worker_ids = {
        str(getattr(worker, "id", ""))
        for worker in workers
        if str(getattr(worker, "id", "")) != lane.owner_id
        and str(getattr(worker, "id", "")) != lane.departing_worker_id
        and str(getattr(worker, "id", "")) not in lane.startup_pending_ids
        and _position(getattr(worker, "position", None))
        in staging_exclusions | core_neighbors
        and str(getattr(worker, "id", "")) != admitted_worker_id
        and not (
            str(getattr(worker, "id", "")) == lane.queued_owner_id
            and _position(getattr(worker, "position", None)) == lane.gateway
            and memory.healing_defender_admitted_id is None
            and admitted_worker_id is None
        )
    }
    lane.yield_unit_ids = {
        str(getattr(unit, "id", ""))
        for unit in getattr(turn, "units", ())
        if _enum_label(getattr(unit, "unit_type", ""))
        in {"VANGUARD", "RANGER"}
        and _position(getattr(unit, "position", None))
        in staging_exclusions | core_neighbors
        and str(getattr(unit, "id", ""))
        != memory.healing_defender_admitted_id
        and not (
            memory.core_visit.purpose == "HEAL"
            and str(getattr(unit, "id", "")) == memory.core_visit.unit_id
        )
    } | lane.core_release_hold_unit_ids
    yielding_ids = lane.yield_worker_ids | lane.yield_unit_ids
    units_by_id = {
        str(getattr(unit, "id", "")): unit
        for unit in getattr(turn, "units", ())
        if str(getattr(unit, "id", "")) in yielding_ids
    }
    owner_approach_neighborhood = {
        _next_position(position, direction)
        for position in owner_approach_path
        for direction in CARDINAL_DIRECTIONS
    }
    clearance_positions = (
        staging_exclusions
        | owner_approach_neighborhood
        | core_neighbors
    )
    unavailable_positions = set(friendly_occupancy)
    unavailable_positions.update(
        _positions(getattr(turn, "resource_cells", ()))
    )
    reserved_yield_positions: set[Position] = set()
    yield_paths: dict[str, tuple[Position, ...]] = {}
    for unit_id, unit in sorted(
        units_by_id.items(),
        key=lambda item: (
            -_manhattan(
                _position(getattr(item[1], "position", None))
                or lane.core_position,
                lane.core_position,
            ),
            item[0],
        ),
    ):
        origin = _position(getattr(unit, "position", None))
        if origin is None:
            continue
        route = _cargo_lane_yield_route(
            origin,
            lane.core_position,
            clearance_positions,
            blocked | danger_cells,
            unavailable_positions | reserved_yield_positions,
        )
        if (
            route is not None
            and origin == lane.core_position
            and lane.owner_id is not None
        ):
            # Keep a Core occupant on its published release route until the
            # admitted Cargo owner takes the Core. Otherwise guard assignment
            # can send it straight back and contest the deposit cell.
            lane.core_release_hold_unit_ids.add(unit_id)
        if route is None:
            # A closed pocket can leave a defender standing on the Core with
            # no route all the way beyond the clearance ring. Moving it to an
            # adjacent non-lane cell is still enough to release the deposit
            # cell for the admitted Cargo owner on the next Tick.
            route = _cargo_lane_core_release_route(
                origin,
                lane,
                blocked | danger_cells,
                unavailable_positions | reserved_yield_positions,
            )
            if route is not None:
                lane.core_release_hold_unit_ids.add(unit_id)
        if route is None:
            route = _cargo_lane_temporary_release_route(
                origin,
                lane,
                owner_approach_positions,
                blocked | danger_cells,
                unavailable_positions,
                reserved_yield_positions,
                held=unit_id in lane.core_release_hold_unit_ids,
            )
            if route is not None:
                lane.core_release_hold_unit_ids.add(unit_id)
        if route is None:
            continue
        yield_paths[unit_id] = route
        reserved_yield_positions.update(route[1:])
    lane.yield_paths = yield_paths
    lane.yield_targets = {
        unit_id: route[-1]
        for unit_id, route in yield_paths.items()
    }
    staged_cargo = [
        worker
        for worker in workers
        if int(getattr(worker, "cargo", 0)) > 0
        and not (
            lane.phase in {"INBOUND", "DEPOSIT"}
            and str(getattr(worker, "id", "")) == lane.owner_id
        )
    ]
    cargo_ids = {str(getattr(worker, "id", "")) for worker in staged_cargo}
    stages_valid = (
        set(lane.stage_targets) == cargo_ids
        and all(
            target not in blocked
            and target not in danger_cells
            and (
                (
                    worker_id == lane.queued_owner_id
                    and admit_queued_to_gateway
                    and target == lane.gateway
                )
                or (
                    not (
                        worker_id == lane.queued_owner_id
                        and admit_queued_to_gateway
                    )
                    and target not in staging_exclusions
                )
            )
            for worker_id, target in lane.stage_targets.items()
        )
    )
    if not stages_valid:
        lane.stage_targets = _cargo_stage_targets(
            staged_cargo,
            lane.core_position,
            lane.gateway,
            staging_exclusions,
            blocked,
            danger_cells,
            friendly_occupancy,
            queued_owner_id=lane.queued_owner_id,
            admit_queued_to_gateway=admit_queued_to_gateway,
        )


def _update_cargo_lane(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    *,
    core_accepts_delivery: bool,
    core_stable: bool,
    threat_level: str,
) -> CargoLanePlan:
    tick = int(getattr(turn, "tick", 0))
    workers = tuple(getattr(turn, "workers", ()))
    workers_by_id = {
        str(getattr(worker, "id", "")): worker
        for worker in workers
        if str(getattr(worker, "id", ""))
    }
    cargo_workers = [
        worker for worker in workers if int(getattr(worker, "cargo", 0)) > 0
    ]
    cargo_ids = {str(getattr(worker, "id", "")) for worker in cargo_workers}
    lane = memory.cargo_lane
    healing_priority_ready = _refresh_healing_priority(
        turn,
        core_position,
        blocked,
        danger_cells,
        friendly_occupancy,
        memory,
    )
    # Lane geometry is refreshed only at an idle handoff boundary. Replacing
    # an active path mid-INBOUND/EGRESS would invalidate the half-duplex
    # reservations of the worker already in the corridor.
    waiting_cargo_ids = {
        worker_id
        for worker_id, stage_target in lane.stage_targets.items()
        if worker_id in cargo_ids
        and worker_id not in {lane.owner_id, lane.departing_worker_id}
        and _position(getattr(workers_by_id.get(worker_id), "position", None))
        == stage_target
    }
    for worker_id in waiting_cargo_ids:
        memory.cargo_lane_wait_ticks[worker_id] = (
            memory.cargo_lane_wait_ticks.get(worker_id, 0) + 1
        )
    for worker_id in cargo_ids - waiting_cargo_ids:
        memory.cargo_lane_wait_ticks.pop(worker_id, None)
    for worker_id in set(memory.cargo_lane_wait_ticks) - cargo_ids:
        memory.cargo_lane_wait_ticks.pop(worker_id, None)

    unsafe = threat_level in {"PRE_EVADE", "ENGAGED", "BREAKOUT"}
    if (
        core_position is None
        or not core_stable
        or unsafe
        or (lane.active and lane.core_position != core_position)
    ):
        _clear_cargo_lane(memory)
        return memory.cargo_lane

    if not lane.active:
        if healing_priority_ready and (
            memory.core_visit_forced_purpose == "HEAL" or not cargo_workers
        ):
            if _admit_healing_defender(
                turn,
                core_position,
                blocked | danger_cells,
                memory,
                tick,
            ):
                return lane
        if not core_accepts_delivery:
            return lane
        if memory.core_visit_forced_purpose == "HEAL":
            return lane
        single_path = _single_open_cargo_lane_path(
            core_position,
            blocked | danger_cells,
            friendly_occupancy,
        )
        startup_pending_ids = (
            _cargo_lane_startup_workers(workers, core_position)
            if single_path is not None
            else set()
        )
        owner = None
        selection_route = None
        if cargo_workers:
            owner, selection_route = _select_cargo_lane_owner(
                cargo_workers,
                core_position,
                blocked,
                memory,
            )
        if owner is None and not startup_pending_ids:
            return lane
        if single_path is not None:
            bridge = CargoLaneBridge(
                path=single_path,
                gateway=single_path[-1],
                blocker_ids=frozenset(),
            )
        elif memory.core_pocket.blocked and owner is not None:
            assert selection_route is not None
            bridge = _local_cargo_lane_bridge(
                core_position,
                owner,
                selection_route.path,
                blocked,
                danger_cells,
                getattr(turn, "units", ()),
            )
            if bridge is None:
                return lane
        else:
            return lane
        owner_id = str(getattr(owner, "id", "")) if owner is not None else None
        lane = CargoLanePlan(
            active=True,
            core_position=core_position,
            owner_id=owner_id,
            path=bridge.path,
            gateway=bridge.gateway,
            startup_pending_ids=startup_pending_ids,
            started_tick=tick,
            phase_started_tick=tick,
            last_planned_tick=tick,
            geometry_source=(
                "SINGLE_OPEN"
                if single_path is not None
                else "CORE_POCKET"
            ),
        )
        memory.cargo_lane = lane
        if lane.owner_id is not None:
            memory.cargo_lane_wait_ticks.pop(lane.owner_id, None)
        lane.startup_pending_ids.discard(owner_id or "")
        if lane.startup_pending_ids:
            _set_cargo_lane_phase(lane, "STARTUP_EVACUATION", tick)
            lane.queued_owner_id = owner_id
            lane.owner_id = None
            _select_cargo_lane_departing_worker(
                lane,
                workers_by_id,
                blocked | danger_cells,
                friendly_occupancy,
                tick,
            )

    if memory.core_visit.unit_id is None:
        if (
            not healing_priority_ready
            and (
                memory.heal_priority_intent_id is None
                or memory.core_visit_deposit_streak < HEAL_PRIORITY_DEPOSIT_LIMIT
                or memory.heal_priority_interleave_pending
            )
        ):
            memory.core_visit_forced_purpose = None
        deposit_worker = workers_by_id.get(memory.cargo_lane.owner_id or "")
        if (
            deposit_worker is not None
            and _position(getattr(deposit_worker, "position", None)) == core_position
            and int(getattr(deposit_worker, "cargo", 0)) > 0
        ):
            memory.core_visit = CoreVisit(
                unit_id=str(getattr(deposit_worker, "id", "")),
                purpose="DEPOSIT",
                granted_tick=tick,
            )

    lane = memory.cargo_lane
    lane.startup_pending_ids.intersection_update(workers_by_id)
    for worker_id in tuple(lane.startup_pending_ids):
        worker = workers_by_id[worker_id]
        position = _position(getattr(worker, "position", None))
        if (
            int(getattr(worker, "cargo", 0)) > 0
            or position is None
            or _cargo_lane_egress_complete(
                position,
                lane,
                blocked | danger_cells,
                friendly_occupancy,
            )
        ):
            lane.startup_pending_ids.discard(worker_id)

    departing = workers_by_id.get(lane.departing_worker_id or "")
    departing_position = _position(getattr(departing, "position", None))
    if lane.departing_worker_id is not None and (
        departing is None
        or int(getattr(departing, "cargo", 0)) > 0
        or departing_position is None
    ):
        lane.startup_pending_ids.discard(lane.departing_worker_id)
        lane.departing_worker_id = None
        lane.egress_path = ()
        lane.egress_target = None
        _reset_cargo_lane_departing_progress(lane)
        if lane.phase == "EGRESS":
            lane.phase_started_tick = tick
    elif (
        departing_position is not None
        and _cargo_lane_egress_complete(
            departing_position,
            lane,
            blocked | danger_cells,
            friendly_occupancy,
        )
    ):
        lane.startup_pending_ids.discard(lane.departing_worker_id or "")
        lane.departing_worker_id = None
        lane.egress_path = ()
        lane.egress_target = None
        _reset_cargo_lane_departing_progress(lane)
        if lane.phase == "EGRESS":
            lane.phase_started_tick = tick

    owner = workers_by_id.get(lane.owner_id or "")
    owner_position = _position(getattr(owner, "position", None))
    owner_has_cargo = owner is not None and int(getattr(owner, "cargo", 0)) > 0
    stalled_inbound_owner_id: str | None = None
    if owner is not None and not owner_has_cargo:
        departing_id = str(getattr(owner, "id", ""))
        memory.cargo_lane_stalled_owners.pop(departing_id, None)
        lane.departing_worker_id = departing_id
        lane.owner_id = None
        _set_cargo_lane_phase(lane, "EGRESS", tick)
        lane.egress_path = ()
        lane.egress_target = None
        lane.owner_approach_path = ()
        _reset_cargo_lane_owner_progress(lane, tick)
        _reset_cargo_lane_departing_progress(lane, tick)
        owner = None
    elif lane.owner_id is not None and owner is None:
        lane.owner_id = None
        lane.owner_approach_path = ()
        _reset_cargo_lane_owner_progress(lane, tick)
    elif _cargo_lane_owner_stalled(
        lane,
        owner_position,
        blocked | danger_cells,
        tick,
    ):
        lane.watchdog_tick = tick
        if owner_position in lane.path:
            lane.watchdog_reason = "INBOUND_OWNER_STALLED_IN_LANE"
            _reset_cargo_lane_owner_progress(lane, tick)
        else:
            # An owner outside the physical corridor can be replaced safely.
            # Once it enters lane.path, preserving half-duplex ownership takes
            # precedence over watchdog recovery.
            stalled_inbound_owner_id = lane.owner_id
            if stalled_inbound_owner_id and owner_position is not None:
                memory.cargo_lane_stalled_owners[stalled_inbound_owner_id] = (
                    owner_position,
                    tick + CARGO_LANE_STALLED_OWNER_COOLDOWN_TICKS,
                )
            lane.watchdog_reason = "INBOUND_OWNER_STALLED"
            lane.owner_id = None
            lane.owner_approach_path = ()
            lane.phase_started_tick = tick
            _reset_cargo_lane_owner_progress(lane, tick)
            owner = None
            owner_position = None
            owner_has_cargo = False

    if lane.departing_worker_id is not None:
        departing = workers_by_id.get(lane.departing_worker_id)
        departing_position = _position(getattr(departing, "position", None))
        if departing_position is not None and (
            not lane.egress_path
            or departing_position not in lane.egress_path
            or departing_position == lane.egress_path[-1]
        ):
            route = _cargo_lane_egress_route(
                departing_position,
                lane,
                blocked | danger_cells,
                friendly_occupancy,
            )
            if route is not None:
                lane.egress_path = route
                lane.egress_target = route[-1]

        if _cargo_lane_departing_stalled(lane, departing_position, tick):
            route = _cargo_lane_egress_route(
                departing_position,
                lane,
                blocked | danger_cells,
                friendly_occupancy,
            )
            if route is not None:
                lane.egress_path = route
                lane.egress_target = route[-1]
            lane.watchdog_tick = tick
            lane.watchdog_reason = "EGRESS_DEPARTING_STALLED"
            _reset_cargo_lane_departing_progress(lane, tick)

    if lane.startup_pending_ids and lane.departing_worker_id is None:
        _select_cargo_lane_departing_worker(
            lane,
            workers_by_id,
            blocked | danger_cells,
            friendly_occupancy,
            tick,
        )

    # A simple open-entrance lane may have become stale while cargo was
    # queued, but changing geometry is safe only after the previous worker has
    # fully left the lane. Never replace a locally-built Core-pocket bridge.
    if (
        lane.owner_id is None
        and lane.departing_worker_id is None
        and not lane.startup_pending_ids
        and lane.phase in {"EGRESS", "STARTUP_EVACUATION"}
        and lane.geometry_source == "SINGLE_OPEN"
    ):
        refreshed_path = _single_open_cargo_lane_path(
            core_position,
            blocked | danger_cells,
            friendly_occupancy,
        )
        if refreshed_path is not None:
            lane.path = refreshed_path
            lane.gateway = refreshed_path[-1]
            lane.last_planned_tick = tick
            lane.egress_path = ()
            lane.egress_target = None

    if lane.departing_worker_id is not None or lane.startup_pending_ids:
        _set_cargo_lane_phase(
            lane,
            "STARTUP_EVACUATION" if lane.startup_pending_ids else "EGRESS",
            tick,
        )
        lane.owner_id = None
    else:
        queued_owner = workers_by_id.get(lane.queued_owner_id or "")
        queued_position = _position(getattr(queued_owner, "position", None))
        queued_route = (
            _complete_route(queued_position, lane.gateway, blocked)
            if queued_position is not None and lane.gateway is not None
            else None
        )
        queued_owner_valid = bool(
            queued_owner is not None
            and int(getattr(queued_owner, "cargo", 0)) > 0
            and queued_route is not None
            and queued_route.status == "SUCCESS"
        )
        safe_heal_handoff = bool(
            lane.owner_id is None
            and lane.departing_worker_id is None
            and not lane.startup_pending_ids
            and memory.core_visit.unit_id is None
        )
        if (
            safe_heal_handoff
            and stalled_inbound_owner_id is None
            and healing_priority_ready
            and memory.core_visit_forced_purpose == "HEAL"
        ):
            _admit_healing_defender(
                turn,
                core_position,
                blocked | danger_cells,
                memory,
                tick,
            )
        if (
            lane.phase == "EGRESS"
            and queued_owner_valid
            and memory.core_visit.unit_id is None
            and memory.heal_priority_interleave_pending
            and memory.healing_defender_admitted_id is None
        ):
            memory.core_visit_forced_purpose = None
            lane.watchdog_tick = tick
            lane.watchdog_reason = "HEAL_PRIORITY_INTERLEAVE"
        if (
            lane.owner_id is None
            and cargo_workers
            and core_accepts_delivery
            and (
                memory.core_visit_forced_purpose != "HEAL"
                or stalled_inbound_owner_id is not None
            )
        ):
            if not queued_owner_valid:
                queued_owner, queued_route = _select_cargo_lane_owner(
                    (
                        worker
                        for worker in cargo_workers
                        if str(getattr(worker, "id", ""))
                        != stalled_inbound_owner_id
                    ),
                    lane.gateway,
                    blocked,
                    memory,
                    onward_steps=max(0, len(lane.path) - 1),
                )
            lane.owner_id = (
                str(getattr(queued_owner, "id", ""))
                if queued_owner is not None
                else None
            )
            if lane.owner_id is not None:
                memory.cargo_lane_wait_ticks.pop(lane.owner_id, None)
            lane.queued_owner_id = None
            _set_cargo_lane_phase(lane, "INBOUND", tick)
            _reset_cargo_lane_owner_progress(lane, tick)

    queued_owner = workers_by_id.get(lane.queued_owner_id or "")
    queued_position = _position(getattr(queued_owner, "position", None))
    queued_route = (
        _complete_route(queued_position, lane.gateway, blocked)
        if queued_position is not None and lane.gateway is not None
        else None
    )
    queued_owner_valid = bool(
        queued_owner is not None
        and int(getattr(queued_owner, "cargo", 0)) > 0
        and str(getattr(queued_owner, "id", "")) != lane.owner_id
        and str(getattr(queued_owner, "id", "")) != lane.departing_worker_id
        and queued_route is not None
        and queued_route.status == "SUCCESS"
    )
    if lane.queued_owner_id is not None and not queued_owner_valid:
        lane.queued_owner_id = None
        queued_owner = None

    if any(position in blocked or position in danger_cells for position in lane.path):
        _clear_cargo_lane(memory)
        return memory.cargo_lane

    active_owner = workers_by_id.get(lane.owner_id or "")
    active_owner_position = _position(getattr(active_owner, "position", None))
    owner_ready_for_queue = bool(
        lane.phase in {"INBOUND", "DEPOSIT"}
        and (
            active_owner_position in set(lane.path)
            or (
                active_owner_position is not None
                and lane.gateway is not None
                and _manhattan(active_owner_position, lane.gateway) == 1
            )
        )
    )
    can_select_queued_owner = owner_ready_for_queue or lane.phase in {
        "EGRESS",
        "STARTUP_EVACUATION",
    }
    if can_select_queued_owner and lane.queued_owner_id is None:
        remaining_cargo = [
            worker
            for worker in cargo_workers
            if str(getattr(worker, "id", ""))
            not in {lane.owner_id, lane.departing_worker_id}
        ]
        queued_owner, _ = _select_cargo_lane_owner(
            remaining_cargo,
            lane.gateway,
            blocked,
            memory,
            onward_steps=max(0, len(lane.path) - 1),
        )
        lane.queued_owner_id = (
            str(getattr(queued_owner, "id", ""))
            if queued_owner is not None
            else None
        )

    if active_owner_position == core_position:
        _set_cargo_lane_phase(lane, "DEPOSIT", tick)

    if (
        not cargo_workers
        and lane.departing_worker_id is None
        and not lane.startup_pending_ids
    ):
        _clear_cargo_lane(memory)
        return memory.cargo_lane

    _refresh_cargo_lane_occupants(
        turn,
        memory,
        blocked,
        danger_cells,
        friendly_occupancy,
    )
    return memory.cargo_lane


def _queue_worker_route(
    worker: Any,
    target: Position | None,
    role: str,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> tuple[bool, str, RouteSearchResult | None]:
    origin = _position(getattr(worker, "position", None))
    worker_id = str(getattr(worker, "id", ""))
    if origin is None or target is None or origin == target:
        return False, "AT_TARGET", None
    route_blocked = set(blocked)
    healing_access = role == "HEAL" and (
        memory.heal_intent_id == worker_id
        or (
            memory.core_visit.unit_id == worker_id
            and memory.core_visit.purpose == "HEAL"
        )
    ) and (
        not memory.cargo_lane.active
        or (
            memory.cargo_lane.owner_id is None
            and memory.cargo_lane.departing_worker_id is None
            and not memory.cargo_lane.startup_pending_ids
        )
    )
    capacity_reservations = reservations
    if healing_access and reservations:
        if isinstance(reservations, dict):
            capacity_reservations = {
                position: count
                for position, count in reservations.items()
                if position != target
            }
        else:
            capacity_reservations = set(reservations)
            capacity_reservations.discard(target)
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        capacity_reservations,
        actor_origin=origin,
    )
    if memory.cargo_lane.active and worker_id not in {
        memory.cargo_lane.owner_id,
        memory.cargo_lane.departing_worker_id,
    }:
        gateway = memory.cargo_lane.gateway
        gateway_was_available = bool(
            gateway is not None and gateway not in capacity_blocked
        )
        if not healing_access:
            capacity_blocked.update(memory.cargo_lane.path)
            capacity_blocked.update(memory.cargo_lane.egress_path)
        if memory.cargo_lane.core_position is not None and not healing_access:
            capacity_blocked.update(
                _next_position(memory.cargo_lane.core_position, direction)
                for direction in CARDINAL_DIRECTIONS
            )
        if (
            worker_id == memory.cargo_lane.queued_owner_id
            and gateway_was_available
        ):
            capacity_blocked.discard(gateway)
        capacity_blocked.discard(origin)
    route_blocked.discard(target)
    contested_positions = memory.contested_worker_positions(
        worker_id,
        int(memory.last_observed_tick or 0),
    )
    route_blocked.update(contested_positions)
    static_route_blocked = set(route_blocked)
    route_blocked.update(capacity_blocked)
    route_blocked.discard(target)
    discouraged = memory.recent_worker_positions(worker_id)
    direction, result = _cached_worker_route_step(
        memory,
        worker_id,
        role,
        origin,
        target,
        route_blocked,
        discouraged=discouraged,
    )
    status = result.status if result is not None else "CACHED"
    if status == "BUDGET_EXCEEDED":
        capacity_pressure = False
        if capacity_blocked:
            static_result = _complete_route(
                origin,
                target,
                static_route_blocked,
                discouraged=discouraged,
            )
            capacity_pressure = static_result.status == "SUCCESS"
        memory.route_diagnostics[worker_id] = (
            "CAPACITY_BLOCKED_BUDGET"
            if capacity_pressure
            else "ROUTE_BUDGET_FALLBACK"
        )
        if (
            capacity_pressure
            and role.startswith("CARGO")
            and (memory.core_pocket.blocked or memory.cargo_lane.active)
            and worker_id != memory.cargo_lane.queued_owner_id
        ):
            memory.worker_routes.pop(worker_id, None)
            return False, "CAPACITY_BLOCKED_BUDGET", result
        direction = choose_step_direction(
            origin,
            target,
            route_blocked,
            discouraged=discouraged,
        )
    elif status == "UNREACHABLE":
        if capacity_blocked:
            static_result = _complete_route(
                origin,
                target,
                static_route_blocked,
                discouraged=discouraged,
            )
            if static_result.status == "SUCCESS":
                memory.route_diagnostics[worker_id] = "CAPACITY_BLOCKED"
                return False, "CAPACITY_BLOCKED", result
        memory.route_diagnostics[worker_id] = "ROUTE_UNREACHABLE"
    if direction is None:
        return False, status, result
    destination = _next_position(origin, direction)
    if destination in capacity_blocked:
        memory.worker_routes.pop(worker_id, None)
        memory.route_diagnostics[worker_id] = "CAPACITY_BLOCKED"
        return False, "CAPACITY_BLOCKED", result
    _reserve_destination(reservations, destination)
    worker.move(direction)
    memory.note_worker_move(worker_id, destination)
    return True, status, result


def _cargo_recovery_boundary(
    explored: Iterable[Position],
    core_position: Position,
    blocked: set[Position],
    recent: set[Position],
) -> Position | None:
    explored_set = set(explored)
    if not explored_set:
        return None
    candidates = []
    for position in explored_set:
        if position in recent:
            continue
        neighbors = {
            _next_position(position, direction) for direction in CARDINAL_DIRECTIONS
        }
        if neighbors <= explored_set:
            continue
        candidates.append(position)
    return min(
        candidates,
        key=lambda position: (
            _manhattan(position, core_position),
            position[0],
            position[1],
        ),
        default=None,
    )


def _visible_beacon_position(turn: Any) -> Position | None:
    beacon = getattr(turn, "beacon", None)
    position = _position(getattr(beacon, "position", None))
    if position is None:
        return None
    status = getattr(beacon, "status", None)
    if status == BeaconStatus.GROUND or str(status).upper().endswith("GROUND"):
        return position
    return None


def _visible_enemies(turn: Any) -> list[Any]:
    enemies: list[Any] = []
    for enemy in getattr(turn, "visible_enemies", ()):
        if _position(getattr(enemy, "position", None)) is not None:
            enemies.append(enemy)
    return enemies


def _enemy_attack_range(unit_type: UnitType) -> int:
    return 3 if unit_type == UnitType.RANGER else 1


def _is_mobile_combat_enemy(enemy: Any) -> bool:
    return getattr(enemy, "unit_type", None) in {
        UnitType.VANGUARD,
        UnitType.RANGER,
    }


def _mobile_combat_enemies(enemies: Iterable[Any]) -> list[Any]:
    return [enemy for enemy in enemies if _is_mobile_combat_enemy(enemy)]


def _is_enemy_core(enemy: Any) -> bool:
    """Distinguish a visible enemy Core from an enemy Unit view."""

    return (
        getattr(enemy, "owner_username", None) is not None
        and getattr(enemy, "unit_type", None) is None
    )


def _confirmed_stationary_targets(
    memory: TacticMemory,
    enemies: list[Any],
    tick: int,
) -> list[Any]:
    """Return visible targets observed at one cell for two complete Ticks."""

    confirmed: list[Any] = []
    for enemy in enemies:
        enemy_key = str(getattr(enemy, "id", ""))
        observation = memory.stationary_enemy_observation_memory.get(enemy_key)
        if observation is None:
            continue
        if observation[0] != tick or observation[2] < STATIONARY_CONFIRMATION_TICKS:
            continue
        confirmed.append(enemy)
    return confirmed


def _allow_stationary_clear(
    threat: ThreatAssessment,
    enemies: list[Any],
    stationary_targets: list[Any],
) -> bool:
    """Allow bounded clearing only when every visible enemy is stationary."""

    if threat.level != "NORMAL" or not enemies or not stationary_targets:
        return False
    stationary_ids = {str(getattr(target, "id", "")) for target in stationary_targets}
    return all(str(getattr(enemy, "id", "")) in stationary_ids for enemy in enemies)


def _nearest(origin: Position, objects: Iterable[Any]) -> Any | None:
    candidates = [
        item for item in objects if _position(getattr(item, "position", None)) is not None
    ]
    return min(
        candidates,
        key=lambda item: (
            _manhattan(origin, _position(item.position)),
            int(getattr(item, "hp", 0)),
            str(getattr(item, "id", "")),
        ),
        default=None,
    )


def _combat_target_key(
    origin: Position,
    enemy: Any,
    priority_enemy_ids: set[str],
) -> tuple[int, int, int, int, str]:
    unit_type = getattr(enemy, "unit_type", None)
    type_priority = (
        0
        if unit_type == UnitType.RANGER
        else 1
        if unit_type == UnitType.VANGUARD
        else 2
    )
    return (
        int(str(getattr(enemy, "id", "")) not in priority_enemy_ids),
        type_priority,
        int(getattr(enemy, "hp", 5)) + int(getattr(enemy, "shield", 0) or 0),
        _manhattan(origin, _position(getattr(enemy, "position", None))),
        str(getattr(enemy, "id", "")),
    )


def _preferred_combat_target(
    origin: Position,
    enemies: Iterable[Any],
    priority_enemy_ids: set[str],
) -> Any | None:
    return min(
        enemies,
        key=lambda enemy: _combat_target_key(origin, enemy, priority_enemy_ids),
        default=None,
    )


def _unit_max_hp(unit_type: UnitType) -> int:
    return 4 if unit_type == UnitType.VANGUARD else 2


def _critical_combat_hp(unit_type: UnitType) -> int:
    if unit_type == UnitType.VANGUARD:
        return 2
    if unit_type == UnitType.RANGER:
        return 1
    return 0


def _is_critical_combat_unit(unit: Any) -> bool:
    threshold = _critical_combat_hp(getattr(unit, "unit_type", None))
    return threshold > 0 and int(getattr(unit, "hp", 0)) <= threshold


def _critical_combat_escape_direction(
    actor: Any,
    enemies: Iterable[Any],
    obstacles: set[Position],
    enemy_occupied_positions: set[Position],
    core_position: Position | None,
    reserved_destinations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> Direction | None:
    """Choose an immediately safer legal step for a critically wounded defender."""

    origin = _position(getattr(actor, "position", None))
    if origin is None:
        return None
    combat_enemies = list(enemies)
    current_damage = _projected_incoming_damage(origin, combat_enemies, obstacles)
    if current_damage <= 0:
        return None
    enemy_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in combat_enemies
        )
        if position is not None
    }
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reserved_destinations,
        actor_origin=origin,
    )
    hard_blocked = (
        set(obstacles)
        | set(enemy_occupied_positions)
        | capacity_blocked
        | memory.contested_unit_positions(getattr(actor, "id", ""), tick)
    )
    candidates: list[tuple[tuple[int, int, int, int], Direction]] = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(origin, direction)
        if destination in hard_blocked:
            continue
        projected_damage = _projected_incoming_damage(
            destination,
            combat_enemies,
            obstacles,
        )
        core_distance = (
            _manhattan(destination, core_position)
            if core_position is not None
            else 0
        )
        nearest_enemy = min(
            (
                _manhattan(destination, enemy_position)
                for enemy_position in enemy_positions
            ),
            default=PATH_COST_UNREACHABLE,
        )
        candidates.append(
            (
                (projected_damage, core_distance, -nearest_enemy, index),
                direction,
            )
        )
    if not candidates:
        return None
    best_score, best_direction = min(candidates, key=lambda candidate: candidate[0])
    if best_score[0] >= current_damage:
        return None
    return best_direction


def _queue_critical_combat_escape(
    actor: Any,
    enemies: Iterable[Any],
    obstacles: set[Position],
    enemy_occupied_positions: set[Position],
    core_position: Position | None,
    reserved_destinations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> bool:
    direction = _critical_combat_escape_direction(
        actor,
        enemies,
        obstacles,
        enemy_occupied_positions,
        core_position,
        reserved_destinations,
        friendly_occupancy,
        memory,
        tick,
    )
    origin = _position(getattr(actor, "position", None))
    if direction is None or origin is None:
        return False
    destination = _next_position(origin, direction)
    if reserved_destinations is not None:
        _reserve_destination(reserved_destinations, destination)
    memory.note_unit_move(
        getattr(actor, "id", ""),
        destination,
        core_position or destination,
    )
    actor.move(direction)
    return True


def _clear_defender_heal_admission(
    memory: TacticMemory,
    *,
    clear_intent: bool,
    clear_priority: bool = True,
) -> None:
    admitted_id = memory.healing_defender_admitted_id
    memory.healing_defender_admitted_id = None
    memory.healing_defender_admitted_tick = -1
    memory.healing_defender_best_distance = None
    memory.healing_defender_last_progress_tick = -1
    memory.healing_defender_last_position = None
    memory.cargo_lane.heal_approach_path = ()
    if clear_intent:
        if clear_priority and memory.heal_priority_intent_id in {
            admitted_id,
            memory.healing_defender_intent_id,
        }:
            memory.heal_priority_intent_id = None
            memory.core_visit_forced_purpose = None
            memory.core_visit_deposit_streak = 0
            memory.heal_priority_started_tick = -1
            memory.heal_priority_admission_timeouts = 0
            memory.heal_priority_interleave_pending = False
        memory.healing_defender_intent_id = None
        memory.healing_defender_intent_tick = -1
        memory.healing_defender_stage_target = None


def _refresh_healing_defenders(
    turn: Any,
    memory: TacticMemory,
    core_position: Position | None,
    blocked: set[Position],
) -> None:
    tick = int(getattr(turn, "tick", 0))
    defenders = tuple(getattr(turn, "vanguards", ())) + tuple(
        getattr(turn, "rangers", ())
    )
    defenders_by_id = {
        str(getattr(defender, "id", "")): defender for defender in defenders
    }
    type_counts = {
        unit_type: sum(defender.unit_type == unit_type for defender in defenders)
        for unit_type in (UnitType.VANGUARD, UnitType.RANGER)
    }
    candidates: list[tuple[float, int, str]] = []
    if core_position is not None:
        for defender in defenders:
            defender_id = str(getattr(defender, "id", ""))
            position = _position(getattr(defender, "position", None))
            max_hp = _unit_max_hp(defender.unit_type)
            missing_hp = max_hp - int(getattr(defender, "hp", max_hp))
            if (
                not defender_id
                or position is None
                or missing_hp <= 0
                or type_counts[defender.unit_type] <= 1
                or memory.healing_defender_cooldowns.get(defender_id, 0) > tick
            ):
                continue
            if (
                int(getattr(turn, "resources", 0))
                < UNIT_HEAL_RESOURCE_RESERVE + missing_hp
            ):
                continue
            candidates.append(
                (
                    int(getattr(defender, "hp", 0)) / max_hp,
                    _manhattan(position, core_position),
                    defender_id,
                )
            )
    candidate_ids = {candidate[2] for candidate in candidates}
    memory.healing_defender_ids = candidate_ids

    if memory.healing_defender_intent_id not in candidate_ids:
        _clear_defender_heal_admission(memory, clear_intent=True)
    if memory.healing_defender_intent_id is None and candidates:
        memory.healing_defender_intent_id = min(candidates)[2]
        memory.healing_defender_intent_tick = tick
        memory.healing_defender_stage_target = None
        if (
            memory.heal_priority_intent_id is not None
            and memory.core_visit_deposit_streak >= HEAL_PRIORITY_DEPOSIT_LIMIT
        ):
            memory.heal_priority_intent_id = memory.healing_defender_intent_id

    admitted_id = memory.healing_defender_admitted_id
    admitted = defenders_by_id.get(admitted_id or "")
    admitted_position = _position(getattr(admitted, "position", None))
    if admitted_id is None or admitted_position is None or core_position is None:
        return
    admitted_route = _complete_route(admitted_position, core_position, blocked)
    admitted_distance = (
        max(0, len(admitted_route.path) - 1)
        if admitted_route.status == "SUCCESS"
        else PATH_COST_UNREACHABLE
    )
    if (
        memory.healing_defender_best_distance is None
        or admitted_distance < memory.healing_defender_best_distance
    ):
        memory.healing_defender_best_distance = admitted_distance
        memory.healing_defender_last_progress_tick = tick
    memory.healing_defender_last_position = admitted_position
    if (
        admitted_position != core_position
        and memory.healing_defender_last_progress_tick >= 0
        and tick - memory.healing_defender_last_progress_tick
        >= DEFENDER_HEAL_ADMISSION_TIMEOUT_TICKS
    ):
        memory.healing_defender_cooldowns[admitted_id] = (
            tick + DEFENDER_HEAL_COOLDOWN_TICKS
        )
        memory.healing_defender_timeout_tick = tick
        memory.heal_priority_admission_timeouts += 1
        _clear_defender_heal_admission(
            memory,
            clear_intent=True,
            clear_priority=False,
        )
        remaining_candidates = [
            candidate
            for candidate in candidates
            if candidate[2] != admitted_id
        ]
        if remaining_candidates:
            memory.healing_defender_intent_id = min(remaining_candidates)[2]
            memory.healing_defender_intent_tick = tick
            memory.heal_priority_intent_id = memory.healing_defender_intent_id
            memory.core_visit_forced_purpose = "HEAL"
        else:
            memory.heal_priority_intent_id = None
            memory.core_visit_forced_purpose = None
            memory.core_visit_deposit_streak = 0
            memory.heal_priority_started_tick = -1
            memory.heal_priority_admission_timeouts = 0
            memory.heal_priority_interleave_pending = False


def _refresh_healing_workers(
    turn: Any,
    memory: TacticMemory,
    core_position: Position | None,
) -> None:
    workers = tuple(getattr(turn, "workers", ()))
    workers_by_id = {
        str(getattr(worker, "id", "")): worker for worker in workers
    }
    memory.healing_worker_ids.intersection_update(workers_by_id)
    for worker_id in tuple(memory.healing_worker_ids):
        worker = workers_by_id[worker_id]
        if (
            int(getattr(worker, "cargo", 0)) > 0
            or int(getattr(worker, "hp", 0)) >= _unit_max_hp(UnitType.WORKER)
        ):
            memory.healing_worker_ids.discard(worker_id)
            if memory.heal_intent_id == worker_id:
                memory.heal_intent_id = None
                memory.heal_intent_tick = -1
                memory.heal_intent_best_distance = None
    if core_position is None:
        return
    candidates = []
    intent_candidates = []
    for worker in workers:
        missing_hp = _unit_max_hp(UnitType.WORKER) - int(getattr(worker, "hp", 0))
        worker_id = str(getattr(worker, "id", ""))
        if (
            missing_hp <= 0
            or int(getattr(worker, "cargo", 0)) > 0
            or worker_id in {memory.cargo_lane.owner_id, memory.cargo_lane.departing_worker_id}
            or int(getattr(turn, "resources", 0)) < UNIT_HEAL_RESOURCE_RESERVE + missing_hp
        ):
            continue
        position = _position(getattr(worker, "position", None))
        if position is None:
            continue
        candidate = (
            int(getattr(worker, "hp", 0)) / _unit_max_hp(UnitType.WORKER),
            _manhattan(position, core_position),
            worker_id,
        )
        candidates.append(candidate)
        if memory.heal_intent_cooldowns.get(worker_id, 0) <= int(
            getattr(turn, "tick", 0)
        ):
            intent_candidates.append(candidate)
    memory.healing_worker_ids = {candidate[2] for candidate in candidates}
    if memory.heal_intent_id not in memory.healing_worker_ids:
        memory.heal_intent_id = None
        memory.heal_intent_tick = -1
        memory.heal_intent_best_distance = None
        memory.healing_worker_stage_target = None
    if memory.heal_intent_id is None and intent_candidates:
        selected = min(intent_candidates)
        memory.heal_intent_id = selected[2]
        memory.heal_intent_tick = int(getattr(turn, "tick", 0))
        memory.heal_intent_best_distance = selected[1]
        memory.healing_worker_stage_target = None


def _refresh_healing_worker_stage_target(
    turn: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> Position | None:
    """Keep a remote injured Worker outside an active Cargo corridor."""

    worker_id = memory.heal_intent_id
    lane = memory.cargo_lane
    if (
        worker_id is None
        or core_position is None
        or not lane.active
    ):
        memory.healing_worker_stage_target = None
        return None
    worker = next(
        (
            candidate
            for candidate in getattr(turn, "workers", ())
            if str(getattr(candidate, "id", "")) == worker_id
        ),
        None,
    )
    origin = _position(getattr(worker, "position", None))
    if origin is None or _chebyshev(origin, core_position) <= max(CARGO_LANE_STAGE_RADII):
        memory.healing_worker_stage_target = None
        return None

    exclusions = {
        _next_position(core_position, direction)
        for direction in CARDINAL_DIRECTIONS
    }
    exclusions.update(lane.path)
    exclusions.update(lane.owner_approach_path)
    exclusions.update(lane.egress_path)
    exclusions.update(lane.stage_targets.values())
    exclusions.update(lane.heal_approach_path)
    route_blocked = set(blocked) | set(danger_cells) | exclusions
    route_blocked.discard(origin)

    def valid(target: Position | None) -> bool:
        if target is None or target in route_blocked:
            return False
        if friendly_occupancy.get(target, 0) - int(target == origin) > 0:
            return False
        return _complete_route(origin, target, route_blocked).status == "SUCCESS"

    if valid(memory.healing_worker_stage_target):
        return memory.healing_worker_stage_target

    candidates: list[tuple[int, int, int, int, Position]] = []
    for radius in reversed(CARGO_LANE_STAGE_RADII):
        for target in _chebyshev_ring_positions(core_position, radius):
            if not valid(target):
                continue
            route = _complete_route(origin, target, route_blocked)
            candidates.append(
                (len(route.path), radius, target[0], target[1], target)
            )
    memory.healing_worker_stage_target = (
        min(candidates)[-1] if candidates else None
    )
    return memory.healing_worker_stage_target


def _adjacent_direction(origin: Position, target: Position) -> Direction | None:
    dx = target[0] - origin[0]
    dy = target[1] - origin[1]
    if abs(dx) + abs(dy) != 1:
        return None
    return _direction_for_delta(dx, dy)


def _vanguard_stationary_approach_position(
    actor: Any,
    target: Position,
    movement_blocked: set[Position],
    reserved_destinations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> Position | None:
    """Choose a reachable empty cell from which a Vanguard can sweep."""

    origin = _position(getattr(actor, "position", None))
    if origin is None:
        return None
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reserved_destinations,
        actor_origin=origin,
    )
    route_blocked = (
        set(movement_blocked)
        | capacity_blocked
        | memory.contested_unit_positions(getattr(actor, "id", ""), tick)
    )
    route_blocked.discard(origin)
    candidates: list[tuple[int, int, int, Position]] = []
    for direction in CARDINAL_DIRECTIONS:
        position = _next_position(target, direction)
        if position in route_blocked:
            continue
        route = _complete_route(origin, position, route_blocked)
        if route.status != "SUCCESS":
            continue
        candidates.append(
            (
                len(route.path) - 1,
                position[0],
                position[1],
                position,
            )
        )
    return min(candidates)[3] if candidates else None


def _aligned_in_range(origin: Position, target: Position, max_range: int = 3) -> bool:
    dx = abs(target[0] - origin[0])
    dy = abs(target[1] - origin[1])
    distance = max(dx, dy)
    return 1 <= distance <= max_range and (dx == 0 or dy == 0 or dx == dy)


def _path_is_clear(origin: Position, target: Position, obstacles: set[Position]) -> bool:
    dx = target[0] - origin[0]
    dy = target[1] - origin[1]
    if not _aligned_in_range(origin, target, max(abs(dx), abs(dy))):
        return False
    steps = max(abs(dx), abs(dy))
    step_x = 0 if dx == 0 else 1 if dx > 0 else -1
    step_y = 0 if dy == 0 else 1 if dy > 0 else -1
    return all((origin[0] + step_x * index, origin[1] + step_y * index) not in obstacles for index in range(1, steps))


def _set_raid_state(memory: TacticMemory, state: str, tick: int) -> None:
    if memory.raid.state != state:
        memory.raid.state = state
        memory.raid_state_changed_tick = tick


def _clear_raid_plan(memory: TacticMemory, tick: int, *, cooldown: bool) -> None:
    memory.raid = CoreRaidPlan(
        state="COOLDOWN" if cooldown else "IDLE",
        cooldown_until=tick + CORE_RAID_COOLDOWN_TICKS if cooldown else 0,
    )
    memory.raid_state_changed_tick = tick


def _abort_raid(memory: TacticMemory, tick: int) -> None:
    raid = memory.raid
    if raid.observer_id is not None:
        memory.core_observer_return_ids.add(raid.observer_id)
    raid.observer_id = None
    raid.observer_position = None
    raid.observer_confirmed = False
    raid.replacement_deadline = 0
    _set_raid_state(memory, "CORE_RECALL", tick)


def _visible_enemy_by_id(enemies: Iterable[Any], enemy_id: str | None) -> Any | None:
    if enemy_id is None:
        return None
    return next(
        (
            enemy
            for enemy in enemies
            if str(getattr(enemy, "id", "")) == enemy_id
        ),
        None,
    )


def _ranger_firing_positions(
    target: Position,
    obstacles: set[Position],
) -> tuple[Position, ...]:
    return tuple(
        position
        for radius in (1, 2, 3)
        for position in _chebyshev_ring_positions(target, radius)
        if _aligned_in_range(position, target)
        and _path_is_clear(position, target, obstacles)
    )


def _observer_positions(
    target: Position,
    blocked: set[Position],
    danger_cells: set[Position],
    enemy_positions: set[Position],
) -> tuple[Position, ...]:
    firing_positions = set(_ranger_firing_positions(target, blocked))
    candidates = [
        position
        for radius in (2, 3)
        for position in _chebyshev_ring_positions(target, radius)
        if position not in blocked
        and position not in danger_cells
        and position not in enemy_positions
    ]
    return tuple(
        sorted(
            candidates,
            key=lambda position: (
                int(position in firing_positions),
                position[0] - target[0],
                position[1] - target[1],
            ),
        )
    )


def _eligible_observers(
    turn: Any,
    memory: TacticMemory,
    allowed_modes: set[str] | None = None,
) -> list[Any]:
    tick = int(getattr(turn, "tick", 0))
    allowed_modes = allowed_modes or {"SCOUT", "IDLE", "SCOUT_COOLDOWN"}
    eligible: list[Any] = []
    for worker in getattr(turn, "workers", ()):
        worker_id = str(getattr(worker, "id", ""))
        if int(getattr(worker, "cargo", 0)) > 0:
            continue
        mode = _worker_mode(worker, memory, tick)
        if mode in allowed_modes:
            eligible.append(worker)
    return sorted(eligible, key=lambda worker: str(getattr(worker, "id", "")))


def _choose_core_observer(
    turn: Any,
    memory: TacticMemory,
    target: Position,
    blocked: set[Position],
    danger_cells: set[Position],
    enemy_positions: set[Position],
    friendly_occupancy: dict[Position, int],
    *,
    max_cost: int | None = None,
    excluded_ids: set[str] | None = None,
    allowed_modes: set[str] | None = None,
) -> tuple[str, Position] | None:
    positions = _observer_positions(target, blocked, danger_cells, enemy_positions)
    excluded_ids = excluded_ids or set()
    choices: list[tuple[int, str, Position]] = []
    for worker in _eligible_observers(turn, memory, allowed_modes):
        worker_id = str(getattr(worker, "id", ""))
        origin = _position(getattr(worker, "position", None))
        if worker_id in excluded_ids or origin is None:
            continue
        occupied_by_others = _unit_capacity_blocked(
            friendly_occupancy,
            actor_origin=origin,
        )
        route_blocked = blocked | danger_cells | enemy_positions | occupied_by_others
        for position in positions:
            if position in occupied_by_others:
                continue
            route = _complete_route(origin, position, route_blocked)
            if route.status != "SUCCESS":
                continue
            cost = len(route.path) - 1
            if max_cost is not None and cost > max_cost:
                continue
            choices.append((cost, worker_id, position))
    if not choices:
        return None
    _, worker_id, position = min(
        choices,
        key=lambda item: (item[0], item[1], item[2][0], item[2][1]),
    )
    return worker_id, position


def _assign_raid_positions(
    units: Iterable[Any],
    candidates: Iterable[Position],
    blocked: set[Position],
    friendly_occupancy: dict[Position, int],
) -> tuple[dict[str, Position], int]:
    ordered_units = sorted(units, key=lambda unit: str(getattr(unit, "id", "")))
    ordered_candidates = list(candidates)
    if not ordered_units:
        return {}, 0
    route_costs: list[list[int]] = []
    costs: list[list[int]] = []
    for unit in ordered_units:
        origin = _position(getattr(unit, "position", None))
        occupied_by_others = _unit_capacity_blocked(
            friendly_occupancy,
            actor_origin=origin,
        )
        row_blocked = blocked | occupied_by_others
        row_routes: list[int] = []
        for candidate in ordered_candidates:
            if origin is None or candidate in occupied_by_others:
                route_cost = PATH_COST_UNREACHABLE
            else:
                route = _complete_route(origin, candidate, row_blocked)
                route_cost = (
                    len(route.path) - 1
                    if route.status == "SUCCESS"
                    and len(route.path) - 1 <= CORE_RAID_MAX_PATH_COST
                    else PATH_COST_UNREACHABLE
                )
            row_routes.append(route_cost)
        route_costs.append(row_routes)
        row = list(row_routes)
        row.extend([IDLE_ASSIGNMENT_COST] * len(ordered_units))
        costs.append(row)
    assignment = _minimum_cost_assignment(costs)
    result: dict[str, Position] = {}
    idle_count = 0
    for row_index, column_index in enumerate(assignment):
        if (
            column_index < 0
            or column_index >= len(ordered_candidates)
            or route_costs[row_index][column_index] >= PATH_COST_UNREACHABLE
        ):
            idle_count += 1
            continue
        result[str(getattr(ordered_units[row_index], "id", ""))] = ordered_candidates[
            column_index
        ]
    return result, idle_count


def _raid_attack_assignments(
    turn: Any,
    memory: TacticMemory,
    core_position: Position,
    target: Position,
    blocked: set[Position],
    danger_cells: set[Position],
    visible_enemies: list[Any],
    friendly_occupancy: dict[Position, int],
) -> tuple[dict[str, Position], set[str], int]:
    vanguards = sorted(
        getattr(turn, "vanguards", ()),
        key=lambda unit: (
            _manhattan(_position(getattr(unit, "position", None)), core_position),
            str(getattr(unit, "id", "")),
        ),
    )
    rangers = sorted(
        getattr(turn, "rangers", ()),
        key=lambda unit: (
            _manhattan(_position(getattr(unit, "position", None)), core_position),
            str(getattr(unit, "id", "")),
        ),
    )
    # The nearest member of each type remains on home defense.
    vanguard_pool = vanguards[1:]
    ranger_pool = rangers[1:]
    enemy_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in visible_enemies
        )
        if position is not None and position != target
    }
    movement_blocked = blocked | danger_cells | enemy_positions
    observer_position = memory.raid.observer_position
    vanguard_candidates = [
        _next_position(target, direction) for direction in CARDINAL_DIRECTIONS
    ]
    vanguard_candidates = [
        position
        for position in vanguard_candidates
        if position not in movement_blocked
        and position != observer_position
    ]
    vanguard_assignments, vanguard_idle = _assign_raid_positions(
        vanguard_pool,
        vanguard_candidates,
        movement_blocked,
        friendly_occupancy,
    )
    reserved = set(vanguard_assignments.values())
    ranger_candidates = [
        position
        for position in _ranger_firing_positions(target, blocked)
        if position not in movement_blocked
        and position != observer_position
        and position not in reserved
        and position != target
    ]
    ranger_assignments, ranger_idle = _assign_raid_positions(
        ranger_pool,
        ranger_candidates,
        movement_blocked | reserved,
        friendly_occupancy,
    )
    assignments = vanguard_assignments | ranger_assignments
    return assignments, set(assignments), vanguard_idle + ranger_idle


def _raid_assignment_invalid_reason(
    raid: CoreRaidPlan,
    combat_by_id: dict[str, Any],
    target: Position,
    blocked: set[Position],
    danger_cells: set[Position],
    enemy_positions: set[Position],
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> str | None:
    if set(raid.assignments) != raid.raid_member_ids:
        return "MISSING_ASSIGNMENT"
    if len(set(raid.assignments.values())) != len(raid.assignments):
        return "DUPLICATE_ASSIGNMENT"
    for member_id in sorted(raid.raid_member_ids):
        unit = combat_by_id.get(member_id)
        assignment = raid.assignments.get(member_id)
        origin = _position(getattr(unit, "position", None))
        if unit is None or assignment is None or origin is None:
            return "MISSING_MEMBER"
        unit_type = getattr(unit, "unit_type", None)
        if unit_type == UnitType.VANGUARD:
            if _manhattan(assignment, target) != 1:
                return "INVALID_ATTACK_CELL"
        elif unit_type == UnitType.RANGER:
            if not _aligned_in_range(assignment, target) or not _path_is_clear(
                assignment,
                target,
                blocked,
            ):
                return "INVALID_ATTACK_CELL"
        else:
            return "INVALID_MEMBER_TYPE"
        if assignment in blocked or assignment in danger_cells or assignment in enemy_positions:
            return "HARD_BLOCKED"
        occupied_by_others = _unit_capacity_blocked(
            friendly_occupancy,
            actor_origin=origin,
        )
        if assignment in occupied_by_others:
            return "FRIENDLY_OCCUPIED"
        avoided = memory.contested_unit_positions(member_id, tick)
        if assignment in avoided:
            return "RECENT_MOVE_FAILURE"
        route_blocked = (
            set(blocked)
            | danger_cells
            | enemy_positions
            | occupied_by_others
            | avoided
        )
        route_blocked.discard(origin)
        route = _complete_route(origin, assignment, route_blocked)
        if (
            route.status != "SUCCESS"
            or len(route.path) - 1 > CORE_RAID_MAX_PATH_COST
        ):
            return "UNREACHABLE"
    return None


def _raid_formation_cost(
    raid: CoreRaidPlan,
    combat_by_id: dict[str, Any],
    blocked: set[Position],
    danger_cells: set[Position],
    enemy_positions: set[Position],
) -> int:
    return sum(
        _estimated_path_cost(
            _position(getattr(combat_by_id[member_id], "position", None)),
            raid.assignments[member_id],
            blocked | danger_cells | enemy_positions,
        )
        for member_id in raid.raid_member_ids
        if member_id in combat_by_id and member_id in raid.assignments
    )


def _raid_target_durability(target: Any, visible_enemies: Iterable[Any]) -> int:
    target_position = _position(getattr(target, "position", None))
    target_id = str(getattr(target, "id", ""))
    durability = int(getattr(target, "hp", 5)) + int(
        getattr(target, "shield", 0) or 0
    )
    durability += sum(
        int(getattr(enemy, "hp", 0))
        for enemy in visible_enemies
        if str(getattr(enemy, "id", "")) != target_id
        and _position(getattr(enemy, "position", None)) == target_position
    )
    return durability


def _raid_retreat_assignments(
    turn: Any,
    memory: TacticMemory,
    target: Position,
    blocked: set[Position],
    danger_cells: set[Position],
    visible_enemies: list[Any],
    friendly_occupancy: dict[Position, int],
) -> dict[str, Position]:
    members = [
        unit
        for unit in tuple(getattr(turn, "vanguards", ()))
        + tuple(getattr(turn, "rangers", ()))
        if str(getattr(unit, "id", "")) in memory.raid.raid_member_ids
    ]
    enemy_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in visible_enemies
        )
        if position is not None
    }
    movement_blocked = blocked | danger_cells | enemy_positions
    positions = [
        position
        for position in _chebyshev_ring_positions(target, 6)
        if position not in movement_blocked
    ]
    assignments, _ = _assign_raid_positions(
        members,
        positions,
        movement_blocked,
        friendly_occupancy,
    )
    return assignments


def _update_core_raid_plan(
    turn: Any,
    memory: TacticMemory,
    config: AgentConfig,
    core: Any,
    core_position: Position | None,
    blocked: set[Position],
    danger_cells: set[Position],
    visible_enemies: list[Any],
    combat_enemies: list[Any],
    threat: ThreatAssessment,
    friendly_occupancy: dict[Position, int],
) -> Any | None:
    tick = int(getattr(turn, "tick", 0))
    raid = memory.raid
    if raid.state == "COOLDOWN":
        if tick >= raid.cooldown_until:
            _clear_raid_plan(memory, tick, cooldown=False)
        return None

    if raid.state == "IDLE":
        if not config.enable_combat or core_position is None:
            return None
        candidates = [
            observation
            for observation in memory.enemy_core_memory.values()
            if observation.state != "MOVING"
        ]
        if not candidates:
            return None
        target = min(
            candidates,
            key=lambda observation: (
                _manhattan(core_position, observation.position),
                observation.id,
            ),
        )
        raid.target_id = target.id
        raid.target_position = target.position
        _set_raid_state(memory, "CORE_TARGET_MEMORY", tick)
        return _visible_enemy_by_id(visible_enemies, raid.target_id)

    target_observation = memory.enemy_core_memory.get(raid.target_id or "")
    visible_target = _visible_enemy_by_id(visible_enemies, raid.target_id)
    if target_observation is None or target_observation.state == "MOVING":
        _abort_raid(memory, tick)
        return visible_target
    if target_observation.position != raid.target_position:
        _abort_raid(memory, tick)
        return visible_target
    target_position = target_observation.position
    raid.target_position = target_position
    enemy_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in visible_enemies
        )
        if position is not None
    }

    if raid.state == "CORE_TARGET_MEMORY":
        selected = _choose_core_observer(
            turn,
            memory,
            target_position,
            blocked,
            danger_cells,
            enemy_positions,
            friendly_occupancy,
        )
        if selected is None:
            return visible_target
        raid.observer_id, raid.observer_position = selected
        memory.clear_resource_intent(raid.observer_id)
        memory.scout_progress.pop(raid.observer_id, None)
        memory.scout_cooldowns.pop(raid.observer_id, None)
        _set_raid_state(memory, "CORE_SCOUTING", tick)
        return visible_target

    workers_by_id = {
        str(getattr(worker, "id", "")): worker
        for worker in getattr(turn, "workers", ())
    }
    combat_units = tuple(getattr(turn, "vanguards", ())) + tuple(
        getattr(turn, "rangers", ())
    )
    combat_by_id = {
        str(getattr(unit, "id", "")): unit for unit in combat_units
    }

    if raid.state == "CORE_SCOUTING":
        observer = workers_by_id.get(raid.observer_id or "")
        observer_position = _position(getattr(observer, "position", None))
        if observer is None or int(getattr(observer, "cargo", 0)) > 0:
            _abort_raid(memory, tick)
            return visible_target
        if (
            visible_target is not None
            and observer_position is not None
            and _chebyshev(observer_position, target_position) <= 3
        ):
            raid.observer_confirmed = True
            _set_raid_state(memory, "CORE_STAGING", tick)
        return visible_target

    if raid.state == "CORE_RECALL":
        if core_position is None:
            return visible_target
        living_members = [
            combat_by_id[member_id]
            for member_id in raid.raid_member_ids
            if member_id in combat_by_id
        ]
        members_home = all(
            _chebyshev(_position(getattr(unit, "position", None)), core_position) <= 3
            for unit in living_members
            if _position(getattr(unit, "position", None)) is not None
        )
        if members_home:
            _clear_raid_plan(memory, tick, cooldown=True)
        return visible_target

    protector_visible = any(
        position is not None and _chebyshev(position, target_position) <= 5
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in combat_enemies
        )
    )
    if threat.level != "NORMAL" or protector_visible:
        _abort_raid(memory, tick)
        return visible_target
    if raid.raid_member_ids and not raid.raid_member_ids <= set(combat_by_id):
        _abort_raid(memory, tick)
        return visible_target
    for member_id in raid.raid_member_ids:
        unit = combat_by_id[member_id]
        hp = int(getattr(unit, "hp", 0))
        if getattr(unit, "unit_type", None) == UnitType.VANGUARD and hp <= 2:
            _abort_raid(memory, tick)
            return visible_target
        if getattr(unit, "unit_type", None) == UnitType.RANGER and hp <= 1:
            _abort_raid(memory, tick)
            return visible_target

    handoff_complete = (
        visible_target is not None
        and any(
            member_id in combat_by_id
            and _position(getattr(combat_by_id[member_id], "position", None)) is not None
            and _chebyshev(
                _position(getattr(combat_by_id[member_id], "position", None)),
                target_position,
            )
            <= 3
            for member_id in raid.raid_member_ids
        )
    )
    if handoff_complete and raid.observer_id is not None:
        raid.observer_id = None
        raid.observer_position = None
        raid.observer_confirmed = False
        raid.replacement_deadline = 0

    if raid.observer_id is not None:
        observer = workers_by_id.get(raid.observer_id)
        observer_position = _position(getattr(observer, "position", None))
        observer_sees_target = (
            observer is not None
            and int(getattr(observer, "cargo", 0)) <= 0
            and observer_position is not None
            and _chebyshev(observer_position, target_position) <= 3
            and visible_target is not None
        )
        if observer_sees_target:
            if raid.replacement_deadline:
                raid.replacement_deadline = 0
                raid.observer_confirmed = True
                raid.assignments.clear()
                raid.raid_member_ids.clear()
        elif raid.observer_confirmed:
            previous_observer = raid.observer_id
            replacement = _choose_core_observer(
                turn,
                memory,
                target_position,
                blocked,
                danger_cells,
                enemy_positions,
                friendly_occupancy,
                max_cost=CORE_OBSERVER_REPLACEMENT_TICKS,
                excluded_ids={previous_observer},
                allowed_modes={"SCOUT", "IDLE"},
            )
            if replacement is None:
                _abort_raid(memory, tick)
                return visible_target
            memory.core_observer_return_ids.add(previous_observer)
            raid.observer_id, raid.observer_position = replacement
            memory.clear_resource_intent(raid.observer_id)
            memory.scout_progress.pop(raid.observer_id, None)
            raid.observer_confirmed = False
            raid.replacement_deadline = tick + CORE_OBSERVER_REPLACEMENT_TICKS
            raid.assignments = _raid_retreat_assignments(
                turn,
                memory,
                target_position,
                blocked,
                danger_cells,
                visible_enemies,
                friendly_occupancy,
            )
        elif raid.replacement_deadline and tick >= raid.replacement_deadline:
            _abort_raid(memory, tick)
            return visible_target

    if raid.replacement_deadline:
        return visible_target

    vanguards = tuple(getattr(turn, "vanguards", ()))
    rangers = tuple(getattr(turn, "rangers", ()))
    roster_ready = (
        len(vanguards) >= 3
        and len(rangers) >= 3
        and all(
            int(getattr(unit, "hp", 0)) >= _unit_max_hp(unit.unit_type)
            for unit in vanguards + rangers
        )
        and core is not None
        and int(getattr(core, "hp", 0)) >= 4
        and int(getattr(core, "shield", 0) or 0) >= 4
        and int(getattr(turn, "resources", 0)) >= UNIT_HEAL_RESOURCE_RESERVE
    )
    if raid.state == "CORE_STAGING":
        if core_position is None:
            _abort_raid(memory, tick)
            return visible_target
        vanguard_ids = {str(getattr(unit, "id", "")) for unit in vanguards}
        ranger_ids = {str(getattr(unit, "id", "")) for unit in rangers}

        def install_staging_assignments(reason: str) -> bool:
            assignments, members, idle_count = _raid_attack_assignments(
                turn,
                memory,
                core_position,
                target_position,
                blocked,
                danger_cells,
                visible_enemies,
                friendly_occupancy,
            )
            if len(members & vanguard_ids) < 2 or len(members & ranger_ids) < 2:
                return False
            raid.assignments = assignments
            raid.raid_member_ids = members
            raid.last_replan_tick = tick
            raid.last_replan_reason = reason
            memory.guard_idle_assignments += idle_count
            return True

        if not raid.assignments and roster_ready:
            install_staging_assignments("INITIAL")

        if raid.raid_member_ids:
            invalid_reason = _raid_assignment_invalid_reason(
                raid,
                combat_by_id,
                target_position,
                blocked,
                danger_cells,
                enemy_positions,
                friendly_occupancy,
                memory,
                tick,
            )
            if invalid_reason is not None and not install_staging_assignments(
                invalid_reason
            ):
                _abort_raid(memory, tick)
                return visible_target

        if not raid.raid_member_ids:
            raid.stalled_ticks += 1
            if raid.stalled_ticks >= CORE_RAID_STALL_TICKS:
                _abort_raid(memory, tick)
            return visible_target

        if all(
            member_id in combat_by_id
            and _position(getattr(combat_by_id[member_id], "position", None))
            == raid.assignments.get(member_id)
            for member_id in raid.raid_member_ids
        ):
            raid.last_durability = (
                _raid_target_durability(visible_target, visible_enemies)
                if visible_target is not None
                else None
            )
            raid.last_formation_cost = 0
            raid.stalled_ticks = 0
            _set_raid_state(memory, "CORE_RAID", tick)
            return visible_target

        formation_cost = _raid_formation_cost(
            raid,
            combat_by_id,
            blocked,
            danger_cells,
            enemy_positions,
        )
        if raid.last_formation_cost is None:
            raid.stalled_ticks = 0
        elif formation_cost < raid.last_formation_cost:
            raid.stalled_ticks = 0
        else:
            raid.stalled_ticks += 1
        raid.last_formation_cost = formation_cost
        if raid.stalled_ticks >= CORE_RAID_STALL_TICKS:
            _abort_raid(memory, tick)
        return visible_target

    if raid.state == "CORE_RAID":
        durability = (
            _raid_target_durability(visible_target, visible_enemies)
            if visible_target is not None
            else None
        )
        formation_cost = _raid_formation_cost(
            raid,
            combat_by_id,
            blocked,
            danger_cells,
            enemy_positions,
        )
        progressed = (
            durability is not None
            and raid.last_durability is not None
            and durability < raid.last_durability
        ) or (
            raid.last_formation_cost is not None
            and formation_cost < raid.last_formation_cost
        )
        raid.stalled_ticks = 0 if progressed else raid.stalled_ticks + 1
        raid.last_durability = durability
        raid.last_formation_cost = formation_cost
        if raid.stalled_ticks >= CORE_RAID_STALL_TICKS:
            _abort_raid(memory, tick)
    return visible_target


def _enemy_danger_cells(
    enemies: Iterable[Any],
    obstacles: set[Position],
) -> set[Position]:
    """Return cells attackable by visible combat enemies on the next Tick."""

    danger_cells: set[Position] = set()
    for enemy in enemies:
        enemy_position = _position(getattr(enemy, "position", None))
        unit_type = getattr(enemy, "unit_type", None)
        if enemy_position is None:
            continue
        if unit_type == UnitType.VANGUARD:
            danger_cells.update(
                _next_position(enemy_position, direction)
                for direction in CARDINAL_DIRECTIONS
            )
            continue
        if unit_type != UnitType.RANGER:
            continue
        for dx, dy in SCOUT_VECTORS:
            for distance in range(1, 4):
                position = (
                    enemy_position[0] + dx * distance,
                    enemy_position[1] + dy * distance,
                )
                if position in obstacles:
                    break
                danger_cells.add(position)
    return danger_cells


def _projected_incoming_damage(
    position: Position,
    enemies: Iterable[Any],
    obstacles: set[Position],
) -> int:
    damage = 0
    for enemy in enemies:
        enemy_position = _position(getattr(enemy, "position", None))
        unit_type = getattr(enemy, "unit_type", None)
        if enemy_position is None:
            continue
        if unit_type == UnitType.VANGUARD:
            damage += int(_manhattan(position, enemy_position) == 1)
        elif (
            unit_type == UnitType.RANGER
            and _aligned_in_range(enemy_position, position)
            and _path_is_clear(enemy_position, position, obstacles)
        ):
            damage += 1
    return damage


def _core_threatening_enemy_ids(
    core_position: Position | None,
    enemies: Iterable[Any],
    obstacles: set[Position],
) -> set[str]:
    if core_position is None:
        return set()
    return {
        str(getattr(enemy, "id", ""))
        for enemy in enemies
        if _projected_incoming_damage(core_position, (enemy,), obstacles) > 0
    }


def _core_is_moving(core: Any) -> bool:
    state = getattr(getattr(core, "view", None), "state", None)
    return state == CoreState.MOVING or str(state).upper().endswith("MOVING")


def _population(turn: Any) -> int:
    state = getattr(turn, "state", None)
    value = getattr(state, "population", None)
    if value is not None:
        return int(value)
    return len(tuple(getattr(turn, "units", ())))


def _resource_capacity(turn: Any) -> int:
    """Read the SDK capacity, with the documented rule as an old-SDK fallback."""

    value = getattr(turn, "resource_capacity", None)
    if value is not None:
        return max(0, int(value))
    return max(10, _population(turn) * 5)


def _resource_space(turn: Any) -> int:
    """Return storage left before this Tick's queued Worker deliveries."""

    value = getattr(turn, "resource_space", None)
    if value is not None:
        return max(0, int(value))
    return max(
        0,
        _resource_capacity(turn) - int(getattr(turn, "resources", 0)),
    )


def _available_resources(turn: Any, pending_delivery: int = 0) -> int:
    """Estimate resources usable by the later Core action in this Turn."""

    stored = max(0, int(getattr(turn, "resources", 0)))
    capacity = _resource_capacity(turn)
    return min(capacity, stored + max(0, int(pending_delivery)))


def _unit_cost(unit_type: UnitType, population: int) -> int:
    """Use the official SDK price helper, retaining a narrow old-SDK fallback."""

    population = max(0, int(population))
    if _sdk_unit_cost is not None:
        return int(_sdk_unit_cost(unit_type, population))

    base_cost = BASE_SPAWN_COSTS[unit_type]
    exponent = 0 if population < 20 else (population - 20) // 5 + 1
    numerator = base_cost * 13**exponent
    denominator = 10**exponent
    return (2 * numerator + denominator) // (2 * denominator)


def _resource_assignments(
    workers: list[Any],
    current_resources: set[Position],
    remembered_resources: set[Position],
    dropped_cargo_resources: set[Position],
    blocked: set[Position],
    memory: TacticMemory,
    tick: int,
) -> dict[str, Position]:
    """Assign distinct resource targets using minimum total route cost."""

    candidates = sorted(
        target
        for target in current_resources | remembered_resources | dropped_cargo_resources
        if not memory.resource_is_cooling_down(target, tick)
    )
    eligible_workers = [
        (worker, _position(getattr(worker, "position", None)))
        for worker in workers
        if int(getattr(worker, "cargo", 0)) <= 0
        and int(getattr(worker, "hp", 0)) > 1
        and str(getattr(worker, "id", "")) not in memory.scout_return_hold_ids
        and (
            str(getattr(worker, "id", "")) not in memory.scout_return_targets
            or (
                _position(getattr(worker, "position", None)) is not None
                and _manhattan(
                    _position(getattr(worker, "position", None)),
                    memory.scout_return_targets[str(getattr(worker, "id", ""))],
                ) <= CARGO_LANE_STAGE_RADII[-1]
            )
        )
        and _position(getattr(worker, "position", None)) is not None
    ]
    if not candidates or not eligible_workers:
        return {}

    route_costs: list[list[int]] = []
    assignment_costs: list[list[int]] = []
    for worker, origin in eligible_workers:
        assert origin is not None
        worker_key = str(getattr(worker, "id", ""))
        worker_route_costs: list[int] = []
        row: list[int] = []
        for target in candidates:
            route_cost = _estimated_path_cost(origin, target, blocked)
            worker_route_costs.append(route_cost)
            cost = route_cost
            if target not in current_resources:
                cost += RESOURCE_MEMORY_PENALTY
            if target in dropped_cargo_resources:
                cost = max(0, cost - DROPPED_CARGO_PRIORITY_BONUS)
            if memory.resource_intents.get(worker_key) == target:
                cost = max(0, cost - RESOURCE_ASSIGNMENT_STICKY_BONUS)
            row.append(cost)
        route_costs.append(worker_route_costs)
        row.extend([IDLE_ASSIGNMENT_COST] * len(eligible_workers))
        assignment_costs.append(row)

    assignment = _minimum_cost_assignment(assignment_costs)
    result: dict[str, Position] = {}
    for row_index, column_index in enumerate(assignment):
        if column_index < 0 or column_index >= len(candidates):
            continue
        if route_costs[row_index][column_index] >= PATH_COST_UNREACHABLE:
            continue
        worker = eligible_workers[row_index][0]
        result[str(getattr(worker, "id", ""))] = candidates[column_index]
    return result


def _core_lane_outward_direction(
    origin: Position,
    core_position: Position,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
) -> Direction | None:
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reservations,
        actor_origin=origin,
    )
    current_distance = _manhattan(origin, core_position)
    candidates: list[tuple[tuple[int, int], Direction]] = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(origin, direction)
        distance = _manhattan(destination, core_position)
        if destination == core_position or distance <= current_distance:
            continue
        if destination in blocked or destination in capacity_blocked:
            continue
        candidates.append(((distance, -index), direction))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _cargo_lane_yield_route(
    origin: Position,
    core_position: Position,
    clearance_positions: set[Position],
    blocked: set[Position],
    unavailable: set[Position],
) -> tuple[Position, ...] | None:
    search_blocked = set(blocked) | set(unavailable) | {core_position}
    search_blocked.discard(origin)
    frontier = deque((origin,))
    parents: dict[Position, Position] = {}
    distances = {origin: 0}
    candidates: list[tuple[int, int, int, int, int, Position]] = []
    current_core_distance = _manhattan(origin, core_position)
    while frontier:
        current = frontier.popleft()
        for direction in CARDINAL_DIRECTIONS:
            destination = _next_position(current, direction)
            if destination in distances or destination in search_blocked:
                continue
            distance_from_origin = distances[current] + 1
            if distance_from_origin > CARGO_LANE_OWNER_ADMISSION_STEPS:
                continue
            distances[destination] = distance_from_origin
            parents[destination] = current
            if destination not in clearance_positions:
                core_distance = _manhattan(destination, core_position)
                candidates.append(
                    (
                        distance_from_origin,
                        int(core_distance < current_core_distance),
                        -core_distance,
                        destination[0],
                        destination[1],
                        destination,
                    )
                )
                continue
            frontier.append(destination)
    if not candidates:
        return None
    target = min(candidates)[-1]
    path = [target]
    while path[-1] != origin:
        path.append(parents[path[-1]])
    path.reverse()
    return tuple(path)


def _cargo_lane_core_release_route(
    origin: Position,
    lane: CargoLanePlan,
    blocked: set[Position],
    unavailable: set[Position],
) -> tuple[Position, ...] | None:
    """Free an occupied Core cell when a closed pocket has no full yield route."""

    if lane.core_position is None or origin != lane.core_position:
        return None
    protected_positions = (
        set(lane.path)
        | set(lane.owner_approach_path)
        | set(lane.heal_approach_path)
    )
    candidates = [
        _next_position(origin, direction)
        for direction in CARDINAL_DIRECTIONS
        if _next_position(origin, direction) not in protected_positions
        and _next_position(origin, direction) not in blocked
        and _next_position(origin, direction) not in unavailable
    ]
    if not candidates:
        return None
    return origin, min(candidates)


def _cargo_lane_temporary_release_route(
    origin: Position,
    lane: CargoLanePlan,
    owner_approach_positions: set[Position],
    blocked: set[Position],
    unavailable: set[Position],
    reserved: set[Position],
    *,
    held: bool = False,
) -> tuple[Position, ...] | None:
    """Move a corridor blocker one cell aside when the pocket has no exit."""

    if lane.core_position is None or origin == lane.core_position:
        return None
    protected_positions = set(lane.path)
    if not held and origin not in owner_approach_positions and origin not in {
        _next_position(lane.core_position, direction)
        for direction in CARDINAL_DIRECTIONS
    }:
        return None
    candidates = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(origin, direction)
        if (
            destination in protected_positions
            or destination in owner_approach_positions
            or destination in blocked
            or destination in unavailable
            or destination in reserved
        ):
            continue
        candidates.append(
            (
                int(destination not in owner_approach_positions),
                int(destination not in {
                    _next_position(lane.core_position, neighbor_direction)
                    for neighbor_direction in CARDINAL_DIRECTIONS
                }),
                _manhattan(destination, lane.core_position),
                -index,
                destination,
            )
        )
    if not candidates:
        # A one-cell sidestep can be unavailable in a dense Core apron. Find
        # a short route to an actually open cell instead of placing the
        # blocker on the admitted owner's approach path.
        search_blocked = (
            set(protected_positions)
            | set(owner_approach_positions)
            | set(blocked)
            | set(unavailable)
            | set(reserved)
        )
        search_blocked.discard(origin)
        frontier = deque((origin,))
        parents: dict[Position, Position] = {}
        distances = {origin: 0}
        alternatives: list[tuple[int, int, int, int, Position]] = []
        while frontier:
            current = frontier.popleft()
            distance = distances[current]
            if distance >= CARGO_LANE_OWNER_ADMISSION_STEPS:
                continue
            for index, direction in enumerate(CARDINAL_DIRECTIONS):
                destination = _next_position(current, direction)
                if destination in distances or destination in search_blocked:
                    continue
                distances[destination] = distance + 1
                parents[destination] = current
                alternatives.append(
                    (
                        int(destination not in {
                            _next_position(lane.core_position, neighbor_direction)
                            for neighbor_direction in CARDINAL_DIRECTIONS
                        }),
                        _manhattan(destination, lane.core_position),
                        -distance,
                        -index,
                        destination,
                    )
                )
                frontier.append(destination)
        if not alternatives:
            return None
        target = max(alternatives)[-1]
        route = [target]
        while route[-1] != origin:
            route.append(parents[route[-1]])
        route.reverse()
        return tuple(route)
    return origin, max(candidates)[-1]


def _cargo_lane_assigned_yield_destination(
    unit_id: str,
    origin: Position,
    lane: CargoLanePlan,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
) -> Position | None:
    route = lane.yield_paths.get(unit_id, ())
    if origin not in route:
        return None
    path_index = route.index(origin)
    if path_index + 1 >= len(route):
        return None
    destination = route[path_index + 1]
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reservations,
        actor_origin=origin,
    )
    if destination in blocked or destination in capacity_blocked:
        return None
    return destination


def _cargo_lane_yield_direction(
    origin: Position,
    core_position: Position,
    lane_path: set[Position],
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
) -> Direction | None:
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reservations,
        actor_origin=origin,
    )
    current_distance = _manhattan(origin, core_position)
    candidates: list[tuple[tuple[int, int, int], Direction]] = []
    for index, direction in enumerate(CARDINAL_DIRECTIONS):
        destination = _next_position(origin, direction)
        if destination == core_position or destination in lane_path:
            continue
        if destination in blocked or destination in capacity_blocked:
            continue
        distance = _manhattan(destination, core_position)
        candidates.append(
            (
                (
                    int(distance >= current_distance),
                    distance,
                    -index,
                ),
                direction,
            )
        )
    if not candidates:
        search_blocked = set(blocked) | capacity_blocked | {core_position}
        search_blocked.discard(origin)
        frontier = deque((origin,))
        visited = {origin}
        first_direction: dict[Position, Direction] = {}
        max_distance = 4
        while frontier:
            current = frontier.popleft()
            for direction in CARDINAL_DIRECTIONS:
                destination = _next_position(current, direction)
                if destination in visited or destination in search_blocked:
                    continue
                if _manhattan(destination, origin) > max_distance:
                    continue
                visited.add(destination)
                first_direction[destination] = first_direction.get(
                    current,
                    direction,
                )
                if destination not in lane_path:
                    return first_direction[destination]
                frontier.append(destination)
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _queue_cargo_lane_owner_step(
    worker: Any,
    lane: CargoLanePlan,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> bool:
    origin = _position(getattr(worker, "position", None))
    worker_id = str(getattr(worker, "id", ""))
    if origin is None or not worker_id or lane.gateway is None:
        return False
    if origin not in lane.path:
        if lane.owner_approach_path and origin in lane.owner_approach_path:
            path_index = lane.owner_approach_path.index(origin)
            if path_index + 1 < len(lane.owner_approach_path):
                destination = lane.owner_approach_path[path_index + 1]
                capacity_blocked = _unit_capacity_blocked(
                    friendly_occupancy,
                    reservations,
                    actor_origin=origin,
                )
                if destination not in blocked and destination not in capacity_blocked:
                    direction = _direction_between(origin, destination)
                    if direction is not None:
                        _reserve_destination(reservations, destination)
                        worker.move(direction)
                        memory.note_worker_move(worker_id, destination)
                        return True
                memory.route_diagnostics[worker_id] = (
                    "CAPACITY_BLOCKED"
                    if destination in capacity_blocked
                    else "CARGO_LANE_BLOCKED"
                )
                return False
        moved, _, _ = _queue_worker_route(
            worker,
            lane.gateway,
            "CARGO_LANE_APPROACH",
            blocked,
            reservations,
            friendly_occupancy,
            memory,
        )
        return moved
    path_index = lane.path.index(origin)
    if path_index <= 0:
        return False
    destination = lane.path[path_index - 1]
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reservations,
        actor_origin=origin,
    )
    if destination in blocked or destination in capacity_blocked:
        memory.route_diagnostics[worker_id] = (
            "CAPACITY_BLOCKED"
            if destination in capacity_blocked
            else "CARGO_LANE_BLOCKED"
        )
        return False
    direction = _direction_between(origin, destination)
    if direction is None:
        return False
    _reserve_destination(reservations, destination)
    worker.move(direction)
    memory.note_worker_move(worker_id, destination)
    return True


def _queue_cargo_lane_egress_step(
    worker: Any,
    lane: CargoLanePlan,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
) -> bool:
    origin = _position(getattr(worker, "position", None))
    worker_id = str(getattr(worker, "id", ""))
    if origin is None or not worker_id or not lane.egress_path:
        return False
    try:
        path_index = lane.egress_path.index(origin)
    except ValueError:
        return False
    if path_index + 1 >= len(lane.egress_path):
        return False
    destination = lane.egress_path[path_index + 1]
    capacity_blocked = _unit_capacity_blocked(
        friendly_occupancy,
        reservations,
        actor_origin=origin,
    )
    if destination in blocked or destination in capacity_blocked:
        memory.route_diagnostics[worker_id] = (
            "CAPACITY_BLOCKED"
            if destination in capacity_blocked
            else "CARGO_LANE_BLOCKED"
        )
        return False
    direction = _direction_between(origin, destination)
    if direction is None:
        return False
    _reserve_destination(reservations, destination)
    worker.move(direction)
    memory.note_worker_move(worker_id, destination)
    return True


def _queue_cargo_lane_defender_yield(
    unit: Any,
    lane: CargoLanePlan,
    blocked: set[Position],
    reservations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> bool:
    origin = _position(getattr(unit, "position", None))
    unit_id = str(getattr(unit, "id", ""))
    if (
        origin is None
        or not unit_id
        or unit_id not in lane.yield_unit_ids
        or lane.core_position is None
    ):
        return False
    destination = _cargo_lane_assigned_yield_destination(
        unit_id,
        origin,
        lane,
        blocked,
        reservations,
        friendly_occupancy,
    )
    direction = (
        _direction_between(origin, destination)
        if destination is not None
        else None
    )
    if destination is None or direction is None:
        # A dense Core pocket may have no complete route to an open component.
        # Release one corridor cell first so the admitted healer can advance;
        # the next Tick will republish a complete route after occupancy clears.
        protected_positions = (
            set(lane.path)
            | set(lane.egress_path)
            | set(lane.owner_approach_path)
            | set(lane.heal_approach_path)
        )
        fallback_direction = _cargo_lane_yield_direction(
            origin,
            lane.core_position,
            protected_positions,
            blocked,
            reservations,
            friendly_occupancy,
        )
        if fallback_direction is not None:
            destination = _next_position(origin, fallback_direction)
            direction = fallback_direction
    if destination is None or direction is None:
        return False
    _reserve_destination(reservations, destination)
    memory.note_unit_move(unit_id, destination, destination, None)
    unit.move(direction)
    return True


def _update_core_lane_clearance(
    workers: list[Any],
    core_position: Position | None,
    worker_blocked: set[Position],
    friendly_occupancy: dict[Position, int],
    core_accepts_delivery: bool,
    memory: TacticMemory,
) -> None:
    workers_by_id = {str(getattr(worker, "id", "")): worker for worker in workers}
    if memory.cargo_lane.active:
        memory.core_lane_departing_worker_id = None
        memory.core_lane_yield_worker_ids.clear()
        return
    if core_position is None or not core_accepts_delivery:
        memory.core_lane_departing_worker_id = None
        memory.core_lane_yield_worker_ids.clear()
        return

    departing_id = memory.core_lane_departing_worker_id
    departing_worker = workers_by_id.get(departing_id) if departing_id else None
    departing_position = _position(getattr(departing_worker, "position", None))
    if (
        departing_worker is None
        or int(getattr(departing_worker, "cargo", 0)) > 0
        or departing_position is None
        or _manhattan(departing_position, core_position) > 1
    ):
        memory.core_lane_departing_worker_id = None
        memory.core_lane_yield_worker_ids.clear()
        departing_worker = None

    exit_cells = {
        _next_position(core_position, direction)
        for direction in CARDINAL_DIRECTIONS
        if _next_position(core_position, direction) not in worker_blocked
    }
    cargo_workers = [
        worker for worker in workers if int(getattr(worker, "cargo", 0)) > 0
    ]
    if departing_worker is None and cargo_workers and exit_cells:
        empty_core_workers = sorted(
            (
                worker
                for worker in workers
                if int(getattr(worker, "cargo", 0)) == 0
                and _position(getattr(worker, "position", None)) == core_position
                and str(getattr(worker, "id", "")) != memory.heal_intent_id
            ),
            key=lambda worker: str(getattr(worker, "id", "")),
        )
        blocking_cargo = [
            worker
            for worker in cargo_workers
            if _position(getattr(worker, "position", None)) in exit_cells
        ]
        if empty_core_workers and blocking_cargo:
            departing_worker = empty_core_workers[0]
            departing_id = str(getattr(departing_worker, "id", ""))
            memory.core_lane_departing_worker_id = departing_id
            memory.core_lane_yield_worker_ids = {
                str(getattr(worker, "id", "")) for worker in blocking_cargo
            }
            memory.clear_resource_intent(departing_id)
            memory.worker_routes.pop(departing_id, None)

    if departing_worker is None:
        return
    cargo_ids = {str(getattr(worker, "id", "")) for worker in cargo_workers}
    memory.core_lane_yield_worker_ids.intersection_update(cargo_ids)
    memory.core_lane_yield_worker_ids.update(
        str(getattr(worker, "id", ""))
        for worker in cargo_workers
        if _position(getattr(worker, "position", None)) in exit_cells
    )


def _plan_workers(
    turn: Any,
    core: Any,
    blocked: set[Position],
    beacon: Position | None,
    memory: TacticMemory,
    resource_memory_ttl: int,
    combat_enemies: list[Any],
    visible_enemies: list[Any],
    danger_cells: set[Position],
    friendly_occupancy: dict[Position, int],
    core_will_move: bool = False,
) -> int:
    """Queue Worker actions and return resources delivered before Core acts."""

    tick = int(getattr(turn, "tick", 0))
    core_position = _position(getattr(core, "position", None))
    obstacles = set(blocked)
    enemy_occupied = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in visible_enemies
        )
        if position is not None
    }
    enemy_occupied.update(
        observation.position for observation in memory.enemy_core_memory.values()
    )
    worker_blocked = obstacles | danger_cells | enemy_occupied
    worker_return_blocked = worker_blocked | memory.recent_worker_return_danger(tick)
    pending_delivery = 0
    delivery_space = _resource_space(turn)
    core_accepts_delivery = (
        core_position is not None
        and not _core_is_moving(core)
        and not core_will_move
    )
    current_resources = _positions(getattr(turn, "resource_cells", ()))
    remembered_resources = memory.recent_resource_targets(tick, resource_memory_ttl)
    dropped_cargo_resources = memory.recent_dropped_cargo_targets(
        tick,
        resource_memory_ttl,
    )
    available_resources = current_resources | remembered_resources | dropped_cargo_resources

    workers = sorted(getattr(turn, "workers", ()), key=lambda worker: str(worker.id))
    for worker in workers:
        worker_position = _position(getattr(worker, "position", None))
        cargo = int(getattr(worker, "cargo", 0))
        worker_key = str(getattr(worker, "id", ""))

        if cargo > 0:
            memory.clear_resource_intent(worker_key)
            continue

        previous_target = memory.resource_intents.get(worker_key)
        if previous_target is None:
            pass
        elif (
            previous_target not in available_resources
            or memory.resource_is_cooling_down(previous_target, tick)
        ):
            memory.clear_resource_intent(worker_key)
        elif worker_position == previous_target:
            if previous_target not in current_resources:
                memory.forget_resource(previous_target)
                memory.clear_resource_intent(worker_key)
        elif worker_position is not None:
            route_cost = _estimated_path_cost(
                worker_position,
                previous_target,
                worker_blocked,
            )
            if (
                route_cost >= PATH_COST_UNREACHABLE
                and previous_target not in current_resources
            ):
                memory.resource_cooldowns[previous_target] = max(
                    memory.resource_cooldowns.get(previous_target, 0),
                    tick + RESOURCE_COOLDOWN_TICKS,
                )
                memory.forget_resource(previous_target)
                memory.clear_resource_intent(worker_key)
            else:
                memory.note_resource_progress(
                    worker_key,
                    previous_target,
                    route_cost,
                    tick,
                )

    # A depleted remembered target may have been removed during the pass above.
    remembered_resources = memory.recent_resource_targets(tick, resource_memory_ttl)
    dropped_cargo_resources = memory.recent_dropped_cargo_targets(
        tick,
        resource_memory_ttl,
    )
    assignments = _resource_assignments(
        workers,
        current_resources,
        remembered_resources,
        dropped_cargo_resources,
        worker_blocked,
        memory,
        tick,
    )
    reserved_destinations: dict[Position, int] = {}
    reserved_exploration_targets: set[Position] = set()
    reserved_exploration_vectors: set[int] = set()
    _update_core_lane_clearance(
        workers,
        core_position,
        worker_blocked,
        friendly_occupancy,
        core_accepts_delivery,
        memory,
    )
    worker_slots = {
        str(getattr(worker, "id", "")): slot for slot, worker in enumerate(workers)
    }
    planning_workers = sorted(
        workers,
        key=lambda worker: (
            (
                0
                if str(getattr(worker, "id", ""))
                == memory.cargo_lane.departing_worker_id
                else 1
                if str(getattr(worker, "id", ""))
                in memory.cargo_lane.startup_pending_ids
                else 2
                if str(getattr(worker, "id", ""))
                in memory.cargo_lane.yield_worker_ids
                else 3
                if str(getattr(worker, "id", ""))
                == memory.cargo_lane.owner_id
                else 4
                if str(getattr(worker, "id", ""))
                == memory.core_lane_departing_worker_id
                else 5
            ),
            str(getattr(worker, "id", "")),
        ),
    )

    for worker in planning_workers:
        worker_position = _position(getattr(worker, "position", None))
        cargo = int(getattr(worker, "cargo", 0))
        worker_key = str(getattr(worker, "id", ""))
        worker_slot = worker_slots[worker_key]
        contested_positions = memory.contested_worker_positions(worker_key, tick)
        injured_far_from_core = bool(
            cargo == 0
            and core_position is not None
            and worker_position is not None
            and int(getattr(worker, "hp", 0)) < _unit_max_hp(UnitType.WORKER)
            and _manhattan(worker_position, core_position)
            > SCOUT_INJURED_MAX_DISTANCE
        )
        if injured_far_from_core:
            memory.begin_scout_return(worker_key, core_position)

        if worker_position is not None and combat_enemies:
            nearest_enemy_distance = min(
                _manhattan(worker_position, enemy_position)
                for enemy_position in (
                    _position(getattr(enemy, "position", None))
                    for enemy in combat_enemies
                )
                if enemy_position is not None
            )
            projected_damage = _projected_incoming_damage(
                worker_position,
                combat_enemies,
                obstacles,
            )
            if nearest_enemy_distance <= WORKER_EVADE_DISTANCE or projected_damage > 0:
                if (
                    core_position is not None
                    and worker_position is not None
                    and _manhattan(worker_position, core_position) > 3
                ):
                    memory.begin_scout_return(worker_key, core_position)
                escape_blocked = (
                    obstacles
                    | enemy_occupied
                    | contested_positions
                    | _unit_capacity_blocked(
                        friendly_occupancy,
                        reserved_destinations,
                        actor_origin=worker_position,
                    )
                )
                direction = _worker_escape_direction(
                    worker_position,
                    combat_enemies,
                    obstacles,
                    escape_blocked,
                    core_position,
                    int(getattr(worker, "hp", 0)),
                )
                if direction is not None:
                    _reserve_destination(
                        reserved_destinations,
                        _next_position(worker_position, direction),
                    )
                    worker.move(direction)
                    memory.note_worker_move(
                        worker_key,
                        _next_position(worker_position, direction),
                    )
                    continue
                if core_position is not None and worker_position != core_position:
                    moved, _, _ = _queue_worker_route(
                        worker,
                        core_position,
                        "COMBAT_ESCAPE",
                        worker_blocked,
                        reserved_destinations,
                        friendly_occupancy,
                        memory,
                    )
                    if moved:
                        continue
                worker.wait()
                continue

        if _healing_worker_lane_access_ready(turn, memory, worker_key):
            if worker_position == core_position:
                if memory.core_visit.unit_id in {None, worker_key}:
                    memory.core_visit = CoreVisit(
                        worker_key, "HEAL", tick, reached_core=True
                    )
                    worker.heal()
                else:
                    worker.wait()
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    core_position,
                    "HEAL",
                    worker_return_blocked | contested_positions,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if not moved:
                    worker.wait()
            continue

        if worker_key == memory.heal_intent_id and memory.cargo_lane.active:
            stage_target = _refresh_healing_worker_stage_target(
                turn,
                core_position,
                worker_return_blocked | contested_positions,
                danger_cells,
                friendly_occupancy,
                memory,
            )
            if stage_target is not None and worker_position != stage_target:
                moved, _, _ = _queue_worker_route(
                    worker,
                    stage_target,
                    "HEAL_STAGE",
                    worker_return_blocked | contested_positions,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if moved:
                    continue
            # A healing Worker must wait outside the active corridor. It
            # will be routed to Core after the half-duplex owner clears.
            worker.wait()
            continue

        if injured_far_from_core:
            moved, _, _ = _queue_worker_route(
                worker,
                core_position,
                "INJURED_RETURN",
                worker_return_blocked | contested_positions,
                reserved_destinations,
                friendly_occupancy,
                memory,
            )
            if not moved:
                worker.wait()
            continue

        lane = memory.cargo_lane
        if (
            lane.active
            and worker_key == lane.departing_worker_id
            and worker_position is not None
        ):
            moved = _queue_cargo_lane_egress_step(
                worker,
                lane,
                worker_return_blocked | contested_positions,
                reserved_destinations,
                friendly_occupancy,
                memory,
            )
            if not moved:
                worker.wait()
            continue

        if lane.active and worker_key in lane.startup_pending_ids:
            worker.wait()
            continue

        if (
            worker_key == memory.core_lane_departing_worker_id
            and worker_position is not None
            and core_position is not None
        ):
            direction = _core_lane_outward_direction(
                worker_position,
                core_position,
                worker_blocked | contested_positions,
                reserved_destinations,
                friendly_occupancy,
            )
            if direction is not None:
                destination = _next_position(worker_position, direction)
                _reserve_destination(reserved_destinations, destination)
                worker.move(direction)
                memory.note_worker_move(worker_key, destination)
            else:
                worker.wait()
            continue

        if worker_key in memory.core_lane_yield_worker_ids:
            if (
                worker_position is not None
                and core_position is not None
                and _manhattan(worker_position, core_position) <= 1
            ):
                direction = _core_lane_outward_direction(
                    worker_position,
                    core_position,
                    worker_blocked | contested_positions,
                    reserved_destinations,
                    friendly_occupancy,
                )
                if direction is not None:
                    destination = _next_position(worker_position, direction)
                    _reserve_destination(reserved_destinations, destination)
                    worker.move(direction)
                    memory.note_worker_move(worker_key, destination)
                    continue
            worker.wait()
            continue

        if (
            lane.active
            and lane.core_position is not None
            and worker_key in lane.yield_worker_ids
            and worker_position is not None
        ):
            destination = _cargo_lane_assigned_yield_destination(
                worker_key,
                worker_position,
                lane,
                worker_return_blocked | contested_positions,
                reserved_destinations,
                friendly_occupancy,
            )
            direction = (
                _direction_between(worker_position, destination)
                if destination is not None
                else None
            )
            if destination is not None and direction is not None:
                _reserve_destination(reserved_destinations, destination)
                worker.move(direction)
                memory.note_worker_move(worker_key, destination)
            else:
                worker.wait()
            continue

        if (
            lane.active
            and cargo > 0
            and worker_position is not None
            and core_position is not None
            and not (worker_key == lane.owner_id and worker_position == core_position)
        ):
            if worker_key == lane.owner_id:
                moved = _queue_cargo_lane_owner_step(
                    worker,
                    lane,
                    worker_return_blocked | contested_positions,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
            else:
                stage_target = lane.stage_targets.get(worker_key)
                if stage_target is None or worker_position == stage_target:
                    worker.wait()
                    continue
                moved, _, _ = _queue_worker_route(
                    worker,
                    stage_target,
                    "CARGO_STAGE",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
            if not moved:
                worker.wait()
            continue

        if cargo > 0:
            recovery = memory.cargo_recovery.setdefault(worker_key, CargoRecovery())
            if core_accepts_delivery and worker_position == core_position:
                # The worker already occupying the Core cell must be allowed to
                # finish its operation. A remote HEAL intent is only an entry
                # reservation and cannot veto this deposit.
                physical_visit_blocked = (
                    memory.core_visit.unit_id is not None
                    and memory.core_visit.unit_id != worker_key
                    and (
                        memory.core_visit.purpose != "HEAL"
                        or memory.core_visit.reached_core
                    )
                )
                if physical_visit_blocked:
                    worker.wait()
                elif delivery_space > 0:
                    memory.core_visit = CoreVisit(worker_key, "DEPOSIT", tick)
                    worker.deposit()
                    delivered = min(cargo, delivery_space)
                    pending_delivery += delivered
                    delivery_space -= delivered
                else:
                    worker.wait()
                memory.cargo_recovery.pop(worker_key, None)
                memory.worker_routes.pop(worker_key, None)
            elif core_position is None or worker_position is None:
                worker.wait()
            elif recovery.target is not None:
                if (
                    worker_position == recovery.target
                    or tick - recovery.selected_at_tick >= CARGO_RECOVERY_RETARGET_TICKS
                ):
                    recovery.target = None
                    memory.worker_routes.pop(worker_key, None)
                    moved, route_status, route_result = _queue_worker_route(
                        worker,
                        core_position,
                        "CARGO_RETURN",
                        worker_return_blocked,
                        reserved_destinations,
                        friendly_occupancy,
                        memory,
                    )
                    if route_status == "UNREACHABLE":
                        recovery.unreachable_ticks = max(
                            CARGO_UNREACHABLE_TICKS,
                            recovery.unreachable_ticks,
                        )
                        recovery.target = _cargo_recovery_boundary(
                            route_result.explored if route_result is not None else (),
                            core_position,
                            worker_return_blocked,
                            memory.recent_worker_positions(worker_key),
                        )
                        recovery.selected_at_tick = tick
                        if recovery.target is None:
                            memory.route_diagnostics[worker_key] = "CARGO_ENCLOSED"
                        elif recovery.target != worker_position:
                            memory.worker_routes.pop(worker_key, None)
                            moved, _, _ = _queue_worker_route(
                                worker,
                                recovery.target,
                                "CARGO_RECOVERY",
                                worker_return_blocked,
                                reserved_destinations,
                                friendly_occupancy,
                                memory,
                            )
                    else:
                        recovery.unreachable_ticks = 0
                    if not moved:
                        worker.wait()
                else:
                    moved, _, _ = _queue_worker_route(
                        worker,
                        recovery.target,
                        "CARGO_RECOVERY",
                        worker_return_blocked,
                        reserved_destinations,
                        friendly_occupancy,
                        memory,
                    )
                    if not moved:
                        worker.wait()
            else:
                moved, route_status, route_result = _queue_worker_route(
                    worker,
                    core_position,
                    "CARGO_RETURN",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if route_status == "UNREACHABLE":
                    recovery.unreachable_ticks += 1
                    if recovery.unreachable_ticks >= CARGO_UNREACHABLE_TICKS:
                        recovery.target = _cargo_recovery_boundary(
                            route_result.explored if route_result is not None else (),
                            core_position,
                            worker_return_blocked,
                            memory.recent_worker_positions(worker_key),
                        )
                        recovery.selected_at_tick = tick
                        if recovery.target is None:
                            memory.route_diagnostics[worker_key] = "CARGO_ENCLOSED"
                else:
                    recovery.unreachable_ticks = 0
                if not moved:
                    worker.wait()
            continue

        memory.cargo_recovery.pop(worker_key, None)

        if worker_key in memory.core_observer_return_ids:
            if (
                core_position is not None
                and worker_position is not None
                and _manhattan(worker_position, core_position) <= 3
            ):
                memory.core_observer_return_ids.discard(worker_key)
                memory.scout_cooldowns[worker_key] = tick + 3
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    core_position,
                    "CORE_RECALL",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if not moved:
                    worker.wait()
                continue

        if (
            cargo == 0
            and int(getattr(worker, "hp", 0)) <= 1
            and core_position is not None
            and worker_position is not None
            and _manhattan(worker_position, core_position) > 3
        ):
            memory.begin_scout_return(worker_key, core_position)

        # A critical Worker may be held at the outer staging ring by the
        # return-state machine. HEAL intent reserves entry priority, but does
        # not own the physical Core slot until the Worker reaches that cell.
        if (
            worker_key == memory.heal_intent_id
            and core_position is not None
            and memory.core_visit_forced_purpose != "DEPOSIT"
        ):
            if worker_position == core_position:
                if memory.core_visit.unit_id in {None, worker_key}:
                    memory.core_visit = CoreVisit(
                        worker_key, "HEAL", tick, reached_core=True
                    )
                    worker.heal()
                else:
                    worker.wait()
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    core_position,
                    "HEAL",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if not moved:
                    worker.wait()
            continue

        target = assignments.get(worker_key)
        return_target = memory.scout_return_targets.get(worker_key)
        local_return_resource = (
            target is not None
            and return_target is not None
            and worker_position is not None
            and _manhattan(worker_position, return_target) <= CARGO_LANE_STAGE_RADII[-1]
        )
        if local_return_resource:
            route_cost = _estimated_path_cost(worker_position, target, blocked)
            memory.set_resource_intent(worker_key, target, route_cost)
            if worker_position == target:
                if target in current_resources:
                    worker.harvest()
                    memory.clear_resource_intent(worker_key)
                    continue
                memory.forget_resource(target)
                memory.clear_resource_intent(worker_key)
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    target,
                    "RESOURCE",
                    worker_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if moved:
                    continue

        if return_target is not None and worker_position is not None:
            route_cost = _estimated_path_cost(
                worker_position,
                return_target,
                worker_return_blocked,
            )
            at_safe_staging = (
                core_position is not None
                and _chebyshev(
                    worker_position,
                    return_target,
                ) == CARGO_LANE_STAGE_RADII[-1]
                and _complete_route(
                    worker_position,
                    return_target,
                    worker_return_blocked,
                ).status
                == "SUCCESS"
            )
            if _manhattan(worker_position, return_target) <= 3 or at_safe_staging:
                if int(getattr(worker, "hp", 0)) <= 1:
                    memory.release_scout_return(worker_key, tick, hold=True)
                    worker.wait()
                    continue
                memory.clear_scout_return(worker_key, tick)
            else:
                stalled = memory.note_scout_return_progress(
                    worker_key,
                    return_target,
                    route_cost,
                    tick,
                )
                if stalled:
                    memory.release_scout_return(
                        worker_key,
                        tick,
                        hold=int(getattr(worker, "hp", 0)) <= 1,
                    )
                    worker.wait()
                    continue
                moved, _, _ = _queue_worker_route(
                    worker,
                    return_target,
                    "SCOUT_RETURN",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if moved:
                    continue
                worker.wait()
                continue

        if worker_key in memory.scout_return_hold_ids:
            if worker_key == memory.heal_intent_id and core_position is not None:
                if worker_position == core_position:
                    if memory.core_visit.unit_id in {None, worker_key}:
                        memory.core_visit = CoreVisit(
                            worker_key, "HEAL", tick, reached_core=True
                        )
                        worker.heal()
                    else:
                        worker.wait()
                else:
                    moved, _, _ = _queue_worker_route(
                        worker,
                        core_position,
                        "HEAL",
                        worker_return_blocked,
                        reserved_destinations,
                        friendly_occupancy,
                        memory,
                    )
                    if not moved:
                        worker.wait()
            else:
                worker.wait()
            continue

        if (
            worker_key == memory.heal_intent_id
            and core_position is not None
            and memory.core_visit_forced_purpose != "DEPOSIT"
        ):
            if worker_position == core_position:
                if memory.core_visit.unit_id in {None, worker_key}:
                    memory.core_visit = CoreVisit(
                        worker_key, "HEAL", tick, reached_core=True
                    )
                    worker.heal()
                else:
                    worker.wait()
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    core_position,
                    "HEAL",
                    worker_return_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if not moved:
                    worker.wait()
            continue

        if (
            memory.raid.observer_id == worker_key
            and memory.raid.state in {"CORE_SCOUTING", "CORE_STAGING"}
        ):
            moved, _, _ = _queue_worker_route(
                worker,
                memory.raid.observer_position,
                "CORE_OBSERVER",
                worker_blocked,
                reserved_destinations,
                friendly_occupancy,
                memory,
            )
            if not moved:
                worker.wait()
            continue

        target = assignments.get(worker_key)
        if target is not None and worker_position is not None:
            route_cost = _estimated_path_cost(worker_position, target, blocked)
            memory.set_resource_intent(worker_key, target, route_cost)
            if worker_position == target:
                if target in current_resources:
                    worker.harvest()
                    memory.clear_resource_intent(worker_key)
                    continue
                memory.forget_resource(target)
                memory.clear_resource_intent(worker_key)
            else:
                moved, _, _ = _queue_worker_route(
                    worker,
                    target,
                    "RESOURCE",
                    worker_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                )
                if moved:
                    continue

        if beacon is not None:
            if worker_position == beacon:
                worker.pickup_beacon()
                continue
            moved, _, _ = _queue_worker_route(
                worker,
                beacon,
                "BEACON",
                worker_blocked,
                reserved_destinations,
                friendly_occupancy,
                memory,
            )
            if moved:
                continue

        if memory.scout_is_cooling_down(worker_key, tick):
            worker.wait()
            continue

        if core_position is None or worker_position is None:
            worker.wait()
            continue

        # Keep rotating through coverage targets when a route is blocked or
        # makes no progress. This prevents one bad scout waypoint from idling
        # the Worker indefinitely.
        max_attempts = SCOUT_MAX_TARGET_ATTEMPTS
        moved = False
        for _ in range(max_attempts):
            progress = memory.scout_progress.get(worker_key)
            progress_vector = (
                _scout_vector_index(core_position, progress.target)
                if progress is not None
                else None
            )
            if (
                progress is not None
                and progress.target not in reserved_exploration_targets
                and (
                    progress_vector is None
                    or progress_vector not in reserved_exploration_vectors
                )
            ):
                # Keep a route stable across Ticks. Re-selecting the frontier
                # after every one-step move can make Workers swap routes.
                exploration_target = progress.target
            else:
                if progress is not None:
                    # A previous route can become occupied by an earlier
                    # Worker after that Worker advances to a new waypoint.
                    # Release the duplicate before choosing a new direction.
                    memory.advance_exploration(
                        worker.id,
                        worker_slot,
                        rotate=True,
                    )
                exploration_target = memory.exploration_target(
                    worker.id,
                    core_position,
                    worker_slot,
                    worker_position,
                    tick,
                    reserved_exploration_targets,
                    reserved_exploration_vectors,
                )
            reserved_exploration_targets.add(exploration_target)
            if worker_position == exploration_target:
                memory.advance_exploration(worker.id, worker_slot)
                continue
            if (
                int(getattr(worker, "hp", 0)) < _unit_max_hp(UnitType.WORKER)
                and _manhattan(worker_position, exploration_target)
                > SCOUT_INJURED_MAX_DISTANCE
            ):
                memory.advance_exploration(worker.id, worker_slot, rotate=True)
                continue
            scout_blocked = worker_blocked | memory.recent_worker_return_danger(tick)
            route_cost = _estimated_path_cost(
                worker_position,
                exploration_target,
                scout_blocked,
            )
            if route_cost >= PATH_COST_UNREACHABLE:
                memory.advance_exploration(
                    worker.id,
                    worker_slot,
                    rotate=True,
                )
                continue
            if memory.note_scout_progress(
                worker.id,
                exploration_target,
                route_cost,
                worker_position,
            ):
                memory.advance_exploration(
                    worker.id,
                    worker_slot,
                    rotate=True,
                )
                continue
            route_moved, route_status, _ = _queue_worker_route(
                worker,
                exploration_target,
                "SCOUT",
                scout_blocked,
                reserved_destinations,
                friendly_occupancy,
                memory,
            )
            if route_moved:
                exploration_vector = _scout_vector_index(
                    core_position,
                    exploration_target,
                )
                if exploration_vector is not None:
                    reserved_exploration_vectors.add(exploration_vector)
                moved = True
                break
            if route_status in {"BUDGET_EXCEEDED", "CAPACITY_BLOCKED"}:
                break
            memory.advance_exploration(
                worker.id,
                worker_slot,
                rotate=True,
            )
        if not moved:
            if worker_position == core_position:
                direction = _core_lane_outward_direction(
                    worker_position,
                    core_position,
                    worker_blocked | contested_positions,
                    reserved_destinations,
                    friendly_occupancy,
                )
                if direction is not None:
                    destination = _next_position(worker_position, direction)
                    memory.note_scout_progress(
                        worker_key,
                        destination,
                        1,
                        worker_position,
                    )
                    _reserve_destination(reserved_destinations, destination)
                    worker.move(direction)
                    memory.note_worker_move(worker_key, destination)
                    moved = True
            if not moved:
                worker.wait()

    return pending_delivery


def _queue_healing_defender_action(
    unit: Any,
    core_position: Position | None,
    movement_blocked: set[Position],
    reserved_destinations: Any,
    friendly_occupancy: dict[Position, int],
    memory: TacticMemory,
    tick: int,
) -> bool:
    unit_id = str(getattr(unit, "id", ""))
    origin = _position(getattr(unit, "position", None))
    if (
        not unit_id
        or origin is None
        or core_position is None
        or unit_id
        not in {
            memory.healing_defender_intent_id,
            memory.healing_defender_admitted_id,
        }
    ):
        return False
    if unit_id == memory.healing_defender_admitted_id:
        if origin == core_position:
            if memory.core_visit.unit_id in {None, unit_id}:
                memory.core_visit = CoreVisit(
                    unit_id,
                    "HEAL",
                    tick,
                    reached_core=True,
                )
                unit.heal()
            else:
                unit.wait()
            return True
        heal_path = memory.cargo_lane.heal_approach_path
        if origin in heal_path:
            path_index = heal_path.index(origin)
            if path_index + 1 < len(heal_path):
                destination = heal_path[path_index + 1]
                capacity_blocked = _unit_capacity_blocked(
                    friendly_occupancy,
                    reserved_destinations,
                    actor_origin=origin,
                )
                if destination in movement_blocked or destination in capacity_blocked:
                    unit.wait()
                    return True
                direction = _direction_between(origin, destination)
                if direction is not None:
                    _reserve_destination(reserved_destinations, destination)
                    memory.note_unit_move(unit_id, destination, core_position)
                    unit.move(direction)
                    return True
        if _queue_move(
            unit,
            core_position,
            movement_blocked,
            reserved_destinations,
            friendly_occupancy=friendly_occupancy,
            memory=memory,
            tick=tick,
        ):
            return True
        unit.wait()
        return True

    stage_target = memory.healing_defender_stage_target
    if (
        core_position is not None
        and _chebyshev(origin, core_position) <= max(CARGO_LANE_STAGE_RADII)
        and _complete_route(origin, core_position, movement_blocked).status
        == "SUCCESS"
        and memory.heal_priority_intent_id == unit_id
        and (
            memory.core_visit_deposit_streak >= HEAL_PRIORITY_DEPOSIT_LIMIT
            or memory.core_visit_forced_purpose == "HEAL"
        )
    ):
        unit.wait()
        return True
    if origin == stage_target:
        unit.wait()
        return True
    lane_exclusions = _defender_heal_staging_exclusions(memory, core_position)
    staging_blocked = set(movement_blocked) | lane_exclusions
    staging_blocked.discard(origin)
    if stage_target is not None and _queue_move(
        unit,
        stage_target,
        staging_blocked,
        reserved_destinations,
        friendly_occupancy=friendly_occupancy,
        memory=memory,
        tick=tick,
    ):
        return True
    unit.wait()
    return True


def _plan_vanguards(
    turn: Any,
    blocked: set[Position],
    beacon: Position | None,
    enemies: list[Any],
    enabled: bool,
    core_position: Position | None,
    reserved_destinations: Any,
    stationary_targets: list[Any],
    allow_stationary_clear: bool,
    danger_cells: set[Position],
    resource_cells: set[Position],
    priority_enemy_ids: set[str],
    memory: TacticMemory,
    guard_assignments: dict[str, Position],
    friendly_occupancy: dict[Position, int],
    enemy_occupied_positions: set[Position],
    raid_target: Any | None,
) -> None:
    movement_blocked = blocked | danger_cells | enemy_occupied_positions
    tick = int(getattr(turn, "tick", 0))
    for slot, vanguard in enumerate(
        sorted(
            getattr(turn, "vanguards", ()),
            key=lambda unit: (
                0
                if str(getattr(unit, "id", ""))
                in memory.cargo_lane.yield_unit_ids
                else 1
                if str(getattr(unit, "id", ""))
                == memory.healing_defender_admitted_id
                else 2,
                str(getattr(unit, "id", "")),
            ),
        )
    ):
        origin = _position(getattr(vanguard, "position", None))
        if origin is None:
            vanguard.wait()
            continue

        vanguard_key = str(getattr(vanguard, "id", ""))
        if _is_critical_combat_unit(vanguard) and _queue_critical_combat_escape(
            vanguard,
            enemies,
            blocked,
            enemy_occupied_positions,
            core_position,
            reserved_destinations,
            friendly_occupancy,
            memory,
            tick,
        ):
            continue
        if (
            vanguard_key == memory.healing_defender_admitted_id
            and _queue_healing_defender_action(
                vanguard,
                core_position,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            )
        ):
            continue
        if vanguard_key in memory.cargo_lane.yield_unit_ids:
            if _queue_cargo_lane_defender_yield(
                vanguard,
                memory.cargo_lane,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            ):
                continue
            vanguard.wait()
            continue
        if (
            vanguard_key == memory.healing_defender_intent_id
            and _queue_healing_defender_action(
                vanguard,
                core_position,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            )
        ):
            continue
        if vanguard_key in memory.raid.raid_member_ids:
            if memory.raid.state == "CORE_RECALL":
                if core_position is not None and _chebyshev(origin, core_position) > 3:
                    if _queue_move(
                        vanguard,
                        core_position,
                        movement_blocked,
                        reserved_destinations,
                        friendly_occupancy=friendly_occupancy,
                        memory=memory,
                        tick=tick,
                    ):
                        continue
                vanguard.wait()
                continue
            raid_position = memory.raid.assignments.get(vanguard_key)
            target_position = _position(getattr(raid_target, "position", None))
            if memory.raid.state == "CORE_RAID" and target_position is not None:
                attack_direction = _adjacent_direction(origin, target_position)
                if attack_direction is not None:
                    vanguard.sweep(attack_direction)
                    continue
            if _queue_move(
                vanguard,
                raid_position,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue
            vanguard.wait()
            continue

        if enabled and enemies:
            adjacent = [
                enemy
                for enemy in enemies
                if _adjacent_direction(origin, _position(enemy.position)) is not None
            ]
            target = _preferred_combat_target(
                origin,
                adjacent,
                priority_enemy_ids,
            )
            if target is not None:
                vanguard.sweep(_adjacent_direction(origin, _position(target.position)))
                continue

        if enabled and allow_stationary_clear and slot > 0:
            nearby_stationary = [
                target
                for target in stationary_targets
                if _manhattan(origin, _position(target.position))
                <= STATIONARY_CLEAR_RADIUS
            ]
            target = _nearest(origin, nearby_stationary)
            if target is not None:
                target_position = _position(target.position)
                if _adjacent_direction(origin, target_position) is not None:
                    vanguard.sweep(_adjacent_direction(origin, target_position))
                    continue
                approach_position = _vanguard_stationary_approach_position(
                    vanguard,
                    target_position,
                    movement_blocked,
                    reserved_destinations,
                    friendly_occupancy,
                    memory,
                    tick,
                )
                if approach_position is not None and _queue_move(
                    vanguard,
                    approach_position,
                    movement_blocked,
                    reserved_destinations,
                    friendly_occupancy=friendly_occupancy,
                    memory=memory,
                    tick=tick,
                    stationary_target=target_position,
                ):
                    continue

        if core_position is not None:
            guard_target = guard_assignments.get(vanguard_key, origin)
            if _queue_move(
                vanguard,
                guard_target,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue

        if beacon is not None:
            if origin == beacon:
                vanguard.pickup_beacon()
            elif _queue_move(
                vanguard,
                beacon,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue
        vanguard.wait()


def _plan_rangers(
    turn: Any,
    blocked: set[Position],
    beacon: Position | None,
    enemies: list[Any],
    enabled: bool,
    core_position: Position | None,
    reserved_destinations: Any,
    threat_level: str,
    stationary_targets: list[Any],
    allow_stationary_clear: bool,
    danger_cells: set[Position],
    resource_cells: set[Position],
    priority_enemy_ids: set[str],
    memory: TacticMemory,
    guard_assignments: dict[str, Position],
    friendly_occupancy: dict[Position, int],
    enemy_occupied_positions: set[Position],
    raid_target: Any | None,
) -> None:
    movement_blocked = blocked | danger_cells | enemy_occupied_positions
    tick = int(getattr(turn, "tick", 0))
    for slot, ranger in enumerate(
        sorted(
            getattr(turn, "rangers", ()),
            key=lambda unit: (
                0
                if str(getattr(unit, "id", ""))
                in memory.cargo_lane.yield_unit_ids
                else 1
                if str(getattr(unit, "id", ""))
                == memory.healing_defender_admitted_id
                else 2,
                str(getattr(unit, "id", "")),
            ),
        )
    ):
        origin = _position(getattr(ranger, "position", None))
        if origin is None:
            ranger.wait()
            continue

        ranger_key = str(getattr(ranger, "id", ""))
        if _is_critical_combat_unit(ranger) and _queue_critical_combat_escape(
            ranger,
            enemies,
            blocked,
            enemy_occupied_positions,
            core_position,
            reserved_destinations,
            friendly_occupancy,
            memory,
            tick,
        ):
            continue
        if (
            ranger_key == memory.healing_defender_admitted_id
            and _queue_healing_defender_action(
                ranger,
                core_position,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            )
        ):
            continue
        if ranger_key in memory.cargo_lane.yield_unit_ids:
            if _queue_cargo_lane_defender_yield(
                ranger,
                memory.cargo_lane,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            ):
                continue
            ranger.wait()
            continue
        if (
            ranger_key == memory.healing_defender_intent_id
            and _queue_healing_defender_action(
                ranger,
                core_position,
                movement_blocked | resource_cells,
                reserved_destinations,
                friendly_occupancy,
                memory,
                tick,
            )
        ):
            continue
        if ranger_key in memory.raid.raid_member_ids:
            if memory.raid.state == "CORE_RECALL":
                if core_position is not None and _chebyshev(origin, core_position) > 3:
                    if _queue_move(
                        ranger,
                        core_position,
                        movement_blocked,
                        reserved_destinations,
                        friendly_occupancy=friendly_occupancy,
                        memory=memory,
                        tick=tick,
                    ):
                        continue
                ranger.wait()
                continue
            raid_position = memory.raid.assignments.get(ranger_key)
            target_position = _position(getattr(raid_target, "position", None))
            if (
                memory.raid.state == "CORE_RAID"
                and target_position is not None
                and _aligned_in_range(origin, target_position)
                and _path_is_clear(origin, target_position, blocked)
            ):
                ranger.shoot(raid_target)
                continue
            if _queue_move(
                ranger,
                raid_position,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue
            ranger.wait()
            continue

        if enabled:
            targets = [
                enemy
                for enemy in enemies
                if _aligned_in_range(origin, _position(enemy.position))
                and _path_is_clear(origin, _position(enemy.position), blocked)
            ]
            if threat_level != "NORMAL":
                target = _preferred_combat_target(
                    origin,
                    targets,
                    priority_enemy_ids,
                )
            elif allow_stationary_clear:
                target = _nearest(
                    origin,
                    [
                        candidate
                        for candidate in stationary_targets
                        if _aligned_in_range(
                            origin,
                            _position(candidate.position),
                        )
                        and _path_is_clear(
                            origin,
                            _position(candidate.position),
                            blocked,
                        )
                    ],
                )
            else:
                target = None
            if target is not None:
                ranger.shoot(target)
                continue

        if core_position is not None:
            guard_target = guard_assignments.get(ranger_key, origin)
            if _queue_move(
                ranger,
                guard_target,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue

        if beacon is not None:
            if origin == beacon:
                ranger.pickup_beacon()
            elif _queue_move(
                ranger,
                beacon,
                movement_blocked,
                reserved_destinations,
                friendly_occupancy=friendly_occupancy,
                memory=memory,
                tick=tick,
            ):
                continue
        ranger.wait()


def _planned_core_move(
    core: Any,
    config: AgentConfig,
    blocked: set[Position],
    enemies: list[Any],
    threat: ThreatAssessment,
    beacon_position: Position | None,
    memory: TacticMemory,
) -> CoreMoveIntent | None:
    if core is None or _core_is_moving(core):
        return None
    core_position = _position(getattr(core, "position", None))
    pressure_enemies = [
        enemy
        for enemy in enemies
        if (
            (enemy_position := _position(getattr(enemy, "position", None)))
            is not None
            and core_position is not None
            and (
                _manhattan(core_position, enemy_position) <= CORE_ALERT_DISTANCE
                or str(getattr(enemy, "id", "")) in memory.pursuing_enemy_ids
                or str(getattr(enemy, "id", "")) in memory.preemptive_enemy_ids
            )
        )
    ]
    if (
        core_position is not None
        and threat.level in {"PRE_EVADE", "ENGAGED", "BREAKOUT"}
        and pressure_enemies
    ):
        direction = _core_escape_direction(
            core_position,
            pressure_enemies,
            blocked,
            previous_direction=memory.last_core_escape_direction,
        )
        if direction is not None:
            return CoreMoveIntent(direction, remember_escape_direction=True)
    if (
        threat.level == "NORMAL"
        and config.beacon_policy == "RETREAT"
        and core_position is not None
        and beacon_position is not None
    ):
        direction = _beacon_escape_direction(
            core_position,
            beacon_position,
            blocked,
        )
        if direction is not None:
            return CoreMoveIntent(direction)
    return None


def _plan_core(
    turn: Any,
    core: Any,
    config: AgentConfig,
    blocked: set[Position],
    enemies: list[Any],
    threat: ThreatAssessment,
    memory: TacticMemory,
    planned_move: CoreMoveIntent | None,
    pending_delivery: int = 0,
) -> None:
    if core is None:
        return
    if _core_is_moving(core):
        current_position = _position(getattr(core, "position", None))
        destination = _position(getattr(getattr(core, "view", None), "destination", None))
        cancel_move = getattr(core, "cancel_move", None)
        if (
            enemies
            and current_position is not None
            and destination is not None
            and callable(cancel_move)
            and (
                destination in blocked
                or _projected_incoming_damage(destination, enemies, blocked)
                > _projected_incoming_damage(current_position, enemies, blocked)
            )
        ):
            cancel_move()
            memory.last_core_escape_direction = None
            return
        core.wait()
        return

    if planned_move is not None:
        core.start_move(planned_move.direction)
        if planned_move.remember_escape_direction:
            memory.last_core_escape_direction = planned_move.direction
        return

    resources = _available_resources(turn, pending_delivery)
    if int(getattr(core, "hp", 0)) < 4 and resources >= 1:
        core.heal()
        return
    if int(getattr(core, "shield", 0) or 0) < 4 and resources >= 1:
        core.repair_shield()
        return

    if memory.core_visit.unit_id is not None:
        core.wait()
        return

    if threat.level != "NORMAL":
        population = _population(turn)
        population_allowed = (
            config.max_population <= 0 or population < config.max_population
        )
        nearest_distance = threat.nearest_distance
        defensive_type: UnitType | None = None
        if (
            config.enable_combat
            and population_allowed
            and nearest_distance is not None
        ):
            if (
                nearest_distance <= 3
                and config.vanguard_target > 0
                and len(tuple(getattr(turn, "vanguards", ())))
                < config.vanguard_target
            ):
                defensive_type = UnitType.VANGUARD
            elif (
                nearest_distance <= 6
                and config.ranger_target > 0
                and len(tuple(getattr(turn, "rangers", ())))
                < config.ranger_target
            ):
                defensive_type = UnitType.RANGER
        if defensive_type is not None:
            defensive_cost = _unit_cost(defensive_type, population)
            if resources >= defensive_cost:
                core.spawn(defensive_type)
                return
        core.wait()
        return

    if (
        not enemies
        and memory.threat_caution_until_tick >= int(getattr(turn, "tick", 0))
    ):
        core.wait()
        return

    population = _population(turn)
    spawn_type = _choose_spawn_unit(turn, config, pending_delivery)
    spawn_cost = _unit_cost(spawn_type, population) if spawn_type is not None else None
    population_allowed = (
        config.max_population <= 0 or population < config.max_population
    )
    production_reserve = _production_reserve(turn, config)
    if (
        spawn_type is not None
        and population_allowed
        and spawn_cost is not None
        and resources >= spawn_cost + production_reserve
    ):
        core.spawn(spawn_type)
        return
    core.wait()


def plan_turn(turn: Any, memory: TacticMemory, config: AgentConfig) -> PlanReport:
    """Queue one complete plan for the current authoritative Turn."""

    memory.observe(turn)
    blocked = set(memory.obstacle_memory)
    core = getattr(turn, "core", None)
    beacon = _visible_beacon_position(turn)
    # RETREAT keeps every unit away from a ground Beacon. HOLD preserves the
    # normal pickup/approach behavior for a deliberate Beacon collection run.
    unit_beacon = beacon if config.beacon_policy == "HOLD" else None
    visible_enemies = _visible_enemies(turn)
    enemies = _mobile_combat_enemies(visible_enemies)
    visible_combat_ids = {str(getattr(enemy, "id", "")) for enemy in enemies}
    remembered_threat_ids = (
        memory.active_enemy_ids
        | memory.preemptive_enemy_ids
        | memory.pursuing_enemy_ids
    ) - visible_combat_ids
    retreat_enemies = enemies + [
        RememberedCombatEnemy(
            id=enemy_id,
            position=memory.enemy_motion_memory[enemy_id].position,
            unit_type=memory.enemy_motion_memory[enemy_id].unit_type,
        )
        for enemy_id in sorted(remembered_threat_ids)
        if enemy_id in memory.enemy_motion_memory
    ]
    danger_cells = _enemy_danger_cells(enemies, blocked)
    friendly_occupancy = _friendly_cell_occupancy(turn)
    threat = _assess_threat(turn, memory, core, enemies)
    lifecycle = _lifecycle_state(turn, core, memory)
    stationary_targets = _confirmed_stationary_targets(
        memory,
        visible_enemies,
        int(turn.tick),
    )
    enemy_core_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None))
            for enemy in visible_enemies
            if _is_enemy_core(enemy)
        )
        if position is not None
    }
    stationary_targets = [
        target
        for target in stationary_targets
        if not _is_enemy_core(target)
        and _position(getattr(target, "position", None)) not in enemy_core_positions
        and _position(getattr(target, "position", None))
        not in memory.stationary_clear_cooldowns
    ]
    allow_stationary_clear = _allow_stationary_clear(
        threat,
        visible_enemies,
        stationary_targets,
    )
    raid_target = _update_core_raid_plan(
        turn,
        memory,
        config,
        core,
        _position(getattr(core, "position", None)),
        blocked,
        danger_cells,
        visible_enemies,
        enemies,
        threat,
        friendly_occupancy,
    )
    planned_core_move = _planned_core_move(
        core,
        config,
        blocked,
        retreat_enemies,
        threat,
        beacon,
        memory,
    )

    core_accepts_delivery = bool(
        core is not None
        and not _core_is_moving(core)
        and planned_core_move is None
        and _resource_space(turn) > 0
    )
    _update_core_pocket(
        turn,
        _position(getattr(core, "position", None)),
        blocked,
        friendly_occupancy,
        memory,
        core_accepts_delivery=core_accepts_delivery,
    )
    # Refresh the concrete HEAL reservation before cargo admission. A timed-out
    # healer may rotate, but an already-earned HEAL quota must not expose a
    # one-Tick gap in which a new cargo owner can take the corridor.
    _refresh_healing_workers(
        turn,
        memory,
        _position(getattr(core, "position", None)),
    )
    _refresh_healing_defenders(
        turn,
        memory,
        _position(getattr(core, "position", None)),
        blocked | danger_cells,
    )
    _update_cargo_lane(
        turn,
        _position(getattr(core, "position", None)),
        blocked,
        danger_cells | memory.recent_worker_return_danger(int(turn.tick)),
        friendly_occupancy,
        memory,
        core_accepts_delivery=core_accepts_delivery,
        core_stable=bool(
            core is not None
            and not _core_is_moving(core)
            and planned_core_move is None
        ),
        threat_level=threat.level,
    )

    pending_delivery = _plan_workers(
        turn,
        core,
        blocked,
        unit_beacon,
        memory,
        config.resource_memory_ttl,
        enemies,
        visible_enemies,
        danger_cells,
        friendly_occupancy,
        core_will_move=planned_core_move is not None,
    )
    core_position = _position(getattr(core, "position", None))
    resource_cells = _positions(getattr(turn, "resource_cells", ()))
    priority_enemy_ids = _core_threatening_enemy_ids(
        core_position,
        enemies,
        blocked,
    ) | set(memory.pursuing_enemy_ids)
    enemy_positions = {
        position
        for position in (
            _position(getattr(enemy, "position", None)) for enemy in visible_enemies
        )
        if position is not None
    }
    defending_vanguards = [
        unit
        for unit in getattr(turn, "vanguards", ())
        if str(getattr(unit, "id", "")) not in memory.raid.raid_member_ids
    ]
    defending_rangers = [
        unit
        for unit in getattr(turn, "rangers", ())
        if str(getattr(unit, "id", "")) not in memory.raid.raid_member_ids
    ]
    core_logistics_apron = (
        {
            _next_position(core_position, direction)
            for direction in CARDINAL_DIRECTIONS
        }
        if core_position is not None
        else set()
    )
    if memory.cargo_lane.active and core_position is not None:
        core_logistics_apron.update(
            _chebyshev_ring_positions(core_position, 1)
        )
        core_logistics_apron.update(memory.cargo_lane.path)
        core_logistics_apron.update(memory.cargo_lane.owner_approach_path)
        core_logistics_apron.update(memory.cargo_lane.heal_approach_path)
        core_logistics_apron.update(memory.cargo_lane.egress_path)
        core_logistics_apron.update(memory.cargo_lane.stage_targets.values())
        core_logistics_apron.discard(core_position)
    if memory.healing_defender_stage_target is not None:
        core_logistics_apron.add(memory.healing_defender_stage_target)
    core_logistics_apron.update(memory.planned_worker_destinations.values())
    vanguard_guards, vanguard_guard_positions, vanguard_idle = _assign_guard_posts(
        defending_vanguards,
        core_position,
        (3, 2, 4),
        blocked,
        danger_cells,
        resource_cells,
        enemy_positions,
        core_logistics_apron,
        friendly_occupancy,
    )
    ranger_guards, _, ranger_idle = _assign_guard_posts(
        defending_rangers,
        core_position,
        (2, 1, 3),
        blocked,
        danger_cells,
        resource_cells,
        enemy_positions,
        vanguard_guard_positions | core_logistics_apron,
        friendly_occupancy,
    )
    memory.guard_idle_assignments += vanguard_idle + ranger_idle
    defense_reservations: dict[Position, int] = {}
    _plan_vanguards(
        turn,
        blocked,
        unit_beacon,
        enemies,
        config.enable_combat,
        core_position,
        defense_reservations,
        stationary_targets,
        allow_stationary_clear,
        danger_cells,
        resource_cells,
        priority_enemy_ids,
        memory,
        vanguard_guards,
        friendly_occupancy,
        enemy_positions,
        raid_target,
    )
    _plan_rangers(
        turn,
        blocked,
        unit_beacon,
        enemies,
        config.enable_combat,
        core_position,
        defense_reservations,
        threat.level,
        stationary_targets,
        allow_stationary_clear,
        danger_cells,
        resource_cells,
        priority_enemy_ids,
        memory,
        ranger_guards,
        friendly_occupancy,
        enemy_positions,
        raid_target,
    )
    spawn_type = _choose_spawn_unit(turn, config, pending_delivery)
    spawn_cost = (
        _unit_cost(spawn_type, _population(turn))
        if spawn_type is not None
        else None
    )
    available_resources = _available_resources(turn, pending_delivery)
    _plan_core(
        turn,
        core,
        config,
        blocked,
        retreat_enemies,
        threat,
        memory,
        planned_core_move,
        pending_delivery,
    )

    current_resources = resource_cells
    remembered_resources = memory.recent_resource_targets(
        int(turn.tick), config.resource_memory_ttl
    ) - current_resources
    mission = _mission_state(
        lifecycle,
        threat,
        len(current_resources),
        len(remembered_resources),
        sum(
            int(getattr(worker, "cargo", 0)) > 0
            for worker in getattr(turn, "workers", ())
        ),
    )
    vanguard_attacks, ranger_attacks = _planned_combat_attack_counts(turn)
    raid_ids = set(memory.raid.raid_member_ids)
    vanguard_raid = sum(
        str(getattr(unit, "id", "")) in raid_ids
        for unit in getattr(turn, "vanguards", ())
    )
    ranger_raid = sum(
        str(getattr(unit, "id", "")) in raid_ids
        for unit in getattr(turn, "rangers", ())
    )
    lane = memory.cargo_lane
    heal_priority_wait_ticks = (
        max(0, int(turn.tick) - memory.heal_priority_started_tick)
        if memory.heal_priority_started_tick >= 0
        else 0
    )

    return PlanReport(
        tick=int(turn.tick),
        resources=int(getattr(turn, "resources", 0)),
        population=_population(turn),
        workers=len(tuple(getattr(turn, "workers", ()))),
        vanguards=len(tuple(getattr(turn, "vanguards", ()))),
        rangers=len(tuple(getattr(turn, "rangers", ()))),
        visible_enemies=len(visible_enemies),
        visible_resources=len(current_resources),
        remembered_resources=len(remembered_resources),
        threat_level=threat.level,
        threat_reason=threat.reason,
        lifecycle=lifecycle,
        mission=mission,
        production_unit=spawn_type,
        production_cost=spawn_cost,
        available_resources=available_resources,
        pending_delivery=pending_delivery,
        visible_combat_enemies=len(enemies),
        danger_cells=len(danger_cells),
        pursuing_enemies=len(memory.pursuing_enemy_ids),
        preemptive_enemies=len(memory.preemptive_enemy_ids),
        vanguard_defenders=len(tuple(defending_vanguards)),
        vanguard_raid=vanguard_raid,
        vanguard_attacks=vanguard_attacks,
        ranger_defenders=len(tuple(defending_rangers)),
        ranger_raid=ranger_raid,
        ranger_attacks=ranger_attacks,
        cargo_lane_phase=lane.phase if lane.active else "IDLE",
        cargo_lane_yield_workers=len(lane.yield_worker_ids),
        cargo_lane_yield_units=len(lane.yield_unit_ids),
        cargo_watchdog_reason=lane.watchdog_reason,
        heal_priority_active=memory.heal_priority_intent_id is not None,
        healing_defender_admitted=memory.healing_defender_admitted_id is not None,
        heal_priority_wait_ticks=heal_priority_wait_ticks,
        heal_priority_interleave_pending=memory.heal_priority_interleave_pending,
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _env_setting(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_file = Path(os.environ.get("ARENA_HERO_ENV_FILE", ".env"))
    return _read_dotenv(env_file).get(name, default)


def _resolve_runtime_path(raw_path: str) -> Path:
    """Resolve relative runtime artifacts next to the agent script."""

    requested_path = Path(raw_path).expanduser()
    if requested_path.is_absolute():
        return requested_path
    return Path(__file__).resolve().parent / requested_path


def _configure_logging(log_level: str, log_file: str) -> Path | None:
    """Configure console output plus a bounded UTF-8 diagnostic log."""

    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    handlers: list[logging.Handler] = []
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level))
    console_handler.setFormatter(console_formatter)
    handlers.append(console_handler)

    resolved_log_path: Path | None = None
    if log_file.strip():
        resolved_log_path = _resolve_runtime_path(log_file)
        resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=handlers,
        force=True,
    )
    LOGGER.setLevel(logging.DEBUG)
    return resolved_log_path


def _submit_turn_with_retry(
    turn: Any,
    *,
    attempts: int = SUBMIT_RETRY_ATTEMPTS,
    sleep_fn: Any = time.sleep,
    error_callback: Any = None,
) -> Any:
    """Retry one Tick with the same process-session idempotency key."""

    attempts = max(1, attempts)
    idempotency_key = f"arena-agent-{_AGENT_SESSION_ID}-{int(turn.tick)}"
    for attempt in range(attempts):
        try:
            return turn.submit(idempotency_key=idempotency_key)
        except TransportError:
            if error_callback is not None:
                error_callback("submit_transport")
            if attempt + 1 >= attempts:
                raise
            delay = SUBMIT_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            LOGGER.warning(
                "TransportError while submitting tick=%s; retrying in %.1fs",
                turn.tick,
                delay,
            )
            sleep_fn(delay)
        except APIError as exc:
            if error_callback is not None:
                error_callback("submit_api")
            if not _is_retryable_api_error(exc):
                raise
            if attempt + 1 >= attempts:
                raise
            delay = SUBMIT_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            LOGGER.warning(
                "APIError while submitting tick=%s status=%s error=%s; retrying in %.1fs",
                turn.tick,
                exc.status_code,
                exc.error,
                delay,
            )
            sleep_fn(delay)
    raise RuntimeError("unreachable submit retry state")


def _is_retryable_api_error(exc: APIError) -> bool:
    """Retry temporary API failures, but fail fast on an idempotency conflict."""

    error_code = str(getattr(exc, "error", "") or "").upper()
    return (
        exc.status_code in RETRYABLE_API_STATUS_CODES
        and error_code not in NON_RETRYABLE_API_ERRORS
    )


def _reconnect_delay(attempt: int, max_delay: float) -> float:
    """Calculate bounded exponential backoff without unbounded integer growth."""

    max_delay = max(0.0, float(max_delay))
    if max_delay == 0:
        return 0.0
    exponent = min(max(0, int(attempt) - 1), 30)
    return min(
        SUBMIT_RETRY_BASE_DELAY_SECONDS * (2**exponent),
        max_delay,
    )


def _log_sdk_compatibility() -> None:
    """Report the pinned SDK expectation and the active rule integration."""

    if ARENA_HERO_SDK_VERSION == "unknown":
        LOGGER.warning(
            "SDK version could not be detected; expected=%s; "
            "dynamic price fallback will be used only if required",
            EXPECTED_ARENA_HERO_SDK_VERSION,
        )
        return
    if ARENA_HERO_SDK_VERSION != EXPECTED_ARENA_HERO_SDK_VERSION:
        LOGGER.warning(
            "SDK version mismatch: installed=%s expected=%s; "
            "legacy compatibility is limited to pre-0.2.9 state fields",
            ARENA_HERO_SDK_VERSION,
            EXPECTED_ARENA_HERO_SDK_VERSION,
        )
        return
    LOGGER.info(
        "SDK=%s expected=%s; dynamic unit pricing enabled; automatic upkeep disabled",
        ARENA_HERO_SDK_VERSION,
        EXPECTED_ARENA_HERO_SDK_VERSION,
    )


def _log_api_error(context: str, exc: APIError) -> None:
    """Log structured API failure fields without exposing credentials."""

    LOGGER.error(
        "%s | status=%s error=%s message=%s details=%s",
        context,
        exc.status_code,
        exc.error,
        exc.message or "-",
        exc.details or "-",
    )


def _is_retryable_protocol_error(exc: ProtocolError) -> bool:
    """Allow only the known initial WebSocket ordering race to reconnect."""

    return str(exc) == RETRYABLE_PROTOCOL_MESSAGE


def _sdk_version_tuple(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _legacy_state_fields_required() -> bool:
    version = _sdk_version_tuple(ARENA_HERO_SDK_VERSION)
    return version is not None and version < (0, 2, 9)


def _parse_stream_message_with_compatibility(raw: str | bytes) -> Any:
    """Keep the pre-0.2.9 state shim isolated from the current SDK schema."""

    global _protocol_compatibility_warned
    parser = arena_hero_protocol_module.parse_stream_message
    if not _legacy_state_fields_required():
        return parser(raw)
    try:
        return parser(raw)
    except ProtocolError as exc:
        if str(exc) != "invalid Arena Hero WebSocket message" or not isinstance(raw, str):
            raise
        try:
            envelope = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise exc
        if not isinstance(envelope, dict) or envelope.get("type") != "state":
            raise exc
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise exc
        missing = [
            field_name
            for field_name in ("population_tier", "upkeep_next_tick")
            if field_name not in data
        ]
        if not missing:
            raise exc
        patched_data = dict(data)
        patched_data.setdefault("population_tier", 0)
        patched_data.setdefault("upkeep_next_tick", 0)
        patched_envelope = dict(envelope)
        patched_envelope["data"] = patched_data
        if not _protocol_compatibility_warned:
            LOGGER.warning(
                "Legacy SDK compatibility mode: server state omitted %s; defaulting to 0",
                ", ".join(missing),
            )
            _protocol_compatibility_warned = True
        return parser(json.dumps(patched_envelope, ensure_ascii=False))


def _install_protocol_compatibility() -> None:
    """Install the legacy parser only when an older SDK actually needs it."""

    if _legacy_state_fields_required():
        arena_hero_client_module.parse_stream_message = (
            _parse_stream_message_with_compatibility
        )


def load_api_key() -> str:
    """Read the credential without ever printing or embedding it in source."""

    key = os.environ.get("ARENA_HERO_API_KEY", "").strip()
    if key:
        return key

    env_file = Path(os.environ.get("ARENA_HERO_ENV_FILE", ".env"))
    key = _read_dotenv(env_file).get("ARENA_HERO_API_KEY", "").strip()
    if key:
        return key

    raise RuntimeError(
        "ARENA_HERO_API_KEY is missing; set it in the environment or in .env"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-turns",
        type=int,
        default=0,
        help="Stop after this many submitted Turns; 0 means run continuously.",
    )
    parser.add_argument(
        "--submit-retries",
        type=int,
        default=int(_env_setting("ARENA_HERO_SUBMIT_RETRIES", "3")),
        help="Retries for one Tick using the same idempotency key.",
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=float(
            _env_setting(
                "ARENA_HERO_RECONNECT_MAX_DELAY",
                str(RECONNECT_MAX_DELAY_SECONDS),
            )
        ),
        help="Maximum seconds to wait between TransportError reconnects.",
    )
    parser.add_argument(
        "--max-reconnect-attempts",
        type=int,
        default=int(
            _env_setting(
                "ARENA_HERO_MAX_RECONNECT_ATTEMPTS",
                str(MAX_RECONNECT_ATTEMPTS),
            )
        ),
        help="Maximum consecutive reconnect attempts before safe stop.",
    )
    parser.add_argument(
        "--max-population",
        type=int,
        default=int(
            _env_setting("ARENA_HERO_MAX_POPULATION", str(DEFAULT_MAX_POPULATION))
        ),
        help="Strategy population cap; 0 disables it. The game itself has no population cap.",
    )
    parser.add_argument(
        "--population-expansion",
        choices=["ON", "OFF"],
        default=_env_setting("ARENA_HERO_POPULATION_EXPANSION", "OFF").upper(),
        help="Continue proportional 8:3:4 growth after configured roster targets.",
    )
    parser.add_argument(
        "--expansion-threshold",
        type=int,
        default=int(
            _env_setting(
                "ARENA_HERO_EXPANSION_RESOURCE_THRESHOLD",
                str(POPULATION_EXPANSION_RESOURCE_FLOOR),
            )
        ),
        help="Minimum available resources required for optional population growth.",
    )
    parser.add_argument(
        "--expansion-casualty-buffer",
        type=int,
        default=int(
            _env_setting(
                "ARENA_HERO_EXPANSION_CASUALTY_BUFFER_UNITS",
                str(POPULATION_EXPANSION_CASUALTY_BUFFER_UNITS),
            )
        ),
        help="Units of spare post-casualty capacity protected by the expansion gate.",
    )
    parser.add_argument(
        "--worker-target",
        type=int,
        default=int(
            _env_setting("ARENA_HERO_WORKER_TARGET", str(DEFAULT_WORKER_TARGET))
        ),
        help="Automatic production target for Workers.",
    )
    parser.add_argument(
        "--vanguard-target",
        type=int,
        default=int(
            _env_setting("ARENA_HERO_VANGUARD_TARGET", str(DEFAULT_VANGUARD_TARGET))
        ),
        help="Automatic production target for Vanguards.",
    )
    parser.add_argument(
        "--ranger-target",
        type=int,
        default=int(
            _env_setting("ARENA_HERO_RANGER_TARGET", str(DEFAULT_RANGER_TARGET))
        ),
        help="Automatic production target for Rangers.",
    )
    parser.add_argument(
        "--spawn-unit",
        choices=["AUTO", *[unit_type.name for unit_type in UnitType]],
        default=_env_setting("ARENA_HERO_SPAWN_UNIT", "AUTO").upper(),
        help="AUTO restores 12/3/4, then expands to configured targets; a unit name forces that type.",
    )
    parser.add_argument(
        "--beacon-policy",
        choices=["RETREAT", "HOLD"],
        default=_env_setting("ARENA_HERO_BEACON_POLICY", "RETREAT").upper(),
        help="RETREAT moves a nearby Core away from a ground Beacon; HOLD disables it.",
    )
    parser.add_argument(
        "--no-spawn",
        action="store_true",
        help="Keep the Core action conservative and never spawn new Units.",
    )
    parser.add_argument(
        "--no-combat",
        action="store_true",
        help="Disable visible-enemy pursuit and attacks.",
    )
    parser.add_argument(
        "--log-level",
        default=_env_setting("ARENA_HERO_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--log-file",
        default=_env_setting("ARENA_HERO_LOG_FILE", DEFAULT_LOG_FILE),
        help="Diagnostic log path; empty value disables file logging.",
    )
    parser.add_argument(
        "--state-file",
        default=_env_setting("ARENA_HERO_STATE_FILE", DEFAULT_STATE_FILE),
        help="Persistent map-memory JSON path.",
    )
    parser.add_argument(
        "--trace-file",
        default=_env_setting("ARENA_HERO_TRACE_FILE", DEFAULT_TRACE_FILE),
        help="Replay-friendly per-Tick JSONL path.",
    )
    parser.add_argument(
        "--stats-file",
        default=_env_setting("ARENA_HERO_STATS_FILE", DEFAULT_STATS_FILE),
        help="Session statistics JSON path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_console()
    try:
        log_path = _configure_logging(args.log_level, args.log_file)
    except OSError as exc:
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
            force=True,
        )
        LOGGER.error("关键日志文件不可用，将仅输出到控制台: %s", exc)
        log_path = None
    for noisy_logger in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    if log_path is not None:
        LOGGER.info(
            "关键日志已启用 | 文件=%s | 轮转上限=%sMB | 备份=%s",
            log_path,
            LOG_MAX_BYTES // (1024 * 1024),
            LOG_BACKUP_COUNT,
        )

    try:
        api_key = load_api_key()
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 2

    automatic_production = not args.no_spawn and args.spawn_unit == "AUTO"
    configured_spawn_type = (
        None
        if args.no_spawn
        else UnitType.WORKER
        if automatic_production
        else UnitType[args.spawn_unit]
    )
    config = AgentConfig(
        max_population=max(0, args.max_population),
        spawn_unit_type=configured_spawn_type,
        auto_production=automatic_production,
        population_expansion_enabled=args.population_expansion == "ON",
        expansion_resource_threshold=max(0, args.expansion_threshold),
        expansion_casualty_buffer_units=max(0, args.expansion_casualty_buffer),
        worker_target=max(0, args.worker_target),
        vanguard_target=max(0, args.vanguard_target),
        ranger_target=max(0, args.ranger_target),
        enable_combat=not args.no_combat,
        beacon_policy=args.beacon_policy,
    )
    state_path = _resolve_runtime_path(args.state_file)
    memory = _load_tactic_memory(state_path)
    atexit.register(_save_tactic_memory, memory, state_path)
    recorder: SessionRecorder | None = None
    try:
        recorder = SessionRecorder(args.trace_file, args.stats_file)
    except OSError as exc:
        LOGGER.error("战局回放文件不可用，将继续运行但不保存回放统计: %s", exc)
    else:
        atexit.register(recorder.close)
        LOGGER.info(
            "持久化与战局记录已启用 | 状态=%s | 回放=%s | 统计=%s",
            state_path,
            recorder.trace_path,
            recorder.stats_path,
        )
    submitted_turns = 0
    spawn_label = (
        f"\u81ea\u52a8({config.worker_target}/{config.vanguard_target}/{config.ranger_target})"
        if config.auto_production
        else UNIT_LABELS.get(config.spawn_unit_type, "\u5173\u95ed")
    )
    combat_label = "\u5f00\u542f" if config.enable_combat else "\u5173\u95ed"
    expansion_label = (
        (
            f"ON(threshold={config.expansion_resource_threshold},"
            f"casualty_buffer={config.expansion_casualty_buffer_units})"
        )
        if config.population_expansion_enabled
        else "OFF"
    )
    population_label = (
        "\u4e0d\u9650\u5236" if config.max_population <= 0 else str(config.max_population)
    )

    LOGGER.info(
        "启动 Arena Hero Agent | 最大人口=%s | 生产=%s | 战斗=%s",
        population_label,
        spawn_label,
        combat_label,
    )
    LOGGER.info("Population expansion=%s", expansion_label)
    _log_sdk_compatibility()

    _install_protocol_compatibility()
    reconnect_attempt = 0
    max_reconnect_attempts = max(1, args.max_reconnect_attempts)
    try:
        while True:
            try:
                with ArenaHeroClient(api_key=api_key) as game:
                    for turn in game.turns():
                        report = plan_turn(turn, memory, config)
                        accepted = _submit_turn_with_retry(
                            turn,
                            attempts=max(1, args.submit_retries),
                            error_callback=(
                                recorder.record_runtime_error
                                if recorder is not None
                                else None
                            ),
                        )
                        submitted_turns += 1
                        reconnect_attempt = 0
                        _render_turn(turn, report, accepted)
                        _log_turn_summary(report, accepted)
                        _log_spawn_resolution(turn)
                        if recorder is not None:
                            recorder.record_turn(turn, report, accepted, memory)
                        if submitted_turns % STATE_SAVE_INTERVAL_TICKS == 0:
                            _save_tactic_memory(memory, state_path, report.tick)
                        if args.max_turns and submitted_turns >= args.max_turns:
                            break
                if args.max_turns and submitted_turns >= args.max_turns:
                    break
                break
            except ProtocolError as exc:
                if not _is_retryable_protocol_error(exc):
                    raise
                if recorder is not None:
                    recorder.record_runtime_error("reconnect_protocol")
                reconnect_attempt += 1
                if reconnect_attempt > max_reconnect_attempts:
                    LOGGER.error(
                        "Protocol reconnect limit reached (%s); stopping safely",
                        max_reconnect_attempts,
                    )
                    return 5
                max_delay = max(0.0, args.reconnect_max_delay)
                delay = _reconnect_delay(reconnect_attempt, max_delay)
                LOGGER.warning(
                    "ProtocolError=%s; reconnect attempt=%s in %.1fs",
                    exc,
                    reconnect_attempt,
                    delay,
                )
                time.sleep(delay)
            except APIError as exc:
                if recorder is not None:
                    recorder.record_runtime_error("reconnect_api")
                if not _is_retryable_api_error(exc):
                    _log_api_error("Agent 因不可重试 API 错误停止", exc)
                    return 4
                reconnect_attempt += 1
                if reconnect_attempt > max_reconnect_attempts:
                    _log_api_error("Agent 因连续 API 错误停止", exc)
                    return 5
                max_delay = max(0.0, args.reconnect_max_delay)
                delay = _reconnect_delay(reconnect_attempt, max_delay)
                LOGGER.warning(
                    "APIError status=%s error=%s; reconnect attempt=%s in %.1fs",
                    exc.status_code,
                    exc.error,
                    reconnect_attempt,
                    delay,
                )
                time.sleep(delay)
            except TransportError as exc:
                if recorder is not None:
                    recorder.record_runtime_error("reconnect_transport")
                reconnect_attempt += 1
                if reconnect_attempt > max_reconnect_attempts:
                    LOGGER.error(
                        "Transport reconnect limit reached (%s); stopping safely: %s",
                        max_reconnect_attempts,
                        exc,
                    )
                    return 5
                max_delay = max(0.0, args.reconnect_max_delay)
                delay = _reconnect_delay(reconnect_attempt, max_delay)
                LOGGER.warning(
                    "TransportError: %s; reconnect attempt=%s in %.1fs",
                    exc,
                    reconnect_attempt,
                    delay,
                )
                time.sleep(delay)
    except (AuthenticationError, PolicyViolationError) as exc:
        LOGGER.error("Agent 因凭据或策略错误停止: %s", type(exc).__name__)
        return 3
    except ProtocolError as exc:
        cause = f" | detail={exc.__cause__}" if exc.__cause__ is not None else ""
        LOGGER.error(
            "Agent 因 SDK 协议错误停止: %s | SDK=%s%s",
            exc,
            ARENA_HERO_SDK_VERSION,
            cause,
        )
        return 4
    except ArenaHeroError as exc:
        LOGGER.error("Agent 因 SDK 错误停止: %s: %s", type(exc).__name__, exc)
        return 4
    except KeyboardInterrupt:
        LOGGER.info("Agent 已由用户停止，共处理 %s 个回合", submitted_turns)
        return 0

    LOGGER.info("Agent 已停止，共处理 %s 个回合", submitted_turns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
