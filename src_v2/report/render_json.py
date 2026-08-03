"""View ModelをそのままJSONとして出力する。分析ロジックは追加しない。"""
from __future__ import annotations

import json


def render_json(vm: dict) -> str:
    return json.dumps(vm, ensure_ascii=False, indent=2)
