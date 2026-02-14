# AI Orchestra

**マルチエージェント協調フレームワーク**

Claude Code + Codex CLI + Gemini CLI を統合したオーケストレーションシステム。

---

## このリポジトリについて

このリポジトリは AI Orchestra の設定ファイルを管理しています。

```
ai-orchestra/
├── agents/        # 25 専門エージェント定義
├── packages/      # パッケージ（hooks・scripts・config）
│   ├── core/      # 共通基盤（hook_common.py）
│   ├── tmux-monitor/ # tmux サブエージェント監視
│   └── ...
├── rules/         # 共通ルール（Codex/Gemini委譲、コーディング規約）
├── scripts/       # 管理CLI
│   ├── orchestra-manager.py  # パッケージ管理
│   └── sync-orchestra.py     # SessionStart 自動同期
├── skills/        # スキル定義（/review など）
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

### 更新

```bash
# ai-orchestra を更新 → hooks は即反映、skills 等は次回起動時に自動同期
git pull
```

---

## 使い方

→ `rules/orchestra-usage.md` を参照

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
- `skills/`, `agents/`, `rules/` は SessionStart の `sync-orchestra.py` で各プロジェクトの `.claude/` に差分同期
