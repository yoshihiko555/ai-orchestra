"""純追記の台帳と、再構築できる写像・取り込み位置。"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import candidates_model as model
import skill_evolution_common as common
from candidates_safety import (
    LedgerUnwritable,
    LockTimeout,
    ValidationError,
    open_regular,
    timestamp,
    validate_directory,
    validate_strings,
    validate_text,
)
from candidates_scenario_events import SCENARIO_EVENTS, validate_files


class CandidateStore:
    def __init__(
        self,
        root: Path,
        directory: Path,
        *,
        lock_wait: float = 5,
        orchestra: Path | None = None,
        own_project: bool = True,
    ) -> None:
        if not math.isfinite(lock_wait) or lock_wait < 0:
            raise ValidationError("ledger.lock_wait_seconds は 0 以上の有限数にしてください")
        self.root, self.directory = root.resolve(), directory.absolute()
        self.lock_wait, self.orchestra = lock_wait, orchestra
        self.own_project = own_project

    @contextmanager
    def locked(self, *, wait: bool = True, create: bool = True) -> Iterator[None]:
        try:
            validate_directory(self.directory, self.root, create=create)
            with open_regular(self.directory / "candidates.lock", os.O_RDWR | os.O_CREAT) as stream:
                os.fchmod(stream.fileno(), 0o600)
                deadline = time.monotonic() + (self.lock_wait if wait else 0)
                while True:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LockTimeout("台帳のロック待ち時間を過ぎました") from exc
                        time.sleep(min(0.01, remaining))
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EACCES, errno.EROFS}:
                raise LedgerUnwritable(
                    "root の台帳に書けません。サンドボックスの外で実行してください"
                ) from exc
            raise

    def _read_json(self, filename: str) -> Any:
        try:
            with open_regular(self.directory / filename) as stream:
                return json.load(stream)
        except (FileNotFoundError, ValueError, UnicodeError):
            return None

    def _projection(self) -> dict | None:
        snapshot = self._read_json("candidates-state.json")
        if not isinstance(snapshot, dict) or snapshot.get("v") != 1:
            return None
        if not all(
            isinstance(snapshot.get(key), dict)
            for key in ("observations", "candidates", "aliases", "records")
        ) or not isinstance(snapshot.get("applied_eids"), list):
            return None
        offset = snapshot.get("ledger_offset")
        if type(offset) is not int or offset < 0:
            return None
        try:
            validate_strings(snapshot)
            if snapshot.get("last_eid") is not None:
                model.require_id(snapshot["last_eid"])
                if snapshot["last_eid"] not in snapshot["applied_eids"]:
                    return None
            for eid in snapshot["applied_eids"]:
                model.require_id(eid)
            for oid, observation in snapshot["observations"].items():
                model.require_id(oid, "O")
                if observation["oid"] != oid or type(observation["dismissed"]) is not bool:
                    return None
                model._create_observation(
                    model.empty_projection(), {"oid": oid, "data": observation}
                )
            for cid, candidate in snapshot["candidates"].items():
                model.require_id(cid, "C")
                if (
                    candidate["cid"] != cid
                    or candidate["state"] not in model.STATES
                    or not isinstance(candidate["observations"], list)
                    or not isinstance(candidate["history"], list)
                    or not common.valid_name(candidate["skill"])
                    or type(candidate["handoff_eligible"]) is not bool
                ):
                    return None
                for oid in candidate["observations"]:
                    model.require_observation(snapshot, oid)
                for field in ("created_at", "updated_at", "state_since"):
                    timestamp(candidate[field])
                validate_text(candidate["hypothesis"]["title"], "仮説の見出し", 200)
                validate_text(
                    candidate["hypothesis"]["description"], "仮説の説明", 1000, multiline=True
                )
            for alias in snapshot["aliases"]:
                model.require_candidate(snapshot, alias)
            for eid, record in snapshot["records"].items():
                model.require_id(eid)
                if (
                    record["eid"] != eid
                    or eid not in snapshot["applied_eids"]
                    or record["type"] not in model.EVENT_TYPES
                    or record["actor"] != "human"
                    or record["v"] != 1
                    or not isinstance(record["data"], dict)
                ):
                    return None
                model.require_candidate(snapshot, record["cid"])
                timestamp(record["ts"])
                if record["type"] in SCENARIO_EVENTS:
                    validate_files(record["data"].get("files"))
        except (KeyError, TypeError, ValidationError):
            return None
        snapshot["applied_eids"] = set(snapshot["applied_eids"])
        return snapshot

    def _ledger_size(self) -> int:
        try:
            with open_regular(self.directory / "candidates.jsonl") as stream:
                return os.fstat(stream.fileno()).st_size
        except FileNotFoundError:
            return 0

    def _catch_up(self, snapshot: dict | None) -> dict:
        size = self._ledger_size()
        if snapshot is None or snapshot["ledger_offset"] > size:
            snapshot = model.empty_projection()
        try:
            with open_regular(self.directory / "candidates.jsonl") as stream:
                stream.seek(snapshot["ledger_offset"])
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    try:
                        snapshot = model.apply_event(snapshot, json.loads(line))
                    except (ValueError, UnicodeError, TypeError, KeyError, AttributeError):
                        pass
                    except ValidationError:
                        pass
                    snapshot["ledger_offset"] = stream.tell()
        except FileNotFoundError:
            pass
        return snapshot

    def _atomic_json(self, filename: str, payload: dict) -> None:
        target = self.directory / filename
        if target.is_symlink():
            raise ValidationError("写像の symlink には書き込めません")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{filename}-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                serialized = (
                    {**payload, "applied_eids": sorted(payload["applied_eids"])}
                    if "applied_eids" in payload
                    else payload
                )
                json.dump(serialized, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self) -> dict:
        if not self.directory.exists():
            return model.empty_projection()
        validate_directory(self.directory, self.root)
        return self._catch_up(self._projection())

    def ingest_state(self) -> dict:
        state = self._read_json("ingest-state.json")
        if not isinstance(state, dict) or type(state.get("failure_offset")) is not int:
            return {"failure_offset": 0}
        return state

    def save_ingest_state(self, payload: dict) -> None:
        self._atomic_json("ingest-state.json", payload)

    def _repair_newline(self) -> None:
        try:
            with open_regular(
                self.directory / "candidates.jsonl", os.O_RDWR | os.O_APPEND
            ) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size and os.pread(stream.fileno(), 1, size - 1) != b"\n":
                    os.write(stream.fileno(), b"\n")
        except FileNotFoundError:
            pass

    def current_locked(self) -> dict:
        self._repair_newline()
        return self._catch_up(self._projection())

    def append_locked(self, snapshot: dict, records: list[dict]) -> dict:
        checked = copy.deepcopy(snapshot)
        for record in records:
            checked = model.apply_event(
                checked, record, orchestra=self.orchestra, own_project=self.own_project
            )
        if records:
            with open_regular(
                self.directory / "candidates.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT
            ) as stream:
                os.fchmod(stream.fileno(), 0o600)
                payload = "".join(
                    json.dumps(record, ensure_ascii=False) + "\n" for record in records
                ).encode("utf-8")
                while payload:
                    count = os.write(stream.fileno(), payload)
                    if count <= 0:
                        raise OSError("台帳に追記できません")
                    payload = payload[count:]
                os.fsync(stream.fileno())
        checked["ledger_offset"] = self._ledger_size()
        self._atomic_json("candidates-state.json", checked)
        return checked

    def write(self, factory: Callable[[dict], list[dict]]) -> dict:
        with self.locked():
            snapshot = self.current_locked()
            return self.append_locked(snapshot, factory(snapshot))
