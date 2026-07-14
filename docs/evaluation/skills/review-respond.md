# review-respond 評価セット（スキルフロー）

**対象スキル群**: `/review-respond`（正本: `facets/instructions/review-respond.md`、所属パッケージ: `packages/git-workflow`）
**単位**: スキルフロー（手動で出した PR に対する bot レビュー指摘を検出→収集→分類→修正→push→返信/resolve→報告まで一気通貫で処理する単一スキルの一連の振る舞い）
**作成日**: 2026-07-14
**最終レビュー日**: 未レビュー（draft、パッケージ実装前に作成）
**情報源**: `.claude/Plans.md`（Project: review-respond スキル。要件整理・Decisions・Notes）, 本タスクの仕様定義（`/review-respond` フロー・`pr_review_threads.py` コントラクト）, `docs/evaluation/git-workflow.md`（`pr_review_threads.py` の API 契約：EV-20〜EV-29）, `packages/loop-harness/lib/pr_review_wait.py`（`verify_origin`/`classify_severity` の再利用元・severity 分類 fail-safe 挙動）, `.claude/rules/skill-review-policy.md`, `.claude/rules/codex-suggestion-compliance.md`（累積的変更の原則）

> **パッケージ評価セットとの違い**: スキルは Markdown 指示書であり pytest で強制できない。
> この評価セットは「振る舞い仕様書」として機能し、テストコードとの突合（`evaluation-set-policy`
> ルールの MUST 手順）の対象外。検証手段は下記「4. 検証方法」に従う。

> **`docs/evaluation/git-workflow.md`（パッケージ評価セット）との責務境界**: パッケージ評価セットは
> `pr_review_threads.py` の `detect`/`fetch`/`reply`/`resolve` サブコマンド自体の決定論的な API 契約
> （bot 識別の fail-closed 挙動、unresolved のみを対象にする冪等性の基盤、ファイル経由の本文渡し、
> `isResolved` の確認等）を pytest で検証可能な単位として扱う（EV-20〜EV-29）。本ファイルは、その API を
> **呼び出す側であるオーケストレーター（Claude Code セッション、`/review-respond` 指示文）**が、収集した
> 指摘をどう分類し、どの指摘を採用・非採用と判断し、修正実装をどう委譲し、返信と resolve をどの順序・
> 条件で行い、全指摘に対応漏れを残さないかを対象とする。`pr_review_threads.py` 側の API 契約自体が
> 正しいことは前提とし、ここでは再検証しない。

## 1. フロー責務定義

`/review-respond` は、ユーザーが手動で作成・push 済みの PR に対して bot（CodeRabbit / Codex 等）が投稿した
レビュー指摘を、引数なしで起動し単発実行で全自動処理するスキルである。カレントブランチから対象 PR を
自動検出し、`pr_review_threads.py` で unresolved な bot 起因の指摘のみを収集し、severity と対応方針
（採用/非採用）を決定し、採用した指摘は implementation subagent へ委譲して修正・テストしてから commit・
push し、すべての指摘（採用・非採用いずれも）に返信を残したうえで resolve 可能なものは resolve し、最後に
対応結果のサマリーをユーザーへ報告する。冪等性は GitHub 上の unresolved スレッド状態のみを SSOT とし、
ローカルに実行状態を持たないことで保証する。

### Non-Goals

- `pr_review_threads.py` の API 契約自体の正しさ（`docs/evaluation/git-workflow.md` EV-20〜EV-29 の責務）
- reviewer allowlist（`.claude/config/loop-harness/loop-harness.local.yaml` 等）の設定・運用そのもの（loop-harness 側の責務。本スキルは allowlist が未設定なら中断するだけで、設定自体は行わない）
- 反復監視型（バックグラウンド常駐でのポーリング）実行。v1 は単発実行型で確定しており、常駐実行は将来拡張（別途 Issue 化）の対象
- レビュー指摘の技術的妥当性判断そのもののドメイン知識（採用/非採用の最終判断ロジックは実装エージェント・スキル指示文の責務であり、本評価セットは「判断のプロセス」を対象とする）
- PR の新規作成自体（`pr-create` の責務。本スキルは既存 PR の追加コミットのみを扱う）

## 2. 期待するフローと成果物

| ステップ | フェーズ                       | 入力                                                             | 期待する成果物・振る舞い                                                                 |
| -------- | ------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| 1        | PR 自動検出                    | カレントブランチ（引数なし）                                       | `pr_review_threads.py detect` で一意特定した PR 番号。0 件/複数件は中断報告                |
| 2        | 指摘収集                       | 検出済み PR 番号                                                   | `pr_review_threads.py fetch --pr N` による unresolved review threads + bot issue comments 一覧 |
| 3        | 分類                           | 収集した各指摘（severity 明示/未確定を含む）                       | severity 確定（明示マーカー採用 or LLM 判断）と対応方針（採用/非採用）の決定               |
| 4        | 修正実装                       | 採用指摘一覧                                                       | implementation subagent への委譲による修正、テスト実行                                    |
| 5        | commit & push                  | 修正後の worktree 差分                                             | commit・push の実行（スキル明示実行を承認とみなす。追加確認なし）                          |
| 6        | 返信 + resolve                 | 各指摘の対応方針・修正内容                                          | 採用: 対応内容を返信し resolve／非採用: 理由を返信し resolve／issue comment: 返信のみで完了 |
| 7        | サマリー報告                   | 全指摘の処理結果                                                    | 対応済み/非対応の一覧と理由をユーザーへ日本語で報告                                        |

## 3. 評価観点

<!-- ID はファイル内一意の連番（欠番は再利用しない）。分類（正常/異常/境界）と優先度（must/should）、
     仕様根拠（.claude/Plans.md / 本タスク仕様 / facets/instructions/review-respond.md）を併記する。
     1 観点 = 1 振る舞い。検証手段（PR レビュー / 実行観察 / config-analyze）を付記する -->

### PR 自動検出

- [ ] EV-01（正常 / must）: PR 番号の引数なしで起動し、カレントブランチから `pr_review_threads.py detect` により対象 PR を自動特定する — 根拠: `.claude/Plans.md` Notes「PR 番号は引数不要」 / 検証: 実行観察
- [ ] EV-02（異常 / must）: 対象 PR が 0 件、または複数件ヒットして一意に特定できない場合は、推測で 1 件を選ばず処理を中断してユーザーへ報告する — 根拠: 本タスク仕様「見つからなければ中断報告」, `docs/evaluation/git-workflow.md` EV-20 / 検証: 実行観察

### 指摘収集と bot 識別

- [ ] EV-03（異常 / must）: bot 識別は `pr_review_threads.py fetch` が返す結果（loop-harness の `reviewer_allowlist` による fail-closed 判定済み）をそのまま用い、オーケストレーター側でユーザー名パターンマッチ等の独自 bot 判定ロジックを実装・上書きしない — 根拠: 本タスク仕様「bot 識別は loop-harness の reviewer_allowlist」 / 検証: PR レビュー
- [ ] EV-04（異常 / must）: `fetch` が allowlist 未設定により exit code 2 で失敗した場合、黙って全件を bot 扱い・空扱いにして処理を続行せず、セットアップ案内（`.claude/config/loop-harness/loop-harness.local.yaml` への `reviewer_allowlist` 設定）を提示してフローを中断する — 根拠: 本タスク仕様「未設定時は exit 2 でセットアップ案内」, `docs/evaluation/git-workflow.md` EV-21 / 検証: 実行観察
- [ ] EV-05（境界 / should）: `fetch` が `origin_verified: false`（loop-harness import 不能によるフォールバック）を返した場合、bot/human 混在の可能性を考慮せず全件を無条件に bot 起因として処理しない。ユーザーへの明示、または処理継続前の追加確認を検討する — 根拠: `docs/evaluation/git-workflow.md` EV-22（`fetch` フォールバック契約） / 検証: 実行観察

### 分類（severity + 対応方針）

- [ ] EV-06（正常 / must）: `fetch` が明示マーカーで severity を確定できた指摘はその結果をそのまま採用し、`needs_classification`（決定論分類で確定できない）指摘のみを LLM 判断ステップに回す。確定済みの指摘まで一律に LLM 再判定へかけ直さない — 根拠: 本タスク仕様「severity（スクリプトの決定論分類 → 不能分は LLM 判断）」, `docs/evaluation/git-workflow.md` EV-25 / 検証: 実行観察
- [ ] EV-07（正常 / must）: 対応方針（採用/非採用）は severity とは独立した軸として各指摘ごとに個別判断する（severity が低くても妥当な指摘は採用し、severity が高くても誤検知・スコープ外なら非採用にする、という組み合わせを許容する） — 根拠: 本タスク仕様「分類: severity と対応方針を決定」 / 検証: PR レビュー

### 修正実装

- [ ] EV-08（正常 / must）: 採用指摘の修正はメインオーケストレーターが直接大量編集せず、implementation subagent（`Task(subagent_type=...)`）へ委譲する。3 箇所以上の変更が見込まれる通常の実装作業として `codex-suggestion-compliance.md` の累積的変更の原則に従う — 根拠: 本タスク仕様「修正実装（implementation subagent 委譲）」, `.claude/rules/codex-suggestion-compliance.md`「累積的変更の原則」 / 検証: PR レビュー
- [ ] EV-09（正常 / must）: 修正実装後は必ずテストを実行し、失敗したテストが残った状態のまま commit & push フェーズへ進まない — 根拠: 本タスク仕様「テスト実行」 / 検証: 実行観察

### commit & push

- [ ] EV-10（境界 / must）: commit・push はユーザーへの追加確認（`AskUserQuestion`）を挟まずに実行してよい。ただしこれは「スキルの明示実行自体が承認とみなされる」旨が `facets/instructions/review-respond.md`（SKILL.md 相当）に明記されていることを前提とし、この明記が欠けたまま無確認 push を行わない — 根拠: 本タスク仕様「commit & push（スキルの明示実行を承認とみなす旨を SKILL.md に明記）」 / 検証: PR レビュー（指示文への明記確認）

### 返信 + resolve

- [ ] EV-11（正常 / must）: 採用した指摘は対応内容の要約を返信で投稿してからスレッドを resolve する（resolve を先に行い返信が後追いになる順序にしない） — 根拠: 本タスク仕様「採用 → 対応内容を返信しスレッドを resolve」 / 検証: 実行観察
- [ ] EV-12（正常 / must）: 非採用（誤検知・不同意・スコープ外）の指摘は判断理由を返信してから resolve する。理由を書かずに resolve のみで済ませない — 根拠: 本タスク仕様「非採用（誤検知/不同意/スコープ外）→ 判断理由を返信して resolve」 / 検証: PR レビュー
- [ ] EV-13（境界 / must）: issue comment 形式（`resolve` 不可）の指摘は返信のみで完了扱いとし、`resolve` API の呼び出し対象から除外する — 根拠: 本タスク仕様「issue comment 形式（resolve 不可）は返信のみで完了扱い」, `docs/evaluation/git-workflow.md` EV-27 / 検証: 実行観察
- [ ] EV-14（異常 / must）: `fetch` で収集した全指摘（採用・非採用を問わず）に対して、返信または判断理由のいずれかのレスポンスを必ず残す。処理漏れ（無反応のまま放置される指摘）を発生させない — 根拠: 本タスク仕様「全指摘に何らかのレスポンス（対応内容 or 理由）が残る」 / 検証: 実行観察
- [ ] EV-15（異常 / should）: 返信本文を投稿する前に、修正内容の要約に含まれうる秘匿情報（API キー・トークン等）やスタックトレースの生ログをそのまま貼り付けず、PR コメントとして公開されることを前提に内容を整形する — 根拠: `.claude/rules/coding-principles.md`「機密情報をログに出力しない」を PR コメントの公開性に適用 / 検証: PR レビュー

### 冪等性（再実行）

- [ ] EV-16（境界 / must）: 再実行時、GitHub 上で既に resolved 済みのスレッドは `fetch` の対象外となるため、再度返信・修正・resolve を行わない。ローカルに実行状態を持たず、GitHub 側の unresolved 状態のみを再処理判定に使う — 根拠: 本タスク仕様「冪等性: 再実行時に resolved 済み指摘を再処理しない」, `.claude/Plans.md` Decisions「冪等性は GitHub 上の unresolved スレッド状態を SSOT とし、ローカル state を持たない」 / 検証: 実行観察

### 実行形態

- [ ] EV-17（境界 / should）: このスキルは単発実行型であり、バックグラウンドでのポーリングや反復監視を行わない。1 回の起動で現在の unresolved 状態全体を 1 パスで処理して終了する — 根拠: `.claude/Plans.md` Decisions「v1 は単発実行型で確定。反復監視型は…将来拡張（Issue 化して保留）」 / 検証: 実行観察

### サマリー報告

- [ ] EV-18（正常 / must）: 実行完了後、対応済み（採用・修正内容）と非対応（非採用・理由、または issue comment 完了）の一覧をユーザーへ日本語で報告する — 根拠: 本タスク仕様「サマリー報告（対応済み/非対応と理由の一覧）」, `.claude/rules/orchestra-usage.md`「ユーザーへの報告: 日本語」 / 検証: 実行観察

## 4. 検証方法

スキルフローは pytest で強制できないため、以下の手段で観点との整合を確認する:

1. **スキル改修 PR のレビュー時**: `facets/instructions/review-respond.md` への変更が本評価セットの観点と矛盾しないか突合する。矛盾する仕様変更の場合は、本評価セットを先に更新して人間レビューを経る
2. **`/config-analyze`**: スキル指示文のルーブリック評価・トリガーテストで観点の記述漏れを検出する
3. **実行観察**: 実際の `/review-respond` 実行（dogfooding。`.claude/Plans.md` Phase 4 で予定している `feat/auto-review-fix` の PR への実地検証を含む）で観点どおりに振る舞ったかを確認する

## 5. レビュー判断基準（フロー固有）

- **allowlist fail-closed の実効性**（EV-04）: `facets/instructions/review-respond.md` の改修時、`fetch` が exit code 2 を返した場合の分岐が「処理を中断してセットアップ案内を出す」以外の経路（黙って空扱いで続行する等）に変わっていないかを重点確認する。fail-open への後退は bot/human 混同のリスクに直結する
- **返信と resolve の順序**（EV-11・EV-12）: 修正実装や返信フォーマットの改修時、「返信 → resolve」の順序が「resolve → 返信」に入れ替わっていないかを確認する。resolve 後は GitHub の通知挙動が変わりうるため、順序の逆転は指摘者（bot 運用者・人間レビュアー）への説明責任を損なう
- **全件レスポンス保証**（EV-14）: 指摘の絞り込み・フィルタリングロジックを追加する改修では、フィルタで除外された指摘が「非対応（理由あり）」としてサマリーに残るか、それとも黙って消えるかを確認する。黙って消える設計変更は EV-14 の観点に反する
- **冪等性の再検証**（EV-16）: ローカル state（キャッシュファイル・実行履歴等）を持ち込む改修提案が出た場合、`.claude/Plans.md` Decisions の「ローカル state を持たない」設計判断と矛盾しないか、GitHub 側の状態のみを SSOT とする原則が崩れていないかを重点確認する
- **implementation subagent 委譲の徹底**（EV-08）: 修正実装フェーズの改修時、メインオーケストレーターが直接 Edit/Write で大量修正する経路が紛れ込んでいないかを確認する（`codex-suggestion-compliance.md` の累積的変更の原則）
