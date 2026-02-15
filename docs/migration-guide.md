# Migration Guide: シンボリックリンク方式 → パッケージ管理方式

旧方式（グローバル `~/.claude/` へのシンボリックリンク配布）から、
新方式（`orchestra-manager.py` によるプロジェクト単位のパッケージ管理）への移行手順。

---

## 1. 各プロジェクトの旧パス hook を確認・修正

旧方式では hook が `$CLAUDE_PROJECT_DIR/hooks/` を参照していた。
新方式では `$AI_ORCHESTRA_DIR/packages/<pkg>/hooks/` を参照する。

### 確認コマンド

```bash
# 全プロジェクトの settings.local.json を検索
find ~/ghq -name "settings.local.json" -path "*/.claude/*" \
  -exec grep -l 'CLAUDE_PROJECT_DIR/hooks' {} \;
```

### 修正方法

該当プロジェクトで `orchestra-manager.py` を再実行すれば、正しいパスで上書きされる:

```bash
# パッケージを再インストール（hooks が正しいパスで再登録される）
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install <package> --project /path/to/project
```

または、まだ init していないプロジェクトの場合:

```bash
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" init --project /path/to/project
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install core --project /path/to/project
python3 "$AI_ORCHESTRA_DIR/scripts/orchestra-manager.py" install tmux-monitor --project /path/to/project
# 必要に応じて他のパッケージも
```

### 旧パス → 新パスの対応表

| 旧パス | 新パス（パッケージ） |
|-------|-------------------|
| `$CLAUDE_PROJECT_DIR/hooks/log-cli-tools.py` | `$AI_ORCHESTRA_DIR/packages/cli-logging/hooks/log-cli-tools.py` |
| `$CLAUDE_PROJECT_DIR/hooks/check-codex-after-plan.py` | `$AI_ORCHESTRA_DIR/packages/codex-suggestions/hooks/check-codex-after-plan.py` |
| `$CLAUDE_PROJECT_DIR/hooks/check-codex-before-write.py` | `$AI_ORCHESTRA_DIR/packages/codex-suggestions/hooks/check-codex-before-write.py` |
| `$CLAUDE_PROJECT_DIR/hooks/suggest-gemini-research.py` | `$AI_ORCHESTRA_DIR/packages/gemini-suggestions/hooks/suggest-gemini-research.py` |
| `$CLAUDE_PROJECT_DIR/hooks/lint-on-save.py` | `$AI_ORCHESTRA_DIR/packages/quality-gates/hooks/lint-on-save.py` |
| `$CLAUDE_PROJECT_DIR/hooks/post-implementation-review.py` | `$AI_ORCHESTRA_DIR/packages/quality-gates/hooks/post-implementation-review.py` |
| `$CLAUDE_PROJECT_DIR/hooks/post-test-analysis.py` | `$AI_ORCHESTRA_DIR/packages/quality-gates/hooks/post-test-analysis.py` |
| `$CLAUDE_PROJECT_DIR/hooks/agent-router.py` | `$AI_ORCHESTRA_DIR/packages/route-audit/hooks/agent-router.py` |
| `$CLAUDE_PROJECT_DIR/hooks/orchestration-expected-route.py` | `$AI_ORCHESTRA_DIR/packages/route-audit/hooks/orchestration-expected-route.py` |
| `$CLAUDE_PROJECT_DIR/hooks/orchestration-route-audit.py` | `$AI_ORCHESTRA_DIR/packages/route-audit/hooks/orchestration-route-audit.py` |
| `$CLAUDE_PROJECT_DIR/hooks/tmux-session-start.py` | `$AI_ORCHESTRA_DIR/packages/tmux-monitor/hooks/tmux-session-start.py` |
| `$CLAUDE_PROJECT_DIR/hooks/tmux-session-end.py` | `$AI_ORCHESTRA_DIR/packages/tmux-monitor/hooks/tmux-session-end.py` |
| `$CLAUDE_PROJECT_DIR/hooks/tmux-subagent-start.py` | `$AI_ORCHESTRA_DIR/packages/tmux-monitor/hooks/tmux-subagent-start.py` |
| `$CLAUDE_PROJECT_DIR/hooks/tmux-subagent-stop.py` | `$AI_ORCHESTRA_DIR/packages/tmux-monitor/hooks/tmux-subagent-stop.py` |
| `$CLAUDE_PROJECT_DIR/hooks/tmux-format-output.py` | `$AI_ORCHESTRA_DIR/packages/tmux-monitor/hooks/tmux-format-output.py` |

## 2. 不要になった `.claude/hooks/` ディレクトリの削除

旧方式ではプロジェクトの `.claude/hooks/` に `hook_common.py` 等がコピーされていた。
新方式では `$AI_ORCHESTRA_DIR/packages/core/hooks/` を直接参照するため不要。

```bash
# 各プロジェクトで確認
ls /path/to/project/.claude/hooks/

# hook_common.py, log_common.py 等の旧ファイルがあれば削除可能
# （orchestra-manager 経由の hook は $AI_ORCHESTRA_DIR を参照する）
```

## 3. 削除済みスキルの確認

以下のスキルは削除済み。各プロジェクトの `.claude/skills/` に同期済みコピーが残っている場合は削除:

```bash
rm -rf /path/to/project/.claude/skills/init-orchestra
rm -rf /path/to/project/.claude/skills/update-orchestra
```

---

## v0.3.0: トップレベル agents/skills/rules → パッケージ内に移動

v0.3.0 でリポジトリトップレベルの `agents/`, `skills/`, `rules/` を廃止し、
各パッケージ内に移動しました。

### 変更概要

| 旧パス | 新パス |
|-------|--------|
| `agents/*.md` | `packages/agent-routing/agents/*.md` |
| `rules/orchestra-usage.md` | `packages/agent-routing/rules/` |
| `rules/codex-delegation.md` | `packages/codex-suggestions/rules/` |
| `rules/gemini-delegation.md` | `packages/gemini-suggestions/rules/` |
| `rules/config-loading.md` | `packages/core/rules/` |
| `rules/coding-principles.md` | `packages/core/rules/` |
| `skills/plan/`, `skills/startproject/` | `packages/agent-routing/skills/` |
| `skills/codex-system/` | `packages/codex-suggestions/skills/` |
| `skills/gemini-system/` | `packages/gemini-suggestions/skills/` |
| `skills/review/`, `skills/tdd/`, `skills/simplify/`, `skills/design-tracker/` | `packages/quality-gates/skills/` |
| `skills/checkpointing/` | `packages/cli-logging/skills/` |

### 配布先プロジェクトへの影響

- **影響なし**: 配布先はパッケージ経由の sync のみを使用しており、トップレベル同期 (`sync_top_level`) は使用していない前提
- manifest.json が更新されているため、次回 SessionStart 時に新しいパスから自動同期される

### ai-orchestra 自身の dogfooding

- `.claude/` 配下のシンボリックリンク（`agents`, `skills`, `rules`, `config`）を廃止
- `.claude/orchestra.json` を作成し、全パッケージをインストール済みとして登録
- SessionStart の `sync-orchestra.py` により packages/ から `.claude/` に自動同期

### `sync_top_level` の廃止

- `orchestra.json` の `sync_top_level` フラグは廃止
- `sync-orchestra.py` の `sync_top_level()` 関数を削除
- `orchestra-manager.py init` で `sync_top_level: true` を設定しなくなった

---

## 完了チェックリスト

- [ ] 全プロジェクトで `settings.local.json` の旧パス hook がないことを確認
- [ ] 全プロジェクトで `orchestra-manager.py init` + `install` を実行済み
- [ ] 各プロジェクトの `.claude/hooks/` に旧ファイルが残っていないことを確認
- [ ] 各プロジェクトの `.claude/skills/` から `init-orchestra`, `update-orchestra` を削除
