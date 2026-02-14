# update-orchestra

<update-orchestra>

プロジェクトの AI Orchestra 設定を最新状態に同期します。
プロジェクト固有データ（CLAUDE.md, research/, logs/ 等）は一切変更しません。

## 更新の仕組み

v2 では `$AI_ORCHESTRA_DIR` 環境変数と `sync-orchestra.py` による自動同期を採用しています。

| 変更内容 | 反映方法 |
|---------|---------|
| Hook スクリプト修正 | `git pull` のみ（`$AI_ORCHESTRA_DIR` 経由で即反映） |
| Skills/Agents/Rules 修正 | `git pull`（次回 Claude Code 起動時に `sync-orchestra.py` が自動同期） |
| 新フックイベント追加 | `git pull` + `install` 再実行 |
| 新パッケージ追加 | `git pull` + `install` |

## 実行手順

### Step 1: 前提チェック

以下を確認:
1. `.claude/orchestra.json` が存在するか
   - 存在しない場合 → **「`/init-orchestra` を先に実行してください」** と伝えて終了
2. `AI_ORCHESTRA_DIR` 環境変数が有効か
   - `orchestra.json` の `orchestra_dir` が存在するディレクトリを指しているか確認

### Step 2: ai-orchestra リポジトリを更新

```bash
cd "$AI_ORCHESTRA_DIR" && git pull
```

### Step 3: パッケージの状態確認

```bash
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" status --project .
```

これにより:
- インストール済みパッケージの hooks が全て登録されているか確認
- partial 状態のパッケージがあれば再インストールを提案

### Step 4: partial / 新規パッケージの対応

status で問題があるパッケージを再インストール:

```bash
# hooks が不足しているパッケージを再インストール
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install <package> --project .
```

新しいパッケージが追加されている場合は、ユーザーに提案:

```bash
# 全パッケージ一覧
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" list
```

### Step 5: Skills/Agents/Rules の即時同期

通常は次回起動時に自動同期されるが、即時反映が必要な場合:

```bash
echo '{"cwd": "'$(pwd)'"}' | python3 "$AI_ORCHESTRA_DIR/scripts/sync-orchestra.py"
```

### Step 6: テンプレートファイルの更新

以下のテンプレートファイルは `$AI_ORCHESTRA_DIR/templates/` から差分チェック:

#### .codex/ の更新（差分確認付き）

対象ファイル:
- `config.toml`
- `AGENTS.md`
- `skills/context-loader/SKILL.md`

**各ファイルについて:**
1. テンプレートとプロジェクトの内容を比較
2. **差分がない場合** → 「変更なし」として記録、スキップ
3. **差分がある場合** → `AskUserQuestion` で「更新する / スキップする」をユーザーに確認
4. **存在しない場合** → テンプレートからコピー（新規追加として報告）

#### .gemini/ の更新（差分確認付き）

対象ファイル:
- `settings.json`
- `GEMINI.md`
- `skills/context-loader/SKILL.md`

.codex/ と同じ方式で処理する。

#### .claude/docs/libraries/_TEMPLATE.md の更新

- 差分があれば更新（確認不要 — テンプレートファイルのため）
- 差分がなければスキップ

### Step 7: 更新レポート

```
## Orchestra 更新完了

### リポジトリ更新
git pull 実行済み

### パッケージ状態
- core: installed (0 hooks)
- tmux-monitor: installed (4/4 hooks)
- ...

### 再インストールしたパッケージ
- （なし or パッケージ名）

### テンプレート更新
- .codex/AGENTS.md（更新）
- .gemini/GEMINI.md（変更なし）
- ...

### 触れていないファイル（安全）
- CLAUDE.md, .claude/docs/DESIGN.md, .claude/docs/research/*
- .claude/docs/libraries/*.md（_TEMPLATE.md 以外）
- .claude/logs/*, .claude/checkpoints/*
```

---

## 絶対に触らないファイル

| ファイル | 理由 |
|---------|------|
| `CLAUDE.md` | プロジェクト固有の指示書 |
| `.claude/docs/DESIGN.md` | プロジェクトの設計決定記録 |
| `.claude/docs/research/*` | Gemini のリサーチ出力 |
| `.claude/docs/libraries/*.md`（`_TEMPLATE.md` 以外） | ライブラリ固有のドキュメント |
| `.claude/logs/*` | CLI ツールログ |
| `.claude/checkpoints/*` | セッションチェックポイント |
| `.claude/settings.local.json` | orchestra-manager が管理（手動変更しない） |
| `.claude/orchestra.json` | orchestra-manager が管理 |

## 重要な注意事項

- hooks は `$AI_ORCHESTRA_DIR` 経由で直接参照されるため、`git pull` だけで即反映
- skills/agents/rules は SessionStart hook で自動同期（`sync-orchestra.py`）
- `.claude/settings.local.json` を直接編集しない（`orchestra-manager.py` で管理）
- テンプレートファイル以外はユーザー確認なしに更新しない

</update-orchestra>
