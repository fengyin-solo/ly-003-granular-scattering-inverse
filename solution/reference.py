#!/usr/bin/env python3
"""Reference reconstruction for the unlabeled scattering-session export."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable


MAX_INPUT_BYTES = 262144
BATCH_RE = re.compile(r"^QS-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$")
EXPERIMENT_RE = re.compile(r"^EX-[0-9A-F]{4}-[0-9A-F]{4}$")
CHANNEL_RE = re.compile(r"^CH-[A-Z0-9]{3}-[0-9A-F]{4}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ENTRY_RE = re.compile(r"^LE-[0-9A-F]{12}$")
TOP_KEYS = {"schema_version", "batch_id", "conventions", "experiments"}
CONVENTION_KEYS = {"length_unit", "time_unit"}
EXPERIMENT_KEYS = {"experiment_id", "wall_labels", "sensors"}
SENSOR_KEYS = {"channel", "time_s", "points_mm", "mass_mg"}
LEDGER_KEYS = {
    "schema_version",
    "entry_id",
    "batch_id",
    "input_sha256",
    "report_sha256",
}
WALL_LABELS = ["W-A", "W-B", "W-C", "W-D"]


class InputError(ValueError):
    """The public input contract was not met."""


class LedgerError(ValueError):
    """An existing ledger cannot be safely extended."""


class ResultAmbiguous(ValueError):
    """The observations do not close into a unique event partition."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover particle tracks and wall parameters from scattering fragments."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate key: {key}")
        result[key] = value
    return result


def decode_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("input is larger than 262144 bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError("input is not UTF-8") from exc
    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_pairs)
    except json.JSONDecodeError as exc:
        raise InputError("input is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InputError("root must be an object")
    return payload


def exact_keys(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    if set(value) != keys:
        raise InputError(f"{label} has the wrong key set")


def is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def number(value: Any, label: str) -> float:
    if not is_number(value):
        raise InputError(f"{label} must be a finite number")
    return float(value)


def parse_sensor(sensor: Any, used_channels: set[str]) -> None:
    exact_keys(sensor, SENSOR_KEYS, "sensor")
    channel = sensor["channel"]
    if not isinstance(channel, str) or CHANNEL_RE.fullmatch(channel) is None:
        raise InputError("sensor channel format is invalid")
    if channel in used_channels:
        raise InputError("duplicate sensor channel")
    used_channels.add(channel)
    times = sensor["time_s"]
    if not isinstance(times, list) or len(times) != 2:
        raise InputError("time_s must contain two numbers")
    start_time = number(times[0], "time start")
    end_time = number(times[1], "time end")
    duration = end_time - start_time
    if (
        start_time < 0.0
        or not start_time < end_time
        or end_time > 12.0
        or duration < 0.010
        or duration > 0.400
    ):
        raise InputError("time range is invalid")
    points = sensor["points_mm"]
    if not isinstance(points, list) or len(points) != 2:
        raise InputError("points_mm must contain two coordinates")
    coordinates: list[list[float]] = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise InputError("coordinate must contain two numbers")
        x = number(point[0], "point x")
        y = number(point[1], "point y")
        if x < -800.0 or x > 800.0 or y < -800.0 or y > 800.0:
            raise InputError("coordinate is outside the public range")
        coordinates.append([x, y])
    distance = math.hypot(
        coordinates[1][0] - coordinates[0][0],
        coordinates[1][1] - coordinates[0][1],
    )
    if distance < 5.0 or distance > 220.0:
        raise InputError("fragment length is invalid")
    mass_mg = number(sensor["mass_mg"], "mass_mg")
    if mass_mg < 500.0 or mass_mg > 2500.0:
        raise InputError("mass_mg is outside the public range")


def validate_payload(payload: dict[str, Any]) -> None:
    exact_keys(payload, TOP_KEYS, "top level")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise InputError("schema_version must be integer 1")
    batch_id = payload["batch_id"]
    if not isinstance(batch_id, str) or BATCH_RE.fullmatch(batch_id) is None:
        raise InputError("batch_id format is invalid")
    conventions = payload["conventions"]
    exact_keys(conventions, CONVENTION_KEYS, "conventions")
    if conventions["length_unit"] != "mm" or conventions["time_unit"] != "s":
        raise InputError("conventions are invalid")
    experiments = payload["experiments"]
    if (
        not isinstance(experiments, list)
        or len(experiments) < 1
        or len(experiments) > 6
    ):
        raise InputError("experiments must contain 1 through 6 items")
    seen_experiments: set[str] = set()
    for experiment in experiments:
        exact_keys(experiment, EXPERIMENT_KEYS, "experiment")
        experiment_id = experiment["experiment_id"]
        if (
            not isinstance(experiment_id, str)
            or EXPERIMENT_RE.fullmatch(experiment_id) is None
        ):
            raise InputError("experiment_id format is invalid")
        if experiment_id in seen_experiments:
            raise InputError("duplicate experiment_id")
        seen_experiments.add(experiment_id)
        labels = experiment["wall_labels"]
        if not isinstance(labels, list) or labels != WALL_LABELS:
            raise InputError("wall_labels must be the ordered four labels")
        sensors = experiment["sensors"]
        if (
            not isinstance(sensors, list)
            or len(sensors) % 2 != 0
            or len(sensors) < 8
            or len(sensors) > 48
        ):
            raise InputError("sensors must be an even array of 8 through 48 items")
        used_channels: set[str] = set()
        for sensor in sensors:
            parse_sensor(sensor, used_channels)


def cross(first: list[float], second: list[float]) -> float:
    return first[0] * second[1] - first[1] * second[0]


def dot(first: list[float], second: list[float]) -> float:
    return first[0] * second[0] + first[1] * second[1]


def add(first: list[float], second: list[float]) -> list[float]:
    return [first[0] + second[0], first[1] + second[1]]


def sub(first: list[float], second: list[float]) -> list[float]:
    return [first[0] - second[0], first[1] - second[1]]


def mul(scale: float, vector: list[float]) -> list[float]:
    return [scale * vector[0], scale * vector[1]]


def norm(vector: list[float]) -> float:
    return math.hypot(vector[0], vector[1])


def unit(vector: list[float]) -> list[float]:
    length = norm(vector)
    if not length > 1e-12:
        raise ResultAmbiguous("zero-length velocity or normal")
    return [vector[0] / length, vector[1] / length]


def canonical(vector: list[float]) -> list[float]:
    value = unit(vector)
    if value[0] < 0.0 or (abs(value[0]) < 1e-12 and value[1] < 0.0):
        return [-value[0], -value[1]]
    return value


def round_half_up(value: float, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    rounded = float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))
    return 0.0 if rounded == 0.0 else rounded


@dataclass
class Fragment:
    channel: str
    start: list[float]
    end: list[float]
    start_time: float
    end_time: float
    velocity: list[float]
    direction: list[float]
    speed: float
    mass_mg: float


@dataclass
class ContactCandidate:
    first: int
    second: int
    point: list[float]
    time: float


@dataclass
class InternalEvent:
    channels: list[str]
    kind: str
    point: list[float]
    time: float
    incoming: list[str]
    outgoing: list[str]
    pairs: list[list[str]]
    incoming_velocity: dict[str, list[float]]
    outgoing_velocity: dict[str, list[float]]
    normal: list[float]
    restitution: float
    momentum_error: float
    energy_loss: float
    wall_cluster: int | None = None


def fragment_from_sensor(sensor: dict[str, Any]) -> Fragment:
    start = [float(sensor["points_mm"][0][0]), float(sensor["points_mm"][0][1])]
    end = [float(sensor["points_mm"][1][0]), float(sensor["points_mm"][1][1])]
    start_time = float(sensor["time_s"][0])
    end_time = float(sensor["time_s"][1])
    velocity = mul(1.0 / (end_time - start_time), sub(end, start))
    speed = norm(velocity)
    return Fragment(
        channel=sensor["channel"],
        start=start,
        end=end,
        start_time=start_time,
        end_time=end_time,
        velocity=velocity,
        direction=unit(velocity),
        speed=speed,
        mass_mg=float(sensor["mass_mg"]),
    )


def projected_gap(fragment: Fragment, point: list[float], contact_time: float) -> tuple[str, float, float] | None:
    if contact_time > fragment.end_time + 1e-8:
        role = "incoming"
        gap = norm(sub(point, fragment.end))
        dt = contact_time - fragment.end_time
    elif contact_time < fragment.start_time - 1e-8:
        role = "outgoing"
        gap = norm(sub(point, fragment.start))
        dt = fragment.start_time - contact_time
    else:
        return None
    if gap < 2.5 or gap > 45.0 or dt < 0.0 or dt > 0.400 or dt == 0.0:
        return None
    return role, gap, dt


def line_contact(first: Fragment, second: Fragment) -> ContactCandidate | None:
    denominator = cross(first.direction, second.direction)
    if abs(denominator) < 2.0e-4:
        return None
    difference = sub(second.start, first.start)
    first_distance = cross(difference, second.direction) / denominator
    second_distance = cross(difference, first.direction) / denominator
    first_point = add(first.start, mul(first_distance, first.direction))
    second_point = add(second.start, mul(second_distance, second.direction))
    if norm(sub(first_point, second_point)) > 0.08:
        return None
    first_time = first.start_time + first_distance / first.speed
    second_time = second.start_time + second_distance / second.speed
    if abs(first_time - second_time) > 0.00045:
        return None
    contact_time = (first_time + second_time) * 0.5
    point = mul(0.5, add(first_point, second_point))
    if (
        point[0] < -830.0
        or point[0] > 830.0
        or point[1] < -830.0
        or point[1] > 830.0
    ):
        return None
    if projected_gap(first, point, contact_time) is None:
        return None
    if projected_gap(second, point, contact_time) is None:
        return None
    return ContactCandidate(
        first=0,
        second=0,
        point=point,
        time=contact_time,
    )


def connected_components(count: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    adjacency = [set() for _ in range(count)]
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    seen: set[int] = set()
    components: list[list[int]] = []
    for root in range(count):
        if root in seen:
            continue
        stack = [root]
        component: list[int] = []
        seen.add(root)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current], reverse=True):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return components


def contact_partition(fragments: list[Fragment]) -> list[tuple[list[int], list[float], float]]:
    candidates: list[ContactCandidate] = []
    edges: list[tuple[int, int]] = []
    for first in range(len(fragments)):
        for second in range(first + 1, len(fragments)):
            candidate = line_contact(fragments[first], fragments[second])
            if candidate is None:
                continue
            candidate.first = first
            candidate.second = second
            candidates.append(candidate)
            edges.append((first, second))
    components = connected_components(len(fragments), edges)
    if any(len(component) not in (2, 4) for component in components):
        raise ResultAmbiguous("contact support does not partition into binary or quaternary events")
    by_pair = {
        (min(candidate.first, candidate.second), max(candidate.first, candidate.second)): candidate
        for candidate in candidates
    }
    result: list[tuple[list[int], list[float], float]] = []
    for component in components:
        matched: list[ContactCandidate] = []
        for offset, first in enumerate(component):
            for second in component[offset + 1 :]:
                candidate = by_pair.get((first, second))
                if candidate is not None:
                    matched.append(candidate)
        if not matched:
            raise ResultAmbiguous("contact component has no supporting edge")
        point = [
            sum(item.point[0] for item in matched) / len(matched),
            sum(item.point[1] for item in matched) / len(matched),
        ]
        contact_time = sum(item.time for item in matched) / len(matched)
        result.append((component, point, contact_time))
    result.sort(key=lambda item: (item[2], min(fragments[index].channel for index in item[0])))
    return result


def group_roles(
    component: list[int],
    fragments: list[Fragment],
    point: list[float],
    contact_time: float,
) -> tuple[list[str], list[str]]:
    incoming: list[str] = []
    outgoing: list[str] = []
    for index in component:
        role = projected_gap(fragments[index], point, contact_time)
        if role is None:
            raise ResultAmbiguous("fragment is not attached to its contact")
        if role[0] == "incoming":
            incoming.append(fragments[index].channel)
        else:
            outgoing.append(fragments[index].channel)
    if len(incoming) * 2 != len(component) or len(outgoing) * 2 != len(component):
        raise ResultAmbiguous("contact does not split evenly into incoming and outgoing")
    return sorted(incoming), sorted(outgoing)


def pairing_score(
    first_in: str,
    first_out: str,
    second_in: str,
    second_out: str,
    incoming: dict[str, list[float]],
    outgoing: dict[str, list[float]],
    masses: dict[str, float],
) -> float:
    first_mass = masses[first_in]
    second_mass = masses[second_in]
    mass_penalty = 0.0
    if abs(first_mass - masses[first_out]) > 0.001:
        mass_penalty += 1.0e6
    if abs(second_mass - masses[second_out]) > 0.001:
        mass_penalty += 1.0e6
    delta_first = mul(first_mass, sub(outgoing[first_out], incoming[first_in]))
    delta_second = mul(second_mass, sub(outgoing[second_out], incoming[second_in]))
    momentum = norm(add(delta_first, delta_second))
    alignment = norm(add(unit(delta_first), unit(delta_second)))
    relative_in = sub(incoming[first_in], incoming[second_in])
    relative_out = sub(outgoing[first_out], outgoing[second_out])
    normal = unit(delta_first)
    tangential_before = sub(relative_in, mul(dot(relative_in, normal), normal))
    tangential_after = sub(relative_out, mul(dot(relative_out, normal), normal))
    tangential_change = norm(sub(tangential_after, tangential_before))
    return mass_penalty + momentum + 10.0 * alignment + 0.02 * tangential_change


def restitution_from_pair(
    first_in: str,
    first_out: str,
    second_in: str,
    second_out: str,
    incoming: dict[str, list[float]],
    outgoing: dict[str, list[float]],
    normal: list[float],
) -> float:
    relative_in = dot(sub(incoming[first_in], incoming[second_in]), normal)
    relative_out = dot(sub(outgoing[first_out], outgoing[second_out]), normal)
    if not abs(relative_in) > 1e-9:
        raise ResultAmbiguous("normal relative velocity is too small")
    # The sorted incoming order may reverse the relative-velocity direction.
    # The coefficient is the magnitude of the normal relative-velocity ratio.
    value = abs(relative_out / relative_in)
    if not math.isfinite(value) or value < 0.0 or value > 1.2:
        raise ResultAmbiguous("restitution is outside the physical range")
    return value


def make_particle_event(
    component: list[int],
    point: list[float],
    contact_time: float,
    fragments: list[Fragment],
) -> InternalEvent:
    incoming_ids, outgoing_ids = group_roles(component, fragments, point, contact_time)
    fragment_by_channel = {item.channel: item for item in fragments}
    incoming_velocity = {
        item: fragment_by_channel[item].velocity for item in incoming_ids
    }
    outgoing_velocity = {
        item: fragment_by_channel[item].velocity for item in outgoing_ids
    }
    masses = {
        item: fragment_by_channel[item].mass_mg for item in incoming_ids + outgoing_ids
    }
    first_in, second_in = incoming_ids
    first_out, second_out = outgoing_ids
    first_assignment = (
        pairing_score(
            first_in,
            first_out,
            second_in,
            second_out,
            incoming_velocity,
            outgoing_velocity,
            masses,
        ),
        [[first_in, first_out], [second_in, second_out]],
    )
    second_assignment = (
        pairing_score(
            first_in,
            second_out,
            second_in,
            first_out,
            incoming_velocity,
            outgoing_velocity,
            masses,
        ),
        [[first_in, second_out], [second_in, first_out]],
    )
    score, pairs = min(first_assignment, second_assignment, key=lambda item: (item[0], item[1]))
    pairs = sorted(pairs, key=lambda item: item[0])
    if score > 95.0:
        raise ResultAmbiguous("no momentum-supported correspondence")
    delta_first = mul(
        masses[pairs[0][0]],
        sub(outgoing_velocity[pairs[0][1]], incoming_velocity[pairs[0][0]]),
    )
    delta_second = mul(
        masses[pairs[1][0]],
        sub(outgoing_velocity[pairs[1][1]], incoming_velocity[pairs[1][0]]),
    )
    if any(
        abs(masses[incoming] - masses[outgoing]) > 0.001
        for incoming, outgoing in pairs
    ):
        raise ResultAmbiguous("paired fragments have incompatible masses")
    normal = canonical(delta_first)
    restitution = restitution_from_pair(
        pairs[0][0],
        pairs[0][1],
        pairs[1][0],
        pairs[1][1],
        incoming_velocity,
        outgoing_velocity,
        normal,
    )
    momentum_error = norm(add(delta_first, delta_second))
    kinetic_in = sum(
        masses[item] * dot(incoming_velocity[item], incoming_velocity[item])
        for item in incoming_ids
    )
    kinetic_out = sum(
        masses[item] * dot(outgoing_velocity[item], outgoing_velocity[item])
        for item in outgoing_ids
    )
    energy_loss = max(0.0, 1.0 - kinetic_out / kinetic_in)
    return InternalEvent(
        channels=sorted(incoming_ids + outgoing_ids),
        kind="particle",
        point=point,
        time=contact_time,
        incoming=incoming_ids,
        outgoing=outgoing_ids,
        pairs=pairs,
        incoming_velocity=incoming_velocity,
        outgoing_velocity=outgoing_velocity,
        normal=normal,
        restitution=restitution,
        momentum_error=momentum_error,
        energy_loss=energy_loss,
    )


def make_wall_event(
    component: list[int],
    point: list[float],
    contact_time: float,
    fragments: list[Fragment],
) -> InternalEvent:
    incoming_ids, outgoing_ids = group_roles(component, fragments, point, contact_time)
    if len(incoming_ids) != 1 or len(outgoing_ids) != 1:
        raise ResultAmbiguous("wall contact must have one incoming and one outgoing fragment")
    fragment_by_channel = {item.channel: item for item in fragments}
    incoming_velocity = {incoming_ids[0]: fragment_by_channel[incoming_ids[0]].velocity}
    outgoing_velocity = {outgoing_ids[0]: fragment_by_channel[outgoing_ids[0]].velocity}
    if (
        abs(
            fragment_by_channel[incoming_ids[0]].mass_mg
            - fragment_by_channel[outgoing_ids[0]].mass_mg
        )
        > 0.001
    ):
        raise ResultAmbiguous("wall pair has incompatible masses")
    before = incoming_velocity[incoming_ids[0]]
    after = outgoing_velocity[outgoing_ids[0]]
    normal = unit(sub(after, before))
    if dot(before, normal) > 0.0:
        normal = [-normal[0], -normal[1]]
    normal_velocity = dot(before, normal)
    incident = normal_velocity < -1e-9
    if not incident:
        raise ResultAmbiguous("wall incident normal velocity is not inward")
    restitution = -dot(after, normal) / normal_velocity
    if restitution < 0.0 or restitution > 1.2:
        raise ResultAmbiguous("wall restitution is outside the physical range")
    energy_loss = max(0.0, 1.0 - dot(after, after) / dot(before, before))
    return InternalEvent(
        channels=sorted([incoming_ids[0], outgoing_ids[0]]),
        kind="wall",
        point=point,
        time=contact_time,
        incoming=incoming_ids,
        outgoing=outgoing_ids,
        pairs=[[incoming_ids[0], outgoing_ids[0]]],
        incoming_velocity=incoming_velocity,
        outgoing_velocity=outgoing_velocity,
        normal=normal,
        restitution=restitution,
        momentum_error=0.0,
        energy_loss=energy_loss,
    )


def event_id(event: InternalEvent) -> str:
    digest = hashlib.sha256("|".join(sorted(event.channels)).encode("utf-8")).hexdigest()
    return "EV-" + digest[:10].upper()


def track_id(channels: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(channels)).encode("utf-8")).hexdigest()
    return "TRK-" + digest[:10].upper()


def fit_normal(points: list[list[float]], reference: list[float]) -> list[float]:
    mean_x = sum(item[0] for item in points) / len(points)
    mean_y = sum(item[1] for item in points) / len(points)
    covariance_xx = sum((item[0] - mean_x) ** 2 for item in points) / len(points)
    covariance_yy = sum((item[1] - mean_y) ** 2 for item in points) / len(points)
    covariance_xy = sum((item[0] - mean_x) * (item[1] - mean_y) for item in points) / len(points)
    if abs(covariance_xy) < 1e-14 and abs(covariance_xx - covariance_yy) < 1e-14:
        return unit(reference)
    angle = 0.5 * math.atan2(2.0 * covariance_xy, covariance_xx - covariance_yy)
    tangent = [math.cos(angle), math.sin(angle)]
    normal = [-tangent[1], tangent[0]]
    if dot(normal, reference) < 0.0:
        normal = [-normal[0], -normal[1]]
    return normal


def angle_distance(first: list[float], second: list[float]) -> float:
    return abs(math.atan2(cross(first, second), dot(first, second)))


def build_wall_models(events: list[InternalEvent]) -> list[dict[str, Any]]:
    wall_events = [event for event in events if event.kind == "wall"]
    if len(wall_events) < 8:
        raise ResultAmbiguous("too few wall contacts to identify four surfaces")
    ordered = sorted(
        wall_events,
        key=lambda event: (math.atan2(event.normal[1], event.normal[0]), event_id(event)),
    )
    clusters: list[list[InternalEvent]] = []
    for event in ordered:
        target = None
        for index, cluster in enumerate(clusters):
            mean = unit(
                [
                    sum(dot(item.normal, [1.0, 0.0]) for item in cluster),
                    sum(dot(item.normal, [0.0, 1.0]) for item in cluster),
                ]
            )
            if not angle_distance(mean, event.normal) > math.radians(8.0):
                target = index
                break
        if target is None:
            clusters.append([event])
        else:
            clusters[target].append(event)
    if len(clusters) != 4 or any(len(cluster) < 2 for cluster in clusters):
        raise ResultAmbiguous("wall supports do not close into four surfaces")
    models: list[tuple[list[float], float, float, list[InternalEvent]]] = []
    for cluster in clusters:
        reference = unit(
            [
                sum(item.normal[0] for item in cluster),
                sum(item.normal[1] for item in cluster),
            ]
        )
        normal = fit_normal([item.point for item in cluster], reference)
        offsets = [dot(normal, item.point) for item in cluster]
        offset = statistics.median(offsets)
        restitutions = []
        for item in cluster:
            before = next(iter(item.incoming_velocity.values()))
            after = next(iter(item.outgoing_velocity.values()))
            value = -dot(after, normal) / dot(before, normal)
            if value < 0.0 or value > 1.2:
                raise ResultAmbiguous("wall model produces an invalid restitution")
            restitutions.append(value)
            item.normal = normal
            item.restitution = value
            item.energy_loss = max(0.0, 1.0 - dot(after, after) / dot(before, before))
        models.append((normal, offset, statistics.fmean(restitutions), cluster))
    models.sort(key=lambda item: math.atan2(item[0][1], item[0][0]))
    output = []
    for index, (normal, offset, restitution, cluster) in enumerate(models):
        for event in cluster:
            event.wall_cluster = index
        output.append(
            {
                "wall_id": WALL_LABELS[index],
                "normal": [round_half_up(value, 6) for value in normal],
                "offset_mm": round_half_up(offset, 3),
                "restitution": round_half_up(restitution, 4),
            }
        )
    return output


def find_root(parent: dict[str, str], item: str) -> str:
    parent.setdefault(item, item)
    if parent[item] != item:
        parent[item] = find_root(parent, parent[item])
    return parent[item]


def union(parent: dict[str, str], first: str, second: str) -> None:
    left = find_root(parent, first)
    right = find_root(parent, second)
    if left != right:
        parent[right] = left


def analyze_experiment(experiment: dict[str, Any]) -> tuple[dict[str, Any], list[InternalEvent]]:
    fragments = [fragment_from_sensor(item) for item in experiment["sensors"]]
    partition = contact_partition(fragments)
    events: list[InternalEvent] = []
    for component, point, contact_time in partition:
        if len(component) == 4:
            events.append(make_particle_event(component, point, contact_time, fragments))
        else:
            events.append(make_wall_event(component, point, contact_time, fragments))
    if not events:
        raise ResultAmbiguous("experiment has no supported events")
    parent: dict[str, str] = {}
    for fragment in fragments:
        find_root(parent, fragment.channel)
    for event in events:
        for incoming, outgoing in event.pairs:
            union(parent, incoming, outgoing)
    groups: dict[str, list[str]] = {}
    for fragment in fragments:
        groups.setdefault(find_root(parent, fragment.channel), []).append(fragment.channel)
    fragment_by_channel = {fragment.channel: fragment for fragment in fragments}
    tracks = [
        {
            "track_id": track_id(group),
            "sensor_ids": sorted(group),
            "mass_mg": round_half_up(
                sum(fragment_by_channel[item].mass_mg for item in group) / len(group),
                3,
            ),
        }
        for group in groups.values()
    ]
    tracks.sort(key=lambda item: item["track_id"])
    sensor_to_track = {
        channel: track_id(group)
        for group in groups.values()
        for channel in sorted(group)
    }
    ordered_events = sorted(events, key=lambda item: (item.time, event_id(item)))
    public_events = [
        {
            "event_id": event_id(item),
            "kind": item.kind,
            "contact_time_s": round_half_up(item.time, 6),
            "contact_mm": [round_half_up(value, 3) for value in item.point],
            "normal": [round_half_up(value, 6) for value in item.normal],
            "restitution": round_half_up(item.restitution, 4),
            "incoming_sensor_ids": item.incoming,
            "outgoing_sensor_ids": item.outgoing,
            "pairs": item.pairs,
        }
        for item in ordered_events
    ]
    return (
        {
            "experiment_id": experiment["experiment_id"],
            "sensor_to_track": sensor_to_track,
            "tracks": tracks,
            "events": public_events,
        },
        ordered_events,
    )


def build_report(raw: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    public_experiments = []
    all_events: list[InternalEvent] = []
    for experiment in payload["experiments"]:
        public, events = analyze_experiment(experiment)
        public_experiments.append(public)
        all_events.extend(events)
    wall_models = build_wall_models(all_events)
    # Wall normal refinement can change the event's rounded restitution. Rebuild
    # the experiment event objects after all wall parameters have been closed.
    public_experiments = []
    for experiment in payload["experiments"]:
        fragments = [fragment_from_sensor(item) for item in experiment["sensors"]]
        partition = contact_partition(fragments)
        events: list[InternalEvent] = []
        for component, point, contact_time in partition:
            if len(component) == 4:
                events.append(make_particle_event(component, point, contact_time, fragments))
            else:
                event = make_wall_event(component, point, contact_time, fragments)
                assigned = min(
                    range(4),
                    key=lambda index: angle_distance(
                        event.normal,
                        [
                            wall_models[index]["normal"][0],
                            wall_models[index]["normal"][1],
                        ],
                    ),
                )
                wall_normal = [
                    wall_models[assigned]["normal"][0],
                    wall_models[assigned]["normal"][1],
                ]
                before = next(iter(event.incoming_velocity.values()))
                after = next(iter(event.outgoing_velocity.values()))
                event.normal = wall_normal
                event.restitution = -dot(after, wall_normal) / dot(before, wall_normal)
                event.energy_loss = max(0.0, 1.0 - dot(after, after) / dot(before, before))
                event.wall_cluster = assigned
                events.append(event)
        parent: dict[str, str] = {}
        for fragment in fragments:
            find_root(parent, fragment.channel)
        for event in events:
            for incoming, outgoing in event.pairs:
                union(parent, incoming, outgoing)
        groups: dict[str, list[str]] = {}
        for fragment in fragments:
            groups.setdefault(find_root(parent, fragment.channel), []).append(fragment.channel)
        fragment_by_channel = {fragment.channel: fragment for fragment in fragments}
        tracks = [
            {
                "track_id": track_id(group),
                "sensor_ids": sorted(group),
                "mass_mg": round_half_up(
                    sum(fragment_by_channel[item].mass_mg for item in group) / len(group),
                    3,
                ),
            }
            for group in groups.values()
        ]
        tracks.sort(key=lambda item: item["track_id"])
        sensor_to_track = {
            channel: track_id(group)
            for group in groups.values()
            for channel in sorted(group)
        }
        events.sort(key=lambda item: (item.time, event_id(item)))
        public_experiments.append(
            {
                "experiment_id": experiment["experiment_id"],
                "sensor_to_track": sensor_to_track,
                "tracks": tracks,
                "events": [
                    {
                        "event_id": event_id(item),
                        "kind": item.kind,
                        "contact_time_s": round_half_up(item.time, 6),
                        "contact_mm": [round_half_up(value, 3) for value in item.point],
                        "normal": [round_half_up(value, 6) for value in item.normal],
                        "restitution": round_half_up(item.restitution, 4),
                        "incoming_sensor_ids": item.incoming,
                        "outgoing_sensor_ids": item.outgoing,
                        "pairs": item.pairs,
                    }
                    for item in events
                ],
            }
        )
    particle_events = [item for item in all_events if item.kind == "particle"]
    return {
        "schema_version": 1,
        "batch_id": payload["batch_id"],
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "experiments": public_experiments,
        "wall_models": wall_models,
        "summary": {
            "experiment_count": len(payload["experiments"]),
            "sensor_count": sum(len(item["sensors"]) for item in payload["experiments"]),
            "event_count": len(all_events),
            "particle_event_count": len(particle_events),
            "wall_event_count": len(all_events) - len(particle_events),
            "max_momentum_error": round_half_up(
                max((item.momentum_error for item in particle_events), default=0.0),
                6,
            ),
            "min_energy_loss": round_half_up(
                max(0.0, min(item.energy_loss for item in all_events)),
                6,
            ),
        },
    }


def compact_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def validate_ledger(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise LedgerError("ledger lacks a final newline")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerError("ledger is not UTF-8") from exc
    lines = text.splitlines()
    if not lines or any(not line for line in lines):
        raise LedgerError("ledger contains blank lines")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        try:
            entry = json.loads(line, object_pairs_hook=reject_duplicate_pairs)
        except (json.JSONDecodeError, InputError) as exc:
            raise LedgerError("ledger line is not valid JSON") from exc
        if not isinstance(entry, dict) or set(entry) != LEDGER_KEYS:
            raise LedgerError("ledger entry has the wrong key set")
        if type(entry["schema_version"]) is not int or entry["schema_version"] != 1:
            raise LedgerError("ledger schema_version is invalid")
        if not isinstance(entry["entry_id"], str) or ENTRY_RE.fullmatch(entry["entry_id"]) is None:
            raise LedgerError("ledger entry_id is invalid")
        if not isinstance(entry["batch_id"], str) or BATCH_RE.fullmatch(entry["batch_id"]) is None:
            raise LedgerError("ledger batch_id is invalid")
        if not isinstance(entry["input_sha256"], str) or HASH_RE.fullmatch(entry["input_sha256"]) is None:
            raise LedgerError("ledger input hash is invalid")
        if not isinstance(entry["report_sha256"], str) or HASH_RE.fullmatch(entry["report_sha256"]) is None:
            raise LedgerError("ledger report hash is invalid")
        expected_id = "LE-" + hashlib.sha256(
            f"{entry['input_sha256']}:{entry['report_sha256']}".encode("utf-8")
        ).hexdigest()[:12].upper()
        if entry["entry_id"] != expected_id:
            raise LedgerError("ledger entry_id does not match its hashes")
        if entry["entry_id"] in seen:
            raise LedgerError("ledger contains a duplicate entry_id")
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != line:
            raise LedgerError("ledger line is not canonical compact JSON")
        seen.add(entry["entry_id"])
        entries.append(entry)
    return entries


def ledger_entry(batch_id: str, input_hash: str, report_hash: str) -> dict[str, Any]:
    entry_id = "LE-" + hashlib.sha256(
        f"{input_hash}:{report_hash}".encode("utf-8")
    ).hexdigest()[:12].upper()
    return {
        "schema_version": 1,
        "entry_id": entry_id,
        "batch_id": batch_id,
        "input_sha256": input_hash,
        "report_sha256": report_hash,
    }


def read_existing(path: Path) -> tuple[str, bytes]:
    if not path.exists() and not path.is_symlink():
        return "missing", b""
    if path.is_symlink():
        raise ResultAmbiguous("output targets must not be symbolic links")
    if path.is_dir():
        return "directory", b""
    if not path.is_file():
        raise ResultAmbiguous("output target is not a regular file")
    return "file", path.read_bytes()


def stage_bytes(path: Path, data: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=".staging-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Path(temporary)


def restore_target(path: Path, kind: str, data: bytes, backup: Path | None) -> None:
    if kind == "missing":
        path.unlink(missing_ok=True)
    elif kind == "file":
        if backup is None:
            raise RuntimeError("missing rollback backup")
        os.replace(backup, path)
    elif kind == "directory":
        return


def publish_transaction(
    output_path: Path,
    report_bytes: bytes,
    ledger_path: Path,
    ledger_bytes: bytes,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    output_kind, output_old = read_existing(output_path)
    ledger_kind, ledger_old = read_existing(ledger_path)
    if ledger_kind != "file" and ledger_kind != "missing":
        # Keep the directory in place so the real second replacement fails.
        ledger_old = b""
    output_stage = stage_bytes(output_path, report_bytes)
    ledger_stage = stage_bytes(ledger_path, ledger_bytes)
    output_backup: Path | None = None
    ledger_backup: Path | None = None
    try:
        if output_kind == "file":
            output_backup = stage_bytes(output_path, output_old)
        if ledger_kind == "file":
            ledger_backup = stage_bytes(ledger_path, ledger_old)
        output_replaced = False
        try:
            os.replace(output_stage, output_path)
            output_replaced = True
            os.replace(ledger_stage, ledger_path)
        except OSError as exc:
            if output_replaced:
                restore_target(output_path, output_kind, output_old, output_backup)
                output_backup = None
            if ledger_kind == "file" and ledger_path.is_file():
                os.replace(ledger_backup, ledger_path)
                ledger_backup = None
            raise ResultAmbiguous("second target replacement failed") from exc
    finally:
        output_stage.unlink(missing_ok=True)
        ledger_stage.unlink(missing_ok=True)
        if output_backup is not None:
            output_backup.unlink(missing_ok=True)
        if ledger_backup is not None:
            ledger_backup.unlink(missing_ok=True)


def run(input_path: Path, ledger_path: Path, output_path: Path) -> None:
    raw = input_path.read_bytes()
    payload = decode_json(raw)
    validate_payload(payload)
    report = build_report(raw, payload)
    report_bytes = compact_bytes(report)
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    input_hash = hashlib.sha256(raw).hexdigest()
    entry = ledger_entry(payload["batch_id"], input_hash, report_hash)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        ledger_raw = ledger_path.read_bytes() if ledger_path.is_file() else b""
        entries = validate_ledger(ledger_raw)
        if all(item["entry_id"] != entry["entry_id"] for item in entries):
            line = json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            ledger_bytes = ledger_raw + line
        else:
            ledger_bytes = ledger_raw
        publish_transaction(output_path, report_bytes, ledger_path, ledger_bytes)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run(Path(args.input), Path(args.ledger), Path(args.output))
    except InputError:
        print("分析未写入: input_invalid", file=sys.stderr)
        return 2
    except LedgerError:
        print("分析未写入: ledger_invalid", file=sys.stderr)
        return 2
    except (ResultAmbiguous, OSError, RuntimeError):
        print("分析未写入: result_ambiguous", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
