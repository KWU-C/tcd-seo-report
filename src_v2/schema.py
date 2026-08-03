"""v2共通のスキーマ定数・例外定義。

data_contract.md に定義したスキーマの単一の正本とする。
他モジュールはここから定数・例外をimportし、値を直書きしない。
"""
from __future__ import annotations

# レポートJSON全体のスキーマバージョン（data-contract.md 6節）。
# data-contract.mdのスキーマ構造を変更するときは必ず上げる。
SCHEMA_VERSION = "1.0.0"

# AIO観測の取得状態（data-contract.md 2.2 / 4.2）。
# 「未取得」と「観測したが引用なし」を区別するための5分類。
ACQUISITION_STATUS_SUCCESS = "success"            # 観測成功・AIOが生成された
ACQUISITION_STATUS_NOT_GENERATED = "not_generated"  # 観測成功・AIOは生成されなかった
ACQUISITION_STATUS_NOT_OBSERVED = "not_observed"    # このクエリは今回の観測対象に含まれなかった
ACQUISITION_STATUS_FAILED = "failed"                # 観測を試行したがAPIエラー等で失敗した
ACQUISITION_STATUS_NOT_RUN = "not_run"              # AIO観測の仕組み自体を実行しなかった（例: APIキー未設定）

ACQUISITION_STATUSES = {
    ACQUISITION_STATUS_SUCCESS,
    ACQUISITION_STATUS_NOT_GENERATED,
    ACQUISITION_STATUS_NOT_OBSERVED,
    ACQUISITION_STATUS_FAILED,
    ACQUISITION_STATUS_NOT_RUN,
}

DATA_QUALITY_PASS = "pass"
DATA_QUALITY_WARNING = "warning"
DATA_QUALITY_FAIL = "fail"


class GscFetchError(RuntimeError):
    """Search Console APIの取得に失敗した（Fail Fast: レポート生成に進まない）。"""


class AioFetchError(RuntimeError):
    """AIO観測の実行そのものが失敗した（個々のクエリの観測失敗＝failedとは区別する）。"""


class DataQualityError(RuntimeError):
    """データ品質検証がfailと判定された（Fail Fast: レポート生成に進まない）。"""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(f"data quality check failed: {report.get('errors')}")
