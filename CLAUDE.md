# AI Orchestra

**マルチエージェント協調フレームワーク**

Claude Code + Codex CLI + Gemini CLI を統合したオーケストレーションシステム。

---

## このリポジトリについて

このリポジトリは AI Orchestra の設定ファイルを管理しています。

```
ai-orchestra/
├── packages/      # パッケージ（hooks・scripts・agents・skills・rules・config）
│   ├── core/              # 共通基盤 + coding-principles / config-loading ルール
│   ├── agent-routing/     # 25 エージェント定義 + ルーティング hooks
│   ├── cli-logging/       # CLI ログ + checkpointing スキル
│   ├── codex-suggestions/ # Codex 相談提案 + codex-delegation ルール
│   ├── gemini-suggestions/# Gemini リサーチ提案 + gemini-delegation ルール
│   ├── quality-gates/     # 品質ゲート + review/tdd/simplify スキル
│   ├── route-audit/       # ルーティング監査
│   └── tmux-monitor/      # tmux サブエージェント監視
├── scripts/       # 管理CLI
│   ├── orchestra-manager.py  # パッケージ管理
│   └── sync-orchestra.py     # SessionStart 自動同期
└── templates/     # テンプレート
```

---

## セットアップ

### パッケージのインストール

```bash
# プロジェクトにパッケージをインストール
python3 scripts/orchestra-manager.py install <package> --project /path/to/project
```

orchestra-manager が以下を自動実行:
1. `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` を設定
2. `.claude/settings.local.json` に hooks を登録（`$AI_ORCHESTRA_DIR/packages/...` 参照）
3. `.claude/orchestra.json` にパッケージ情報を記録
4. SessionStart hook で skills/agents/rules を自動同期

### セットアップ完了条件

以下を満たしたらセットアップ完了です。

- `~/.claude/settings.json` に `env.AI_ORCHESTRA_DIR` が設定されている
- `.claude/settings.local.json` に AI Orchestra の hooks が登録されている
- `.claude/orchestra.json` が存在し、インストール済みパッケージが記録されている
- Claude Code の次回起動時に SessionStart hook が走り、`.claude/` 配下へ差分同期される

### 更新

```bash
# ai-orchestra を更新 → hooks は即反映、skills 等は次回起動時に自動同期
git pull
```

### 運用ルール（更新後チェック）

`git pull` 後は以下を確認してください。

- hooks 変更は即時反映される（`$AI_ORCHESTRA_DIR/packages/.../hooks` を直接参照）
- agents/skills/rules/config の変更は次回 Claude Code 起動時の SessionStart で同期される
- 同期結果は `.claude/` 配下に反映される（差分があるファイルのみ更新）

---

## 使い方

→ `packages/agent-routing/rules/orchestra-usage.md` を参照

または、Claude Code で以下を実行：

```
Task(subagent_type="planner", prompt="計画: {機能名}")
Task(subagent_type="frontend-dev", prompt="実装: {機能名}")
/review
```

---

## アーキテクチャ

```
Claude Code (Orchestrator)
    │
    ├── Codex CLI    # 深い推論・設計判断・デバッグ
    ├── Gemini CLI   # リサーチ・大規模分析・マルチモーダル
    │
    └── 25 Specialized Agents
        ├── Planning: planner, researcher, requirements
        ├── Design: architect, api-designer, data-modeler, auth-designer, spec-writer
        ├── Implementation: frontend-dev, backend-*-dev, ai-*, debugger, tester
        └── Review: code-reviewer, security-reviewer, performance-reviewer, ...
```

---

## 開発

- `packages/` 内の hooks は `$AI_ORCHESTRA_DIR` 経由で直接参照される（`git pull` で即反映）
- agents/skills/rules は各パッケージ内に配置され、SessionStart の `sync-orchestra.py` で `.claude/` に差分同期

---

## References

必須（上から優先）:
1. `packages/agent-routing/rules/orchestra-usage.md`
2. `packages/agent-routing/config/cli-tools.yaml`

任意:
- `scripts/orchestra-manager.py`
- `scripts/sync-orchestra.py`
