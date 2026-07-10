#!/usr/bin/env python3
"""meta-harness proposer helpers（Phase 2 M2/M3/M4）。

M2 で proposal schema / prompt、M3 で filtered view、M4 で propose CLI の
検証・登録経路を提供する。
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import meta_harness_common as mh  # noqa: E402
import redaction  # noqa: E402
from proposer_backend import ProposalValidationError, ProposerError  # noqa: E402,F401

PROPOSAL_SCHEMA_NAME = "proposal.schema.json"
PROPOSER_PROMPT_TEMPLATE_NAME = "proposer-prompt-template.md"
DEFAULT_MAX_OVERLAY_BYTES = 200000
# events.jsonl.gz の展開上限（decompression-bomb ガード）。included run 数は
# view snapshot で有界のため、per-file 上限で view 全体の展開量も有界になる。
DEFAULT_MAX_EXPANDED_EVENTS_BYTES = 50_000_000
_EXPAND_CHUNK_BYTES = 1 << 20
_MISSING_FOCUS = "(none)"
_VIEW_PREFIX = "meta-harness-view-"
_INSTRUCTION_FILE_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}
_EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


class ViewBuildError(RuntimeError):
    """filtered view を fail-closed で破棄すべき場合に送出する。"""


@dataclass(frozen=True)
class FilteredView:
    """M4 の proposer launch が利用する filtered view のスナップショット。"""

    path: Path
    included_run_ids: frozenset[str]
    holdout_run_ids: frozenset[str]

    def cleanup(self) -> None:
        """view ディレクトリを削除する。呼び出し側は `finally` で実行する。"""
        shutil.rmtree(self.path, ignore_errors=True)


@dataclass(frozen=True)
class FilteredStoreSnapshot:
    """store.lock 下で読み取った proposer view 用の短期スナップショット。"""

    frontier_doc: dict[str, Any]
    ledger_events: tuple[dict[str, Any], ...]
    candidate_ids: tuple[str, ...]
    non_holdout_run_ids: tuple[str, ...]
    holdout_run_ids: frozenset[str]


def validate_proposal(proposal: dict[str, Any], schema_dir: Path) -> list[str]:
    """proposal JSON を `proposal.schema.json` に照らして検証する（空 list = valid）。"""
    schema = mh.load_schema(schema_dir, PROPOSAL_SCHEMA_NAME)
    return mh.validate_against_schema(proposal, schema, schema_dir)


def materialize_overlay_from_proposal(
    proposal: dict[str, Any], overlay_dir: Path, *, max_overlay_bytes: int
) -> list[str]:
    """proposal.changes を register 用 overlay ディレクトリへ実体化する。"""
    total_bytes = 0
    seen: set[str] = set()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for change in proposal.get("changes", []):
        rel = str(change.get("path") or "")
        content = str(change.get("new_content") or "")
        if rel in seen:
            raise ProposalValidationError(f"duplicate proposal change path: {rel}")
        seen.add(rel)
        if _unsafe_overlay_path(rel):
            raise ProposalValidationError(f"unsafe proposal change path: {rel}")
        encoded = content.encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > max_overlay_bytes:
            raise ProposalValidationError(
                f"proposal overlay exceeds max_overlay_bytes={max_overlay_bytes}"
            )
        target = overlay_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        _remove_executable_bits(target)
    return mh.list_overlay_files(overlay_dir)


def validate_based_on_runs(
    proposal: dict[str, Any],
    *,
    events: list[dict],
    target: str,
    included_run_ids: frozenset[str],
) -> list[str]:
    """proposal.based_on_runs の実体参照を ledger / filtered view snapshot と突合する。"""
    errors: list[str] = []
    runs_by_id = {
        str(event.get("run_id")): event
        for event in events
        if event.get("event") == "run_completed" and event.get("run_id")
    }
    for run_id in proposal.get("based_on_runs", []):
        event = runs_by_id.get(str(run_id))
        if event is None:
            errors.append(f"based_on_runs references unknown run_id: {run_id}")
            continue
        if bool(event.get("holdout")):
            errors.append(f"based_on_runs references holdout run_id: {run_id}")
        if event.get("target") != target:
            errors.append(f"based_on_runs target mismatch for {run_id}: {event.get('target')}")
        if str(run_id) not in included_run_ids:
            errors.append(f"based_on_runs run_id is not present in filtered view: {run_id}")
    return errors


def save_rejected_proposal(
    *,
    main_root: Path,
    config: dict,
    reason: str,
    proposal: dict[str, Any] | None = None,
    raw_output: str | None = None,
) -> Path:
    """rejected/ に redaction 済み診断 JSON を保存する。"""
    rejected = mh.rejected_dir(main_root, config)
    rejected.mkdir(parents=True, exist_ok=True)
    path = rejected / f"{_compact_timestamp()}-proposal.json"
    payload = {
        "schema_version": "1.0",
        "rejected_at": mh.now_iso(),
        "reason": reason,
        "proposal": proposal,
        "raw_output": raw_output,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    redaction.write_atomic(path, redaction.redact_secrets(content))
    return path


def build_filtered_view(
    *,
    main_root: Path,
    config: dict,
    source_commit: str,
    snapshot: FilteredStoreSnapshot | None = None,
    view_parent: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> FilteredView:
    """§11-2 の filtered view を $TMPDIR 配下に構築し、自己検証まで行う。"""
    view_dir = _create_view_dir(main_root=main_root, config=config, view_parent=view_parent)
    included_run_ids: set[str] = set()
    holdout_run_ids: set[str] = set()
    try:
        store_snapshot = snapshot or snapshot_filtered_store(main_root, config)
        _build_view_contents(
            view_dir=view_dir,
            main_root=main_root,
            config=config,
            source_commit=source_commit,
            snapshot=store_snapshot,
            included_run_ids=included_run_ids,
            holdout_run_ids=holdout_run_ids,
            runner=runner,
        )
        verify_filtered_view(view_dir, known_holdout_run_ids=holdout_run_ids)
    except Exception:
        shutil.rmtree(view_dir, ignore_errors=True)
        raise
    return FilteredView(
        path=view_dir,
        included_run_ids=frozenset(included_run_ids),
        holdout_run_ids=frozenset(holdout_run_ids),
    )


def snapshot_filtered_store(main_root: Path, config: dict) -> FilteredStoreSnapshot:
    """`store.lock` 保持中に呼ぶ、propose 用の短期 store スナップショット取得。"""
    frontier_doc = _read_frontier_doc(main_root, config)
    events = tuple(_read_ledger_events_strict(mh.ledger_path(main_root, config)))
    candidate_ids = tuple(mh.list_candidate_ids(main_root, config))
    non_holdout_run_ids: list[str] = []
    seen_non_holdout: set[str] = set()
    holdout_run_ids: set[str] = set()
    for event in events:
        if event.get("event") != "run_completed":
            continue
        run_id = event.get("run_id")
        if not run_id:
            continue
        run_id = str(run_id)
        if bool(event.get("holdout")):
            holdout_run_ids.add(run_id)
            continue
        if run_id not in seen_non_holdout:
            seen_non_holdout.add(run_id)
            non_holdout_run_ids.append(run_id)
    holdout_run_ids.update(_list_holdout_run_dirs(main_root, config))
    return FilteredStoreSnapshot(
        frontier_doc=frontier_doc,
        ledger_events=events,
        candidate_ids=candidate_ids,
        non_holdout_run_ids=tuple(non_holdout_run_ids),
        holdout_run_ids=frozenset(holdout_run_ids),
    )


def verify_filtered_view(view_dir: Path, *, known_holdout_run_ids: set[str]) -> None:
    """filtered view 完成直前の fail-closed 自己検証を行う。"""
    if not view_dir.is_dir():
        raise ViewBuildError(f"filtered view is missing: {view_dir}")
    _verify_no_symlinks(view_dir)
    _verify_no_git_entries(view_dir)
    _verify_no_instruction_files(view_dir)
    _verify_no_executable_files(view_dir)
    _verify_no_holdout_ledger_rows(view_dir)
    _verify_holdout_ids_absent(view_dir, known_holdout_run_ids)


def render_proposer_prompt(
    *,
    view_dir: Path,
    frontier_doc: dict[str, Any] | None,
    config: dict[str, Any],
    package_dir: Path,
    target: str,
    focus_run_id: str | None = None,
    focus_run_ids: list[str] | tuple[str, ...] | None = None,
    valid_based_on_run_ids: list[str] | tuple[str, ...] | None = None,
    focus_candidate_id: str | None = None,
) -> str:
    """package resource の prompt template に実行時コンテキストを埋め込む。"""
    template = _load_prompt_template(package_dir)
    proposer_cfg = config.get("proposer") or {}
    max_overlay_bytes = proposer_cfg.get("max_overlay_bytes", DEFAULT_MAX_OVERLAY_BYTES)
    rendered_focus_runs = _format_focus_runs(focus_run_id, focus_run_ids)
    rendered_valid_runs = _join_or_none([str(run_id) for run_id in valid_based_on_run_ids or ()])
    return Template(template).safe_substitute(
        view_dir=str(view_dir.resolve()),
        target=target,
        focus_run_ids=rendered_focus_runs,
        valid_based_on_run_ids=rendered_valid_runs,
        focus_candidate_id=focus_candidate_id or _MISSING_FOCUS,
        max_overlay_bytes=max_overlay_bytes,
        frontier_summary=summarize_frontier(frontier_doc),
    )


def summarize_frontier(frontier_doc: dict[str, Any] | None, *, max_points: int = 5) -> str:
    """proposer prompt に埋め込む Pareto frontier の短い要約を作る。"""
    if not frontier_doc:
        return "- frontier: (none)\n- dominated: (none)\n- points: (none)"

    frontier_ids = _string_list(frontier_doc.get("frontier"))
    dominated_ids = _string_list(frontier_doc.get("dominated"))
    points = [p for p in frontier_doc.get("points", []) if isinstance(p, dict)]
    points_by_id = {str(p.get("cand_id")): p for p in points if p.get("cand_id") is not None}

    lines = [
        f"- frontier: {_join_or_none(frontier_ids)}",
        f"- dominated: {_join_or_none(dominated_ids)}",
        "- points:",
    ]
    selected_ids = frontier_ids[:max_points] or [str(p.get("cand_id")) for p in points[:max_points]]
    if not selected_ids:
        lines[-1] = "- points: (none)"
        return "\n".join(lines)

    for cand_id in selected_ids:
        point = points_by_id.get(cand_id)
        if point is None:
            lines.append(f"  - {cand_id}: point details unavailable")
            continue
        lines.append(
            "  - "
            f"{cand_id}: quality_mean={_format_metric(point.get('quality_mean'))}, "
            f"cost_mean={_format_metric(point.get('cost_mean'))}, "
            f"runs={point.get('runs', 'unknown')}"
        )
    return "\n".join(lines)


def _load_prompt_template(package_dir: Path) -> str:
    path = package_dir / "config" / PROPOSER_PROMPT_TEMPLATE_NAME
    return path.read_text(encoding="utf-8")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _join_or_none(values: list[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _format_metric(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{value:.3f}"
    return "unknown"


def _format_focus_runs(
    focus_run_id: str | None, focus_run_ids: list[str] | tuple[str, ...] | None
) -> str:
    if focus_run_ids is not None:
        return _join_or_none([str(run_id) for run_id in focus_run_ids])
    return focus_run_id or _MISSING_FOCUS


def _build_view_contents(
    *,
    view_dir: Path,
    main_root: Path,
    config: dict,
    source_commit: str,
    snapshot: FilteredStoreSnapshot,
    included_run_ids: set[str],
    holdout_run_ids: set[str],
    runner: SubprocessRunner,
) -> None:
    store_view = view_dir / "store"
    store_view.mkdir(parents=True, exist_ok=False)
    _copy_candidates(main_root, config, store_view, snapshot.candidate_ids)
    _copy_non_holdout_runs(
        main_root,
        config,
        store_view,
        snapshot.non_holdout_run_ids,
        included_run_ids,
        holdout_run_ids,
    )
    holdout_run_ids.update(_project_ledger_events(snapshot.ledger_events, store_view))
    holdout_run_ids.update(snapshot.holdout_run_ids)
    _write_frontier_snapshot(snapshot.frontier_doc, store_view)
    _expand_baseline_facets(main_root, source_commit, view_dir / "baseline", runner=runner)


def _unsafe_overlay_path(rel: str) -> bool:
    # proposal.schema.json と mh.validate_overlay が主防御。ここでは二次防御として facets/ に固定する。
    path = Path(rel)
    return path.is_absolute() or ".." in path.parts or not rel.startswith("facets/")


def _compact_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def _create_view_dir(*, main_root: Path, config: dict, view_parent: Path | None) -> Path:
    parent = _resolve_view_parent(view_parent)
    view_dir = Path(tempfile.mkdtemp(prefix=_VIEW_PREFIX, dir=str(parent)))
    try:
        _assert_view_outside_repo(view_dir, main_root, config)
    except Exception:
        shutil.rmtree(view_dir, ignore_errors=True)
        raise
    return view_dir


def _resolve_view_parent(view_parent: Path | None) -> Path:
    if view_parent is not None:
        view_parent.mkdir(parents=True, exist_ok=True)
        return view_parent.resolve()
    tmp_parent = Path(tempfile.gettempdir()).resolve()
    tmp_parent.mkdir(parents=True, exist_ok=True)
    return tmp_parent


def _assert_view_outside_repo(view_dir: Path, main_root: Path, config: dict) -> None:
    resolved_view = view_dir.resolve()
    protected_roots = [main_root.resolve(), mh.store_dir(main_root, config).resolve()]
    for root in protected_roots:
        if _is_same_or_descendant(resolved_view, root):
            raise ViewBuildError(f"filtered view must be outside repo/store: {resolved_view}")


def _read_frontier_doc(main_root: Path, config: dict) -> dict[str, Any]:
    path = mh.frontier_path(main_root, config)
    if not path.is_file():
        raise ViewBuildError(f"frontier cache is missing: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ViewBuildError(f"frontier cache is invalid: {path}") from exc
    if not isinstance(doc, dict):
        raise ViewBuildError(f"frontier cache must be an object: {path}")
    return doc


def _read_ledger_events_strict(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ViewBuildError(f"ledger contains invalid JSON at line {line_no}") from exc
        if not isinstance(event, dict):
            raise ViewBuildError(f"ledger event must be an object at line {line_no}")
        events.append(event)
    return events


def _copy_candidates(
    main_root: Path, config: dict, store_view: Path, candidate_ids: tuple[str, ...]
) -> None:
    src = mh.candidates_dir(main_root, config)
    dst = store_view / "candidates"
    if not candidate_ids:
        dst.mkdir(parents=True)
        return
    if not src.exists():
        raise ViewBuildError(f"candidate snapshot source is missing: {src}")
    _assert_not_symlink(src)
    dst.mkdir(parents=True)
    for cand_id in candidate_ids:
        cand_src = src / cand_id
        if cand_src.is_symlink():
            raise ViewBuildError(f"symlink is not allowed in candidates directory: {cand_src}")
        if not cand_src.is_dir():
            raise ViewBuildError(f"candidate from snapshot is missing: {cand_src}")
        _copy_tree_without_symlinks(cand_src, dst / cand_id)


def _copy_non_holdout_runs(
    main_root: Path,
    config: dict,
    store_view: Path,
    run_ids: tuple[str, ...],
    included_run_ids: set[str],
    holdout_run_ids: set[str],
) -> None:
    src = mh.runs_dir(main_root, config)
    dst = store_view / "runs"
    dst.mkdir(parents=True)
    if not run_ids:
        return
    if not src.exists():
        raise ViewBuildError(f"run snapshot source is missing: {src}")
    _assert_not_symlink(src)
    for run_id in run_ids:
        run_dir = src / run_id
        if run_dir.is_symlink():
            raise ViewBuildError(f"symlink is not allowed in runs directory: {run_dir}")
        if not run_dir.is_dir():
            raise ViewBuildError(f"run from snapshot is missing: {run_dir}")
        metadata = _read_run_metadata(run_dir)
        if bool(metadata.get("holdout")):
            holdout_run_ids.add(run_id)
            raise ViewBuildError(f"snapshot non-holdout run became holdout: {run_id}")
        _copy_run_dir(run_dir, dst / run_id)
        included_run_ids.add(run_id)


def _copy_run_dir(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=False)
    for entry in sorted(src.rglob("*")):
        rel = entry.relative_to(src)
        if entry.is_symlink():
            raise ViewBuildError(f"symlink is not allowed in run artifacts: {entry}")
        if rel.as_posix() == "events.jsonl.gz":
            continue
        target = dst / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if entry.is_file():
            _copy_regular_file(entry, target)
            continue
        raise ViewBuildError(f"unsupported run artifact type: {entry}")
    _expand_events_jsonl(src / "events.jsonl.gz", dst / "events.jsonl")


def _read_run_metadata(run_dir: Path) -> dict[str, Any]:
    metadata_path = run_dir / "metadata.json"
    _assert_not_symlink(metadata_path)
    if not metadata_path.is_file():
        raise ViewBuildError(f"run metadata is missing: {metadata_path}")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ViewBuildError(f"run metadata is invalid: {metadata_path}") from exc
    if not isinstance(data, dict):
        raise ViewBuildError(f"run metadata must be an object: {metadata_path}")
    return data


def _expand_events_jsonl(
    src: Path, dst: Path, *, max_bytes: int = DEFAULT_MAX_EXPANDED_EVENTS_BYTES
) -> None:
    if not src.is_file():
        return
    _assert_not_symlink(src)
    written = 0
    try:
        with gzip.open(src, "rb") as gz, dst.open("wb") as out:
            while True:
                chunk = gz.read(_EXPAND_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ViewBuildError(
                        f"events.jsonl.gz expands beyond max_bytes={max_bytes}: {src}"
                    )
                out.write(chunk)
    except (OSError, gzip.BadGzipFile) as exc:
        raise ViewBuildError(f"could not expand events.jsonl.gz: {src}") from exc
    _remove_executable_bits(dst)


def _project_ledger_events(events: tuple[dict[str, Any], ...], store_view: Path) -> set[str]:
    dst = store_view / "ledger.jsonl"
    holdout_run_ids: set[str] = set()
    projected_lines: list[str] = []
    for event in events:
        if event.get("event") == "run_completed" and bool(event.get("holdout")):
            run_id = event.get("run_id")
            if run_id:
                holdout_run_ids.add(str(run_id))
            continue
        projected_lines.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
    dst.write_text("\n".join(projected_lines) + ("\n" if projected_lines else ""), encoding="utf-8")
    return holdout_run_ids


def _list_holdout_run_dirs(main_root: Path, config: dict) -> set[str]:
    base = mh.holdout_runs_dir(main_root, config)
    if not base.is_dir():
        return set()
    _assert_not_symlink(base)
    names: set[str] = set()
    for path in base.iterdir():
        if path.is_symlink():
            raise ViewBuildError(f"symlink is not allowed in holdout runs directory: {path}")
        if path.is_dir():
            names.add(path.name)
    return names


def _write_frontier_snapshot(frontier_doc: dict[str, Any], store_view: Path) -> None:
    (store_view / "frontier.json").write_text(
        json.dumps(frontier_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _expand_baseline_facets(
    main_root: Path, source_commit: str, baseline_dir: Path, *, runner: SubprocessRunner
) -> None:
    baseline_dir.mkdir(parents=True, exist_ok=False)
    try:
        completed = runner(
            ["git", "archive", "--format=tar", source_commit, "facets"],
            cwd=main_root,
            capture_output=True,
            timeout=mh.GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ViewBuildError(f"git archive failed for source_commit={source_commit}") from exc
    if completed.returncode != 0:
        stderr = _decode_subprocess_output(completed.stderr).strip()
        raise ViewBuildError(
            f"git archive failed for source_commit={source_commit}: {stderr[:500]}"
        )
    _extract_tar_safely(completed.stdout, baseline_dir)


def _extract_tar_safely(payload: bytes, dst: Path) -> None:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                _extract_one_member(archive, member, dst)
    except (tarfile.TarError, OSError) as exc:
        raise ViewBuildError("could not extract baseline facets archive") from exc


def _extract_one_member(archive: tarfile.TarFile, member: tarfile.TarInfo, dst: Path) -> None:
    rel = Path(member.name)
    if rel.is_absolute() or ".." in rel.parts:
        raise ViewBuildError(f"unsafe baseline archive member: {member.name}")
    target = dst / rel
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return
    if member.issym() or member.islnk():
        raise ViewBuildError(f"symlink is not allowed in baseline facets: {member.name}")
    if not member.isfile():
        raise ViewBuildError(f"unsupported baseline archive member: {member.name}")
    source = archive.extractfile(member)
    if source is None:
        raise ViewBuildError(f"could not read baseline archive member: {member.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, target.open("wb") as out:
        shutil.copyfileobj(source, out)
    _remove_executable_bits(target)


def _copy_tree_without_symlinks(src: Path, dst: Path) -> None:
    _assert_not_symlink(src)
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.rglob("*")):
        rel = entry.relative_to(src)
        target = dst / rel
        if entry.is_symlink():
            raise ViewBuildError(f"symlink is not allowed in filtered view source: {entry}")
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if entry.is_file():
            _copy_regular_file(entry, target)
            continue
        raise ViewBuildError(f"unsupported filtered view source type: {entry}")


def _copy_regular_file(src: Path, dst: Path) -> None:
    _assert_not_symlink(src)
    if not src.is_file():
        raise ViewBuildError(f"expected regular file in filtered view source: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    _remove_executable_bits(dst)


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ViewBuildError(f"symlink is not allowed in filtered view source: {path}")


def _remove_executable_bits(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode & ~_EXECUTABLE_BITS)


def _verify_no_symlinks(view_dir: Path) -> None:
    for path in view_dir.rglob("*"):
        if path.is_symlink():
            raise ViewBuildError(f"symlink must not be present in filtered view: {path}")


def _verify_no_git_entries(view_dir: Path) -> None:
    for path in view_dir.rglob("*"):
        if path.name == ".git":
            raise ViewBuildError(f".git entry must not be present in filtered view: {path}")


def _verify_no_instruction_files(view_dir: Path) -> None:
    for path in view_dir.rglob("*"):
        if path.name in _INSTRUCTION_FILE_NAMES:
            raise ViewBuildError(f"instruction file must not be present in filtered view: {path}")


def _verify_no_executable_files(view_dir: Path) -> None:
    for path in view_dir.rglob("*"):
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) & _EXECUTABLE_BITS:
            raise ViewBuildError(f"executable file must not be present in filtered view: {path}")


def _verify_no_holdout_ledger_rows(view_dir: Path) -> None:
    ledger = view_dir / "store" / "ledger.jsonl"
    if not ledger.is_file():
        raise ViewBuildError("filtered view ledger is missing")
    for line_no, raw_line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ViewBuildError(f"filtered view ledger is invalid at line {line_no}") from exc
        if not isinstance(event, dict):
            raise ViewBuildError(f"filtered view ledger event must be an object at line {line_no}")
        if event.get("event") == "run_completed" and bool(event.get("holdout")):
            raise ViewBuildError(
                f"holdout run_completed leaked into filtered view at line {line_no}"
            )


def _verify_holdout_ids_absent(view_dir: Path, known_holdout_run_ids: set[str]) -> None:
    if not known_holdout_run_ids:
        return
    runs_dir = view_dir / "store" / "runs"
    for run_id in known_holdout_run_ids:
        if (runs_dir / run_id).exists():
            raise ViewBuildError(f"holdout run directory leaked into filtered view: {run_id}")
    _scan_files_for_holdout_ids(view_dir, known_holdout_run_ids)


def _scan_files_for_holdout_ids(view_dir: Path, known_holdout_run_ids: set[str]) -> None:
    needles = [run_id.encode("utf-8") for run_id in known_holdout_run_ids if run_id]
    for path in view_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(needle in data for needle in needles):
            raise ViewBuildError(f"holdout run id leaked into filtered view file: {path}")


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _decode_subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
