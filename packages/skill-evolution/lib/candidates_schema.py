"""シナリオの JSON Schema で使われる語彙の検査。"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from candidates_safety import walk_nodes


def schema_errors(
    value: Any, schema: dict, path: str = "$", root: dict | None = None
) -> list[tuple[str, str]]:
    root = schema if root is None else root
    list(walk_nodes(value))
    errors: list[tuple[str, str]] = []
    pending = [("validate", value, schema, path, errors)]
    visited = set()
    count = 0
    while pending:
        action, child, rules, child_path, output = pending.pop()
        count += 1
        if count > 5000:
            return errors + [(child_path, "スキーマのノード数が上限を超えています")]
        if action == "then":
            condition_errors, then_rules = rules
            if not condition_errors:
                pending.append(("validate", child, then_rules, child_path, output))
            continue
        pair = (id(child), id(rules))
        if isinstance(child, (dict, list)):
            if pair in visited:
                continue
            visited.add(pair)
        if "$ref" in rules:
            target = root
            for part in rules["$ref"].removeprefix("#/").split("/"):
                target = target[part]
            pending.append(("validate", child, target, child_path, output))
            continue
        matches = {
            "null": child is None,
            "object": isinstance(child, dict),
            "array": isinstance(child, list),
            "string": isinstance(child, str),
            "boolean": type(child) is bool,
            "integer": type(child) is int,
            "number": type(child) is int or (type(child) is float and math.isfinite(child)),
        }
        types = rules.get("type", [])
        types = [types] if isinstance(types, str) else types
        if types and not any(matches[kind] for kind in types):
            output.append((child_path, "型がスキーマと一致しません"))
            continue
        if "const" in rules and (
            type(child) is not type(rules["const"]) or child != rules["const"]
        ):
            output.append((child_path, "const と一致しません"))
        if "enum" in rules and child not in rules["enum"]:
            output.append((child_path, "enum にありません"))
        if (
            isinstance(child, str)
            and "pattern" in rules
            and not re.fullmatch(rules["pattern"], child)
        ):
            output.append((child_path, "pattern に合いません"))
        if type(child) in (int, float) and "minimum" in rules and child < rules["minimum"]:
            output.append((child_path, "minimum 未満です"))
        if type(child) in (int, float) and "maximum" in rules and child > rules["maximum"]:
            output.append((child_path, "maximum を超えています"))
        if isinstance(child, dict):
            for key in rules.get("required", []):
                if key not in child:
                    output.append((f"{child_path}.{key}", "必須項目がありません"))
            properties = rules.get("properties", {})
            for key, item in child.items():
                if key in properties:
                    pending.append(
                        ("validate", item, properties[key], f"{child_path}.{key}", output)
                    )
                elif rules.get("additionalProperties") is False:
                    output.append((f"{child_path}.{key}", "未定義の項目です"))
        if isinstance(child, list):
            if len(child) < rules.get("minItems", 0):
                output.append((child_path, "minItems 未満です"))
            if rules.get("uniqueItems"):
                keys = [json.dumps(item, sort_keys=True) for item in child]
                if len(set(keys)) != len(keys):
                    output.append((child_path, "要素が重複しています"))
            for index, item in enumerate(child):
                pending.append(
                    ("validate", item, rules.get("items", {}), f"{child_path}[{index}]", output)
                )
        for extra in rules.get("allOf", []):
            pending.append(("validate", child, extra, child_path, output))
        if "if" in rules:
            condition_errors: list[tuple[str, str]] = []
            pending.append(
                ("then", child, (condition_errors, rules.get("then", {})), child_path, output)
            )
            pending.append(("validate", child, rules["if"], child_path, condition_errors))
    return errors
