# git-workflow 評価セット

**パッケージ**: `packages/git-workflow`
**類型**: スキル型（単独）
**作成日**: 2026-07-03
**最終レビュー日**: 2026-07-04（人間レビュー完了・指摘なし。評価観点の変更なし。テストギャップは Issue #132 で追跡）／EV-20〜EV-29（`pr_review_threads.py` 追加分、2026-07-14）は実装前の先行作成につき未レビュー（人間レビュー待ち）
**情報源**: docs/reference/packages.md, packages/git-workflow/manifest.json, facets/instructions/issue-create.md, facets/instructions/issue-fix.md, facets/instructions/pr-create.md, facets/policies/pr-standards.md, packages/git-workflow/scripts/resolve_base_branch.py, packages/git-workflow/config/sandbox-requirements.json, `.claude/Plans.md`（Project: review-respond スキル、Phase 2「pr_review_threads.py」の仕様・Decisions）, packages/loop-harness/lib/pr_review_wait.py（`verify_origin`/`classify_severity`/`_parse_reviewer_allowlist` の再利用元・fail-closed 挙動の踏襲元）

> **類型分類について**: タスク開始時の想定は「主: スキル型 + 副: hook 型」だったが、`packages/git-workflow/manifest.json` の `"hooks": {}` は空であり、本パッケージに hook コンポーネントは存在しない。よって類型は **スキル型（単独）** に修正した。`scripts/resolve_base_branch.py` は独立 CLI ではなく 3 スキルが共通利用する内部ユーティリティであり、CLI ツール型（README 定義: 「lib + scripts で提供されるコマンド」）の要件である独立コマンド性を満たさないため副類型としない。

## 1. 責務定義

git-workflow パッケージは、GitHub Issue の作成（`issue-create`）、Issue 起点の計画→実装→テスト→レビュー開発フロー（`issue-fix`）、Pull Request の作成（`pr-create`）という 3 スキルと、それらが共通利用する base branch 解決スクリプトを提供する。「正しい状態」とは、(1) PR の base branch が固定ハードコードされず multi-branch 構成（`main` + `stage` 等）でも常に resolver で解決されること、(2) PR/Issue の本文・ラベル・タイトルがプロジェクトのテンプレートと規約（PR Standards Policy）に沿って自動生成されること、(3) 破壊的操作（Issue 作成・コミット・PR 作成・ブランチ作成の分岐判断）の前に `AskUserQuestion` でユーザー確認を挟むこと、を保証している状態である。

### Non-Goals

- `gh` コマンドの認証設定そのもの（各スキルは認証済みであることを前提とする）
- worktree の作成自体（issue-fix は「作業ブランチが準備済みか」の判定のみを担当し、worktree 作成は別プロセスの責務）
- レビュー内容そのものの品質保証（`skill-review-policy.md` および各レビュアーサブエージェントの責務）
- `/review-respond` スキルのフロー仕様そのもの（PR 検出→収集→分類→修正→push→返信/resolve→報告の一連のオーケストレーション判断）。本ファイルは `pr_review_threads.py` 単体の API 契約のみを対象とし、フロー全体は `docs/evaluation/skills/review-respond.md` の責務

## 2. 期待する入出力・副作用

| 構成要素                           | 入力                                                                          | 期待する出力                                                                           | 副作用                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| skill `issue-create`               | `$ARGUMENTS`（種類+タイトル or 空）、`gh label list` の結果                   | 種類別テンプレートで構成された Issue 本文とプレビュー、確認後の `gh issue create` 実行 | GitHub Issue 作成、未存在ラベルの `gh label create`                         |
| skill `issue-fix`                  | `$ARGUMENTS`（Issue 番号 or 空）、`gh issue view` の結果                      | 計画提示→承認→実装→テスト→レビュー→コミット（→任意で PR 作成）の一連の実行結果         | ブランチ作成（未準備時のみ）、ファイル変更、`git commit`、（選択時）PR 作成 |
| skill `pr-create`                  | `--base` / `--issue` / `--reviewers` 引数、git/gh の現在状態                  | 解決済み `$BASE` に基づく PR タイトル・本文・ラベル、`gh pr create` の実行結果         | `git push -u`、GitHub PR 作成                                               |
| script `resolve_base_branch.py`    | `--base` 引数、環境変数 `AI_ORCHESTRA_BASE_BRANCH`、ローカル/リモート git ref | stdout に解決済み base branch 名（`origin/` プレフィックス除去済み）を 1 行            | なし（読み取り専用の git 呼び出しのみ）                                     |
| script `pr_review_threads.py`（計画中。`/review-respond` が利用） | サブコマンド（`detect`/`fetch --pr N`/`reply --pr N --comment-id ID --body-file F`/`resolve --thread-id ID`）と gh/GraphQL API 応答 | `detect`/`fetch` は JSON（PR 番号、unresolved review threads + bot issue comments、`origin_verified`）、`reply`/`resolve` は投稿・解決結果 | GitHub へのコメント投稿（review thread reply / issue comment）、`resolveReviewThread` mutation によるスレッド解決 |
| config `sandbox-requirements.json` | （実行時入力なし。導入時に読み込まれる静的設定）                              | `gh` を sandbox `excludedCommands` として宣言                                          | 導入先プロジェクトの sandbox 設定への反映（config-loading 経由）            |

## 3. 評価観点

- [ ] EV-01（正常 / must）: `pr-create` は base branch を「`--base` 明示 > 環境変数 `AI_ORCHESTRA_BASE_BRANCH` > 自動推定 > fallback `main`」の優先順で解決し、以降の差分収集・プレビュー・`gh pr create` のすべてで解決済み `$BASE` を使う — 根拠: facets/policies/pr-standards.md（Base Branch Resolution）, facets/instructions/pr-create.md
- [ ] EV-02（境界 / must）: multi-branch 構成での自動推定は、候補（`staging`/`stage`/`develop`/`main`/`master`、remote 優先）ごとに `merge-base(candidate, HEAD)` から candidate tip までのコミット数を計算し最小値のものを選ぶ。同距離の場合は候補リストの先頭優先（`staging` > `stage` > `develop` > `main` > `master`）で tie-break する — 根拠: packages/git-workflow/scripts/resolve_base_branch.py（`_distance_to_tip` docstring）, pr-standards.md 検証手順表
- [ ] EV-03（異常 / must）: `AI_ORCHESTRA_DIR` が未設定の場合、resolver 呼び出し前のガードで即座に失敗し、`$BASE` が空のまま `gh pr create --base ""` が実行される事故を防ぐ — 根拠: facets/policies/pr-standards.md
- [ ] EV-04（異常 / must）: 解決済み `$BASE` と現在ブランチが一致する場合（base branch 上で `pr-create` を実行した場合）はエラーで終了し、PR 作成対象ブランチへの移動を案内する — 根拠: facets/instructions/pr-create.md（Step 1-1）
- [ ] EV-05（異常 / must）: `$BASE..HEAD` のコミットが 0 件の場合はエラーで終了する — 根拠: facets/instructions/pr-create.md（Step 1-2）
- [ ] EV-06（正常 / must）: PR テンプレートは `.github/PULL_REQUEST_TEMPLATE.md` を最優先とし、なければ `gh api .../community/profile` のテンプレート、それも無ければ pr-standards.md のフォールバックテンプレートを使用する — 根拠: facets/instructions/pr-create.md（Step 1-4）, facets/policies/pr-standards.md
- [ ] EV-07（正常 / must）: PR のラベルはブランチプレフィックス対応表（`fix/`→`bug`, `feat/`→`enhancement`, `docs/`→`documentation`, `chore/`→`task`, `refactor/`→`refactor`, `test/`→`task`, `task/`→`task`, `release/`→`task`, その他→`task`）に従い自動決定される — 根拠: facets/policies/pr-standards.md（ブランチプレフィックスとラベルの対応）
- [ ] EV-08（正常 / must）: PR タイトルのプレフィックスは同じ対応表のタイトルプレフィックス列（`fix:`/`feat:`/`docs:`/`chore:`/`refactor:`/`test:`/`chore:`/`release:`/`chore:`）に従い、Issue がある場合は Issue タイトルベース、ない場合はコミット履歴要約ベースで `{prefix}: {要約}` 形式になる — 根拠: facets/instructions/pr-create.md（Step 2-1）, pr-standards.md
- [ ] EV-09（正常 / should）: 同一ブランチに既存の open PR がある場合、`AskUserQuestion` で「既存 PR を開く」/「新規 PR を作成」を選択させる — 根拠: facets/instructions/pr-create.md（Step 1-3）
- [ ] EV-10（正常 / must）: `--issue` 指定時、PR 本文冒頭に `Closes #{番号}` を追加する — 根拠: facets/instructions/pr-create.md（Step 2-2）, pr-standards.md（Issue 連携）
- [ ] EV-11（境界 / must）: `issue-fix` Phase 2-1 で「worktree 内で実行している（`git rev-parse --git-dir` ≠ `--git-common-dir`）」または「現在ブランチが解決済み `$BASE` と異なる」のいずれかを満たす場合は「準備済み」と判定し、追加のブランチ作成を行わず現在ブランチをそのまま採用する — 根拠: facets/instructions/issue-fix.md（Phase 2-1）
- [ ] EV-12（異常 / should）: `$BASE` の解決が失敗し空になった場合、現在ブランチが統合ブランチ相当（`main`/`master`/`develop`/`stage`/`staging`）なら「未準備」としてブランチを作成し、それ以外（既に feature ブランチ等）は「準備済み」とみなしてスキップする安全側の判断を行う — 根拠: facets/instructions/issue-fix.md（Phase 2-1「安全側の判断」）
- [ ] EV-13（正常 / must）: `issue-fix` のブランチ作成（未準備時のみ実行するフォールバック）は Issue のラベル（`bug`→`fix/`, `feature`→`feat/`, `task`→`chore/`, その他→`fix/`）からプレフィックスを決定し、`{prefix}issue-{番号}-{slug}` の形式で作成する — 根拠: facets/instructions/issue-fix.md（Phase 2-1 フォールバック）
- [ ] EV-14（正常 / must）: `issue-fix` Phase 4-4 でレビュー指摘が Critical の場合は必ず Phase 2 に戻って修正してから先のフェーズに進む（High はユーザー確認、Medium 以下は素通り） — 根拠: facets/instructions/issue-fix.md（Phase 4-4）, `.claude/rules/skill-review-policy.md`
- [ ] EV-15（正常 / should）: `issue-fix` Phase 1-4 で実装計画がユーザーに承認されるまで Phase 2（実装）に進まない — 根拠: facets/instructions/issue-fix.md（Phase 1-4, 注意事項）
- [ ] EV-16（正常 / must）: `issue-create` は種類（`bug`/`feature`/`task`）に応じたラベルを `gh issue create --label` で自動付与し、未存在のラベルは事前に `gh label create` を試みる。ラベル作成が失敗（権限不足等）しても Issue 作成自体は続行する — 根拠: facets/instructions/issue-create.md（Step 4, 注意事項）

### `pr_review_threads.py`（`/review-respond` スキルが利用する内部スクリプト）

> **`docs/evaluation/skills/review-respond.md` との責務境界**: 以下は `pr_review_threads.py` 単体の決定論的な
> API 契約（サブコマンドの入出力・fail-closed 挙動）を対象とし、pytest でモック（`gh`/GraphQL レスポンス
> フィクスチャ）により検証可能な単位として扱う。`/review-respond` スキルがこの API をどの順序・条件で
> 呼び出し、分類結果や修正内容をどう扱うか（オーケストレーション側の振る舞い）は `skills/review-respond.md`
> の責務であり、本ファイルでは再掲しない。

- [ ] EV-20（境界 / must）: `detect` はカレントブランチから対象 PR を一意に特定し PR 番号を含む JSON を返す。ヒットが 0 件または複数件で一意に特定できない場合は非ゼロ exit code で終了し、呼び出し側が PR 番号を推測で補完できないようにする — 根拠: 仕様（`.claude/Plans.md` Project: review-respond スキル, Notes「PR 番号は引数不要」）
- [ ] EV-21（異常 / must）: `fetch --pr N` は bot 識別に loop-harness の `reviewer_allowlist`（`pr_review_wait.py` の `_parse_reviewer_allowlist`）を再利用し、allowlist が未設定または空の場合は黙って全投稿者を許可対象にせず、exit code 2 とセットアップ案内で失敗する（fail-closed） — 根拠: 仕様, packages/loop-harness/lib/pr_review_wait.py（`_parse_reviewer_allowlist` の `ConfigError` 挙動）
- [ ] EV-22（境界 / must）: `fetch` は loop-harness の allowlist ロジックを import できない環境では、黙って全件を bot 起因として扱う代わりに `origin_verified: false` を明示したうえで全件を返すフォールバックを取り、呼び出し側が bot/human 混在の可能性を判断できる状態を維持する — 根拠: 仕様（本タスク定義「fetch のコントラクト」）
- [ ] EV-23（正常 / must）: `fetch` が収集する review thread は unresolved のもののみで、既に resolved なスレッドは対象から除外される。ローカル state を持たず GitHub 上の unresolved 状態を都度参照するため、これが再実行時の冪等性（resolved 済み指摘の再処理防止）の基盤になる — 根拠: 仕様（`.claude/Plans.md` Decisions「冪等性は GitHub 上の unresolved スレッド状態を SSOT とし、ローカル state を持たない」）
- [ ] EV-24（異常 / must）: allowlist に一致しない投稿者（人間のレビューコメントを含む）による指摘は `fetch` の返却対象から除外され、bot 起因の指摘のみが後続の分類・修正フローに渡る — 根拠: 仕様（Notes「対象は bot レビューのみ（人間のレビューコメントは触らない）」）
- [ ] EV-25（境界 / must）: `fetch` は severity の明示マーカーがない指摘を、無条件で low/軽微とみなさず「要 LLM 判断」（`classify_severity` の `fail_safe`/`classification_required` 相当）として返す。決定論分類で確定できない指摘を安全側でなく楽観側に倒さない — 根拠: 実装挙動（packages/loop-harness/lib/pr_review_wait.py `classify_severity` の `fail_safe` パス）を踏襲する仕様
- [ ] EV-26（正常 / must）: `reply --pr N --comment-id ID --body-file F`（`--issue-comment` 指定時も同様）は本文をコマンドライン引数へ直接展開せず、必ずファイル経由（`--body-file`）で受け取る — 根拠: 仕様（本タスク定義「本文は必ずファイル渡し」）
- [ ] EV-27（境界 / must）: `reply --issue-comment` は review thread への reply とは別の投稿先（issue comment API）に投稿し、`resolve` の対象にしない。resolve 不可な指摘は返信のみで完了扱いとする設計と整合させる — 根拠: 仕様（`.claude/Plans.md` Decisions「resolveReviewThread は inline review thread のみ対象。issue comment 形式の bot 指摘は返信のみで完了扱い」）
- [ ] EV-28（正常 / must）: `resolve --thread-id ID` は `resolveReviewThread` mutation を実行した後、レスポンスの `isResolved` を確認してから成功と判定する。mutation の実行自体だけをもって成功とみなさない — 根拠: 仕様（本タスク定義「resolve のコントラクト」）
- [ ] EV-29（正常 / should）: `detect`/`fetch` の JSON 出力スキーマ（フィールド名・型）は安定しており、`/review-respond` スキルがテキスト整形やアドホックな正規表現抽出に頼らず構造化データとしてそのまま消費できる — 根拠: README.md「CLI ツール型」類型別観点（出力の安定性）に準拠
- [ ] EV-30（異常 / must）[2026-07-20 追加（Issue #235 PR #276 レビュー）]: `fetch --project-dir DIR`（および loop-harness からの in-process 呼び出し `fetch_review_threads(pr_number, project_dir, timeout)`）が対象リポジトリを解決する `gh repo view` は、呼び出しプロセスの実際の作業ディレクトリではなく `project_dir` を cwd として実行される。呼び出し元プロセスの cwd が `project_dir` と異なる場合（例: loop worktree 外から `--project` 指定で起動）でも、無関係な別リポジトリのスレッドを取得しない — 根拠: `packages/git-workflow/scripts/pr_review_threads.py` の `resolve_repo`/`fetch_review_threads`、対応テスト: `test_fetch_review_threads_resolves_repo_with_project_dir_cwd`
- [ ] EV-31（異常 / must）[2026-07-20 追加（Issue #235 PR #276 レビュー）]: `fetch` が 1 スレッドあたり取得するコメント件数（`THREAD_COMMENTS_PAGE_SIZE`）を超えるスレッドについても、後続ページを追加の GraphQL 呼び出しで取得し尽くしてからスレッドを bot/human 混在判定（`has_non_bot_comments`）にかける。先頭ページのみで判定を打ち切らないため、ページ境界より後ろに付いた人間の返信を bot 発信元と誤認しない — 根拠: `packages/git-workflow/scripts/pr_review_threads.py` の `_fill_thread_comments`/`THREAD_COMMENTS_QUERY`、対応テスト: `test_fetch_paginates_thread_comments_past_first_page`
- [ ] EV-32（異常 / must）[2026-07-20 追加（Issue #235 PR #276 レビュー、coderabbit minor）]: `fetch` の後続コメントページ取得中に対象スレッドが削除・非公開化され、GraphQL が `errors` なしで `data.node: null` を返すケースでも `TypeError` を発生させず、`cmd_fetch` の JSON エラー契約（`GhCommandError` 経由の `{"error": ...}` 出力）を破らない。ページングをその時点で打ち切り、null 応答より前に取得済みのコメントは保持する — 根拠: `packages/git-workflow/scripts/pr_review_threads.py` の `_fill_thread_comments`、対応テスト: `test_fetch_thread_comments_pagination_stops_on_null_node`

## 4. 類型別観点

<!-- スキル型チェックリスト（対話規約 / 非対話完結性 / フォールバック / ルーティング尊重 / 成果物規約）を具体化 -->

- [ ] EV-17（境界 / must）（対話規約）: 3 スキルとも重要な意思決定点（Issue 種類・タイトルの不足確認、Issue 作成前プレビュー確認、実装計画の承認、PR 作成前プレビュー確認、既存 PR がある場合の分岐、レビュー High 指摘対応）で `AskUserQuestion` を使用し、確認なしに Issue 作成・コミット・PR 作成・ブランチ作成分岐といった破壊的操作を進めない — 根拠: facets/instructions/issue-create.md（Step 1, Step 3）, facets/instructions/issue-fix.md（Phase 1-4, Phase 4-4, Phase 4-6）, facets/instructions/pr-create.md（Step 1-3, Step 3）, facets/compositions/skills/*.yaml（`policies: dialog-rules` 参照）
- N/A: 非対話完結性 — git-workflow の 3 スキルは Codex/Antigravity CLI を直接呼び出さない（`gh`/`git`/`python3 resolve_base_branch.py` の呼び出しのみ）。`issue-fix` Phase 2-2 の `Task(subagent_type=...)` によるエージェント委譲はあるが、外部 CLI の stdin 封じ・タイムアウト制御はその委譲先エージェントと `cli-tools.yaml` 側の責務であり、本パッケージの対象コンポーネントではない
- N/A: フォールバック — 上記と同じ理由で、Codex/Antigravity 不能時の claude-direct フォールバックは本パッケージの責務範囲外
- [ ] EV-18（正常 / should）（ルーティング尊重）: `issue-fix` Phase 2-2 で変更が 3 箇所以上の場合、implementation agent への `Task` プロンプトに「`cli-tools.yaml` の設定に従い実装すること」という指示を含め、エージェント側のルーティング設定を尊重させる — 根拠: facets/instructions/issue-fix.md（Phase 2-2）
- [ ] EV-19（境界 / should）（成果物規約）: `issue-fix` が新規作成するブランチ名は `{prefix}issue-{番号}-{slug}`（`slug` は Issue タイトルから生成する英語 kebab-case、最大 30 文字）の命名規則に従う — 根拠: facets/instructions/issue-fix.md（Phase 2-1 フォールバック）

## 5. テストレビュー判断基準（パッケージ固有）

- `resolve_base_branch.py` は決定的な純粋ロジック（git ref 探索・距離計算・優先順位）を持つため、`tests/unit/test_resolve_base_branch.py` のような git fixture ベースの単体テストで EV-01〜EV-03 を直接検証できる。新規テストがこのスクリプトに触れる場合、まずこの既存テストとの重複がないか確認する
- `issue-create` / `issue-fix` / `pr-create` の本体はプロンプト形式の指示書であり実行コードを持たないため、EV-04〜EV-19 の多くは「指示書の記述が観点を満たしているか」を静的にレビューする形になる。実行トレース（トランスクリプト）ベースのテストを追加する場合は、`gh`/`git` をモックし、決定表（ラベル対応表・ブランチプレフィックス表）の入力網羅を優先する
- ラベル・プレフィックス対応表（EV-07, EV-08, EV-13, EV-16）はテーブル駆動テストで全行を網羅すること。一部の代表値のみのテストは gap として扱う
- PR Standards Policy の「検証手順」表（`main` only / `main`+`stage` 分岐 / tie-break / 明示指定 / 環境変数）は EV-01, EV-02 の境界値セットとして完全に対応させる
