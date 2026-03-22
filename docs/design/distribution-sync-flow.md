# 開発 → 配布 → 自動同期フロー

**概要**: ai-orchestra リポジトリでの開発から、各プロジェクトへの配布・自動同期までの全体像。

---

## 全体像

```
ai-orchestra リポジトリ（開発）
│
├─ packages/{pkg}/          スキル・ルール・hooks・config
├─ facets/                  ポリシー・instruction・composition
└─ scripts/orchestra-manager.py  CLI（orchex）

        │
        ├─── orchex init / install ──→ 初期導入（手動）
        │
        └─── SessionStart hook ─────→ 自動同期（毎セッション）
                                          │
                                          ▼
                                    各プロジェクトの .claude/
```

---

## Phase 1: 初期導入（手動）

### orchex init

```bash
orchex init --project /path/to/project
```

| 処理 | 内容 |
|------|------|
| ディレクトリ作成 | `.claude/{docs,logs,state,checkpoints}/` |
| テンプレート配置 | `Plans.md`, `.claudeignore`, `CLAUDE.md`, `.codex/`, `.gemini/` |
| orchestra.json 初期化 | `installed_packages: []` |
| 環境変数登録 | `$AI_ORCHESTRA_DIR` を `~/.claude/settings.json` に設定 |
| hook 登録 | `sync-orchestra.py` を SessionStart hook に登録 |

### orchex install

```bash
orchex install {package} --project /path/to/project
```

| 処理 | 内容 |
|------|------|
| manifest.json 読み込み | パッケージの宣言内容を取得 |
| hooks 登録 | `.claude/settings.local.json` にイベント→コマンドを追加 |
| config コピー | `packages/{pkg}/config/` → `.claude/config/{pkg}/` |
| orchestra.json 更新 | `installed_packages`, `synced_files` に記録 |
| 初回同期実行 | agents/rules をコピー（skills は facet build で生成） |

---

## Phase 2: 自動同期（SessionStart）

毎セッション開始時に `sync-orchestra.py` が自動実行される。

```
SessionStart hook 発火
│
├─ 1. orchestra.json 読み込み
│     installed_packages, synced_files を取得
│
├─ 2. ファイル同期（mtime 比較で差分のみ）
│     packages/{pkg}/agents/   → .claude/agents/
│     packages/{pkg}/config/   → .claude/config/{pkg}/
│     ※ skills/rules は facet build で生成（packages からは同期しない）
│
├─ 3. Facet Build（composition 更新時のみ）
│     facets/ の部品を組み立て → SKILL.md / rules を再生成
│
├─ 4. Agent Model Patching
│     cli-tools.yaml の model → agents/*.md の frontmatter に反映
│
├─ 5. Stale File Cleanup
│     前回 synced_files にあって今回ないファイルを削除
│     ※ *.local.yaml は絶対に削除しない
│
├─ 6. Hook 同期
│     manifest.json の hooks と settings.local.json を比較・更新
│
├─ 7. .claudeignore 生成
│     テンプレート + .claudeignore.local をマージ
│
└─ 8. orchestra.json 更新
      synced_files, last_sync を書き込み
```

---

## Phase 3: 変更の伝播パターン

| 変更箇所 | 同期方法 | 反映タイミング |
|----------|---------|--------------|
| `packages/*/hooks/*.py` | **同期不要**（`$AI_ORCHESTRA_DIR` から直接実行） | 即時 |
| `packages/*/agents/` | mtime ベースのファイルコピー | 次回 SessionStart |
| `packages/*/config/` | mtime ベースのファイルコピー | 次回 SessionStart |
| `facets/policies/*.md` | facet build で全参照スキル・ルール再生成 | 次回 SessionStart |
| `facets/instructions/*.md` | facet build で該当 composition のみ再生成 | 次回 SessionStart |
| `facets/compositions/*.yaml` | facet build で該当スキル・ルールのみ再生成 | 次回 SessionStart |
| 新パッケージ追加 | `orchex install` が必要 | install 実行時 |

---

## 設計上のポイント

### hooks は参照実行（コピーしない）

```json
// .claude/settings.local.json
{
  "hooks": {
    "PreToolUse": [{
      "hooks": [{
        "command": "python3 \"$AI_ORCHESTRA_DIR/packages/agent-routing/hooks/agent-router.py\""
      }]
    }]
  }
}
```

- `$AI_ORCHESTRA_DIR` のファイルを直接参照するため、orchestra リポで更新すれば全プロジェクトに即反映
- 再登録や再インストールは不要

### skills は facet build で生成（パッケージ内に置かない）

- スキル（SKILL.md）は `facets/compositions/*.yaml` の定義をもとに `facet build` で生成される
- 生成先は `.claude/skills/{name}/SKILL.md`（プロジェクト側）
- composition の所有パッケージは manifest.json の `skills` リストから解決される（composition YAML に `package` フィールドは不要）

### rules は facet build で生成（skills と同様）

- ルール（`.claude/rules/{name}.md`）は `facets/compositions/*.yaml` の `type: rule` 定義をもとに `facet build` で生成される
- composition の所有パッケージは manifest.json の `rules` リストから解決される

### config はコピー（上書き可能）

- プロジェクト側の `.claude/config/` にコピーされる
- プロジェクト固有のカスタマイズが可能（`.local.yaml` / `.local.json` で上書き）

### .local ファイルの保護

| ファイル種別 | 同期対象 | 削除対象 |
|------------|---------|---------|
| `*.yaml` / `*.json`（ベース） | Yes | Yes（manifest から消えたら） |
| `*.local.yaml` / `*.local.json` | **No** | **No（絶対に削除しない）** |

### mtime ベースの差分同期

- ソースの mtime > 宛先の mtime の場合のみコピー
- 不要な書き込みを抑制し、SessionStart の高速化に寄与

---

## $AI_ORCHESTRA_DIR の役割

| 利用箇所 | 用途 |
|----------|------|
| hooks のコマンドパス | `python3 "$AI_ORCHESTRA_DIR/packages/*/hooks/*.py"` |
| sync-orchestra.py | packages/ から同期元ファイルを特定 |
| facet build | facets/compositions/ の読み込み元 |
| orchestra.json | `orchestra_dir` に記録（同期元の絶対パス） |

---

## orchestra.json の構造

```json
{
  "installed_packages": ["core", "agent-routing", "quality-gates"],
  "orchestra_dir": "/path/to/ai-orchestra",
  "last_sync": "2026-03-19T03:30:00+00:00",
  "synced_files": [
    "skills/review/SKILL.md",
    "config/agent-routing/cli-tools.yaml",
    "agents/code-reviewer.md",
    "rules/coding-principles.md"
  ]
}
```

| フィールド | 用途 |
|-----------|------|
| `installed_packages` | インストール済みパッケージの追跡 |
| `orchestra_dir` | 同期元リポジトリの絶対パス |
| `last_sync` | 最終同期日時 |
| `synced_files` | 前回同期したファイル一覧（stale cleanup に使用） |

---

## Facet Build の位置づけ

```
facets/policies/*.md          共有ルール（1箇所修正 → 全スキルに反映）
facets/output-contracts/*.md  共有出力形式
facets/instructions/*.md      スキル・ルール固有の手順
facets/compositions/*.yaml    組み立て定義

    ↓ SessionStart で自動同期 + facet build

.claude/skills/{name}/SKILL.md   生成物（Claude Code 用）
.claude/rules/{name}.md          生成物（Claude Code 用）
.codex/skills/{name}/SKILL.md    生成物（Codex CLI 用、パッケージ依存）
```

- SessionStart の同期フローの中で facet build が自動実行される
- composition の mtime がアウトプットより新しい場合のみ再ビルド
- 手動実行: `orchex facet build --project .`
