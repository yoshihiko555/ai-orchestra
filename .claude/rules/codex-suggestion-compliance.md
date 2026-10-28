# Codex Suggestion Compliance

**`[Codex Suggestion]` は任意の助言であり、編集の保留や追加相談を義務付けない。**

## 必須 routing との区別

- `[Resolved Routing]` と `cli-tools.yaml` の `agents.<name>.tool` による明示的な routing は必須のまま。助言が任意であることを理由に、指定 CLI を省略したり、実行先を直接編集へ切り替えたりしない。
- スキル固有の implementation subagent への委譲義務も維持する。助言の採否と、実装を誰が担当するかは別の判断である。

## トリガー

`check-codex-before-write.py`（PreToolUse: Edit|Write）は、次の設計・契約シグナルに限定して `[Codex Suggestion]` を出力する:

- basename が `DESIGN.md` / `ARCHITECTURE.md` のファイル、または `design/`・`architecture/`・`schema/`・`schemas/`・`interfaces/`・`contracts/` 等の明確な設計パスへの変更
- `interface` / `abstract class` / `Protocol` / `ABC` / `@abstractmethod` 等の契約宣言の追加・削除・変更。ただし TS/JS の `interface` は通常パスではコードシグナルとせず、設計パス内の変更としてのみ対象とする。`Props` という名前だけでは除外しない

`core` / `config` / `model`、通常の `class`、文字数、`src/` だけでは提案しない。Edit は `old_string` / `new_string` の契約宣言が同じならコードシグナルを抑制する。

設計パス以外のソース（`.py` / `.pyi` / `.ts` / `.tsx` / `.js` / `.jsx` / `.java`）への Write は、ディスク上の旧本文と新本文の契約宣言を比較し、同じならコードシグナルを抑制する。新規ファイルの旧本文は空とする。旧本文が読取不能・大き過ぎ・非通常ファイル・非 UTF-8 の場合は任意通知を抑制し、exit 0 とする。設計パスは旧本文比較と独立に判定する。

プロジェクト外のファイル（scratchpad・メモリ・他リポジトリ等）や `codex.enabled: false` の場合も出力しない。

## 助言を受けたとき

- 相談済み・軽微・適用外の場合は、そのまま編集を続行してよい。それ以外でも追加相談は任意であり、採否やスキップ理由を毎回説明する義務はない。
- 設計上の不確実性があり相談が有用なら、相談内容を絞ってサブエージェント等へ依頼する。
- 相談する場合は `codex-delegation.md` / `codex-system` の既存手順に従う。`[Resolved Routing]` があればその解決結果を使い、なければ base + `.local.yaml` から enabled / tool / model / sandbox / flags を解決する。sandbox の無効化条件、内側 sandbox と flags の安全性検証、prompt の安全な受け渡し、非対話実行条件を省略しない。

## 通知の抑制と相談済み判断

- トップレベルの `agent_id` が非空の文字列（`str`）の場合だけ、サブエージェント／インプロセス teammate 内と判定して提案を抑制する。`agent_type` は `--agent` で起動したメインにも付くため、単独では抑制しない。`tool_input.subagent_type` は呼出先であり、実行中のエージェントの判定には使わない。
- config・対象範囲・marker に使う project / worktree は、環境変数 `CLAUDE_PROJECT_DIR` が非空ならそのルートを使い、未設定または空の場合だけ hook 入力の `cwd` で解決する。
- `session_id` があれば、同じ project / worktree 内の同一 session・同一ファイルへの通知は 1 度だけとする。`.claude/state/suggestions/` に session / 対象を hash 化した空 marker のみを置き、質問本文や生の識別情報は保存しない。marker の自動削除は今回追加せず、終了後も session ディレクトリが残る。cleanup は将来課題とする。
- 状態書き込みに失敗した場合は無出力・exit 0 とする。`session_id` がなければ state を作らず、その呼び出しごとの助言として扱う。
- **通知済みは相談済みを意味しない。** 意味的に同じ変更について相談済みかどうかはオーケストレーターが判断し、hook は transcript を解析しない。

## 実装委譲との境界

変更箇所の累積数（「3 箇所以上」等）だけを根拠に、この助言から一律の委譲義務は発生させない。`issue-fix` / `tdd` / `review-respond` 等のスキルが定める実装委譲は、それぞれの手順に従う。

`check-codex-after-plan.py` の発火条件と既存の advisory としての扱いは変更しない。
