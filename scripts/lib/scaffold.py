"""プロジェクト scaffold 管理。"""

from __future__ import annotations

import shutil
from pathlib import Path


def ensure_claude_scaffold(project_dir: Path, orchestra_path: Path) -> int:
    """`.claude` の最低限ディレクトリとテンプレートを不足時のみ作成する。"""
    created = 0
    claude_dirs = [
        project_dir / ".claude" / "docs",
        project_dir / ".claude" / "docs" / "research",
        project_dir / ".claude" / "docs" / "libraries",
        project_dir / ".claude" / "logs",
        project_dir / ".claude" / "logs" / "orchestration",
        project_dir / ".claude" / "state",
        project_dir / ".claude" / "checkpoints",
    ]

    for d in claude_dirs:
        if d.is_dir():
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            created += 1
        except OSError:
            continue

    template_root = orchestra_path / "templates" / "project"
    template_pairs: list[tuple[str, str]] = [
        ("docs/DESIGN.md", ".claude/docs/DESIGN.md"),
        ("docs/libraries/_TEMPLATE.md", ".claude/docs/libraries/_TEMPLATE.md"),
        ("docs/research/.gitkeep", ".claude/docs/research/.gitkeep"),
        ("logs/orchestration/.gitkeep", ".claude/logs/orchestration/.gitkeep"),
        ("state/.gitkeep", ".claude/state/.gitkeep"),
        ("checkpoints/.gitkeep", ".claude/checkpoints/.gitkeep"),
        ("Plans.md", ".claude/Plans.md"),
    ]

    for src_rel, dst_rel in template_pairs:
        src = template_root / src_rel
        dst = project_dir / dst_rel
        if not src.is_file() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            created += 1
        except OSError:
            continue

    return created
