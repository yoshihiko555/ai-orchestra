# init-orchestra

<init-orchestra>

このプロジェクトで AI Orchestra を有効化し、必要な設定ファイルを配置します。

## 実行手順

### 1. 既存ファイルの確認

以下のファイルの存在を確認:
- `.claude/settings.json`
- `CLAUDE.md`
- `.claude/docs/DESIGN.md`
- `.codex/config.toml`
- `.gemini/settings.json`

### 2. .claude/settings.json の作成/マージ

**ファイルが存在しない場合:**
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/agent-router.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/check-codex-before-write.py\"",
            "timeout": 10
          }
        ]
      },
      {
        "matcher": "WebSearch|WebFetch",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/suggest-gemini-research.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Task",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/check-codex-after-plan.py\"",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/post-test-analysis.py\"",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/log-cli-tools.py\"",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/lint-on-save.py\"",
            "timeout": 30
          },
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/hooks/post-implementation-review.py\"",
            "timeout": 5
          }
        ]
      }
    ]
  },
  "permissions": {
    "allow": [
      "Bash(git *)",
      "Bash(ruff *)",
      "Bash(ty *)",
      "Bash(pytest *)",
      "Bash(npm *)",
      "Bash(pnpm *)",
      "Bash(yarn *)",
      "Bash(uv *)",
      "Bash(poe *)",
      "Bash(ls *)",
      "Bash(cat *)",
      "Bash(head *)",
      "Bash(tail *)",
      "Bash(mkdir *)",
      "Bash(cp *)",
      "Bash(mv *)",
      "Bash(rm *)"
    ],
    "deny": [
      "Bash(rm -rf /)",
      "Bash(rm -rf ~)",
      "Bash(*> .env*)",
      "Bash(*credentials*)",
      "Bash(*secret*)",
      "Bash(*password*)"
    ]
  }
}
```

**ファイルが存在する場合:**
- `hooks` セクションをマージ（既存の hooks を保持しつつ、新しい hooks を追加）
- `permissions` セクションが存在しない場合は追加

### 3. CLAUDE.md の作成（条件付き）

**既存の CLAUDE.md がある場合: 何もしない（プロジェクト固有情報を尊重）**

**存在しない場合のみ:** `~/.claude/templates/project/CLAUDE.md` のテンプレートを配置

### 4. .claude/docs/ と .claude/logs/ と .claude/checkpoints/ の作成

```
.claude/
├── docs/
│   ├── DESIGN.md              # 既存がなければテンプレート作成
│   ├── research/.gitkeep      # 常に作成
│   └── libraries/_TEMPLATE.md # 既存がなければテンプレート作成
├── logs/
│   └── .gitkeep               # 常に作成（Codex/Gemini ログ用）
└── checkpoints/
    └── .gitkeep               # 常に作成（チェックポイント保存用）
```

**logs ディレクトリ**: `log-cli-tools.py` フックが Codex/Gemini CLI の入出力を `.claude/logs/cli-tools.jsonl` に記録します。

### 5. .codex/ の作成

```
.codex/
├── config.toml
├── AGENTS.md
└── skills/context-loader/SKILL.md
```

`~/.claude/templates/codex/` の内容をコピー

### 6. .gemini/ の作成

```
.gemini/
├── settings.json
├── GEMINI.md
└── skills/context-loader/SKILL.md
```

`~/.claude/templates/gemini/` の内容をコピー

### 7. 完了レポート

作成・スキップしたファイルを報告:

```
## Orchestra 有効化完了

### 作成したファイル:
- .claude/settings.json
- .claude/docs/DESIGN.md
- .claude/logs/.gitkeep
- .codex/config.toml
- ...

### スキップしたファイル（既存）:
- CLAUDE.md (プロジェクト固有設定を維持)
- ...

### 次のステップ:
1. CLAUDE.md にプロジェクト固有の情報を追加
2. .claude/docs/DESIGN.md に設計決定を記録
3. 開発を開始
```

## 重要な注意事項

- **CLAUDE.md は既存があれば触らない**（プロジェクト固有情報を尊重）
- オーケストラの使い方は `~/.claude/rules/orchestra-usage.md` から自動読み込み
- Codex/Gemini 委譲ルールも `~/.claude/rules/` から自動読み込み
- プロジェクト固有の Tech Stack, テストコマンド等は `CLAUDE.md` に記載

## Hook 一覧

| Hook | トリガー | 動作 |
|------|---------|------|
| agent-router.py | UserPromptSubmit | エージェント/CLI ルーティング提案 |
| check-codex-before-write.py | PreToolUse (Edit\|Write) | 設計ファイル編集時に Codex 提案 |
| suggest-gemini-research.py | PreToolUse (WebSearch\|WebFetch) | リサーチ時に Gemini 提案 |
| check-codex-after-plan.py | PostToolUse (Task) | Plan 後に Codex レビュー提案 |
| post-test-analysis.py | PostToolUse (Bash) | テスト失敗時に Codex デバッグ提案 |
| lint-on-save.py | PostToolUse (Edit\|Write) | Python 編集後に ruff/ty 実行 |
| post-implementation-review.py | PostToolUse (Edit\|Write) | 大量変更後にレビュー提案 |
| log-cli-tools.py | PostToolUse (Bash) | Codex/Gemini ログ記録 |

</init-orchestra>
