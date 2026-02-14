#!/usr/bin/env python3
"""hooks 間で共有する汎用ユーティリティ関数。"""

import json
import sys


def read_hook_input() -> dict:
    """stdin から JSON を読み取って dict を返す。"""
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def get_field(data: dict, key: str) -> str:
    """dict からフィールドを取得する。存在しなければ空文字を返す。"""
    return data.get(key) or ""
