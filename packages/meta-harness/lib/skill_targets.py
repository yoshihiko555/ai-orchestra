"""Resolve safe facet source closures for ``skill:<slug>`` targets."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class SkillTargetError(ValueError):
    """A skill target cannot be resolved without widening its authority."""


@dataclass(frozen=True)
class SkillTargetResolution:
    target: str
    composition_path: str
    closure_paths: frozenset[str]
    private_paths: frozenset[str]
    closure_hash: str


@dataclass(frozen=True)
class SkillImpactContext:
    impacted_targets: tuple[str, ...]
    input_hash: str


@contextmanager
def materialized_baseline(
    project_root: Path,
    source_ref: str,
) -> Iterator[Path]:
    """Materialize tracked ``facets/`` from one immutable git ref."""
    project_root = project_root.resolve()
    try:
        completed = subprocess.run(
            ["git", "archive", "--format=tar", source_ref, "facets"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillTargetError(f"could not materialize source ref {source_ref!r}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SkillTargetError(f"could not materialize source ref {source_ref!r}: {stderr[:500]}")
    with tempfile.TemporaryDirectory(prefix="meta-harness-skill-baseline-") as raw_dir:
        baseline = Path(raw_dir)
        _extract_facets_archive(completed.stdout, baseline)
        yield baseline


def resolve_skill_target(root: Path, target: str) -> SkillTargetResolution:
    """Resolve a composition and every referenced facet source under ``root``."""
    root = root.resolve()
    slug = _target_skill_slug(target)
    composition_rel = f"facets/compositions/skills/{slug}.yaml"
    composition_path = _safe_regular_file(root, composition_rel)
    composition = _load_composition(composition_path)
    if composition.get("name") != slug:
        raise SkillTargetError(
            f"composition name mismatch: expected {slug!r}, got {composition.get('name')!r}"
        )

    closure = _composition_paths(root, composition_rel, composition)
    private = _private_overlay_paths(slug, composition_rel, closure)
    hasher = hashlib.sha256()
    for relative in sorted(closure):
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(_safe_regular_file(root, relative).read_bytes())
        hasher.update(b"\0")
    return SkillTargetResolution(
        target=target,
        composition_path=composition_rel,
        closure_paths=frozenset(closure),
        private_paths=private,
        closure_hash=hasher.hexdigest(),
    )


def allowed_overlay_paths(root: Path, target: str, config: dict) -> SkillTargetResolution:
    """Return the target resolution used by the configured overlay authority."""
    return resolve_skill_target(root, target)


def overlay_allowlist(resolution: SkillTargetResolution, config: dict) -> frozenset[str]:
    """Use the full closure when regression protection is enabled, otherwise private paths."""
    if bool((config.get("regression") or {}).get("enabled", True)):
        return resolution.closure_paths
    return resolution.private_paths


def resolve_skill_impacts(
    root: Path,
    overlay_paths: set[str] | frozenset[str] | list[str],
    *,
    candidate_target: str,
) -> SkillImpactContext:
    """Resolve skills whose baseline closure intersects the candidate overlay."""
    root = root.resolve()
    composition_dir = root / "facets" / "compositions" / "skills"
    if composition_dir.is_symlink():
        raise SkillTargetError(f"skill composition directory is missing: {composition_dir}")
    if composition_dir.exists() and not composition_dir.is_dir():
        raise SkillTargetError(f"skill composition directory is missing: {composition_dir}")
    # Non-facets-based deployments (no facets/compositions/skills at all) have no skill
    # targets to resolve; treat as zero impact instead of failing evaluate/promote. Irregular
    # cases (symlink, or a regular file at this path) are still rejected above. `glob()` on a
    # missing directory below returns an empty iterator (no OSError), so `resolutions` stays
    # empty and this naturally yields a deterministic empty-input SkillImpactContext.

    resolutions: list[SkillTargetResolution] = []
    for path in sorted(composition_dir.glob("*.yaml"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise SkillTargetError(f"skill composition must be a regular file: {path}")
        slug = path.stem
        if not _SLUG_RE.fullmatch(slug):
            raise SkillTargetError(f"invalid skill composition slug: {slug!r}")
        resolutions.append(resolve_skill_target(root, f"skill:{slug}"))

    hasher = hashlib.sha256()
    impacted: list[str] = []
    changed = frozenset(str(path) for path in overlay_paths if str(path).startswith("facets/"))
    for resolution in resolutions:
        hasher.update(resolution.target.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(resolution.closure_hash.encode("ascii"))
        hasher.update(b"\0")
        for relative in sorted(resolution.closure_paths):
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\0")
        if changed.intersection(resolution.closure_paths):
            impacted.append(resolution.target)

    if candidate_target.startswith("skill:"):
        impacted = [target for target in impacted if target != candidate_target]
    return SkillImpactContext(
        impacted_targets=tuple(sorted(impacted)),
        input_hash=hasher.hexdigest(),
    )


def _target_skill_slug(target: str) -> str:
    if not target.startswith("skill:"):
        raise SkillTargetError(f"not a skill target: {target!r}")
    slug = target.split(":", 1)[1]
    if not _SLUG_RE.fullmatch(slug):
        raise SkillTargetError(f"invalid skill target slug: {slug!r}")
    return slug


def _load_composition(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillTargetError(f"could not load composition {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillTargetError(f"composition must be an object: {path}")
    return value


def _composition_paths(root: Path, composition_rel: str, composition: dict[str, Any]) -> set[str]:
    paths = {composition_rel}
    for key, kind in (
        ("policies", "policies"),
        ("output_contracts", "output-contracts"),
        ("knowledge", "knowledge"),
    ):
        for name in _string_list(composition, key):
            _validate_slug_reference(key, name)
            paths.add(f"facets/{kind}/{name}.md")

    instruction = composition.get("instruction", "")
    if not isinstance(instruction, str):
        raise SkillTargetError("composition.instruction must be a string")
    stripped = instruction.strip()
    if stripped and "\n" not in instruction and len(instruction) <= 100:
        _validate_slug_reference("instruction", stripped)
        paths.add(f"facets/instructions/{stripped}.md")

    for script in _string_list(composition, "scripts"):
        if (
            not _SCRIPT_RE.fullmatch(script)
            or Path(script).is_absolute()
            or ".." in Path(script).parts
        ):
            raise SkillTargetError(f"unsafe composition script reference: {script!r}")
        paths.add(f"facets/scripts/{script}")

    for relative in paths:
        _safe_regular_file(root, relative)
    return paths


def _string_list(composition: dict[str, Any], key: str) -> list[str]:
    value = composition.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillTargetError(f"composition.{key} must be a string array")
    return [item.strip() for item in value]


def _validate_slug_reference(key: str, value: str) -> None:
    if not _SLUG_RE.fullmatch(value):
        raise SkillTargetError(f"unsafe composition {key} reference: {value!r}")


def _private_overlay_paths(slug: str, composition_rel: str, closure: set[str]) -> frozenset[str]:
    """Return the PR1 category allowlist; shared facet categories stay closed."""
    instruction_rel = f"facets/instructions/{slug}.md"
    return frozenset(
        relative
        for relative in closure
        if relative == composition_rel
        or relative == instruction_rel
        or relative.startswith("facets/scripts/")
    )


def _safe_regular_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise SkillTargetError(f"unsafe facet path: {relative!r}")
    candidate = root / rel
    current = root
    for part in rel.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise SkillTargetError(f"facet source is missing: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise SkillTargetError(f"facet source symlink rejected: {current}")
    resolved = candidate.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise SkillTargetError(f"facet source escapes repository: {relative}")
    if not resolved.is_file():
        raise SkillTargetError(f"facet source must be a regular file: {relative}")
    return resolved


def _extract_facets_archive(payload: bytes, destination: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or relative.parts[0] != "facets"
                ):
                    raise SkillTargetError(f"unsafe member in facets archive: {member.name!r}")
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    raise SkillTargetError(
                        f"symlink is not allowed in facets archive: {member.name}"
                    )
                if not member.isfile():
                    raise SkillTargetError(f"unsupported member in facets archive: {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise SkillTargetError(
                        f"could not read member in facets archive: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    while chunk := source.read(1 << 20):
                        output.write(chunk)
    except (OSError, tarfile.TarError) as exc:
        raise SkillTargetError("could not extract facets archive") from exc
