# Antigravity Suggestion Compliance

**`[Antigravity Suggestion]` は任意の助言であり、WebSearch / WebFetch の保留や追加相談を義務付けない。**

## 必須 routing との区別

`[Resolved Routing]` と `cli-tools.yaml` の `agents.<name>.tool` による明示的な routing、およびスキル固有の委譲義務は必須のまま。助言が任意であることを理由に、指定 CLI を試さず直接調査へ切り替えない。

## トリガー

`suggest-antigravity-research.py`（PreToolUse: WebSearch|WebFetch）は、`query` / `prompt` に比較・選定・移行等の調査意図がある場合に `[Antigravity Suggestion]` を出力する。明確な選択比較（`vs` / `vs.` / `versus` / `違い` / `どっち` / `which is better`）も対象とする。文字列等の演算比較や単純な migration エラー検索は除外するが、明確な選択比較が同居すればそちらを優先する。

URL や文字数、`documentation` / `library` / `guide` / `example` / `best practice` 等の一般語だけでは提案しない。`antigravity.enabled: false` の場合は出力しない（旧 `gemini.enabled` の後方互換は維持する）。

## 助言を受けたとき

- 相談済み・軽微・適用外の場合は、そのまま WebSearch / WebFetch を続行してよい。それ以外でも追加相談は任意であり、採否やスキップ理由を毎回説明する義務はない。
- 比較調査などに Antigravity が有用なら、相談内容を絞ってサブエージェント等へ依頼する。
- 相談する場合は `antigravity-delegation.md` / `antigravity-system` の既存手順に従う。`[Resolved Routing]` があればその解決結果を使い、なければ base + `.local.yaml` から enabled / tool / model 等を解決する。model allowlist、sandbox 内での実行・ネットワーク許可、非対話実行、リトライとフォールバックの条件を維持する。

## 通知の抑制と相談済み判断

- トップレベルの `agent_id` が非空の文字列（`str`）の場合だけ、サブエージェント／インプロセス teammate 内と判定して提案を抑制する。`agent_type` は `--agent` で起動したメインにも付くため、単独では抑制しない。`tool_input.subagent_type` は呼出先であり、実行中のエージェントの判定には使わない。
- config・対象範囲・marker に使う project / worktree は、環境変数 `CLAUDE_PROJECT_DIR` が非空ならそのルートを使い、未設定または空の場合だけ hook 入力の `cwd` で解決する。
- `session_id` があれば、同じ project / worktree 内の同一 session・正規化した同一質問への通知は 1 度だけとする。`.claude/state/suggestions/` に session / 対象を hash 化した空 marker のみを置き、質問本文や URL、生の識別情報は保存しない。marker の自動削除は今回追加せず、終了後も session ディレクトリが残る。cleanup は将来課題とする。
- 状態書き込みに失敗した場合は無出力・exit 0 とする。`session_id` がなければ state を作らず、その呼び出しごとの助言として扱う。
- **通知済みは相談済みを意味しない。** 意味的に同じトピックについて相談済みかどうかはオーケストレーターが判断し、hook は transcript を解析しない。
- 次回監査では researcher の指定 CLI 試行率とメインの任意提案の採用率を分離する。サブエージェント内の通知は `agent_id` で抑制し、researcher の `agy` 実行は `researcher.md` の指示が担うため、hook 通知数を researcher の遵守率の代用にしない。
