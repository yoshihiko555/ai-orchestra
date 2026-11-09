"""下書きの構造・公開可否・実行行の検査。実行はしない。"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from candidates_safety import (
    REJECTED_CATEGORIES,
    Masker,
    RawDraftText,
    ValidationError,
    open_regular,
    walk_nodes,
)
from candidates_schema import schema_errors

FIXTURES = "packages/meta-harness/scenarios/fixtures/"
RELATIVE = re.compile(r"[A-Za-z0-9._/-]+")
ARGUMENT = re.compile(r"[A-Za-z0-9._=:/-]+")
PRIVATE_PATTERNS = (
    r"(?:^|[^a-z0-9._/-])(?:/|~)[a-z0-9._/-]+",
    r"\.\.",
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}",
    r"[a-z][a-z0-9+.-]*://\S+",
    r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b",
    r"\b(?:github|gitlab)\.com/\S+",
    r"(?<![\w./-])[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*#[0-9]+\b",
    r"\b(?:users|home)/[^/\s]+/",
    r"\b[a-z]:\\",
    r"(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+(?:com|net|org|io|dev|jp|corp|local|internal|lan)\b",
)


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise ValueError("YAML のキーが重複しています")
        return mapping


def load_yaml(text: str) -> Any:
    nodes = depth = 0
    for event in yaml.parse(text):
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("YAML のアンカーとエイリアスは使えません")
        if isinstance(event, (yaml.events.ScalarEvent, yaml.events.CollectionStartEvent)):
            nodes += 1
            if nodes > 5000:
                raise ValueError("YAML のノード数が上限を超えています")
        if isinstance(event, yaml.events.CollectionStartEvent):
            depth += 1
            if depth > 50:
                raise ValueError("YAML の深さが上限を超えています")
        elif isinstance(event, yaml.events.CollectionEndEvent):
            depth -= 1
    return yaml.load(text, Loader=UniqueKeyLoader)


def relative_path(value: str) -> bool:
    return bool(
        RELATIVE.fullmatch(value) and not value.startswith(("/", "-")) and ".." not in value
    )


def read_yaml(path: Path) -> Any:
    with open_regular(path) as stream:
        return load_yaml(stream.read().decode("utf-8"))


def existing_scenarios(orchestra: Path, warnings: list | None = None) -> list[tuple[Path, dict]]:
    scenarios = []
    schema = json.loads(
        (orchestra / "packages/meta-harness/schemas/scenario.schema.json").read_text()
    )
    for path in sorted((orchestra / "packages/meta-harness/scenarios").rglob("*.yaml")):
        try:
            scenario = read_yaml(path)
            if not isinstance(scenario, dict) or schema_errors(scenario, schema):
                raise ValueError("シナリオがスキーマに合いません")
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            if warnings is not None:
                warnings.append(
                    {
                        "file": str(path.relative_to(orchestra)),
                        "path": "$",
                        "line": "",
                        "category": "schema",
                        "reason": f"既存 YAML を検査できません: {exc}",
                    }
                )
            continue
        if isinstance(scenario, dict):
            scenarios.append((path, scenario))
    return scenarios


def human_scenarios(existing: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
    return [
        (path, scenario)
        for path, scenario in existing
        if not path.name.startswith("sevo-") and not str(scenario.get("id", "")).startswith("sevo-")
    ]


def fixture_allowlist(orchestra: Path, existing: list | None = None) -> dict[str, list[str]]:
    allowed: dict[str, set[str]] = {}
    for path, scenario in human_scenarios(
        existing if existing is not None else existing_scenarios(orchestra)
    ):
        if "skill" not in path.relative_to(orchestra / "packages/meta-harness/scenarios").parts[:1]:
            continue
        lines = list(scenario.get("setup", []))
        for field in ("critical", "checks", "graded"):
            lines.extend(check.get("command", "") for check in scenario.get(field, []))
        for line in lines:
            for match in re.finditer(
                r"python3 " + re.escape(FIXTURES) + r"([a-z0-9-]+\.py)([^\n;&|]*)", line
            ):
                script, arguments = match.groups()
                if (
                    script == "assert-function-behavior.py"
                    or not (orchestra / FIXTURES / script).is_file()
                ):
                    continue
                flags = set(re.findall(r"(?<!\S)--[a-z0-9-]+", arguments))
                if script == "merge-local-cli-tools-override.py":
                    flags.discard("--project-root")
                allowed.setdefault(script, set()).update(flags)
    return {script: sorted(flags) for script, flags in sorted(allowed.items())}


def command_allowed(line: str, allowed: dict[str, list[str]]) -> bool:
    words = line.split(" ")
    if len(words) < 2 or words[0] != "python3" or not words[1].startswith(FIXTURES):
        return False
    script = words[1][len(FIXTURES) :]
    if script not in allowed or script == "assert-function-behavior.py":
        return False
    for argument in words[2:]:
        if not ARGUMENT.fullmatch(argument) or ".." in argument or argument.startswith("/"):
            return False
        if argument.startswith("-"):
            flag = argument.split("=", 1)[0]
            if flag not in allowed[script] or (
                script == "merge-local-cli-tools-override.py" and flag == "--project-root"
            ):
                return False
            if "=" in argument and argument.split("=", 1)[1].startswith(("/", "-")):
                return False
    return True


def work_path(value: str) -> bool:
    return (
        relative_path(value)
        and value.startswith(".meta-harness/")
        and all(part and not part.startswith(".") for part in value.split("/")[1:])
    )


def fixture_copy_pairs(existing: list[tuple[Path, dict]], orchestra: Path) -> set[tuple[str, str]]:
    pairs = set()
    for path, scenario in human_scenarios(existing):
        if path.relative_to(orchestra / "packages/meta-harness/scenarios").parts[0] != "skill":
            continue
        for line in scenario.get("setup", []):
            if not isinstance(line, str):
                continue
            for match in re.finditer(
                r"(?:^|[\n;&]\s*)cp "
                + re.escape(FIXTURES)
                + r"([A-Za-z0-9._-]+) ([A-Za-z0-9._/-]+)(?=$|[\s;&])",
                line,
            ):
                pairs.add(match.groups())
    return pairs


def setup_allowed(line: str, orchestra: Path, pairs: set | None = None) -> bool:
    for prefix in ("mkdir -p ", "touch "):
        if line.startswith(prefix):
            return work_path(line[len(prefix) :])
    match = re.fullmatch(r"printf '([A-Za-z0-9 ._,:#/-]*)' > ([A-Za-z0-9._/-]+)", line)
    if match:
        return work_path(match[2])
    match = re.fullmatch(
        r"cp " + re.escape(FIXTURES) + r"([A-Za-z0-9._-]+) ([A-Za-z0-9._/-]+)", line
    )
    if not match or ".." in match[1] or not relative_path(match[2]):
        return False
    if pairs is None:
        pairs = fixture_copy_pairs(existing_scenarios(orchestra), orchestra)
    return (orchestra / FIXTURES / match[1]).is_file() and (
        work_path(match[2]) or match.groups() in pairs
    )


def strings(value: Any, path: str = "$"):
    for child_path, child in walk_nodes(value, path):
        if isinstance(child, str):
            yield child_path, child
        elif isinstance(child, dict):
            for key in child:
                yield f"{child_path}.{key}#key", str(key)


class DraftBaseline:
    def __init__(self, orchestra: Path):
        self.warnings: list[dict] = []
        self.existing = existing_scenarios(orchestra, self.warnings)
        self.human = human_scenarios(self.existing)
        self.fixtures = fixture_allowlist(orchestra, self.existing)
        self.copy_pairs = fixture_copy_pairs(self.existing, orchestra)


def inspect_draft(
    files: dict[str, bytes],
    orchestra: Path,
    cid: str,
    slug: str,
    excerpts: list[str],
    masker: Masker | None,
    baseline: DraftBaseline | None = None,
) -> dict:
    schema = json.loads(
        (orchestra / "packages/meta-harness/schemas/scenario.schema.json").read_text()
    )
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValidationError("スキルの slug が正しくありません")
    baseline = baseline or DraftBaseline(orchestra)
    existing = baseline.existing
    existing_ids = {scenario.get("id") for _, scenario in existing}
    train = [
        scenario
        for path, scenario in baseline.human
        if path.parent.name == slug
        and scenario.get("target") == f"skill:{slug}"
        and not scenario.get("holdout", False)
    ]
    graded = {
        "graded" in scenario
        for path, scenario in existing
        if path.parent.name == slug
        and scenario.get("target") == f"skill:{slug}"
        and not scenario.get("holdout", False)
    }
    tools = (
        set.intersection(*(set(scenario.get("allowed_tools", [])) for scenario in train))
        if train
        else {"Read", "Write", "Edit", "Glob", "Grep"}
    )
    suite = [
        scenario
        for path, scenario in baseline.human
        if path.parent.name == slug and scenario.get("target") == f"skill:{slug}"
    ]
    maxima = {}
    for scenario in suite:
        for path, value in walk_nodes(scenario):
            if type(value) in (int, float):
                key = re.sub(r"\[[0-9]+\]", "[]", path)
                maxima[key] = max(maxima.get(key, value), value)
    allowed = baseline.fixtures
    overlaps = {
        text[index : index + 12]
        for text in map(normalized, excerpts)
        for index in range(max(0, len(text) - 11))
    }
    findings, executable, parsed, evaluation = [], [], [], []

    def report(file: str, path: str, line: Any, category: str, reason: str) -> None:
        findings.append(
            {
                "file": file,
                "path": path,
                "line": RawDraftText(str(line)),
                "category": category,
                "reason": reason,
            }
        )

    def inspect_publication(file: str, values: list[tuple[str, str]]) -> None:
        for path, value in values:
            reasons = []
            normalized_value = normalized(value)
            try:
                if (
                    masker is None
                    or masker(value) != value
                    or masker(normalized_value) != normalized_value
                ):
                    reasons.append("伏せ字で変わる文字列、または伏せ字が使えません")
            except Exception:
                reasons.append("伏せ字が使えません")
            if any(re.search(pattern, normalized_value) for pattern in PRIVATE_PATTERNS):
                reasons.append("公開できないパス・識別情報があります")
            if path != "$raw" and any(
                normalized_value[index : index + 12] in overlaps
                for index in range(max(0, len(normalized_value) - 11))
            ):
                reasons.append("根拠の抜粋と正規化した 12 字以上一致します")
            if any(
                unicodedata.category(char) in REJECTED_CATEGORIES and char != "\n" for char in value
            ):
                reasons.append("使用できない文字があります")
            for reason in reasons:
                report(file, path, value, "publish", reason)

    for file, raw in files.items():
        text = raw.decode("utf-8", errors="backslashreplace")
        # YAML が読めない場合も、コメントや制御文字を検査結果から落とさない。
        inspect_publication(
            file,
            [("$raw", text)]
            + [
                (f"$comment[{index}]", line.partition("#")[2].lstrip())
                for index, line in enumerate(text.splitlines())
                if "#" in line
            ],
        )
        try:
            scenario = load_yaml(raw.decode("utf-8"))
            errors = schema_errors(scenario, schema)
        except (
            UnicodeError,
            yaml.YAMLError,
            RecursionError,
            TypeError,
            ValueError,
            OverflowError,
        ) as exc:
            report(file, "$", text, "schema", f"YAML を検査できません: {exc}")
            escaped = "".join(
                f"\\u{ord(char):04x}"
                if unicodedata.category(char) in REJECTED_CATEGORIES and char != "\n"
                else char
                for char in text
            )
            try:
                scenario = load_yaml(escaped)
                errors = schema_errors(scenario, schema)
            except (yaml.YAMLError, RecursionError, TypeError, ValueError, OverflowError):
                continue
        for path, reason in errors:
            report(file, path, text, "schema", reason)
        inspect_publication(file, list(strings(scenario)))
        if errors or not isinstance(scenario, dict):
            continue
        parsed.append(scenario)
        identifier = scenario["id"]
        if (
            not re.fullmatch(r"[a-z0-9-]+", identifier)
            or not identifier.startswith(f"sevo-{cid[2:]}-")
            or identifier in existing_ids
        ):
            report(file, "$.id", identifier, "structure", "ID の接頭辞または一意性が不正です")
        existing_ids.add(identifier)
        if scenario["target"] != f"skill:{slug}":
            report(file, "$.target", scenario["target"], "structure", "対象スキルが違います")
        if not scenario.get("holdout", False):
            graded.add("graded" in scenario)
        for path, value in walk_nodes(scenario):
            if type(value) in (int, float):
                maximum = maxima.get(re.sub(r"\[[0-9]+\]", "[]", path))
                if maximum is None or value > maximum:
                    report(
                        file,
                        path,
                        value,
                        "structure",
                        "数値の資源設定が既存スイートの最大値を超えています",
                    )
            field = path.rsplit(".", 1)[-1]
            if isinstance(value, str) and field in {"prompt", "rubric", "text", "description"}:
                evaluation.append({"file": file, "path": path, "line": RawDraftText(value)})
                limit = 4000 if field == "prompt" else 2000
                if len(value) > limit:
                    report(file, path, value, "structure", f"評価に渡る文は {limit} 字以内です")
            if isinstance(value, str) and field == "id" and not re.fullmatch(r"[a-z0-9-]+", value):
                report(file, path, value, "structure", "ID の形式が正しくありません")
        if "allowed_tools" not in scenario:
            report(file, "$.allowed_tools", "", "structure", "allowed_tools は省略できません")
        for index, line in enumerate(scenario.get("setup", [])):
            path = f"$.setup[{index}]"
            executable.append({"file": file, "path": path, "line": RawDraftText(line)})
            if not setup_allowed(line, orchestra, baseline.copy_pairs):
                report(file, path, line, "shape", "許可されない準備行です")
        for field in ("critical", "checks", "graded"):
            for index, check in enumerate(scenario.get(field, [])):
                for key in ("oracle", "command"):
                    if key not in check:
                        continue
                    path, line = f"$.{field}[{index}].{key}", check[key]
                    executable.append({"file": file, "path": path, "line": RawDraftText(line)})
                    if key == "command" and not command_allowed(line, allowed):
                        report(file, path, line, "shape", "許可されない検査行です")
                for key in ("path", "schema"):
                    if key in check and not relative_path(check[key]):
                        report(
                            file,
                            f"$.{field}[{index}].{key}",
                            check[key],
                            "structure",
                            "検査のパスは安全な相対パスで指定してください",
                        )
                required = {
                    "artifact_exists": ("path",),
                    "command_exit": ("command",),
                    "json_schema": ("path", "schema"),
                    "rubric_judge": ("rubric",),
                }[check["oracle"]]
                for key in required:
                    if key not in check:
                        report(
                            file,
                            f"$.{field}[{index}].{key}",
                            "",
                            "structure",
                            "oracle の必須項目がありません",
                        )
        for field in ("permission_mode", "allowed_tools", "path_prepend"):
            if field not in scenario:
                continue
            value = scenario[field]
            line = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            executable.append({"file": file, "path": f"$.{field}", "line": RawDraftText(line)})
            valid = (
                value == "acceptEdits"
                if field == "permission_mode"
                else (
                    set(value) <= tools
                    and not any(tool == "Bash" or tool.startswith("Bash(") for tool in value)
                )
                if field == "allowed_tools"
                else not value
            )
            if not valid:
                report(
                    file, f"$.{field}", line, "shape", "権限・ツール・実行パスの形が許可範囲外です"
                )
    if not any(not scenario.get("holdout", False) for scenario in parsed) or not any(
        scenario.get("holdout") is True for scenario in parsed
    ):
        report("*", "$", "", "structure", "train と holdout が各 1 本以上必要です")
    if len(graded) > 1:
        report("*", "$.graded", "", "structure", "train の graded の有無がそろっていません")
    return {
        "passed": not findings,
        "findings": findings,
        "warnings": list(baseline.warnings),
        "executable_lines": executable,
        "evaluation_texts": evaluation,
    }
