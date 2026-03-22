# manifest-SSOT アーキテクチャ設計

**作成日**: 2026-03-22
**関連 Issue**: #20 — refactor: facets を正本としてパッケージ skills を廃止する

---

## 1. 概要

facet composition システム導入後の二重管理問題を解決するため、
`manifest.json` をパッケージ全体像の Single Source of Truth（SSOT）とし、
`composition.yaml` の `package` フィールドを廃止する。

### 解決する問題

| 問題 | 現状 | 解決後 |
|------|------|--------|
| skills の所有者定義が二重 | manifest.json + composition.yaml | manifest.json のみ |
| SKILL.md 実体が二重 | packages/*/skills/ + facet build 出力 | facet build 出力のみ |
| 管理形態の分断 | facets=skills/rules, manifest=hooks/config | manifest=全リソース一覧 |

---

## 2. 設計方針

### 2-1. manifest.json が SSOT

`manifest.json` を見ればパッケージの全容（skills, rules, hooks, config, agents）がわかる状態にする。

### 2-2. composition.yaml はビルドレシピ

composition は「SKILL.md をどう組み立てるか」のみを定義する。
所有者情報（package フィールド）は持たない。

### 2-3. ビルド対象の決定ロジック

```
全 composition をスキャン
├─ manifest に参照あり → パッケージがインストール済みならビルド
└─ manifest に参照なし → 常にビルド（グローバル）
```

---

## 3. データモデル変更

### 3-1. manifest.json（変更）

**Before:**
```json
{
  "name": "core",
  "version": "0.4.0",
  "skills": ["skills/preflight", "skills/startproject", "skills/design", "skills/task-state", "skills/checkpointing"],
  "rules": ["rules/config-loading.md", "rules/coding-principles.md", "rules/skill-review-policy.md", "rules/task-memory-usage.md", "rules/context-sharing.md"],
  "hooks": { ... },
  "config": ["config/task-memory.yaml"]
}
```

**After:**
```json
{
  "name": "core",
  "version": "0.5.0",
  "skills": ["preflight", "startproject", "design", "task-state", "checkpointing"],
  "rules": ["config-loading", "coding-principles", "skill-review-policy", "task-memory-usage", "context-sharing"],
  "hooks": { ... },
  "config": ["config/task-memory.yaml"]
}
```

**変更点:**
- `skills`: パス形式 → composition 名（`"skills/preflight"` → `"preflight"`）
- `rules`: パス形式 → composition 名（`"rules/config-loading.md"` → `"config-loading"`）
- その他フィールド（hooks, config, agents, files, scripts）: 変更なし

### 3-2. composition.yaml（変更）

**Before:**
```yaml
name: review
package: quality-gates    # ← 削除対象
type: skill
frontmatter: ...
instruction: review
```

**After:**
```yaml
name: review
# package フィールド削除 — 所有者は manifest から解決
type: skill
frontmatter: ...
instruction: review
```

### 3-3. packages/*/skills/（削除）

以下のディレクトリを完全削除:
- `packages/core/skills/`
- `packages/quality-gates/skills/`
- `packages/issue-workflow/skills/`
- `packages/codex-suggestions/skills/`
- `packages/gemini-suggestions/skills/`

---

## 4. 全パッケージ manifest → composition マッピング

### 4-1. 変更後の manifest skills/rules

| パッケージ | skills | rules |
|-----------|--------|-------|
| **core** | preflight, startproject, design, task-state, checkpointing | config-loading, coding-principles, task-memory-usage, context-sharing |
| **quality-gates** | review, tdd, design-tracker, release-readiness | skill-review-policy |
| **issue-workflow** | issue-create, issue-fix | *(なし)* |
| **codex-suggestions** | codex-system | codex-delegation, codex-suggestion-compliance |
| **gemini-suggestions** | gemini-system | gemini-delegation, gemini-suggestion-compliance |
| **agent-routing** | *(なし)* | orchestra-usage, agent-routing-policy |
| **cocoindex** | *(なし)* | cocoindex-usage |
| **route-audit** | *(なし)* | *(なし)* |
| **cli-logging** | *(なし)* | *(なし)* |
| **tmux-monitor** | *(なし)* | *(なし)* |

**既存不整合の修正（本リファクタと同時に実施）:**
- `skill-review-policy`: core manifest → quality-gates manifest に移動（レビュー品質ゲートのルール）
- `checkpointing`: composition の `package: cli-logging` は誤り → core に帰属（core manifest に既存）

### 4-2. グローバル composition（どの manifest にも属さない）

現時点では **0 件** を目標とする。全 composition をいずれかのパッケージ manifest に帰属させる。

### 4-3. 帰属の検証（ビルド時）

```
WARNING: composition 'foo' is not referenced by any installed manifest.
         It will be built as a global composition.
```

グローバル composition が意図せず発生した場合に警告を出す。

---

## 5. ビルドフロー変更

### 5-1. 現在のフロー

```
sync-orchestra.py (SessionStart)
  ├─ collect_facet_managed_paths()
  │    composition.yaml をスキャン → name/type からパスを収集
  │    → facet_managed: set[str]
  │
  ├─ sync_packages()
  │    manifest の skills/rules からファイルを同期
  │    facet_managed パスはスキップ
  │
  └─ build_facets()
       → facet_builder.build_all()
         各 composition の package フィールドで installed_packages をチェック
         → ビルド or スキップ
```

### 5-2. 新しいフロー

```
sync-orchestra.py (SessionStart)
  ├─ collect_manifest_compositions()         [NEW]
  │    全 installed manifest → skills/rules 名を収集
  │    → manifest_compositions: dict[str, str]  # {composition_name: package_name}
  │
  ├─ collect_facet_managed_paths()           [SIMPLIFIED]
  │    manifest_compositions + 全 composition をスキャン
  │    → facet_managed: set[str]
  │
  ├─ sync_packages()                         [SIMPLIFIED]
  │    skills カテゴリの同期を削除（facet build に完全委譲）
  │    hooks, config, agents, rules(非facet) のみ同期
  │
  └─ build_facets()
       → facet_builder.build_all()           [MODIFIED]
         manifest_compositions を受け取り、ビルド対象を決定
         composition.package は参照しない
```

### 5-3. FacetBuilder の変更

```python
@dataclass
class FacetBuilder:
    orchestra_dir: Path
    project_facets_dir: Path | None = None
    # installed_packages → manifest_compositions に変更
    manifest_compositions: dict[str, str] | None = None
    # {composition_name: package_name}
    # None = パッケージフィルタリングなし（全ビルド）

    def build_one(self, name: str, target: str, project_dir: Path) -> Path | None:
        composition = self.load_composition(composition_path)
        # package フィールドは参照しない

        # manifest_compositions によるフィルタリング
        if self.manifest_compositions is not None:
            if name in self.manifest_compositions:
                # manifest に含まれる → そのパッケージがインストール済み
                pass  # ビルドする
            else:
                # manifest に含まれない → グローバル composition
                print(f"[facet] building global composition: {name}")
        ...
```

---

## 6. sync_engine.py 変更詳細

### 6-1. 新関数: collect_manifest_compositions()

```python
def collect_manifest_compositions(
    orchestra_path: Path,
    installed_packages: list[str],
) -> dict[str, str]:
    """全 installed manifest から skills/rules の composition 名を収集する。

    Returns:
        {composition_name: package_name} マッピング
    """
    result: dict[str, str] = {}
    for pkg_name in installed_packages:
        manifest_path = orchestra_path / "packages" / pkg_name / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for skill_name in manifest.get("skills", []):
            result[skill_name] = pkg_name
        for rule_name in manifest.get("rules", []):
            result[rule_name] = pkg_name
    return result
```

### 6-2. collect_facet_managed_paths() の変更

```python
def collect_facet_managed_paths(
    orchestra_path: Path,
    project_dir: Path,
    manifest_compositions: dict[str, str] | None = None,
) -> set[str]:
    """facet composition で管理される skill/rule のパスを収集する。

    manifest_compositions が指定された場合:
    - manifest に含まれる composition → managed に追加
    - manifest に含まれない composition → グローバルとして managed に追加
    """
    # 既存ロジックを維持（composition YAML をスキャンして name/type を読む）
    # package フィールドの参照を削除
    ...
```

### 6-3. sync_packages() の変更

```python
def sync_packages(...) -> tuple[int, set[str]]:
    for pkg_name in installed_packages:
        ...
        # "skills" カテゴリを同期対象から除外
        # rules は facet_managed チェックで既にスキップされるため変更不要
        for category in ("agents", "rules", "config"):  # "skills" を削除
            ...
```

---

## 7. orchestra-manager.py 変更詳細

### 7-1. install コマンド

**変更なし** — install は manifest の hooks, files, config を同期する。
skills は facet build で生成されるため、install 時の個別処理は不要。

### 7-2. list コマンド

skills/rules の表示を composition 名で出力するように調整。

### 7-3. facet build コマンド

`--manifest-compositions` オプションは不要。
build_all() 内で manifest を読み込んで manifest_compositions を構築する。

### 7-4. Package モデル

```python
@dataclass
class Package:
    ...
    skills: list[str]   # composition 名のリスト（パスではなく名前）
    rules: list[str]    # composition 名のリスト（パスではなく名前）
    ...
```

---

## 8. 影響範囲サマリ

| ファイル | 変更種別 | 概要 |
|---------|---------|------|
| `packages/*/skills/` (5パッケージ) | **削除** | 中間 SKILL.md ファイル全削除 |
| `packages/*/manifest.json` (5パッケージ) | **修正** | skills/rules を composition 名形式に変更 |
| `facets/compositions/*.yaml` (17件) | **修正** | `package` フィールド削除 |
| `scripts/lib/facet_builder.py` | **修正** | installed_packages → manifest_compositions、package フィールド無視 |
| `scripts/lib/sync_engine.py` | **修正** | collect_manifest_compositions() 追加、skills 同期削除 |
| `scripts/orchestra-manager.py` | **修正** | facet build の manifest_compositions 連携 |
| `tests/` | **修正** | テストデータ・アサーション更新 |

---

## 9. テスト計画

### 9-1. ユニットテスト

| テスト | 対象 | 検証内容 |
|--------|------|---------|
| `test_collect_manifest_compositions` | `collect_manifest_compositions()` | manifest から composition マッピング収集 |
| `test_facet_build_with_manifest` | `FacetBuilder.build_all()` | manifest ベースのビルドフィルタリング |
| `test_facet_build_global` | `FacetBuilder.build_all()` | グローバル composition の常時ビルド |
| `test_sync_packages_no_skills` | `sync_packages()` | skills カテゴリの同期除外 |

### 9-2. 統合テスト

| テスト | 検証内容 |
|--------|---------|
| `test_full_sync_flow` | SessionStart 相当のフル同期が正常完了する |
| `test_install_uninstall_cycle` | パッケージの install/uninstall で skills が正しく追加/削除される |

---

## 10. 実装順序

1. **manifest.json 更新** — skills/rules を composition 名形式に変更 + 帰属修正
2. **composition.yaml 更新** — `package` フィールド削除
3. **facet_builder.py 修正** — manifest_compositions ベースのビルドロジック
4. **sync_engine.py 修正** — collect_manifest_compositions() 追加、skills 同期削除
5. **orchestra-manager.py 修正** — facet build 連携
6. **packages/*/skills/ 削除**
7. **テスト更新・実行**
8. **ドキュメント更新** — architecture.md, distribution-sync-flow.md
