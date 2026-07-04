"""task_result.schema.json / review_result.schema.json の構造検証テスト。

テスト対象:
- 両スキーマが妥当な JSON であること
- required / enum 構造が設計通りであること（EV-36, EV-46）

意図的に jsonschema 等の新規依存は追加しない。json.load + 構造アサーションのみで
「JSON Schema としての最低限の骨格（type/required/properties/enum）」を検証する。
"""

from __future__ import annotations

import json

from tests.module_loader import REPO_ROOT

TASK_RESULT_SCHEMA_PATH = (
    REPO_ROOT / "packages" / "codex-harness" / "codex" / "schemas" / "task_result.schema.json"
)
REVIEW_RESULT_SCHEMA_PATH = (
    REPO_ROOT / "packages" / "codex-harness" / "codex" / "schemas" / "review_result.schema.json"
)


class TestTaskResultSchema:
    def test_is_valid_json(self) -> None:
        json.loads(TASK_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_requires_top_level_keys(self) -> None:
        schema = json.loads(TASK_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        assert set(schema["required"]) == {
            "status",
            "summary",
            "files_changed",
            "validation",
            "risks",
        }

    def test_status_enum_matches_design(self) -> None:
        schema = json.loads(TASK_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["properties"]["status"]["enum"] == ["success", "partial", "failed"]

    def test_validation_item_status_enum(self) -> None:
        schema = json.loads(TASK_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        item_schema = schema["properties"]["validation"]["items"]
        assert set(item_schema["required"]) == {"command", "status", "summary"}
        assert item_schema["properties"]["status"]["enum"] == ["passed", "failed", "skipped"]

    def test_risks_item_severity_enum(self) -> None:
        schema = json.loads(TASK_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        item_schema = schema["properties"]["risks"]["items"]
        assert set(item_schema["required"]) == {"severity", "description", "mitigation"}
        assert item_schema["properties"]["severity"]["enum"] == ["low", "medium", "high"]


class TestReviewResultSchema:
    def test_is_valid_json(self) -> None:
        json.loads(REVIEW_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_requires_top_level_keys(self) -> None:
        schema = json.loads(REVIEW_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"status", "summary", "findings"}

    def test_status_enum_matches_design(self) -> None:
        schema = json.loads(REVIEW_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["properties"]["status"]["enum"] == ["success", "partial", "failed"]

    def test_findings_item_severity_enum_and_required(self) -> None:
        schema = json.loads(REVIEW_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        item_schema = schema["properties"]["findings"]["items"]
        assert set(item_schema["required"]) == {
            "severity",
            "file",
            "line",
            "rationale",
            "suggested_fix",
        }
        assert item_schema["properties"]["severity"]["enum"] == [
            "low",
            "medium",
            "high",
            "critical",
        ]
