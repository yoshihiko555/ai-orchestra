"""教訓候補の台帳の読み書き（設計 docs/design/fail-logs.md §6・§8.2〜§8.5）。

- 台帳（``lessons.jsonl``）への追記、写像（``lessons-state.json``）と一覧（``lessons.md``）の更新は、
  ``lessons.lock`` の flock の中で、追いつき → 追記 → 写像の置換 → 一覧の置換 の順に行う。
- 読むだけのプロセスは写像を読む。台帳の大きさと写像の位置が違えば、ロックを LOCK_NB で取れたときだけ追いつく。
- root に書けない環境では、候補の追加だけを作業中の worktree の spool に退避し、hook とワーカーが取り込む。
"""

from __future__ import annotations

import copy
import errno
import fcntl
import glob
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import lessons_model as model
from sanitize import ValidationError, frame, neutralize, one_line

LEDGER_FILE = "lessons.jsonl"
STATE_FILE = "lessons-state.json"
MARKDOWN_FILE = "lessons.md"
LOCK_FILE = "lessons.lock"
SPOOL_DIR = "spool"

DIR_MODE = 0o700
FILE_MODE = 0o600
LOCK_POLL_SECONDS = 0.05
SPOOL_MAX_FILE_BYTES = 64 * 1024
SPOOL_IMPORT_LIMIT = 100
GIT_TIMEOUT_SECONDS = 3.0
SPOOL_EVENT_TYPES = frozenset({model.EV_CREATED, model.EV_EVIDENCE})
_UNWRITABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


class LockTimeout(Exception):
    """台帳のロックを決められた時間内に取れなかった。"""


class LedgerUnwritable(Exception):
    """root の台帳に書けない（サンドボックスの書き込み許可の外など）。"""


def is_unwritable_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in _UNWRITABLE_ERRNOS


def ensure_dir(path: str) -> None:
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def _open_nofollow(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, FILE_MODE)


def atomic_write(path: str, text: str) -> None:
    """一時ファイル（0600）に書いてから置き換える。"""
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def file_lock(directory: str, wait_seconds: float, *, nonblocking: bool = False) -> Iterator[bool]:
    """``lessons.lock`` に flock をかける。nonblocking なら取れなくても待たずに False を渡す。"""
    fd = _open_nofollow(os.path.join(directory, LOCK_FILE), os.O_RDWR | os.O_CREAT)
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if nonblocking:
                    break
                if time.monotonic() >= deadline:
                    raise LockTimeout("lessons.lock is busy") from None
                time.sleep(LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class LessonStore:
    """root の ``.claude/logs/fail-logs/`` にある台帳・写像・一覧を扱う。"""

    def __init__(
        self,
        log_dir: str,
        *,
        lock_wait_seconds: float = 5.0,
        threshold: int = 2,
        stale_days: int = 14,
    ) -> None:
        self.log_dir = log_dir
        self.lock_wait_seconds = lock_wait_seconds
        self.threshold = threshold
        self.stale_days = stale_days
        self.ledger_path = os.path.join(log_dir, LEDGER_FILE)
        self.state_path = os.path.join(log_dir, STATE_FILE)
        self.markdown_path = os.path.join(log_dir, MARKDOWN_FILE)

    # --- 読み取り -------------------------------------------------------------

    def _ledger_size(self) -> int:
        try:
            return os.lstat(self.ledger_path).st_size
        except FileNotFoundError:
            return 0

    def _load_state(self) -> dict | None:
        try:
            fd = _open_nofollow(self.state_path, os.O_RDONLY)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(snapshot, dict) or snapshot.get("v") != model.SCHEMA_VERSION:
            return None
        if not isinstance(snapshot.get("lessons"), dict) or not isinstance(
            snapshot.get("ledger_offset"), int
        ):
            return None
        snapshot.setdefault("aliases", {})
        snapshot.setdefault("daily_adds", {"date": "", "counts": {}})
        return snapshot

    def _catch_up(self, snapshot: dict | None) -> dict:
        """写像に台帳の差分を畳み込む。写像が無い・壊れている・位置が台帳より先なら作り直す。"""
        size = self._ledger_size()
        current: dict = (
            snapshot
            if snapshot is not None and snapshot["ledger_offset"] <= size
            else model.empty_snapshot()
        )
        offset = int(current["ledger_offset"])
        if offset == size:
            return current
        try:
            fd = _open_nofollow(self.ledger_path, os.O_RDONLY)
        except FileNotFoundError:
            return model.empty_snapshot()
        with os.fdopen(fd, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(size - offset)
        consumed = chunk.rfind(b"\n") + 1  # 行の途中（書き込み中の中断）は読まない
        for raw in chunk[:consumed].split(b"\n"):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            model.apply_event(current, event)
            if isinstance(event, dict) and isinstance(event.get("eid"), str):
                current["last_eid"] = event["eid"]
        current["ledger_offset"] = offset + consumed
        return current

    def read(self) -> dict | None:
        """写像を読む（読むだけのプロセス用）。差分があれば LOCK_NB で取れたときだけ追いつく。

        Returns:
            写像。台帳も写像も無ければ空の写像。写像が壊れていてロックも取れなければ None。
        """
        snapshot = self._load_state()
        size = self._ledger_size()
        if snapshot is not None and snapshot["ledger_offset"] == size:
            return snapshot
        if snapshot is None and size == 0:
            return model.empty_snapshot()
        try:
            with file_lock(self.log_dir, 0, nonblocking=True) as acquired:
                if not acquired:
                    return snapshot
                loaded = self._load_state()
                updated = self._catch_up(loaded)
                if loaded is None or updated["ledger_offset"] != loaded["ledger_offset"]:
                    self._persist(updated)
                return updated
        except OSError:
            return snapshot

    # --- 書き込み -------------------------------------------------------------

    def _persist(self, snapshot: dict) -> None:
        """一覧を先に、写像を後に置き換える（写像の置換の前に落ちても、次の追いつきで一覧も作り直す）。"""
        atomic_write(self.markdown_path, render_markdown(snapshot, self.threshold, self.stale_days))
        atomic_write(
            self.state_path, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        )

    def _append_lines(self, events: list[dict]) -> None:
        """台帳に追記する。末尾が改行で終わっていなければ、改行を足してからつなぐ。"""
        payload = "".join(
            json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events
        )
        fd = _open_nofollow(self.ledger_path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
        try:
            os.fchmod(fd, FILE_MODE)
            size = os.fstat(fd).st_size
            prefix = b""
            if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                prefix = b"\n"
            os.write(fd, prefix + payload.encode("utf-8"))
        finally:
            os.close(fd)

    def append_events(
        self,
        build: Callable[[dict], list[dict]],
        *,
        nonblocking: bool = False,
    ) -> tuple[dict, list[dict]]:
        """ロックの中で、追いつき → 出来事を作る → 検査 → 追記 → 写像と一覧の置換、を行う。

        Args:
            build: 追いついた写像を受け取り、追記する出来事を返す関数（状態の検査に写像を使える）。
            nonblocking: ロックを待たない（SessionStart の hook 用）。取れなければ LockTimeout。

        Raises:
            LedgerUnwritable: root に書けない。
            LockTimeout: ロックを取れない。
            ValidationError: 出来事の検査（状態の遷移など）に失敗した。
        """
        try:
            ensure_dir(self.log_dir)
            with file_lock(
                self.log_dir, self.lock_wait_seconds, nonblocking=nonblocking
            ) as acquired:
                if not acquired:
                    raise LockTimeout("lessons.lock is busy")
                snapshot = self._catch_up(self._load_state())
                events = build(copy.deepcopy(snapshot))
                if not events:
                    return snapshot, []
                checked = copy.deepcopy(snapshot)
                for event in events:
                    model.apply_event(checked, event, strict=True)
                self._append_lines(events)
                # 追記した行は台帳から読み直して畳み込む（改行だけ欠けていた末尾の行も、作り直しと同じく反映される）。
                snapshot = self._catch_up(snapshot)
                self._persist(snapshot)
                return snapshot, events
        except OSError as exc:
            if is_unwritable_error(exc):
                raise LedgerUnwritable(str(exc)) from exc
            raise

    def rebuild(self) -> dict:
        """台帳の全体から写像を作り直す。"""
        try:
            ensure_dir(self.log_dir)
            with file_lock(self.log_dir, self.lock_wait_seconds):
                snapshot = self._catch_up(None)
                self._persist(snapshot)
                return snapshot
        except OSError as exc:
            if is_unwritable_error(exc):
                raise LedgerUnwritable(str(exc)) from exc
            raise

    def render(self) -> dict:
        """一覧（lessons.md）を写像から作り直す（追いついてから書く）。"""
        try:
            ensure_dir(self.log_dir)
            with file_lock(self.log_dir, self.lock_wait_seconds):
                snapshot = self._catch_up(self._load_state())
                self._persist(snapshot)
                return snapshot
        except OSError as exc:
            if is_unwritable_error(exc):
                raise LedgerUnwritable(str(exc)) from exc
            raise


# --- 一覧（lessons.md）-----------------------------------------------------------

_STATE_HEADINGS = (
    (model.STATE_CANDIDATE, "候補"),
    (model.STATE_PROMOTING, "昇格中"),
    (model.STATE_PROMOTED, "昇格済み"),
    (model.STATE_REJECTED, "却下"),
)


def _lesson_block(lesson: dict) -> list[str]:
    return [
        f"### {neutralize(lesson['lid'])} {one_line(lesson.get('title', ''), 120)}",
        "",
        f"- 状態: {lesson.get('state')} / 再発回数: {lesson.get('recurrence', 0)} / "
        f"重大度: {lesson.get('severity')} / 対象範囲: {lesson.get('scope')} / "
        f"根拠: {len(lesson.get('evidence', []))} 件",
        f"- 事象: {one_line(lesson.get('event', ''), 400)}",
        f"- 原因: {one_line(lesson.get('cause', ''), 400)}",
        f"- 回避策: {one_line(lesson.get('avoidance', ''), 600)}",
        "",
    ]


def render_markdown(snapshot: dict, threshold: int, stale_days: int) -> str:
    """写像を状態ごとの Markdown にする。候補の文は中和し、未承認であることを冒頭に書く（設計 §8.4）。"""
    lines = [
        "# fail-logs 教訓候補",
        "",
        f"> {neutralize(frame([]).splitlines()[0])}",
        "> このファイルは台帳から自動で作り直される。手で編集しない。",
        "",
    ]
    presented = model.presentable(snapshot, threshold, stale_days)
    lines += ["## 提示対象", ""]
    if presented:
        lines += [f"- {neutralize(lesson['lid'])}（{reason}）" for lesson, reason in presented]
    else:
        lines.append("- なし")
    lines.append("")
    lessons = sorted(
        snapshot.get("lessons", {}).values(), key=lambda item: item.get("created_at", "")
    )
    for state, heading in _STATE_HEADINGS:
        lines += [f"## {heading}", ""]
        selected = [lesson for lesson in lessons if lesson.get("state") == state]
        if not selected:
            lines += ["- なし", ""]
            continue
        for lesson in selected:
            lines += _lesson_block(lesson)
    return "\n".join(lines) + "\n"


# --- spool（root に書けない環境）-------------------------------------------------


_SPOOL_NAME = re.compile(r"[0-9a-f]{32}\.json")
SPOOL_IMPORT_MAX_BYTES = 1024 * 1024
SPOOL_COUNT_LIMIT = 10 * SPOOL_IMPORT_LIMIT


def write_spool(spool_dir: str, event: dict) -> str:
    """候補の追加を spool に退避する。ファイル名は出来事の eid（32 桁の 16 進）。"""
    ensure_dir(spool_dir)
    path = os.path.join(spool_dir, f"{event['eid']}.json")
    fd = _open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False)
    return path


def _open_spool_dir(spool_dir: str) -> int | None:
    """spool のディレクトリを開く。symlink・自分の所有でないもの・ディレクトリでないものは開かない。"""
    try:
        fd = os.open(spool_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        return None
    return fd


def _read_spool_entry(dir_fd: int, name: str) -> dict | None:
    """spool のファイルを安全に読む。通常ファイル・自分の uid・64 KiB 以下でなければ None。"""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if info.st_size > SPOOL_MAX_FILE_BYTES:
            return None
        data = os.read(fd, SPOOL_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        event = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return event if isinstance(event, dict) else None


def _spool_names(dir_fd: int, limit: int) -> list[str]:
    """決まった形（32 桁の 16 進 + .json）の名前だけを、上限まで集める（全件は読まない）。"""
    names: list[str] = []
    try:
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                if _SPOOL_NAME.fullmatch(entry.name):
                    names.append(entry.name)
                    if len(names) >= limit:
                        break
    except OSError:
        return []
    return sorted(names)


def count_spool(spool_dir: str) -> int:
    """spool に溜まっている追加の数（退避する側の上限の判定に使う）。"""
    fd = _open_spool_dir(spool_dir)
    if fd is None:
        return 0
    try:
        return len(_spool_names(fd, SPOOL_COUNT_LIMIT))
    finally:
        os.close(fd)


def read_spool(spool_dir: str, limit: int = SPOOL_IMPORT_LIMIT) -> list[tuple[str, dict]]:
    """spool の中身を (eid, 出来事) で読む（消さない。CLI の一覧で重ねて表示する用）。"""
    fd = _open_spool_dir(spool_dir)
    if fd is None:
        return []
    try:
        result = []
        for name in _spool_names(fd, limit):
            raw = _read_spool_entry(fd, name)
            if raw is not None:
                result.append((name[: -len(".json")], raw))
        return result
    finally:
        os.close(fd)


def transcript_roots() -> list[str]:
    """セッションの記録を置く許可するルート（設計 §7.7）。"""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return [os.path.join(config_dir, "projects")]


def transcript_exists(session_ref: str, roots: list[str]) -> bool:
    """そのセッションの記録が許可するルートの配下にあるか（自己申告の根拠の確認）。"""
    pattern_name = f"{glob.escape(session_ref)}.jsonl"
    for root in roots:
        for candidate in glob.glob(os.path.join(glob.escape(root), "*", pattern_name)):
            if os.path.isfile(candidate) and not os.path.islink(candidate):
                return True
    return False


def normalize_spool_event(
    raw: dict,
    eid: str,
    masker: model.Masker,
    roots: list[str],
    verified_cache: dict[str, bool] | None = None,
) -> dict | None:
    """spool のファイルを、取り込める出来事にする。種類・actor・source を固定し、検査を通す（設計 §8.5）。

    根拠の日時は使わず取り込み時刻にする（昇格後の再発回数を未来の日時で偽装させない）。
    """
    if raw.get("type") not in SPOOL_EVENT_TYPES or not model.is_lid(raw.get("lid")):
        return None
    data = raw.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("evidence"), dict):
        return None
    evidence = {key: data["evidence"].get(key) for key in ("kind", "ref", "failures")}
    try:
        normalized = model.build_lesson_data(data, masker, source="agent", evidence=evidence)
        ref = normalized["evidence"]["ref"]
        if normalized["evidence"]["kind"] == "session":
            cache = verified_cache if verified_cache is not None else {}
            if ref not in cache:
                cache[ref] = transcript_exists(ref, roots)
            if cache[ref]:
                normalized["evidence"]["verified"] = True
        sid = raw.get("sid") if isinstance(raw.get("sid"), str) else ""
        return model.make_event(
            raw["type"], raw["lid"], model.ACTOR_SPOOL, sid[:64], normalized, eid=eid
        )
    except ValidationError:
        return None


def _collect_spool(
    dir_fd: int, masker: model.Masker, roots: list[str], cache: dict, budget: list[int]
) -> tuple[list[dict], list[str]]:
    """1 つの spool から取り込む出来事と、消す名前を集める。1 ファイルの失敗で全体を止めない。"""
    events: list[dict] = []
    processed: list[str] = []
    for name in _spool_names(dir_fd, SPOOL_IMPORT_LIMIT):
        if budget[0] <= 0:
            break
        try:
            size = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_size
            budget[0] -= min(size, SPOOL_MAX_FILE_BYTES)
            raw = _read_spool_entry(dir_fd, name)
            eid = name[: -len(".json")]
            event = normalize_spool_event(raw, eid, masker, roots, cache) if raw else None
        except Exception:  # noqa: BLE001 - 不正なファイルは消して、次回の取り込みを止めない
            event = None
        if event is not None:
            events.append(event)
        processed.append(name)
    return events, processed


def import_spool(
    store: LessonStore, spool_dirs: list[str], masker: model.Masker, *, nonblocking: bool = False
) -> int:
    """spool を取り込み、取り込んだ（または取り込めない）ファイルを消す。取り込んだ出来事の数を返す。

    spool のディレクトリは symlink をたどらずに開き、その中の決まった形の名前だけを、開いたディレクトリからの
    相対で読んで消す（symlink を仕込んで別の場所のファイルを消させない）。
    """
    roots = transcript_roots()
    cache: dict[str, bool] = {}
    budget = [SPOOL_IMPORT_MAX_BYTES]
    opened: list[tuple[int, list[str]]] = []
    events: list[dict] = []
    try:
        for spool_dir in spool_dirs:
            fd = _open_spool_dir(spool_dir)
            if fd is None:
                continue
            opened.append((fd, []))
            collected, processed = _collect_spool(fd, masker, roots, cache, budget)
            events.extend(collected)
            opened[-1] = (fd, processed)
        if events:
            store.append_events(lambda _snapshot: events, nonblocking=nonblocking)
        for fd, processed in opened:
            for name in processed:
                try:
                    os.unlink(name, dir_fd=fd)
                except OSError:
                    pass
        return len(events)
    finally:
        for fd, _processed in opened:
            os.close(fd)


def list_worktree_dirs(
    root: str, env: dict[str, str], timeout: float = GIT_TIMEOUT_SECONDS
) -> list[str]:
    """``git worktree list`` に出る全 worktree のパス（引数の配列で起動し、シェルを介さない）。"""
    try:
        result = subprocess.run(
            ["git", "-C", root, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [root]
    if result.returncode != 0:
        return [root]
    dirs = [
        line[len("worktree ") :]
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    return dirs or [root]


def local_date() -> str:
    return datetime.now(UTC).astimezone().date().isoformat()
