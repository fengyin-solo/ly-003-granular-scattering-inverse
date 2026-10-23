#!/usr/bin/env python3
"""Legacy scattering-session reporter used by the laboratory batch host."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a scattering fragment export into a laboratory report."
    )
    parser.add_argument("--input", required=True, help="fragment export JSON")
    parser.add_argument("--ledger", required=True, help="append-only JSONL ledger")
    parser.add_argument("--output", required=True, help="report JSON")
    return parser.parse_args(argv)


def event_token(channels: list[str]) -> str:
    joined = "|".join(sorted(channels)).encode("utf-8")
    return "EV-" + hashlib.sha256(joined).hexdigest()[:10].upper()


def track_token(channels: list[str]) -> str:
    joined = "|".join(sorted(channels)).encode("utf-8")
    return "TRK-" + hashlib.sha256(joined).hexdigest()[:10].upper()


def velocity(sensor: dict[str, Any]) -> list[float]:
    start, end = sensor["time_s"]
    first = sensor["points_mm"][0]
    second = sensor["points_mm"][1]
    duration = float(end) - float(start)
    return [
        (float(second[0]) - float(first[0])) / duration,
        (float(second[1]) - float(first[1])) / duration,
    ]


def sensor_mass(sensor: dict[str, Any]) -> float:
    return round(float(sensor.get("mass_mg", 1000.0)), 3)


def chunk_sensors(sensors: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(sensors, key=lambda item: (item["time_s"][0], item["channel"]))
    chunks: list[list[dict[str, Any]]] = []
    cursor = 0
    while cursor + 4 <= len(ordered):
        chunks.append(ordered[cursor : cursor + 4])
        cursor += 4
    if cursor < len(ordered):
        chunks.append(ordered[cursor:])
    return chunks


def make_event(chunk: list[dict[str, Any]]) -> dict[str, Any]:
    channels = [item["channel"] for item in chunk]
    split = max(1, len(chunk) // 2)
    incoming = sorted(channels[:split])
    outgoing = sorted(channels[split:])
    first = chunk[0]
    second = chunk[min(1, len(chunk) - 1)]
    point_a = first["points_mm"][1]
    point_b = second["points_mm"][0]
    contact = [
        round((float(point_a[0]) + float(point_b[0])) * 0.5, 3),
        round((float(point_a[1]) + float(point_b[1])) * 0.5, 3),
    ]
    contact_time = round(
        (float(first["time_s"][1]) + float(second["time_s"][0])) * 0.5, 6
    )
    pair_count = min(len(incoming), len(outgoing))
    pairs = [[incoming[index], outgoing[index]] for index in range(pair_count)]
    return {
        "event_id": event_token(channels),
        "kind": "particle" if len(chunk) == 4 else "wall",
        "contact_time_s": contact_time,
        "contact_mm": contact,
        "normal": [1.0, 0.0],
        "restitution": 0.8,
        "incoming_sensor_ids": incoming,
        "outgoing_sensor_ids": outgoing,
        "pairs": pairs,
    }


def make_experiment(experiment: dict[str, Any]) -> dict[str, Any]:
    chunks = chunk_sensors(experiment["sensors"])
    events = [make_event(chunk) for chunk in chunks]
    track_groups: list[list[str]] = []
    for chunk in chunks:
        channels = sorted(item["channel"] for item in chunk)
        if len(channels) >= 2:
            track_groups.append(channels)
    mass_by_channel = {
        sensor["channel"]: sensor_mass(sensor)
        for sensor in experiment["sensors"]
    }
    tracks = [
        {
            "track_id": track_token(group),
            "sensor_ids": group,
            "mass_mg": round(sum(mass_by_channel[item] for item in group) / len(group), 3),
        }
        for group in sorted(track_groups, key=track_token)
    ]
    sensor_to_track = {
        channel: track_token(group)
        for group in track_groups
        for channel in group
    }
    return {
        "experiment_id": experiment["experiment_id"],
        "sensor_to_track": sensor_to_track,
        "tracks": tracks,
        "events": sorted(events, key=lambda item: (item["contact_time_s"], item["event_id"])),
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


def build_report(raw: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    experiments = [
        make_experiment(experiment) for experiment in payload.get("experiments", [])
    ]
    events = [
        event
        for experiment in experiments
        for event in experiment["events"]
    ]
    walls = [
        {
            "wall_id": label,
            "normal": [1.0, 0.0],
            "offset_mm": 0.0,
            "restitution": 0.8,
        }
        for label in ["W-A", "W-B", "W-C", "W-D"]
    ]
    return {
        "schema_version": 1,
        "batch_id": payload.get("batch_id", ""),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "experiments": experiments,
        "wall_models": walls,
        "summary": {
            "experiment_count": len(experiments),
            "sensor_count": sum(
                len(experiment.get("sensors", [])) for experiment in payload.get("experiments", [])
            ),
            "event_count": len(events),
            "particle_event_count": sum(event["kind"] == "particle" for event in events),
            "wall_event_count": sum(event["kind"] == "wall" for event in events),
            "max_momentum_error": 0.0,
            "min_energy_loss": 0.0,
        },
    }


def load_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            entries.append(json.loads(line))
    return entries


def write_result(
    raw: bytes,
    payload: dict[str, Any],
    ledger_path: Path,
    output_path: Path,
) -> None:
    report = build_report(raw, payload)
    report_bytes = compact_bytes(report)
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    input_hash = hashlib.sha256(raw).hexdigest()
    entry_id = "LE-" + hashlib.sha256(
        f"{input_hash}:{report_hash}".encode("utf-8")
    ).hexdigest()[:12].upper()
    entry = {
        "schema_version": 1,
        "entry_id": entry_id,
        "batch_id": payload.get("batch_id", ""),
        "input_sha256": input_hash,
        "report_sha256": report_hash,
    }
    entries = load_ledger(ledger_path)
    if all(item.get("entry_id") != entry_id for item in entries):
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(report_bytes)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        raw = Path(args.input).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        write_result(raw, payload, Path(args.ledger), Path(args.output))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        print("分析未写入: input_invalid", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
