"""raw層に対するデータ検証（正規化より前に実行する）。

パイプラインは raw → validate → normalized → report の順で責務を分離する
（正規化はここでは行わない。normalize.py の責務）。

status=="fail" の場合は DataQualityError を送出し、呼び出し側で
レポート生成に進まないようにする（Fail Fast）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from src_v2.schema import (
    ACQUISITION_STATUS_FAILED,
    ACQUISITION_STATUS_NOT_GENERATED,
    ACQUISITION_STATUS_NOT_OBSERVED,
    ACQUISITION_STATUS_NOT_RUN,
    ACQUISITION_STATUS_SUCCESS,
    DATA_QUALITY_FAIL,
    DATA_QUALITY_PASS,
    DATA_QUALITY_WARNING,
    DataQualityError,
)

MANIFEST_PATH = Path("data_v2/processed/_last_run_manifest.json")

_COMPLETED_AIO_STATUSES = {ACQUISITION_STATUS_SUCCESS, ACQUISITION_STATUS_NOT_GENERATED}


def _parse(d: str) -> datetime:
    return datetime.strptime(d, "%Y-%m-%d")


def validate_period(cur_start: str, cur_end: str, prv_start: str, prv_end: str) -> list[str]:
    """期間の整合性を検証する。エラーがあれば文字列のリストで返す（空なら問題なし）。"""
    errors: list[str] = []

    cur_days = (_parse(cur_end) - _parse(cur_start)).days + 1
    prv_days = (_parse(prv_end) - _parse(prv_start)).days + 1
    if cur_days != 7:
        errors.append(f"今週の期間が7日ではない: {cur_start}〜{cur_end} ({cur_days}日)")
    if prv_days != 7:
        errors.append(f"比較週の期間が7日ではない: {prv_start}〜{prv_end} ({prv_days}日)")

    expected_prv_end = _parse(cur_start) - timedelta(days=1)
    if _parse(prv_end) != expected_prv_end:
        errors.append(
            f"比較週が今週と連続していない: 比較週終了={prv_end}, 期待値={expected_prv_end:%Y-%m-%d}"
        )

    return errors


def _duplicate_keys(rows: list[dict], dimensions: list[str]) -> list[tuple]:
    seen: dict[tuple, int] = {}
    for row in rows:
        key = tuple(row.get("keys", []))
        seen[key] = seen.get(key, 0) + 1
    return [k for k, count in seen.items() if count > 1]


def validate_gsc_period(raw_by_dimension: dict[str, dict], label: str) -> tuple[list[str], list[str]]:
    """1期間分（date/date_query/date_page/date_page_query）のGSC rawを検証する。
    (errors, warnings) を返す。label は "今週"/"比較週" 等のログ用ラベル。
    """
    errors: list[str] = []
    warnings: list[str] = []

    for dimension_key in ("date_query", "date_page", "date_page_query"):
        raw = raw_by_dimension.get(dimension_key)
        if raw is None:
            errors.append(f"{label}: {dimension_key} のrawデータが存在しない")
            continue

        rows = raw.get("response", {}).get("rows", [])
        if not rows:
            if label == "今週":
                errors.append(f"{label}: {dimension_key} が空（0行）")
            else:
                warnings.append(f"{label}: {dimension_key} が空（0行）。比較ができない")
            continue

        dups = _duplicate_keys(rows, raw.get("dimensions", []))
        if dups:
            errors.append(f"{label}: {dimension_key} に重複キー行がある: {dups[:5]}")

    return errors, warnings


def validate_aio(records: list[dict], priority_queries: list[str]) -> tuple[list[str], list[str], dict]:
    """AIO観測結果を検証する。(errors, warnings, counts) を返す。
    AIOの取得不備は原則warning止まりとする（GSCベースのレポートまで止めない）。
    """
    warnings: list[str] = []
    counts = {
        ACQUISITION_STATUS_SUCCESS: 0,
        ACQUISITION_STATUS_NOT_GENERATED: 0,
        ACQUISITION_STATUS_NOT_OBSERVED: 0,
        ACQUISITION_STATUS_FAILED: 0,
        ACQUISITION_STATUS_NOT_RUN: 0,
    }
    by_query = {r["query"]: r["acquisition_status"] for r in records}

    for q in priority_queries:
        status = by_query.get(q, ACQUISITION_STATUS_NOT_OBSERVED)
        counts[status] = counts.get(status, 0) + 1

    total = len(priority_queries)
    completed = sum(counts[s] for s in _COMPLETED_AIO_STATUSES)
    success_rate = (completed / total) if total else None

    if success_rate is not None and success_rate < 1.0:
        warnings.append(
            f"重点クエリのAIO観測が一部未完了（成功+未生成 {completed}/{total}）。"
            f"failed={counts[ACQUISITION_STATUS_FAILED]} "
            f"not_run={counts[ACQUISITION_STATUS_NOT_RUN]} "
            f"not_observed={counts[ACQUISITION_STATUS_NOT_OBSERVED]}"
        )

    return [], warnings, {"aio_success_rate": success_rate, "aio_counts": counts}


def _check_fetch_condition_repro(
    dimensions_used: dict[str, list[str]],
    row_limit: int,
    manifest_path: Path,
) -> list[str]:
    """重点クエリ取得条件が前週と同一かを確認する（requirements.md 8節）。
    マニフェストが無ければ初回実行として警告のみ（不明値は推測しない）。
    """
    warnings: list[str] = []
    current = {"dimensions": dimensions_used, "row_limit": row_limit}

    if not manifest_path.exists():
        warnings.append("前回実行のマニフェストが存在しない（初回実行、または比較不可）")
    else:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("dimensions") != current["dimensions"] or previous.get("row_limit") != current["row_limit"]:
            warnings.append(f"取得条件が前回実行と異なる: 前回={previous} 今回={current}")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return warnings


def run_validation(
    gsc_current: dict[str, dict],
    gsc_comparison: dict[str, dict],
    aio_records: list[dict],
    priority_queries: list[str],
    period: dict,
    manifest_path: Path | str = MANIFEST_PATH,
) -> dict:
    """検証を実行し data_quality_report を返す。status=="fail" なら DataQualityError を送出する。"""
    errors: list[str] = []
    warnings: list[str] = []

    errors += validate_period(period["cur_start"], period["cur_end"], period["prv_start"], period["prv_end"])

    cur_errors, cur_warnings = validate_gsc_period(gsc_current, "今週")
    prv_errors, prv_warnings = validate_gsc_period(gsc_comparison, "比較週")
    errors += cur_errors + prv_errors
    warnings += cur_warnings + prv_warnings

    aio_errors, aio_warnings, aio_meta = validate_aio(aio_records, priority_queries)
    errors += aio_errors
    warnings += aio_warnings

    dims_used = {k: v.get("dimensions", []) for k, v in gsc_current.items()}
    row_limit = next(iter(gsc_current.values()), {}).get("row_limit") if gsc_current else None
    warnings += _check_fetch_condition_repro(dims_used, row_limit, Path(manifest_path))

    if errors:
        status = DATA_QUALITY_FAIL
    elif warnings:
        status = DATA_QUALITY_WARNING
    else:
        status = DATA_QUALITY_PASS

    report = {
        "run_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "current_period": {"start": period["cur_start"], "end": period["cur_end"]},
        "comparison_period": {"start": period["prv_start"], "end": period["prv_end"]},
        "aio_success_rate": aio_meta["aio_success_rate"],
        "aio_counts": aio_meta["aio_counts"],
    }

    if status == DATA_QUALITY_FAIL:
        raise DataQualityError(report)

    return report
