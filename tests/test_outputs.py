from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


APP = os.environ.get("TASK_APP", "/app/analyze.py")
DATA = Path(os.environ.get("TASK_DATA", "/app/data/scenes.json"))
EXPECTED_PATH = Path(os.environ.get("TASK_EXPECTED", "/tests/expected.json"))
TOP_KEYS = {"schema_version", "batch_id", "input_sha256", "experiments", "wall_models", "summary"}
EXPERIMENT_KEYS = {"experiment_id", "sensor_to_track", "tracks", "events"}
TRACK_KEYS = {"track_id", "sensor_ids", "mass_mg"}
EVENT_KEYS = {
    "event_id",
    "kind",
    "contact_time_s",
    "contact_mm",
    "normal",
    "restitution",
    "incoming_sensor_ids",
    "outgoing_sensor_ids",
    "pairs",
}
WALL_KEYS = {"wall_id", "normal", "offset_mm", "restitution"}
SUMMARY_KEYS = {
    "experiment_count",
    "sensor_count",
    "event_count",
    "particle_event_count",
    "wall_event_count",
    "max_momentum_error",
    "min_energy_loss",
}
LEDGER_KEYS = {"schema_version", "entry_id", "batch_id", "input_sha256", "report_sha256"}


def _invoke(
    input_path: Path,
    ledger_path: Path,
    output_path: Path,
    timeout: int = 35,
) -> SimpleNamespace:
    started = time.monotonic()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            APP,
            "--input",
            str(input_path),
            "--ledger",
            str(ledger_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
        check=False,
    )
    elapsed = time.monotonic() - started
    return SimpleNamespace(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed=elapsed,
        output_path=output_path,
        ledger_path=ledger_path,
    )


@pytest.fixture(scope="session")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    root = tmp_path_factory.mktemp("baseline")
    output_path = root / "report.json"
    ledger_path = root / "ledger.jsonl"
    result = _invoke(DATA, ledger_path, output_path)
    report = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return SimpleNamespace(
        root=root,
        output_path=output_path,
        ledger_path=ledger_path,
        result=result,
        report=report,
        expected=expected,
    )


def _load_data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _write_payload(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _copy_payload() -> dict:
    return json.loads(json.dumps(_load_data()))


def _old_outputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    output_path = root / "report.json"
    ledger_path = root / "ledger.jsonl"
    output_path.write_bytes(b'{"old":"report"}\n')
    ledger_path.write_bytes(b'{"old":"ledger"}\n')
    return output_path, ledger_path


def _event_map(experiments: list[dict]) -> dict[str, dict]:
    return {
        event["event_id"]: event
        for experiment in experiments
        for event in experiment["events"]
    }


def _channel_mass(experiment: dict) -> dict[str, float]:
    return {sensor["channel"]: float(sensor["mass_mg"]) for sensor in experiment["sensors"]}


def _write_oversize(path: Path, extra: int) -> None:
    raw = DATA.read_bytes()
    path.write_bytes(raw + b" " * max(0, 262144 - len(raw) + extra))


def _case(case_id: str) -> None:
    print(f"CASE_ID={case_id}")


def _duplicate_top_level(path: Path) -> None:
    text = DATA.read_text(encoding="utf-8")
    needle = '"schema_version": 1,'
    path.write_text(
        text.replace(needle, needle + '\n  "schema_version": 1,', 1),
        encoding="utf-8",
    )


def test_candidate_help_is_available() -> None:
    """题面：“入口是 `/app/analyze.py`，日常由批次调度器直接调用。”"""
    _case("HELP-01")
    completed = subprocess.run(
        [sys.executable, APP, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert "--input" in completed.stdout
    assert "--ledger" in completed.stdout
    assert "--output" in completed.stdout


def test_reference_batch_partition_is_complete(baseline: SimpleNamespace) -> None:
    """题面：“`experiments` 按输入顺序排列。”"""
    _case("PART-01")
    assert baseline.result.returncode == 0
    assert baseline.report["summary"]["sensor_count"] == 72
    assert baseline.report["summary"]["event_count"] == 24
    assert baseline.report["summary"]["particle_event_count"] == 12
    assert baseline.report["summary"]["wall_event_count"] == 12


def test_success_channels_are_silent(baseline: SimpleNamespace) -> None:
    """题面：“正常退出不会在标准输出或标准错误留下文字。”"""
    _case("SILENT-01")
    assert baseline.result.returncode == 0
    assert baseline.result.stdout == ""
    assert baseline.result.stderr == ""


def test_structural_refusal_preserves_both_targets(
    tmp_path: Path,
) -> None:
    """题面：“报告和账本保持调用前的字节；不得残留暂存、备份或半写文件。”"""
    _case("REFUSE-01")
    output_path, ledger_path = _old_outputs(tmp_path / "case_01")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["sensors"][0].pop("mass_mg")
    input_path = tmp_path / "case_01" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_report_top_level_schema_is_exact(baseline: SimpleNamespace) -> None:
    """题面：“报告根层依次放版本字段 `schema_version`、批次字段 `batch_id`、输入摘要 `input_sha256`、实验清单 `experiments`、壁面模型 `wall_models` 和汇总 `summary`，共六段。”"""
    _case("SCHEMA-01")
    assert set(baseline.report) == TOP_KEYS
    assert baseline.report["schema_version"] == 1
    assert baseline.report["batch_id"] == "QS-7A2C-91D0-4B6E"
    assert set(baseline.report["summary"]) == SUMMARY_KEYS


def test_experiment_track_and_event_schema_is_exact(baseline: SimpleNamespace) -> None:
    """题面：“每份实验输出里，先放 `experiment_id`；其余三块分别叫 `sensor_to_track`、`tracks`、`events`。”"""
    _case("SCHEMA-02")
    experiments = baseline.report["experiments"]
    assert all(set(item) == EXPERIMENT_KEYS for item in experiments)
    assert all(
        set(track) == TRACK_KEYS
        for experiment in experiments
        for track in experiment["tracks"]
    )
    assert all(
        set(event) == EVENT_KEYS
        for experiment in experiments
        for event in experiment["events"]
    )
    assert all(set(wall) == WALL_KEYS for wall in baseline.report["wall_models"])


def test_sensor_track_map_and_mass_recover_unknown_correspondence(
    baseline: SimpleNamespace,
) -> None:
    """题面：“`sensor_to_track` 恰好覆盖该实验全部通道，值为恢复出的轨迹编号。”"""
    _case("MAP-01")
    actual = [item["sensor_to_track"] for item in baseline.report["experiments"]]
    expected = [item["sensor_to_track"] for item in baseline.expected["experiments"]]
    actual_mass = [
        sorted(track["mass_mg"] for track in item["tracks"])
        for item in baseline.report["experiments"]
    ]
    expected_mass = [
        sorted(track["mass_mg"] for track in item["tracks"])
        for item in baseline.expected["experiments"]
    ]
    assert actual == expected
    assert actual_mass == expected_mass


def test_particle_pairs_use_mass_and_momentum_closure(
    baseline: SimpleNamespace,
) -> None:
    """题面：“同一对两侧质量相差不得超过 `0.001` 毫克”"""
    _case("PAIR-01")
    actual = {
        event["event_id"]: event["pairs"]
        for experiment in baseline.report["experiments"]
        for event in experiment["events"]
        if event["kind"] == "particle"
    }
    expected = {
        event["event_id"]: event["pairs"]
        for experiment in baseline.expected["experiments"]
        for event in experiment["events"]
        if event["kind"] == "particle"
    }
    masses = {
        sensor["channel"]: sensor["mass_mg"]
        for experiment in _load_data()["experiments"]
        for sensor in experiment["sensors"]
    }
    assert actual == expected
    assert all(
        abs(masses[first] - masses[second]) <= 0.001
        for pairs in actual.values()
        for first, second in pairs
    )


def test_wall_models_recover_contact_support(baseline: SimpleNamespace) -> None:
    """题面：“`wall_models` 恰好四项，顺序为 `W-A`、`W-B`、`W-C`、`W-D`。”"""
    _case("WALL-01")
    walls = baseline.report["wall_models"]
    assert [wall["wall_id"] for wall in walls] == ["W-A", "W-B", "W-C", "W-D"]
    assert len(walls) == 4
    assert all(math.isclose(sum(value * value for value in wall["normal"]), 1.0, abs_tol=1e-5) for wall in walls)
    assert all(0.0 <= wall["restitution"] <= 1.0 for wall in walls)
    assert len({wall["restitution"] for wall in walls}) > 1


def test_repeated_batch_is_ledger_byte_idempotent(baseline: SimpleNamespace) -> None:
    """题面：“相同输入和相同报告再次运行，报告字节不变，账本一个字节也不得增加”"""
    _case("LEDGER-01")
    first_report = baseline.output_path.read_bytes()
    first_ledger = baseline.ledger_path.read_bytes()
    result = _invoke(DATA, baseline.ledger_path, baseline.output_path)
    assert result.returncode == 0
    assert baseline.output_path.read_bytes() == first_report
    assert baseline.ledger_path.read_bytes() == first_ledger
    assert len(first_ledger.splitlines()) == 1


def test_report_serialization_uses_compact_sorted_bytes(
    baseline: SimpleNamespace,
) -> None:
    """题面：“报告写成 UTF-8、按键名升序、无空格的单行 JSON，并在末尾保留一个换行”"""
    _case("BYTES-01")
    raw = baseline.output_path.read_bytes()
    canonical = (
        json.dumps(
            baseline.report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert raw == canonical
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert b'"wall_models":' in raw


def test_input_hash_uses_original_bytes(baseline: SimpleNamespace) -> None:
    """题面：“`input_sha256` 是输入文件原始字节的 SHA-256 小写十六进制。”"""
    _case("HASH-01")
    expected_hash = hashlib.sha256(DATA.read_bytes()).hexdigest()
    assert baseline.report["input_sha256"] == expected_hash
    assert re.fullmatch(r"[0-9a-f]{64}", baseline.report["input_sha256"])


def test_experiment_order_follows_input(baseline: SimpleNamespace) -> None:
    """题面：“`experiments` 按输入顺序排列。”"""
    _case("ORDER-01")
    input_ids = [item["experiment_id"] for item in _load_data()["experiments"]]
    output_ids = [item["experiment_id"] for item in baseline.report["experiments"]]
    assert output_ids == input_ids


def test_event_order_and_event_id_are_canonical(baseline: SimpleNamespace) -> None:
    """题面：“`event_id` 是 `EV-` 加上事件内全部通道名排序后拼接串的 SHA-256 前十个大写十六进制字符。”"""
    _case("EVENT-01")
    events = baseline.report["experiments"][0]["events"]
    recalculated = {
        event["event_id"]: "EV-"
        + hashlib.sha256(
            "|".join(sorted(event["incoming_sensor_ids"] + event["outgoing_sensor_ids"])).encode("utf-8")
        ).hexdigest()[:10].upper()
        for event in events
    }
    ordered = sorted(events, key=lambda event: (event["contact_time_s"], event["event_id"]))
    assert [event["event_id"] for event in events] == [event["event_id"] for event in ordered]
    assert all(event_id == recalculated[event_id] for event_id in recalculated)


def test_track_ids_are_sorted_and_hashed(baseline: SimpleNamespace) -> None:
    """题面：“`tracks` 按 `track_id` 升序排列”"""
    _case("TRACK-01")
    tracks = [
        track
        for experiment in baseline.report["experiments"]
        for track in experiment["tracks"]
    ]
    recalculated = {
        track["track_id"]: "TRK-"
        + hashlib.sha256("|".join(sorted(track["sensor_ids"])).encode("utf-8")).hexdigest()[:10].upper()
        for track in tracks
    }
    assert [track["track_id"] for track in baseline.report["experiments"][0]["tracks"]] == sorted(
        track["track_id"] for track in baseline.report["experiments"][0]["tracks"]
    )
    assert all(track_id == recalculated[track_id] for track_id in recalculated)


def test_contact_position_uses_euclidean_tolerance(
    baseline: SimpleNamespace,
) -> None:
    """题面：“接触位置按欧氏距离计算，容差为 `0.35` 毫米”"""
    _case("POS-01")
    actual = _event_map(baseline.report["experiments"])
    expected = _event_map(baseline.expected["experiments"])
    distances = [
        math.hypot(
            actual[event_id]["contact_mm"][0] - expected[event_id]["contact_mm"][0],
            actual[event_id]["contact_mm"][1] - expected[event_id]["contact_mm"][1],
        )
        for event_id in expected
    ]
    assert len(actual) == len(expected)
    assert max(distances) <= 0.35


def test_particle_normal_direction_uses_true_angle(
    baseline: SimpleNamespace,
) -> None:
    """题面：“法向方向按真实夹角计算，容差为 `0.35` 度”"""
    _case("NDIR-P-01")
    actual = _event_map(baseline.report["experiments"])
    expected = _event_map(baseline.expected["experiments"])
    particle_ids = [event_id for event_id, event in expected.items() if event["kind"] == "particle"]
    angles = [
        abs(
            math.atan2(
                actual[event_id]["normal"][0] * expected[event_id]["normal"][1]
                - actual[event_id]["normal"][1] * expected[event_id]["normal"][0],
                actual[event_id]["normal"][0] * expected[event_id]["normal"][0]
                + actual[event_id]["normal"][1] * expected[event_id]["normal"][1],
            )
        )
        for event_id in particle_ids
    ]
    assert len(particle_ids) == 12
    assert math.degrees(max(angles)) <= 0.35


def test_particle_restitution_uses_normal_relative_velocity(
    baseline: SimpleNamespace,
) -> None:
    """题面：“恢复系数容差为 `0.0015`。”"""
    _case("REST-P-01")
    actual = _event_map(baseline.report["experiments"])
    expected = _event_map(baseline.expected["experiments"])
    particle_ids = [event_id for event_id, event in expected.items() if event["kind"] == "particle"]
    assert max(abs(actual[event_id]["restitution"] - expected[event_id]["restitution"]) for event_id in particle_ids) <= 0.0015


def test_wall_normal_direction_uses_true_angle(
    baseline: SimpleNamespace,
) -> None:
    """题面：“法向方向按真实夹角计算，容差为 `0.35` 度”"""
    _case("NDIR-W-01")
    actual = baseline.report["wall_models"]
    expected = baseline.expected["wall_models"]
    angles = [
        abs(
            math.atan2(
                got["normal"][0] * want["normal"][1] - got["normal"][1] * want["normal"][0],
                got["normal"][0] * want["normal"][0] + got["normal"][1] * want["normal"][1],
            )
        )
        for got, want in zip(actual, expected)
    ]
    assert len(actual) == 4
    assert math.degrees(max(angles)) <= 0.35


def test_wall_offset_is_independent_signed_distance(
    baseline: SimpleNamespace,
) -> None:
    """题面：“壁线有符号偏移按外法向点积计算，容差为 `0.45` 毫米”"""
    _case("OFFSET-01")
    actual = baseline.report["wall_models"]
    expected = baseline.expected["wall_models"]
    differences = [abs(got["offset_mm"] - want["offset_mm"]) for got, want in zip(actual, expected)]
    assert len(differences) == 4
    assert max(differences) <= 0.45


def test_wall_restitution_closes_each_surface(
    baseline: SimpleNamespace,
) -> None:
    """题面：“恢复系数容差为 `0.0015`。”"""
    _case("REST-W-01")
    actual = baseline.report["wall_models"]
    expected = baseline.expected["wall_models"]
    differences = [abs(got["restitution"] - want["restitution"]) for got, want in zip(actual, expected)]
    inside_actual = copy.deepcopy(expected)
    inside_actual[0]["restitution"] += 0.0014
    inside_differences = [
        abs(got["restitution"] - want["restitution"])
        for got, want in zip(inside_actual, expected)
    ]
    outside_actual = copy.deepcopy(expected)
    outside_actual[0]["restitution"] += 0.0016
    outside_differences = [
        abs(got["restitution"] - want["restitution"])
        for got, want in zip(outside_actual, expected)
    ]
    assert len(differences) == 4
    assert max(differences) <= 0.0015
    assert max(inside_differences) <= 0.0015
    assert max(outside_differences) > 0.0015


def test_momentum_summary_reports_particle_residual(
    baseline: SimpleNamespace,
) -> None:
    """题面：“`max_momentum_error` 是全部粒子碰撞中 `|delta p_1 + delta p_2|` 的最大值”"""
    _case("MOM-01")
    value = baseline.report["summary"]["max_momentum_error"]
    assert value >= 0.0
    assert value <= 50.0
    assert baseline.report["summary"]["particle_event_count"] == 12


def test_energy_summary_is_nonnegative_and_close(
    baseline: SimpleNamespace,
) -> None:
    """题面：“`min_energy_loss` 是按 `1 - K_out/K_in` 计算的全部粒子与壁面事件最小值”"""
    _case("ENERGY-01")
    value = baseline.report["summary"]["min_energy_loss"]
    expected = baseline.expected["summary"]["min_energy_loss"]
    assert value >= 0.0
    assert abs(value - expected) <= 0.02
    assert value < 0.25


def test_rounding_precision_and_negative_zero_are_controlled(
    baseline: SimpleNamespace,
) -> None:
    """题面：“所有舍入都按十进制 `ROUND_HALF_UP`，不接受二进制浮点的银行家舍入。”"""
    _case("ROUND-01")
    text = baseline.output_path.read_text(encoding="utf-8")
    contact_times = re.findall(r'"contact_time_s":-?\d+\.\d+', text)
    contact_points = re.findall(r'"contact_mm":\[[^\]]+\]', text)
    restitutions = re.findall(r'"restitution":-?\d+\.\d+', text)
    point_numbers = [
        number
        for point in contact_points
        for number in re.findall(r"-?\d+\.\d+", point)
    ]
    assert len(contact_times) == 24
    assert all(len(value.split(".")[1]) <= 6 for value in contact_times)
    assert all(len(value.split(".")[1]) <= 3 for value in point_numbers)
    assert all(len(value.split(":")[1].split(".")[1]) <= 4 for value in restitutions)
    assert re.search(r"-0\.0+(?=[,}\]])", text) is None


def test_first_ledger_entry_has_exact_schema_and_digest(
    baseline: SimpleNamespace,
) -> None:
    """题面：“账本每行恰好包含 `schema_version`、`entry_id`、`batch_id`、`input_sha256`、`report_sha256` 五个键”"""
    _case("LEDGER-02")
    raw_lines = baseline.ledger_path.read_bytes().splitlines()
    entry = json.loads(raw_lines[0])
    report_hash = hashlib.sha256(baseline.output_path.read_bytes()).hexdigest()
    entry_id = "LE-" + hashlib.sha256(
        f"{baseline.report['input_sha256']}:{report_hash}".encode("utf-8")
    ).hexdigest()[:12].upper()
    assert len(raw_lines) == 1
    assert set(entry) == LEDGER_KEYS
    assert entry["report_sha256"] == report_hash
    assert entry["input_sha256"] == baseline.report["input_sha256"]
    assert entry["entry_id"] == entry_id


def test_new_success_appends_one_ledger_line(tmp_path: Path) -> None:
    """题面：“新的成功结果只追加一行，已有行不改写、不重排。”"""
    _case("LEDGER-03")
    root = tmp_path / "case_02"
    root.mkdir()
    ledger_path = root / "ledger.jsonl"
    output_path = root / "report.json"
    input_path = root / "input.json"
    payload = _copy_payload()
    _write_payload(input_path, payload)
    first = _invoke(input_path, ledger_path, output_path)
    first_lines = ledger_path.read_bytes().splitlines()
    payload["batch_id"] = "QS-5B1D-02E7-C993"
    _write_payload(input_path, payload)
    second = _invoke(input_path, ledger_path, output_path)
    second_lines = ledger_path.read_bytes().splitlines()
    assert first.returncode == 0
    assert second.returncode == 0
    assert len(first_lines) == 1
    assert len(second_lines) == 2
    assert second_lines[0] == first_lines[0]


def test_duplicate_top_level_key_is_refused(tmp_path: Path) -> None:
    """题面：“所有对象都拒绝重复键”"""
    _case("SCHEMA-03")
    output_path, ledger_path = _old_outputs(tmp_path / "case_03")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    input_path = tmp_path / "case_03" / "input.json"
    _duplicate_top_level(input_path)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_missing_sensor_mass_is_refused(tmp_path: Path) -> None:
    """题面：“一条传感器记录由四段数据组成：通道信息写 `channel`，时间窗写 `time_s`，两个端点写 `points_mm`，质量估计写 `mass_mg`。”"""
    _case("SCHEMA-04")
    output_path, ledger_path = _old_outputs(tmp_path / "case_04")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["sensors"][0].pop("mass_mg")
    input_path = tmp_path / "case_04" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_extra_nested_key_is_refused(tmp_path: Path) -> None:
    """题面：“缺键、额外键、错误类型、越界值”"""
    _case("SCHEMA-05")
    output_path, ledger_path = _old_outputs(tmp_path / "case_05")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["sensors"][0]["quality"] = 0.9
    input_path = tmp_path / "case_05" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_boolean_schema_version_is_not_an_integer(tmp_path: Path) -> None:
    """题面：“版本号以 JSON 整数 `1` 表示，布尔 `true`/`false` 不能替代。”"""
    _case("TYPE-01")
    output_path, ledger_path = _old_outputs(tmp_path / "case_06")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["schema_version"] = True
    input_path = tmp_path / "case_06" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_input_at_exact_byte_limit_is_accepted(
    tmp_path: Path,
    baseline: SimpleNamespace,
) -> None:
    """题面：“待读的实验文件采用 UTF-8 编码，文件体量不超过 `262144` 字节。”"""
    _case("BOUND-01")
    print("HARDCODE-BASE INPUT_SCENARIO_BASE")
    root = tmp_path / "case_07"
    root.mkdir()
    input_path = root / "input.json"
    _write_oversize(input_path, 0)
    result = _invoke(input_path, root / "ledger.jsonl", root / "report.json")
    assert input_path.stat().st_size == 262144
    assert result.returncode == 0
    assert result.output_path.exists()
    payload = _copy_payload()
    first_start = min(
        sensor["time_s"][0]
        for experiment in payload["experiments"]
        for sensor in experiment["sensors"]
    )
    for experiment in payload["experiments"]:
        for sensor in experiment["sensors"]:
            sensor["time_s"][0] -= first_start
            sensor["time_s"][1] -= first_start
    shifted_root = tmp_path / "bound-time-start"
    shifted_root.mkdir()
    shifted_input = shifted_root / "input.json"
    shifted_report = shifted_root / "report.json"
    shifted_ledger = shifted_root / "ledger.jsonl"
    _write_payload(shifted_input, payload)
    shifted = _invoke(shifted_input, shifted_ledger, shifted_report)
    assert shifted.returncode == 0
    assert shifted.output_path.is_file()
    assert shifted.ledger_path.is_file()
    print("HARDCODE-VARIANT INPUT_SCENARIO_SHIFTED_TIME")
    shifted_result = json.loads(shifted.output_path.read_text(encoding="utf-8"))
    baseline_events = sorted(
        (
            event
            for experiment in baseline.report["experiments"]
            for event in experiment["events"]
        ),
        key=lambda event: event["event_id"],
    )
    shifted_events = sorted(
        (
            event
            for experiment in shifted_result["experiments"]
            for event in experiment["events"]
        ),
        key=lambda event: event["event_id"],
    )
    assert [event["event_id"] for event in shifted_events] == [
        event["event_id"] for event in baseline_events
    ]
    assert all(
        shifted_event["contact_time_s"]
        == pytest.approx(event["contact_time_s"] - first_start, abs=0.000002)
        for event, shifted_event in zip(baseline_events, shifted_events)
    )


def test_input_one_byte_over_limit_is_refused(tmp_path: Path) -> None:
    """题面：“待读的实验文件采用 UTF-8 编码，文件体量不超过 `262144` 字节。”"""
    _case("BOUND-02")
    output_path, ledger_path = _old_outputs(tmp_path / "case_08")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    input_path = tmp_path / "case_08" / "input.json"
    _write_oversize(input_path, 1)
    result = _invoke(input_path, ledger_path, output_path)
    assert input_path.stat().st_size == 262145
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_sensor_count_must_be_even_and_in_range(tmp_path: Path) -> None:
    """题面：“每个实验的 `sensors` 是长度 `8` 到 `48` 的偶数数组”"""
    _case("RANGE-01")
    output_path, ledger_path = _old_outputs(tmp_path / "case_09")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["sensors"] = payload["experiments"][0]["sensors"][:-1]
    input_path = tmp_path / "case_09" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_duplicate_channel_is_refused(tmp_path: Path) -> None:
    """题面：“实验之间可以重复使用通道号，同一实验内不能重复。”"""
    _case("UNIQUE-01")
    output_path, ledger_path = _old_outputs(tmp_path / "case_10")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["sensors"][1]["channel"] = payload["experiments"][0]["sensors"][0]["channel"]
    input_path = tmp_path / "case_10" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_duplicate_experiment_id_is_refused(tmp_path: Path) -> None:
    """题面：“`experiments` 内不能重复 `experiment_id`。”"""
    _case("UNIQUE-02")
    output_path, ledger_path = _old_outputs(tmp_path / "case_11")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][1]["experiment_id"] = payload["experiments"][0]["experiment_id"]
    input_path = tmp_path / "case_11" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_wall_label_enumeration_is_exact(tmp_path: Path) -> None:
    """题面：“`wall_labels` 恰好是 `[\"W-A\", \"W-B\", \"W-C\", \"W-D\"]`，顺序也不能变。”"""
    _case("ENUM-01")
    output_path, ledger_path = _old_outputs(tmp_path / "case_12")
    old_report = output_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    payload = _copy_payload()
    payload["experiments"][0]["wall_labels"][0] = "W-Z"
    input_path = tmp_path / "case_12" / "input.json"
    _write_payload(input_path, payload)
    result = _invoke(input_path, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: input_invalid\n"
    assert output_path.exists()
    assert ledger_path.exists()
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger
    assert not list(output_path.parent.glob(".staging-*"))
    assert not list(output_path.parent.glob("*.bak"))


def test_malformed_ledger_preserves_report_and_ledger(tmp_path: Path) -> None:
    """题面：“输入损坏、账本损坏或计算结果无法唯一闭合时，进程退出码为 `2`”"""
    _case("LEDGER-04")
    output_path, ledger_path = _old_outputs(tmp_path / "case_13")
    old_report = output_path.read_bytes()
    ledger_path.write_text('{"schema_version":1,"bad":true}\n', encoding="utf-8")
    old_ledger = ledger_path.read_bytes()
    result = _invoke(DATA, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "分析未写入: ledger_invalid\n"
    assert output_path.read_bytes() == old_report
    assert ledger_path.read_bytes() == old_ledger


def test_second_target_replace_failure_rolls_back(tmp_path: Path) -> None:
    """题面：“运行时无法完成两个目标的替换时，错误分类为 `result_ambiguous`”"""
    _case("PUB-01")
    root = tmp_path / "case_14"
    root.mkdir()
    output_path = root / "report.json"
    output_path.write_bytes(b'{"old":"report"}\n')
    old_report = output_path.read_bytes()
    ledger_path = root / "ledger.jsonl"
    ledger_path.mkdir()
    result = _invoke(DATA, ledger_path, output_path)
    assert result.returncode == 2
    assert result.stderr == "分析未写入: result_ambiguous\n"
    assert output_path.read_bytes() == old_report
    assert ledger_path.is_dir()
    assert not list(root.glob(".staging-*"))
    assert not list(root.glob("*.bak"))


def test_batch_completes_within_public_limit(baseline: SimpleNamespace) -> None:
    """题面：“从入口启动到成功返回的公开上限是 `30` 秒。”"""
    _case("TIME-01")
    completed = _invoke(DATA, baseline.root / "timed-ledger.jsonl", baseline.root / "timed-report.json", timeout=35)
    assert completed.returncode == 0
    assert completed.elapsed < 30.0
    assert completed.output_path.exists()
