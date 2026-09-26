"""CLI ごとの既定モデルを正本と関連ファイルに反映する。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

SSOT = Path("packages/agent-routing/config/cli-tools.yaml")
MIRROR = Path(".claude/config/agent-routing/cli-tools.yaml")
LOCAL = Path(".claude/config/agent-routing/cli-tools.local.yaml")
# ミラーを書き換えたら sync 台帳のハッシュも追従させる（次回 sync の変更検知を狂わせないため）
LEDGER = Path(".claude/orchestra.json")
LEDGER_HASH_LINE = re.compile(r'("config/agent-routing/cli-tools\.yaml": ")[0-9a-f]{64}(")')
MODEL_KEYS = {
    "codex": ("codex", "model"),
    "antigravity": ("antigravity", "model"),
    "claude": ("subagent", "default_model"),
}
MODEL_ID = re.compile(r"[A-Za-z0-9._:\-]+\Z")
CLAUDE_ID = re.compile(r"claude-[a-z0-9.\-]+\Z")
CLAUDE_ALIASES = {"sonnet", "opus", "haiku", "inherit"}
LIST_ITEM = re.compile(r"^(\s*)-\s+(\S.*?)\s*$")
CHOICES_COMMENT = "# 選択肢: sonnet, opus, haiku"
UPDATED_COMMENT = (
    "# 選択肢: sonnet, opus, haiku, inherit, またはフルモデル ID（例: claude-sonnet-5）"
)


def _read_text(path: Path) -> str:
    """改行コードを変換せずに読む。"""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _write_text(path: Path, content: str) -> None:
    """元の改行コードを維持して書く。"""
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(content)


def _line_parts(line: str) -> tuple[str, str]:
    """行本文と改行を分ける。"""
    body = line.rstrip("\r\n")
    return body, line[len(body) :]


def _section_range(lines: list[str], key: str) -> range | None:
    """最上位キーの範囲を返す。"""
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(rf"{re.escape(key)}:\s*", _line_parts(line)[0])
        ),
        None,
    )
    if start is None:
        return None
    end = next(
        (index for index in range(start + 1, len(lines)) if re.match(r"^\S", lines[index])),
        len(lines),
    )
    return range(start + 1, end)


def _replace_field(lines: list[str], section: str, field: str, old: str, new: str) -> bool:
    """指定セクション内の現在値だけを置換する。"""
    section_lines = _section_range(lines, section)
    if section_lines is None:
        return False
    pattern = re.compile(rf"^(\s*){re.escape(field)}:\s*{re.escape(old)}\s*$")
    for index in section_lines:
        body, ending = _line_parts(lines[index])
        match = pattern.fullmatch(body)
        if match:
            lines[index] = f"{match.group(1)}{field}: {new}{ending}"
            return True
    return False


def _allowlist(
    lines: list[str], section: str
) -> tuple[int, list[tuple[int, re.Match[str]]]] | None:
    """指定セクション内の allowlist と連続した項目を見つける。"""
    section_lines = _section_range(lines, section)
    if section_lines is None:
        return None
    for index in section_lines:
        if not re.fullmatch(r"\s*model_allowlist:\s*", _line_parts(lines[index])[0]):
            continue
        items: list[tuple[int, re.Match[str]]] = []
        for item_index in range(index + 1, section_lines.stop):
            match = LIST_ITEM.fullmatch(_line_parts(lines[item_index])[0])
            if match is None:
                break
            items.append((item_index, match))
        return index, items
    return None


def _patch_allowlist(lines: list[str], section: str, new: str) -> bool:
    """Codex は単一項目、Antigravity は既存項目を残して先頭に追加する。"""
    block = _allowlist(lines, section)
    if block is None:
        return False
    heading, items = block
    if section == "codex":
        if not items:
            return False
        first, match = items[0]
        _, ending = _line_parts(lines[first])
        lines[first : items[-1][0] + 1] = [f"{match.group(1)}- {new}{ending}"]
        return True
    if any(match.group(2).strip() == new for _, match in items):
        return True
    heading_body, heading_ending = _line_parts(lines[heading])
    indent = items[0][1].group(1) if items else re.match(r"^\s*", heading_body).group() + "  "
    ending = _line_parts(lines[items[0][0]])[1] if items else heading_ending or "\n"
    lines.insert(heading + 1, f"{indent}- {new}{ending}")
    return True


def _patch_comment(lines: list[str]) -> bool:
    """Claude の選択肢コメントを新しい表記にそろえる。"""
    section_lines = _section_range(lines, "subagent")
    if section_lines is None:
        return False
    for index in section_lines:
        body, ending = _line_parts(lines[index])
        if body.strip() == UPDATED_COMMENT:
            return True
        if body.strip() == CHOICES_COMMENT:
            indent = re.match(r"^\s*", body).group()
            lines[index] = f"{indent}{UPDATED_COMMENT}{ending}"
            return True
    return False


def _patch_yaml(text: str, tool: str, old: str, new: str, required: bool) -> tuple[str, list[str]]:
    """YAML の対象セクションだけを編集し、欠けた任意項目を返す。"""
    lines = text.splitlines(keepends=True)
    section, field = MODEL_KEYS[tool]
    missing: list[str] = []
    if not _replace_field(lines, section, field, old, new):
        missing.append(f"{section}.{field}")
    if tool in {"codex", "antigravity"}:
        if not _patch_allowlist(lines, section, new):
            missing.append(f"{section}.model_allowlist")
    elif not _patch_comment(lines):
        missing.append("subagent comment")
    if required and any(item != "subagent comment" for item in missing):
        raise ValueError(f"required pattern not found in {SSOT}: {', '.join(missing)}")
    return "".join(lines), missing


def _patch_literal_line(text: str, pattern: re.Pattern[str], new: str) -> str | None:
    """一致する行のモデル値だけを置換する。"""
    lines = text.splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        body, ending = _line_parts(line)
        match = pattern.fullmatch(body)
        if match:
            lines[index] = f"{match.group(1)}{new}{ending}"
            found = True
    return "".join(lines) if found else None


def _patch_antigravity_doc(text: str, old: str, new: str) -> str | None:
    """Antigravity の説明節だけでモデル名を置換する。"""
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if re.match(r"^###\s+antigravity\b", line)), None
    )
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^###\s+", lines[i])), len(lines)
    )
    token = re.compile(rf"(?<![A-Za-z0-9._:\-]){re.escape(old)}(?![A-Za-z0-9._:\-])")
    bullet = re.compile(rf"^\s*-\s+{re.escape(old)}\s*$")
    found = False
    for index in range(start, end):
        body, ending = _line_parts(lines[index])
        if token.search(body) and ("model" in body or bullet.fullmatch(body)):
            lines[index] = token.sub(new, body) + ending
            found = True
    return "".join(lines) if found else None


def _patch_hooks_doc(text: str, old: str, new: str) -> str | None:
    """agy の --model 引数だけを置換する。"""
    pattern = re.compile(rf"--model {re.escape(old)}(?![A-Za-z0-9._:\-])")
    updated, count = pattern.subn(f"--model {new}", text)
    return updated if count else None


def _optional_patch(
    root: Path,
    relative: Path,
    patch: Callable[[str], str | None],
    changes: dict[Path, tuple[str, str]],
    skips: list[str],
) -> None:
    """任意ファイルの変更案を作り、欠落時は理由を記録する。"""
    path = root / relative
    if not path.is_file():
        skips.append(f"skip: {relative} (file not found)")
        return
    before = _read_text(path)
    after = patch(before)
    if after is None:
        skips.append(f"skip: {relative} (pattern not found)")
    elif after != before:
        changes[relative] = before, after


def _model_value(data: object, section: str, field: str) -> str:
    """必須モデル値を YAML から取得する。"""
    if isinstance(data, dict) and isinstance(data.get(section), dict):
        value = data[section].get(field)
        if isinstance(value, str):
            return value
    raise ValueError(f"missing or invalid {section}.{field} in {SSOT}")


def _load_yaml(path: Path) -> object:
    """YAML を安全に読み込む。"""
    return yaml.safe_load(_read_text(path))


def _validate_model(tool: str, model: str) -> None:
    """シェルや設定に埋め込めないモデル名を拒否する。"""
    if not MODEL_ID.fullmatch(model):
        raise ValueError(f"invalid model id: {model!r}")
    if tool == "claude" and model not in CLAUDE_ALIASES and not CLAUDE_ID.fullmatch(model):
        raise ValueError(f"invalid claude model id: {model}")


def _show(root: Path) -> None:
    """正本と存在するローカル上書きのモデル値を表示する。"""
    base = _load_yaml(root / SSOT)
    for tool, (section, field) in MODEL_KEYS.items():
        print(f"{section}.{field}: {_model_value(base, section, field)}")
    if not (root / LOCAL).is_file():
        return
    local = _load_yaml(root / LOCAL)
    if not isinstance(local, dict):
        return
    for section, field in MODEL_KEYS.values():
        values = local.get(section)
        if isinstance(values, dict) and field in values:
            print(f"{section}.{field} (cli-tools.local.yaml override): {values[field]}")


def _update(root: Path, tool: str, model: str, dry_run: bool) -> None:
    """変更案をすべて計算してから出力または書き込みを行う。"""
    _validate_model(tool, model)
    before = _read_text(root / SSOT)
    section, field = MODEL_KEYS[tool]
    old = _model_value(yaml.safe_load(before), section, field)
    if old == model:
        print(f"already {model}, no changes made")
        return

    changes: dict[Path, tuple[str, str]] = {}
    skips: list[str] = []
    after, _ = _patch_yaml(before, tool, old, model, required=True)
    if after != before:
        changes[SSOT] = before, after
    _optional_patch(
        root,
        MIRROR,
        lambda text: _patch_optional_yaml(text, tool, old, model, skips),
        changes,
        skips,
    )
    if tool == "codex":
        _add_codex_patches(root, old, model, changes, skips)
    elif tool == "antigravity":
        _add_antigravity_patches(root, old, model, changes, skips)
    else:
        _add_claude_patches(root, old, model, changes, skips)
    _add_ledger_patch(root, changes, skips)

    if dry_run:
        for relative, (original, updated) in changes.items():
            print(
                "".join(
                    difflib.unified_diff(
                        original.splitlines(keepends=True),
                        updated.splitlines(keepends=True),
                        fromfile=str(relative),
                        tofile=str(relative),
                    )
                ),
                end="",
            )
        for skipped in skips:
            print(skipped)
        return
    for relative, (_, updated) in changes.items():
        _write_text(root / relative, updated)
        print(f"changed: {relative}")
    for skipped in skips:
        print(skipped)
    print("次を実行してください: uv run pytest -q （関連テスト）")
    if tool == "codex":
        print(
            f'確認: codex exec --model {model} --sandbox read-only "Reply with OK only" < /dev/null （Claude Code の sandbox 外で実行すること）'
        )
    elif tool == "antigravity":
        print("確認: agy models")
    print("CHANGELOG.md の Unreleased セクションを更新してください")


def _add_ledger_patch(root: Path, changes: dict[Path, tuple[str, str]], skips: list[str]) -> None:
    """ミラーの変更後内容のハッシュで orchestra.json の台帳を更新する。"""
    if MIRROR not in changes:
        return
    new_hash = hashlib.sha256(changes[MIRROR][1].encode("utf-8")).hexdigest()
    _optional_patch(
        root,
        LEDGER,
        lambda text: _patch_ledger(text, new_hash),
        changes,
        skips,
    )


def _patch_ledger(text: str, new_hash: str) -> str | None:
    """台帳の cli-tools.yaml ハッシュ行を 1 箇所だけ置換する。"""
    updated, count = LEDGER_HASH_LINE.subn(rf"\g<1>{new_hash}\g<2>", text, count=1)
    if count == 0:
        return None
    return updated


def _patch_optional_yaml(text: str, tool: str, old: str, new: str, skips: list[str]) -> str | None:
    """ミラーの各項目を独立に処理する。"""
    updated, missing = _patch_yaml(text, tool, old, new, required=False)
    for item in missing:
        skips.append(f"skip: {MIRROR} ({item} pattern not found)")
    return updated


def _add_codex_patches(
    root: Path, old: str, new: str, changes: dict[Path, tuple[str, str]], skips: list[str]
) -> None:
    """Codex の関連ファイルを変更案に追加する。"""
    targets = [
        (
            Path("packages/core/hooks/hook_common.py"),
            rf'^(DEFAULT_CODEX_MODEL = "){re.escape(old)}"$',
        ),
        (Path("templates/codex/config.toml"), rf'^(model = "){re.escape(old)}"$'),
        (Path(".codex/config.toml"), rf'^(model = "){re.escape(old)}"$'),
    ]
    for relative, expression in targets:
        _optional_patch(
            root,
            relative,
            lambda text, expr=expression: _patch_literal_line(text, re.compile(expr), f'{new}"'),
            changes,
            skips,
        )
    relative = Path("docs/design/codex-cli-harness.md")
    quoted_old = f'"{old}"'
    _optional_patch(
        root,
        relative,
        lambda text: text.replace(quoted_old, f'"{new}"') if quoted_old in text else None,
        changes,
        skips,
    )


def _add_antigravity_patches(
    root: Path, old: str, new: str, changes: dict[Path, tuple[str, str]], skips: list[str]
) -> None:
    """Antigravity の文書を変更案に追加する。"""
    _optional_patch(
        root,
        Path("docs/reference/configuration.md"),
        lambda text: _patch_antigravity_doc(text, old, new),
        changes,
        skips,
    )
    _optional_patch(
        root,
        Path("docs/reference/hooks.md"),
        lambda text: _patch_hooks_doc(text, old, new),
        changes,
        skips,
    )


def _add_claude_patches(
    root: Path, old: str, new: str, changes: dict[Path, tuple[str, str]], skips: list[str]
) -> None:
    """Claude の例示設定を変更案に追加する。"""
    expression = re.compile(rf"^(\s*default_model:\s*){re.escape(old)}\s*$")
    for relative in (Path("docs/reference/configuration.md"), Path("docs/design/architecture.md")):
        _optional_patch(
            root, relative, lambda text: _patch_literal_line(text, expression, new), changes, skips
        )


def main(argv: list[str] | None = None) -> int:
    """引数を解釈して表示または変更を実行する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (*MODEL_KEYS, "show"):
        command = commands.add_parser(name)
        if name != "show":
            command.add_argument("model")
            command.add_argument("--dry-run", action="store_true")
        command.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            _show(args.root)
        else:
            _update(args.root, args.command, args.model, args.dry_run)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
