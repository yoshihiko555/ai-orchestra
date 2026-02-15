# パッケージプロモーションワークフロー

ai-valification で検証した機能を ai-orchestra のパッケージとして正式採用する手順。

## 前提

- ai-valification: 検証・実験用リポジトリ
- ai-orchestra: 本番パッケージリポジトリ
- tmux-monitor パッケージが完成例として参考になる

## ワークフロー概要

```
ai-valification で検証
    ↓ 検証OK
ai-orchestra/packages/{pkg}/ にファイル配置
    ↓
manifest.json を更新
    ↓
import パスを $AI_ORCHESTRA_DIR 方式に変更
    ↓
動作確認（install --dry-run → install → 実動作）
    ↓
コミット
```

## Step 1: 検証完了の確認

ai-valification 側で以下を確認:

- [ ] hook が期待通りに動作する
- [ ] エッジケース（空入力、タイムアウト等）を確認済み
- [ ] パフォーマンス問題がない（hook の実行時間が timeout 内）

## Step 2: ファイル配置

### 2-1. hooks をコピー

```bash
# ai-valification の .claude/hooks/ → ai-orchestra の packages/{pkg}/hooks/
cp ai-valification/.claude/hooks/{hook-file}.py \
   ai-orchestra/packages/{pkg}/hooks/
```

### 2-2. import パスの変更

ai-valification では `.claude/hooks/` にフラットに配置されているため、`hook_common` 等の import パスを `$AI_ORCHESTRA_DIR` 方式に変更する。

```python
# 変更前（ai-valification 方式）
# hook_common.py が同じディレクトリにある前提
from hook_common import get_field, read_hook_input

# 変更後（ai-orchestra 方式）
import os, sys
_orchestra_dir = os.environ.get("AI_ORCHESTRA_DIR", "")
if _orchestra_dir:
    _core_hooks = os.path.join(_orchestra_dir, "packages", "core", "hooks")
    if _core_hooks not in sys.path:
        sys.path.insert(0, _core_hooks)
from hook_common import get_field, read_hook_input
```

**注意**: `hook_common` を使っていない hook はこの変更不要。

### 2-3. config ファイル（必要な場合のみ）

hook が設定ファイルを参照する場合:

```bash
# config ファイルを packages/{pkg}/config/ に配置
cp ai-valification/.claude/config/{config-file} \
   ai-orchestra/packages/{pkg}/config/
```

manifest.json の `config` フィールドにパスを追加:
```json
"config": ["config/some-config.yaml"]
```

### 2-4. scripts（必要な場合のみ）

分析スクリプト等がある場合:

```bash
cp ai-valification/path/to/script.py \
   ai-orchestra/packages/{pkg}/scripts/
```

## Step 3: manifest.json の更新

```json
{
  "name": "{pkg}",
  "version": "0.1.0",
  "description": "パッケージの説明",
  "depends": ["core"],
  "hooks": {
    "PostToolUse": [
      {"file": "some-hook.py", "matcher": "Bash"}
    ],
    "PreToolUse": [
      {"file": "another-hook.py", "matcher": "Edit|Write"}
    ]
  },
  "files": [
    "hooks/some-hook.py",
    "hooks/another-hook.py"
  ],
  "skills": [],
  "agents": [],
  "rules": [],
  "scripts": [],
  "config": []
}
```

### hooks フィールドのルール

| キー | 値 |
|------|-----|
| イベント名 | `SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd` 等 |
| `file` | `hooks/` ディレクトリ内のファイル名 |
| `matcher` | ツール名のパイプ区切り（例: `"Edit\|Write"`, `"Bash"`）。省略可 |

matcher なしの場合は文字列でも可:
```json
"SessionStart": ["my-hook.py"]
```

### files フィールド

hooks/ 配下の全ファイルを列挙（共通モジュール含む）。

## Step 4: 動作確認

```bash
# 1. dry-run でインストール内容を確認
python3 ~/ai-orchestra/scripts/orchestra-manager.py install {pkg} --project /path/to/project --dry-run

# 2. 実際にインストール
python3 ~/ai-orchestra/scripts/orchestra-manager.py install {pkg} --project /path/to/project

# 3. status で確認
python3 ~/ai-orchestra/scripts/orchestra-manager.py status --project /path/to/project

# 4. Claude Code を起動して実際の hook 動作を確認

# 5. 問題があれば uninstall
python3 ~/ai-orchestra/scripts/orchestra-manager.py uninstall {pkg} --project /path/to/project
```

## Step 5: コミット

```bash
cd ~/ai-orchestra
git add packages/{pkg}/
git commit -m "feat: {pkg} パッケージを正式採用"
```

## チェックリスト

パッケージ完成時の最終確認:

- [ ] `hooks/` に全 hook ファイルが配置されている
- [ ] `hook_common` の import が `$AI_ORCHESTRA_DIR` 方式になっている
- [ ] `manifest.json` の hooks フィールドに全 hook が登録されている
- [ ] `manifest.json` の files フィールドに全ファイルが列挙されている
- [ ] matcher が正しく設定されている
- [ ] `depends` に必要な依存（通常 `core`）が含まれている
- [ ] `install --dry-run` が正常に動作する
- [ ] `install` → Claude Code 起動 → hook 動作確認
- [ ] `uninstall` が正常に動作する（フック削除、config 削除）

## 参考: 完成パッケージの構造例（tmux-monitor）

```
packages/tmux-monitor/
├── manifest.json
└── hooks/
    ├── tmux_common.py          # 共通ユーティリティ
    ├── tmux-session-start.py   # SessionStart hook
    ├── tmux-session-end.py     # SessionEnd hook
    ├── tmux-subagent-start.py  # SubagentStart hook
    ├── tmux-subagent-stop.py   # SubagentStop hook
    └── tmux-format-output.py   # ヘルパースクリプト
```
