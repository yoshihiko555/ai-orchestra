"""下書きの承認・差し戻し・コピーと main のスイート照合。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from difflib import unified_diff
from pathlib import Path

import candidates_model as model
import yaml
from candidates_checks import DraftBaseline, load_yaml
from candidates_draft import file_hashes, inspect_files, load_draft
from candidates_ingest import CandidatesContext
from candidates_safety import (
    REJECTED_CATEGORIES,
    RawDraftText,
    ValidationError,
    load_masker,
    open_regular,
    walk_nodes,
)
from candidates_scenario_events import latest_record, validate_answers, validate_files

TRACKING_NOTE = "origin/{default} はローカルの追跡参照です。先に git fetch origin {default} を実行してください。"

INSTALL_GUIDANCE = (
    "回帰予算 regression.max_budget_usd と max_affected_suites を人が見直してください。"
    "予算の変更と .claude/orchestra.json の file_hashes 更新を同じ PR に含めてください。"
    "予算変更は evaluator_hash を変えるため、進行中の meta-harness の昇格が無いときにマージしてください。"
    "PR 本文に候補 ID と承認したハッシュを書き、人が commit・push・PR 作成・マージを行ってください。"
)


def state_event(candidate: dict, target: str, **extra: object) -> dict:
    return model.event(
        "candidate.state_changed",
        candidate["cid"],
        {"from": candidate["state"], "to": target, **extra},
    )


def approve(context: CandidatesContext, cid: str, payload: dict) -> dict:
    if payload.keys() != {"draft", "files", "answers", "human_written_lines"}:
        raise ValidationError("承認の JSON の項目が正しくありません")
    validate_answers(payload["answers"], payload["human_written_lines"])
    validate_files(payload["files"])
    with context.store.locked():
        snapshot = context.store.current_locked()
        candidate = model.require_candidate(snapshot, cid)
        drafted = latest_record(snapshot, candidate["cid"], "scenario.drafted")
        if (
            candidate["state"] != "drafting"
            or not drafted
            or type(payload["draft"]) is not int
            or payload["draft"] != drafted["data"]["draft"]
        ):
            raise ValidationError("最新の下書きを drafting 状態で承認してください")
        files = load_draft(context, drafted)
        if file_hashes(files) != payload["files"]:
            raise ValidationError(
                "表示した sha256 と現在の下書きが一致しません。show をやり直してください"
            )
        original = load_draft(context, drafted, original=True)
        baseline = DraftBaseline(context.orchestra)
        masker = load_masker()
        original_lines = inspect_files(context, snapshot, candidate, original, baseline, masker)[
            "executable_lines"
        ]
        checks = inspect_files(context, snapshot, candidate, files, baseline, masker)
        human_lines = payload["human_written_lines"]
        if any(line not in checks["executable_lines"] for line in human_lines):
            raise ValidationError("人が書いた行が現在の下書きと一致しません")
        if any(
            any(
                line["file"] == old["file"] and line["line"] == old["line"]
                for old in original_lines
            )
            for line in human_lines
        ):
            raise ValidationError("LLM の元の行を人が書いた行として承認できません")
        refused = [
            finding
            for finding in checks["findings"]
            if finding["category"] != "shape"
            or {key: finding[key] for key in ("file", "path", "line")} not in human_lines
            or payload["answers"]["human_lines_reviewed"] is not True
        ]
        if refused:
            return {"approved": False, "checks": checks}
        approval = model.event(
            "scenario.approved",
            candidate["cid"],
            {**payload, "draft_eid": drafted["eid"], "files": file_hashes(files)},
        )
        records = [approval, state_event(candidate, "scenario_approved", refs=[approval["eid"]])]
        context.store.append_locked(snapshot, records)
    return {"approved": True, "events": records, "checks": checks}


def send_back(context: CandidatesContext, cid: str, note: str) -> dict:
    records = []

    def prepare(snapshot: dict) -> list[dict]:
        candidate = model.require_candidate(snapshot, cid)
        if candidate["state"] != "scenario_approved":
            raise ValidationError("scenario_approved の候補を差し戻してください")
        records.append(state_event(candidate, "drafting", note=note))
        return records

    context.store.write(prepare)
    return {"events": records}


def current_approval(snapshot: dict, cid: str) -> tuple[dict, dict]:
    candidate = model.require_candidate(snapshot, cid)
    approval = latest_record(snapshot, candidate["cid"], "scenario.approved")
    drafted = latest_record(snapshot, candidate["cid"], "scenario.drafted")
    if (
        candidate["state"] not in {"scenario_approved", "improving"}
        or not approval
        or not drafted
        or approval["data"]["draft_eid"] != drafted["eid"]
    ):
        raise ValidationError("現在の下書きに対する承認が必要です")
    return candidate, approval


def approved_files(context: CandidatesContext, approval: dict) -> dict[str, bytes]:
    files = load_draft(context, approval)
    if file_hashes(files) != approval["data"]["files"]:
        raise ValidationError("承認した下書きが変わっています")
    return files


def git_read(
    directory: Path, *arguments: str, check: bool = True, binary: bool = False
) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=not binary,
        env=env,
        timeout=15,
    )
    if check and completed.returncode:
        raise ValidationError("git の読み取り検査に失敗しました")
    return completed


def default_branch(directory: Path) -> str:
    resolved = git_read(
        directory, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False
    )
    if resolved.returncode == 1:
        return "main"
    if resolved.returncode:
        raise ValidationError("既定ブランチの読み取りに失敗しました")
    prefix = "refs/remotes/origin/"
    reference = resolved.stdout.strip()
    if not reference.startswith(prefix) or not reference[len(prefix) :]:
        raise ValidationError("既定ブランチの参照が正しくありません")
    return reference[len(prefix) :]


def validate_destination(context: CandidatesContext, destination: Path) -> Path:
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError("導入先がありません。既存の worktree を指定してください") from None
    if not stat.S_ISDIR(mode):
        raise ValidationError("導入先は symlink でないディレクトリを指定してください")
    destination = destination.resolve()
    listing = git_read(context.orchestra, "worktree", "list", "--porcelain", "-z").stdout
    worktrees = {
        Path(field.removeprefix("worktree ")).resolve()
        for field in listing.split("\0")
        if field.startswith("worktree ")
    }
    if destination not in worktrees:
        raise ValidationError("このリポジトリの worktree を指定してください")
    branch = git_read(destination, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    default = default_branch(context.orchestra)
    tracking = f"origin/{default}"
    if branch in {default, "HEAD", ""}:
        raise ValidationError("既定ブランチから切った作業ブランチの worktree を指定してください")
    if git_read(
        destination, "merge-base", "--is-ancestor", tracking, "HEAD", check=False
    ).returncode:
        raise ValidationError(f"導入先の HEAD は {tracking} を祖先に持つ必要があります")
    return destination


@contextmanager
def suite_descriptor(destination: Path, slug: str, *, create: bool) -> Iterator[int]:
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValidationError("スキルの slug が正しくありません")
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(destination.lstat().st_mode):
            raise ValidationError("導入先は通常のディレクトリが必要です")
        for part in ("packages", "meta-harness", "scenarios", "skill", slug):
            created = False
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                created = True
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValidationError(
                    "スイートの途中に symlink または通常でないディレクトリがあります"
                )
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            if created:
                os.fchmod(child, 0o755)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def install(context: CandidatesContext, cid: str, destination: Path) -> dict:
    with context.store.locked():
        snapshot = context.store.current_locked()
        candidate, approval = current_approval(snapshot, cid)
        files = approved_files(context, approval)
        destination = validate_destination(context, destination)
        with suite_descriptor(destination, candidate["skill"], create=True) as directory:
            existing = set(os.listdir(directory))
            if existing & files.keys():
                raise ValidationError("同じ名前のファイルが既にあります。上書きしません")
            created = []
            try:
                for name, raw in files.items():
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o644,
                        dir_fd=directory,
                    )
                    created.append(name)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                    with os.fdopen(descriptor, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValidationError("コピー先が通常ファイルではありません")
                        digest = hashlib.sha256(stream.read()).hexdigest()
                    if digest != approval["data"]["files"][name]:
                        raise ValidationError("コピー後の sha256 が承認と一致しません")
            except Exception:
                for name in created:
                    os.unlink(name, dir_fd=directory)
                raise
    return {"installed": approval["data"]["files"], "guidance": INSTALL_GUIDANCE}


def compute_suite_hash(paths: list[Path]) -> str:
    digests = []
    for path in sorted(paths, key=lambda path: path.name):
        with open_regular(path) as stream:
            digests.append(hashlib.sha256(stream.read()).hexdigest())
    return hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()


def installed_suite_hash(destination: Path, slug: str) -> str:
    digests = []
    with suite_descriptor(destination, slug, create=False) as directory:
        for name in sorted(name for name in os.listdir(directory) if name.endswith(".yaml")):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValidationError("スイートには通常のシナリオファイルが必要です")
                digests.append(hashlib.sha256(stream.read()).hexdigest())
    return hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()


def deep_merge(base: dict, override: dict) -> dict:
    list(walk_nodes(base))
    list(walk_nodes(override))
    merged = dict(base)
    pending = [(merged, override)]
    visited = set()
    budget = 5000
    while pending:
        target, source = pending.pop()
        pair = (id(target), id(source))
        if pair in visited:
            continue
        visited.add(pair)
        for key, value in source.items():
            budget -= 1
            if budget < 0:
                raise ValidationError("設定のノード数が上限を超えています")
            if isinstance(target.get(key), dict) and isinstance(value, dict):
                target[key] = dict(target[key])
                pending.append((target[key], value))
            else:
                target[key] = value
    return merged


def effective_meta_config(context: CandidatesContext) -> dict:
    project_base = context.project / ".claude/config/meta-harness/meta-harness.yaml"
    base = (
        project_base
        if project_base.is_file()
        else context.orchestra / "packages/meta-harness/config/meta-harness.yaml"
    )
    project_local = context.project / ".claude/config/meta-harness/meta-harness.local.yaml"
    local = project_local if project_local.is_file() else base.with_name("meta-harness.local.yaml")
    config = {}
    for path in (base, local):
        if not path.exists():
            continue
        try:
            with open_regular(path) as stream:
                payload = load_yaml(stream.read().decode("utf-8"))
            if payload is not None and not isinstance(payload, dict):
                raise ValueError
        except (ValueError, yaml.YAMLError):
            raise ValidationError("meta-harness の設定が不正です") from None
        config = deep_merge(config, payload or {})
    return config


def committed_suite(context: CandidatesContext, slug: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValidationError("スキルの slug が正しくありません")
    suite = f"packages/meta-harness/scenarios/skill/{slug}"
    listing = git_read(context.project, "ls-tree", "-rz", "HEAD", "--", suite).stdout
    blobs = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, _ = metadata.split(" ", 2)
        name = path.removeprefix(suite + "/")
        if "/" in name or not name.endswith(".yaml"):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValidationError("コミットしたシナリオは通常ファイルである必要があります")
        blobs[name] = git_read(
            context.project, "cat-file", "blob", f"HEAD:{path}", binary=True
        ).stdout
    return blobs


def verify_suite(context: CandidatesContext, cid: str) -> dict:
    records = []
    with context.store.locked():
        snapshot = context.store.current_locked()
        candidate, approval = current_approval(snapshot, cid)
        default = default_branch(context.project)
        tracking = f"origin/{default}"
        tracking_note = TRACKING_NOTE.format(default=default)
        if git_read(context.project, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != default:
            raise ValidationError(
                "既定ブランチの checkout で verify-suite を実行してください。" + tracking_note
            )
        if git_read(
            context.project, "merge-base", "--is-ancestor", "HEAD", tracking, check=False
        ).returncode:
            raise ValidationError(f"HEAD が {tracking} から辿れません。" + tracking_note)
        if git_read(
            context.project, "status", "--porcelain", "--", "packages/meta-harness/scenarios"
        ).stdout:
            raise ValidationError("シナリオ置き場に未コミットの変更があります。" + tracking_note)
        mismatch = False
        try:
            with suite_descriptor(context.project, candidate["skill"], create=False) as directory:
                for name in os.listdir(directory):
                    if name.endswith(".yaml"):
                        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if not stat.S_ISREG(metadata.st_mode):
                            raise ValidationError(
                                "シナリオに symlink や通常でないファイルは使えません"
                            )
            draft_files = load_draft(context, approval)
        except FileNotFoundError:
            mismatch = True
            draft_files = {}
        except ValidationError:
            # symlink・権限・構成エラーは差し戻し理由に変換しない。
            raise
        blobs = committed_suite(context, candidate["skill"])
        hashes = file_hashes(blobs)
        mismatch = (
            mismatch
            or file_hashes(draft_files) != approval["data"]["files"]
            or any(hashes.get(name) != digest for name, digest in approval["data"]["files"].items())
        )
        if mismatch:
            records.append(
                state_event(
                    candidate,
                    "drafting",
                    note="承認したファイルの欠落または sha256 の不一致",
                    verification_mismatch=True,
                )
            )
        else:
            approved = {
                (name, digest)
                for record in snapshot["records"].values()
                if record["type"] == "scenario.approved"
                and model.require_candidate(snapshot, record["cid"])["skill"] == candidate["skill"]
                for name, digest in record["data"]["files"].items()
            }
            if any(
                name.startswith("sevo-") and (name, digest) not in approved
                for name, digest in hashes.items()
            ):
                raise ValidationError(
                    "スイートに未承認の sevo- シナリオがあります。" + tracking_note
                )
            config = effective_meta_config(context)
            if ((config.get("scenario_run") or {}).get("isolation") or {}).get(
                "execution_backend"
            ) != "docker":
                raise ValidationError(
                    "scenario_run.isolation.execution_backend は docker が必要です"
                )
            suite_hash = hashlib.sha256(
                "".join(hashes[name] for name in sorted(hashes)).encode("utf-8")
            ).hexdigest()
            commit = git_read(context.project, "rev-parse", "HEAD").stdout.strip()
            verified = model.event(
                "scenario.suite_verified",
                candidate["cid"],
                {
                    "approval_eid": approval["eid"],
                    "files": approval["data"]["files"],
                    "suite_hash": suite_hash,
                    "commit": commit,
                },
            )
            records.append(verified)
            if candidate["state"] == "scenario_approved":
                records.append(state_event(candidate, "improving", refs=[verified["eid"]]))
        context.store.append_locked(snapshot, records)
    if mismatch:
        return {
            "verified": False,
            "events": records,
            "note": tracking_note,
            "reason": "承認したファイルが一致しないため drafting に差し戻しました",
        }
    return {"verified": True, "events": records, "suite_hash": suite_hash, "note": tracking_note}


def escaped_text(text: str) -> str:
    return RawDraftText(
        "".join(
            f"\\u{ord(char):04x}" if unicodedata.category(char) in REJECTED_CATEGORIES else char
            for char in text
        )
    )


def draft_review(context: CandidatesContext, snapshot: dict, candidate: dict) -> dict:
    drafted = latest_record(snapshot, candidate["cid"], "scenario.drafted")
    if drafted is None:
        return {}
    approval = latest_record(snapshot, candidate["cid"], "scenario.approved")
    try:
        files = load_draft(context, drafted)
        original = load_draft(context, drafted, original=True)
    except (FileNotFoundError, ValidationError) as exc:
        return {
            "draft": drafted["data"]["draft"],
            "approval_valid": False if approval else None,
            "files": {},
            "sha256": {},
            "error": str(exc),
        }
    hashes = file_hashes(files)
    return {
        "draft": drafted["data"]["draft"],
        "sha256": hashes,
        "approval_valid": (
            hashes == approval["data"]["files"]
            and candidate["state"] in {"scenario_approved", "improving"}
            if approval and approval["data"]["draft_eid"] == drafted["eid"]
            else None
        ),
        "evaluation_section": "評価に渡る文",
        "files": {
            name: escaped_text(raw.decode("utf-8", errors="backslashreplace"))
            for name, raw in files.items()
        },
        "diffs": {
            name: escaped_text(
                "".join(
                    unified_diff(
                        original[name].decode("utf-8", errors="backslashreplace").splitlines(True),
                        raw.decode("utf-8", errors="backslashreplace").splitlines(True),
                        fromfile=f"original/{name}",
                        tofile=name,
                    )
                )
            )
            for name, raw in files.items()
        },
        "checks": inspect_files(context, snapshot, candidate, files),
    }
