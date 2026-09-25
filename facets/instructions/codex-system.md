# Codex System — Config-Driven Integration

**Codex の役割は固定しない。`cli-tools.yaml` を SSOT として解決する。**

> **詳細ルール**: `.claude/rules/codex-delegation.md`, `.claude/rules/config-loading.md`

## Routing Rules

| 条件                                      | 動作                                                                |
| ----------------------------------------- | ------------------------------------------------------------------- |
| `agents.<target>.tool == "codex"`         | Codex CLI を使用（analysis / implementation を用途で選択）          |
| `agents.<target>.tool == "claude-direct"` | Codex を強制しない                                                  |
| `agents.<target>.tool == "antigravity"`   | Antigravity CLI（`agy`）を使用                                      |
| `agents.<target>.tool == "auto"`          | タスク特性で選択（深い推論・デバッグ・比較・レビューは Codex 候補） |

**重要**: 「Codex は設計専用」「Codex は実装専用」などの固定役割を前提にしない。
役割は `cli-tools.yaml` の変更で切り替わる。

## When to Consult Codex

- ユーザーが明示的に Codex 利用を指示したとき
- ルーティング解決結果が `tool: codex` のとき
- `tool: auto` で深い推論が必要な分析（設計・デバッグ・比較検討・レビュー）を行うとき

## How to Consult

### Subagent Pattern (推奨)

**Use Task tool with `subagent_type='general-purpose'` to preserve main context.**

```
Task tool parameters:
- subagent_type: "general-purpose"
- run_in_background: true (optional)
- prompt: |
    Resolve target agent/tool from cli-tools.yaml first.
    If tool resolves to codex, run:

    実効値（base + .local.yaml マージ後）で codex.requires_sandbox_disable が true の場合は
    sandbox を無効化して codex を実行する（codex-delegation.md の Bash サンドボックス制約に従う）。
    エラー時は claude-direct にフォールバック。

    1) Bash (keep the sandbox on):
    PROMPT_FILE=$(mktemp "${TMPDIR:-/tmp}/codex-prompt.XXXXXX")
    cat > "$PROMPT_FILE" <<'PROMPT'
    {question}
    PROMPT
    echo "$PROMPT_FILE"

    2) Bash (disable the sandbox only when the codex-delegation conditions allow it; run `codex exec`
       alone; write the printed path literally because shell variables do not survive
       between Bash calls). `<codex.sandbox>` is `codex.sandbox` from `[Resolved Routing]`;
       if the block lists `codex.sandbox.analysis` / `codex.sandbox.implementation` instead,
       use the analysis one. Resolve from the config files only when there is no block:
    codex exec --model <codex.model> --sandbox <codex.sandbox> <codex.flags> "$(cat '<printed path>')" < /dev/null 2>/dev/null

    Return CONCISE summary (recommendation + rationale).
```

### Direct Call (Short Questions)

For quick questions:

```bash
codex exec --model <codex.model> --sandbox <codex.sandbox.analysis> <codex.flags> "Brief question" < /dev/null 2>/dev/null
```

### Implementation Task (when route == codex)

サブエージェント内であれば `[Resolved Routing]` の `codex.sandbox` を使う（ブロックに
`codex.sandbox.implementation` が出ている場合はその値。ブロックがなければ
`<codex.sandbox.implementation>` のまま）。

```bash
# 1 回目（sandbox 内のまま）: タスク本文を一時ファイルへ書き出し絶対パスを表示
# （シェル文字列への直接埋め込み禁止）
PROMPT_FILE=$(mktemp "${TMPDIR:-/tmp}/codex-prompt.XXXXXX")
cat > "$PROMPT_FILE" <<'PROMPT'
{implementation task}
PROMPT
echo "$PROMPT_FILE"
```

```bash
# 2 回目（`codex exec` 単体。表示されたパスを文字列で書く）
codex exec --model <codex.model> --sandbox <codex.sandbox.implementation> <codex.flags> "$(cat '<表示されたパス>')" < /dev/null 2>/dev/null
```

### Sandbox Modes

| Mode              | Use Case                     |
| ----------------- | ---------------------------- |
| `read-only`       | 分析、レビュー、デバッグ助言 |
| `workspace-write` | 実装、修正、リファクタリング |

## Integration with Antigravity

| Task                                | Use                            |
| ----------------------------------- | ------------------------------ |
| 外部調査が必要                      | Antigravity → (必要なら) Codex |
| 実装タスクで route が codex         | Codex                          |
| 実装タスクで route が claude-direct | Claude direct                  |
| route が auto                       | タスク特性で選択               |

## Why This Skill

- config 変更だけで Codex の役割を切り替えられる
- エージェント定義とスキル文書の責務齟齬を防げる
- 将来のモデル評価変化（実装担当の入れ替え）に追従しやすい
