"""facet composition から SKILL.md / rule を生成するビルダー。"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lib.md_format import format_markdown_files

FACET_MANIFEST_NAME = "facet-manifest.json"
FACET_CACHE_DIR = ".cache"


@dataclass
class FacetBuilder:
    """facet composition から SKILL.md を生成するビルダー。"""

    orchestra_dir: Path
    project_facets_dir: Path | None = None  # .claude/facets/ in the target project
    manifest_compositions: dict[str, str] | None = (
        None  # {composition_name: package_name} from ALL packages
    )
    installed_packages: list[str] | None = None  # currently installed packages

    def load_composition(self, path: Path) -> dict[str, Any]:
        """composition YAML をロードして最低限の検証を行う。"""
        if not path.exists():
            print(f"エラー: composition が見つかりません: {path}", file=sys.stderr)
            sys.exit(1)

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"エラー: composition の読み込みに失敗しました: {path} ({e})", file=sys.stderr)
            sys.exit(1)

        try:
            composition = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            print(f"エラー: YAML の解析に失敗しました: {path} ({e})", file=sys.stderr)
            sys.exit(1)

        if not isinstance(composition, dict):
            print(f"エラー: composition の形式が不正です: {path}", file=sys.stderr)
            sys.exit(1)

        name = composition.get("name")
        if not isinstance(name, str) or not name.strip():
            print(f"エラー: composition.name が不正です: {path}", file=sys.stderr)
            sys.exit(1)

        comp_type = composition.get("type", "skill")
        if not isinstance(comp_type, str) or not comp_type.strip():
            print(f"エラー: composition.type が不正です: {path}", file=sys.stderr)
            sys.exit(1)
        comp_type = comp_type.strip()
        composition["type"] = comp_type

        frontmatter = composition.get("frontmatter")
        if comp_type == "skill":
            if not isinstance(frontmatter, dict) or not frontmatter:
                print(f"エラー: composition.frontmatter が不正です: {path}", file=sys.stderr)
                sys.exit(1)

        policies = composition.get("policies")
        if policies is None:
            policies = []
            composition["policies"] = policies
        if not isinstance(policies, list):
            print(f"エラー: composition.policies が不正です: {path}", file=sys.stderr)
            sys.exit(1)
        for policy in policies:
            if not isinstance(policy, str) or not policy.strip():
                print(f"エラー: composition.policies の要素が不正です: {path}", file=sys.stderr)
                sys.exit(1)

        output_contracts = composition.get("output_contracts")
        if output_contracts is not None:
            if not isinstance(output_contracts, list):
                print(f"エラー: composition.output_contracts が不正です: {path}", file=sys.stderr)
                sys.exit(1)
            for contract in output_contracts:
                if not isinstance(contract, str) or not contract.strip():
                    print(
                        f"エラー: composition.output_contracts の要素が不正です: {path}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        instruction = composition.get("instruction")
        if comp_type == "skill":
            if not isinstance(instruction, str):
                print(f"エラー: composition.instruction が不正です: {path}", file=sys.stderr)
                sys.exit(1)
        elif instruction is not None and not isinstance(instruction, str):
            print(f"エラー: composition.instruction が不正です: {path}", file=sys.stderr)
            sys.exit(1)

        knowledge = composition.get("knowledge")
        if knowledge is None:
            composition["knowledge"] = []
        else:
            if not isinstance(knowledge, list):
                print(f"エラー: composition.knowledge が不正です: {path}", file=sys.stderr)
                sys.exit(1)
            for item in knowledge:
                if not isinstance(item, str) or not item.strip():
                    print(
                        f"エラー: composition.knowledge の要素が不正です: {path}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        scripts = composition.get("scripts")
        if scripts is None:
            composition["scripts"] = []
        else:
            if not isinstance(scripts, list):
                print(f"エラー: composition.scripts が不正です: {path}", file=sys.stderr)
                sys.exit(1)
            for item in scripts:
                if not isinstance(item, str) or not item.strip():
                    print(
                        f"エラー: composition.scripts の要素が不正です: {path}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        return composition

    def resolve_facet(self, kind: str, name: str) -> str:
        """facet ファイル本文を読み込む。プロジェクトローカル → orchestra の順で解決。"""
        if self.project_facets_dir:
            local_path = self.project_facets_dir / kind / f"{name}.md"
            if local_path.exists():
                try:
                    return local_path.read_text(encoding="utf-8").strip()
                except OSError as e:
                    print(
                        f"エラー: facet の読み込みに失敗しました: {local_path} ({e})",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        facet_path = self.orchestra_dir / "facets" / kind / f"{name}.md"
        if not facet_path.exists():
            print(f"エラー: facet ファイルが見つかりません: {facet_path}", file=sys.stderr)
            sys.exit(1)
        try:
            return facet_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"エラー: facet の読み込みに失敗しました: {facet_path} ({e})", file=sys.stderr)
            sys.exit(1)

    def resolve_instruction(self, instruction: str) -> str:
        """instruction を解決する。"""
        stripped = instruction.strip()
        if not stripped:
            return ""

        if "\n" in instruction or len(instruction) > 100:
            return stripped

        return self.resolve_facet("instructions", stripped)

    def resolve_knowledge(self, name: str) -> Path:
        """knowledge ファイルのパスを解決する。プロジェクトローカル → orchestra の順で解決。"""
        if self.project_facets_dir:
            local_path = self.project_facets_dir / "knowledge" / f"{name}.md"
            if local_path.exists():
                return local_path

        facet_path = self.orchestra_dir / "facets" / "knowledge" / f"{name}.md"
        if not facet_path.exists():
            print(f"エラー: knowledge ファイルが見つかりません: {facet_path}", file=sys.stderr)
            sys.exit(1)
        return facet_path

    def resolve_script(self, name: str) -> Path:
        """script ファイルのパスを解決する。プロジェクトローカル → orchestra の順で解決。"""
        if self.project_facets_dir:
            local_path = self.project_facets_dir / "scripts" / name
            if local_path.exists():
                return local_path

        facet_path = self.orchestra_dir / "facets" / "scripts" / name
        if not facet_path.exists():
            print(f"エラー: script ファイルが見つかりません: {facet_path}", file=sys.stderr)
            sys.exit(1)
        return facet_path

    def build_skill_md(self, composition: dict[str, Any]) -> str:
        """composition から SKILL.md 本文を組み立てる。"""
        frontmatter = composition["frontmatter"]
        frontmatter_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        frontmatter_block = f"---\n{frontmatter_yaml}\n---"

        sections: list[str] = []
        for policy_name in composition["policies"]:
            sections.append(self.resolve_facet("policies", policy_name))

        output_contracts = composition.get("output_contracts", [])
        for contract_name in output_contracts:
            sections.append(self.resolve_facet("output-contracts", contract_name))

        instruction = self.resolve_instruction(composition["instruction"])
        if instruction:
            sections.append(instruction)

        knowledge = composition.get("knowledge", [])
        if knowledge:
            lines = ["## Additional resources", ""]
            for kname in knowledge:
                lines.append(
                    f"- For {kname} details, see [references/{kname}.md](references/{kname}.md)"
                )
            sections.append("\n".join(lines))

        if not sections:
            return f"{frontmatter_block}\n"

        return f"{frontmatter_block}\n\n" + "\n\n---\n\n".join(sections) + "\n"

    def build_rule_md(self, composition: dict[str, Any]) -> str:
        """composition から rule 本文を組み立てる。"""
        sections: list[str] = []
        for policy_name in composition["policies"]:
            sections.append(self.resolve_facet("policies", policy_name))

        output_contracts = composition.get("output_contracts", [])
        for contract_name in output_contracts:
            sections.append(self.resolve_facet("output-contracts", contract_name))

        instruction = self.resolve_instruction(composition.get("instruction", ""))
        if instruction:
            sections.append(instruction)

        if not sections:
            return ""

        return "\n\n---\n\n".join(sections) + "\n"

    def _build_output_path(
        self,
        name: str,
        target: str,
        project_dir: Path,
        comp_type: str = "skill",
    ) -> Path:
        """target に応じた出力先パスを返す。"""
        if comp_type == "rule":
            if target == "claude":
                return project_dir / ".claude" / "rules" / f"{name}.md"
            return project_dir / ".codex" / "rules" / f"{name}.md"

        if target == "claude":
            return project_dir / ".claude" / "skills" / name / "SKILL.md"
        # 非 claude ターゲットのスキルは .agents/skills/ に出力する。
        # この配置は Codex CLI と Antigravity CLI(agy) の両方が自動検出する共有ディレクトリ。
        return project_dir / ".agents" / "skills" / name / "SKILL.md"

    def _find_composition(self, name: str) -> Path:
        """name から composition YAML のパスを解決する。サブディレクトリも再帰検索。"""
        if self.project_facets_dir:
            local_dir = self.project_facets_dir / "compositions"
            if local_dir.is_dir():
                candidates = sorted(local_dir.rglob(f"{name}.yaml"))
                if len(candidates) > 1:
                    print(
                        f"エラー: composition '{name}' が複数見つかりました: {candidates}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if candidates:
                    return candidates[0]

        orch_dir = self.orchestra_dir / "facets" / "compositions"
        candidates = sorted(orch_dir.rglob(f"{name}.yaml"))
        if len(candidates) > 1:
            print(
                f"エラー: composition '{name}' が複数見つかりました: {candidates}",
                file=sys.stderr,
            )
            sys.exit(1)
        if candidates:
            return candidates[0]

        print(
            f"エラー: composition '{name}' が見つかりません"
            f" ({orch_dir}/{{skills,rules}}/{name}.yaml を検索)",
            file=sys.stderr,
        )
        sys.exit(1)

    def build_one(self, name: str, target: str, project_dir: Path) -> Path | None:
        """単一 composition をビルドして出力する。"""
        generated: list[Path] = []
        result = self._build_one(name, target, project_dir, generated)
        format_markdown_files(generated, project_dir)
        return result

    def _build_one(
        self,
        name: str,
        target: str,
        project_dir: Path,
        generated: list[Path],
    ) -> Path | None:
        """build_one の本体。生成した Markdown のパスを generated に積む。

        整形は呼び出し側でまとめて行う。composition ごとに prettier を起動すると
        build_all で数十プロセスになるため。
        """
        composition_path = self._find_composition(name)

        composition = self.load_composition(composition_path)

        # 非 claude ターゲットにはルールを配布しない（ルール同期は廃止）。
        # Claude のルールは振る舞い指示、Codex/agy のルールは別思想（execpolicy 等の
        # コマンドポリシー）のため、Markdown ルールの外部 CLI への同期は行わない。
        if target != "claude" and composition.get("type", "skill") == "rule":
            # targeted build でも旧 .codex/rules/<name>.md 生成物を掃除する。
            self._cleanup_legacy_codex_rule(project_dir, composition["name"])
            return None

        # Package filtering:
        # - manifest_compositions is None → build all (no filtering)
        # - name in manifest_compositions → package-owned → build only if package is installed
        # - name not in manifest_compositions → global → always build
        if self.manifest_compositions is not None and name in self.manifest_compositions:
            owning_pkg = self.manifest_compositions[name]
            installed = set(self.installed_packages or [])
            if owning_pkg not in installed:
                output_name = composition["name"]
                comp_type = composition.get("type", "skill")
                old_path = self._build_output_path(output_name, target, project_dir, comp_type)
                if old_path.exists():
                    old_path.unlink()
                    if (
                        comp_type == "skill"
                        and old_path.parent.exists()
                        and not any(old_path.parent.iterdir())
                    ):
                        old_path.parent.rmdir()
                    relative = old_path.relative_to(project_dir)
                    print(
                        f"[facet] removed {output_name} ({owning_pkg} not installed) <- {relative}"
                    )
                return None

        output_name = composition["name"]
        comp_type = composition.get("type", "skill")
        if comp_type == "rule":
            content = self.build_rule_md(composition)
        else:
            content = self.build_skill_md(composition)
        output_path = self._build_output_path(output_name, target, project_dir, comp_type)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        generated.append(output_path)

        if comp_type != "rule":
            skill_dir = output_path.parent

            # Clear existing references/ and scripts/ to remove stale files
            refs_dir = skill_dir / "references"
            if refs_dir.is_dir():
                shutil.rmtree(refs_dir)
            scripts_dir_path = skill_dir / "scripts"
            if scripts_dir_path.is_dir():
                shutil.rmtree(scripts_dir_path)

            for kname in composition.get("knowledge", []):
                src = self.resolve_knowledge(kname)
                dst = skill_dir / "references" / f"{kname}.md"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                generated.append(dst)

            seen_script_basenames: set[str] = set()
            for sname in composition.get("scripts", []):
                src = self.resolve_script(sname)
                # Flatten to basename so subdirectory-organized sources land in scripts/ flat
                # (matches Claude Code's expected `.claude/skills/<name>/scripts/<file>` layout)
                basename = Path(sname).name
                # Detect basename collisions to prevent silent overwrites when paths like
                # `a/run.py` and `b/run.py` flatten to the same destination
                if basename in seen_script_basenames:
                    print(
                        f"エラー: composition.scripts に basename 重複があります: {basename}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                seen_script_basenames.add(basename)
                dst = skill_dir / "scripts" / basename
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        # スキル出力先が .agents/skills/ に移行したため、targeted build でも
        # 旧 .codex/skills/<name> に残った同名スキルを掃除する。
        if target != "claude" and comp_type != "rule":
            self._cleanup_legacy_codex_skills(project_dir, {output_name})

        relative = output_path.relative_to(project_dir)
        print(f"[facet] built {output_name} -> {relative}")
        return output_path

    def _load_manifest(self, target: str, project_dir: Path) -> dict[str, list[str]]:
        """前回ビルド時のマニフェストを読み込む。"""
        import json

        manifest_path = self._manifest_path(target, project_dir)
        if not manifest_path.exists():
            return {"skills": [], "rules": []}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"skills": [], "rules": []}

    def _save_manifest(
        self, target: str, project_dir: Path, skills: list[str], rules: list[str]
    ) -> None:
        """今回ビルドしたスキル/ルール名をマニフェストに記録する。"""
        import json

        manifest_path = self._manifest_path(target, project_dir)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"skills": sorted(skills), "rules": sorted(rules)}
        manifest_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _manifest_path(self, target: str, project_dir: Path) -> Path:
        """マニフェストファイルのパスを返す。"""
        target_dir = ".claude" if target == "claude" else ".codex"
        return project_dir / target_dir / FACET_CACHE_DIR / FACET_MANIFEST_NAME

    def _cleanup_orphans(
        self,
        target: str,
        project_dir: Path,
        built_skills: set[str],
        built_rules: set[str],
    ) -> None:
        """前回マニフェストに存在し今回ビルドされなかった生成物を削除する。"""
        prev = self._load_manifest(target, project_dir)

        for name in prev.get("skills", []):
            if name not in built_skills:
                orphan = self._build_output_path(name, target, project_dir, "skill")
                skill_dir = orphan.parent
                if skill_dir.exists():
                    if orphan.exists():
                        orphan.unlink()
                        relative = orphan.relative_to(project_dir)
                        print(f"[facet] cleanup: removed orphan skill {name} <- {relative}")
                    refs_dir = skill_dir / "references"
                    if refs_dir.is_dir():
                        shutil.rmtree(refs_dir)
                    scripts_dir = skill_dir / "scripts"
                    if scripts_dir.is_dir():
                        shutil.rmtree(scripts_dir)
                    if not any(skill_dir.iterdir()):
                        skill_dir.rmdir()

        for name in prev.get("rules", []):
            if name not in built_rules:
                orphan = self._build_output_path(name, target, project_dir, "rule")
                if orphan.exists():
                    orphan.unlink()
                    relative = orphan.relative_to(project_dir)
                    print(f"[facet] cleanup: removed orphan rule {name} <- {relative}")

    def _cleanup_legacy_codex_skills(self, project_dir: Path, facet_skill_names: set[str]) -> None:
        """旧 .codex/skills/<name> に残った facet スキルを掃除する（移行後は no-op）。

        スキル出力先が .codex/skills/ から .agents/skills/ へ移行したため、
        facet が所有するスキル名（今回ビルド分 + 前回マニフェスト分）のみを対象に
        旧ディレクトリを削除する。union により、未インストール等で今回 skip された
        スキルも前回マニフェスト経由で対象に含まれる。template 配布の context-loader 等
        （manifest 非記録）や手書きファイルは対象外。symlink は辿らずスキップする。
        """
        legacy_root = project_dir / ".codex" / "skills"
        if not legacy_root.is_dir() or legacy_root.is_symlink():
            return
        for name in sorted(facet_skill_names):
            legacy_dir = legacy_root / name
            if not legacy_dir.is_dir() or legacy_dir.is_symlink():
                continue
            try:
                shutil.rmtree(legacy_dir)
                print(f"[facet] migrate: removed legacy .codex/skills/{name}")
            except OSError as e:
                print(
                    f"[facet] migrate: failed to remove legacy .codex/skills/{name}: {e}",
                    file=sys.stderr,
                )

    def _cleanup_legacy_codex_rule(self, project_dir: Path, name: str) -> None:
        """旧 .codex/rules/<name>.md（生成物）を 1 件削除する。symlink はスキップ。"""
        legacy_file = project_dir / ".codex" / "rules" / f"{name}.md"
        if legacy_file.is_symlink() or not legacy_file.is_file():
            return
        try:
            legacy_file.unlink()
            print(f"[facet] migrate: removed legacy .codex/rules/{name}.md")
        except OSError as e:
            print(
                f"[facet] migrate: failed to remove legacy .codex/rules/{name}.md: {e}",
                file=sys.stderr,
            )

    def _cleanup_legacy_codex_rules(self, project_dir: Path) -> None:
        """旧 .codex/rules/*.md（生成物）を削除する。ルール同期廃止に伴う後始末。

        対象は Markdown 生成物（*.md）のみ。Codex の execpolicy 用 *.rules や
        その他のファイルは削除しない。symlink は辿らずスキップする。
        """
        legacy_root = project_dir / ".codex" / "rules"
        if not legacy_root.is_dir() or legacy_root.is_symlink():
            return
        for md_file in sorted(legacy_root.glob("*.md")):
            if md_file.is_symlink() or not md_file.is_file():
                continue
            self._cleanup_legacy_codex_rule(project_dir, md_file.stem)

    def build_all(self, target: str, project_dir: Path) -> list[Path]:
        """全 composition をビルドして出力する。"""
        generated: list[Path] = []
        try:
            return self._build_all(target, project_dir, generated)
        finally:
            # 生成物が途中までしか無くても、書けた分は整形して確定させる。
            format_markdown_files(generated, project_dir)

    def _build_all(self, target: str, project_dir: Path, generated: list[Path]) -> list[Path]:
        """build_all の本体。整形は 1 回にまとめるため呼び出し側で行う。"""
        output_paths: list[Path] = []
        seen_names: set[str] = set()
        found_yaml_files = 0
        built_skills: set[str] = set()
        built_rules: set[str] = set()

        if self.project_facets_dir:
            local_compositions_dir = self.project_facets_dir / "compositions"
            if local_compositions_dir.is_dir():
                for composition_path in sorted(local_compositions_dir.rglob("*.yaml")):
                    found_yaml_files += 1
                    stem = composition_path.stem
                    if stem in seen_names:
                        print(
                            f"[warn] duplicate composition '{stem}' in local: {composition_path}",
                            file=sys.stderr,
                        )
                        continue
                    seen_names.add(stem)
                    result = self._build_one(stem, target, project_dir, generated)
                    if result:
                        output_paths.append(result)
                        self._track_built(composition_path, built_skills, built_rules)

        compositions_dir = self.orchestra_dir / "facets" / "compositions"
        if compositions_dir.is_dir():
            for composition_path in sorted(compositions_dir.rglob("*.yaml")):
                found_yaml_files += 1
                stem = composition_path.stem
                if stem in seen_names:
                    continue
                seen_names.add(stem)
                result = self._build_one(stem, target, project_dir, generated)
                if result:
                    output_paths.append(result)
                    self._track_built(composition_path, built_skills, built_rules)

        if found_yaml_files == 0:
            print("エラー: compositions が見つかりません", file=sys.stderr)
            sys.exit(1)

        self._cleanup_orphans(target, project_dir, built_skills, built_rules)
        if target != "claude":
            prev = self._load_manifest(target, project_dir)
            legacy_names = built_skills | set(prev.get("skills", []))
            self._cleanup_legacy_codex_skills(project_dir, legacy_names)
            # ルール同期廃止: 旧 .codex/rules/*.md 生成物を掃除する
            self._cleanup_legacy_codex_rules(project_dir)
        self._save_manifest(target, project_dir, list(built_skills), list(built_rules))

        return output_paths

    def _track_built(
        self,
        composition_path: Path,
        built_skills: set[str],
        built_rules: set[str],
    ) -> None:
        """ビルドされた composition の名前を種別ごとに記録する。"""
        try:
            comp = yaml.safe_load(composition_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            return
        if not isinstance(comp, dict):
            return
        name = comp.get("name", composition_path.stem)
        comp_type = comp.get("type", "skill")
        if comp_type == "rule":
            built_rules.add(name)
        else:
            built_skills.add(name)

    def extract_one(self, name: str, target: str, project_dir: Path) -> Path | None:
        """生成済みファイルから instruction を抽出してソースに書き戻す。"""
        composition_path = self._find_composition(name)

        composition = self.load_composition(composition_path)
        comp_type = composition.get("type", "skill")
        output_name = composition["name"]

        # 非 claude ターゲットにルールは配布しないため、書き戻し対象も存在しない
        if target != "claude" and comp_type == "rule":
            return None

        generated_path = self._build_output_path(output_name, target, project_dir, comp_type)
        if not generated_path.exists():
            print(f"エラー: 生成済みファイルが見つかりません: {generated_path}", file=sys.stderr)
            return None

        content = generated_path.read_text(encoding="utf-8")

        if comp_type == "skill":
            if content.startswith("---"):
                end_idx = content.index("---", 3)
                content = content[end_idx + 3 :].lstrip("\n")

        sections = content.split("\n\n---\n\n")

        num_policies = len(composition.get("policies", []))
        num_contracts = len(composition.get("output_contracts", []))
        skip = num_policies + num_contracts

        if skip >= len(sections):
            print(f"エラー: instruction セクションが見つかりません: {name}", file=sys.stderr)
            return None

        instruction_content = "\n\n---\n\n".join(sections[skip:])

        # Remove auto-generated Additional resources section
        marker = "\n\n---\n\n## Additional resources"
        if marker in instruction_content:
            instruction_content = instruction_content[: instruction_content.index(marker)]

        instruction_path: Path | None = None
        if self.project_facets_dir:
            local_instr = self.project_facets_dir / "instructions" / f"{name}.md"
            if local_instr.exists():
                instruction_path = local_instr
        if instruction_path is None:
            instruction_path = self.orchestra_dir / "facets" / "instructions" / f"{name}.md"

        instruction_path.parent.mkdir(parents=True, exist_ok=True)
        instruction_path.write_text(instruction_content, encoding="utf-8")

        print(
            f"[facet] extracted {output_name} -> {instruction_path.relative_to(instruction_path.parent.parent.parent)}"
        )
        return instruction_path

    def extract_all(self, target: str, project_dir: Path) -> list[Path]:
        """全 composition の instruction を抽出する。"""
        paths: list[Path] = []
        seen: set[str] = set()

        if self.project_facets_dir:
            local_dir = self.project_facets_dir / "compositions"
            if local_dir.is_dir():
                for p in sorted(local_dir.rglob("*.yaml")):
                    stem = p.stem
                    if stem in seen:
                        print(
                            f"[warn] duplicate composition '{stem}' in local: {p}",
                            file=sys.stderr,
                        )
                        continue
                    seen.add(stem)
                    result = self.extract_one(stem, target, project_dir)
                    if result:
                        paths.append(result)

        compositions_dir = self.orchestra_dir / "facets" / "compositions"
        if compositions_dir.is_dir():
            for p in sorted(compositions_dir.rglob("*.yaml")):
                stem = p.stem
                if stem in seen:
                    continue
                seen.add(stem)
                result = self.extract_one(stem, target, project_dir)
                if result:
                    paths.append(result)

        return paths
