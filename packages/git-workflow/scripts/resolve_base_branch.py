"""PR の base branch を解決する。

解決優先順位:
    1. ``--base <branch>`` 明示指定
    2. 環境変数 ``AI_ORCHESTRA_BASE_BRANCH``
    3. 自動推定: 候補ブランチのうち現在の HEAD の親に最も近いもの
    4. Fallback: ``main``

出力は ``origin/`` プレフィックスを剥がした bare branch 名を stdout に一行で返す。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ENV_VAR = "AI_ORCHESTRA_BASE_BRANCH"
# 同距離の候補が複数ある場合は先頭が優先される。
# 多段ブランチ運用（main + stage 等）では feature PR の target を stage 側にしたいため、
# staging / stage / develop を main / master より先に並べる。
CANDIDATES: tuple[str, ...] = ("staging", "stage", "develop", "main", "master")
FALLBACK = "main"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _ref_exists(ref: str, cwd: Path) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", ref], cwd).returncode == 0


def _current_branch(cwd: Path) -> str:
    result = _run_git(["branch", "--show-current"], cwd)
    return result.stdout.strip() if result.returncode == 0 else ""


def _strip_origin(ref: str) -> str:
    prefix = "origin/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def _distance_to_tip(candidate: str, cwd: Path) -> int | None:
    """merge-base(candidate, HEAD) から candidate の tip までのコミット数を返す。

    値が 0 のとき candidate は HEAD の祖先、または HEAD 自身（= 候補は HEAD の親）。
    値が大きいほど candidate が分岐点から先に進んでおり、親としては遠い。
    複数候補がある場合は最小値のものを親として選ぶ。
    同距離の候補が複数ある場合は ``CANDIDATES`` の先頭優先
    （``staging`` > ``stage`` > ``develop`` > ``main`` > ``master``）。
    多段ブランチ運用で main と stage が同一コミットを指すときに
    stage 側が選ばれるよう、staging 系が main 系より先になっている。
    merge-base が取れない場合は None。
    """
    mb = _run_git(["merge-base", candidate, "HEAD"], cwd)
    if mb.returncode != 0 or not mb.stdout.strip():
        return None
    base_sha = mb.stdout.strip()
    count = _run_git(["rev-list", "--count", f"{base_sha}..{candidate}"], cwd)
    if count.returncode != 0:
        return None
    try:
        return int(count.stdout.strip())
    except ValueError:
        return None


def _enumerate_refs(cwd: Path, current_branch: str) -> list[str]:
    """探索対象の ref を集める。remote を優先し、なければローカル。現在ブランチは除外。"""
    refs: list[str] = []
    for name in CANDIDATES:
        if name == current_branch:
            continue
        remote = f"origin/{name}"
        if _ref_exists(remote, cwd):
            refs.append(remote)
            continue
        if _ref_exists(name, cwd):
            refs.append(name)
    return refs


def _auto_detect(cwd: Path, current_branch: str) -> str | None:
    refs = _enumerate_refs(cwd, current_branch)
    best_ref: str | None = None
    best_dist: int | None = None
    for ref in refs:
        dist = _distance_to_tip(ref, cwd)
        if dist is None:
            continue
        if best_dist is None or dist < best_dist:
            best_ref = ref
            best_dist = dist
    return best_ref


def resolve(
    explicit: str | None = None,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    cwd = Path(cwd) if cwd else Path.cwd()
    env = env if env is not None else os.environ

    if explicit:
        return _strip_origin(explicit.strip())

    env_value = env.get(ENV_VAR, "").strip()
    if env_value:
        return _strip_origin(env_value)

    current = _current_branch(cwd)
    auto = _auto_detect(cwd, current)
    if auto:
        return _strip_origin(auto)

    return FALLBACK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the PR base branch for the current git repo.",
    )
    parser.add_argument(
        "--base",
        dest="explicit",
        default=None,
        help="Explicit base branch override (highest priority).",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory (default: current directory).",
    )
    args = parser.parse_args(argv)
    print(resolve(explicit=args.explicit, cwd=Path(args.cwd) if args.cwd else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
