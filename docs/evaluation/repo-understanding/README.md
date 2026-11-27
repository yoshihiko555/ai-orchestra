# リポジトリ把握の比較評価

`questions.yaml` は場所の特定・流れの追跡・設定の解決・ファイル横断の 4 種類各 5 問を持つ。
各問の `expected_points` が採点の要点、`evidence` が path・開始行・終了行・その範囲に含まれる文字列である。
根拠は作成時に実ファイルで照合する。コード変更後に check が失敗したら、根拠を読み直してから更新する。
行番号だけを機械的にずらし、正解の意味を未確認のまま維持しない。

## API 不要の確認

このリポジトリのルートで、Python 3.12 と開発依存を使う。

```bash
AI_ORCHESTRA_DIR="$PWD" uv run --no-project --python 3.12 --with-editable ".[dev]" python tests/experiments/repo_understanding_eval.py check
AI_ORCHESTRA_DIR="$PWD" uv run --no-project --python 3.12 --with-editable ".[dev]" python -m pytest -q tests/unit/test_repo_understanding_eval.py
```

YAML は `questions` 配列を持つ。各問は一意な `id`、`category`（location / flow / configuration / cross_file）、
`question`、1 件以上の `expected_points` と `evidence` を指定する。
ID は英数字・ハイフン・下線だけとし、出力先のパス要素へ安全に使える形にする。

## 実際の比較の前提

**このタスクでは実行しない。** 人がキーを設定し、外部呼び出しと費用を了承した後に実行する。
`--confirm-real-api` はその了承を表す。フラグなしでは実行しない。

- Claude CLI をインストールし、`claude -p` が使えるようにログインする。
- `orchex[llm]` を導入し、対象 provider のキーは `orchex auth set <provider>` で設定する。キーを問題・コマンド引数・報告へ書かない。
- `.claude/config/agent-routing/cli-tools.local.yaml` に、対象 provider と `tools.repo_read: true` を持つプロファイルを用意する。
  例として既存 research をもとに別名を作る場合は、次のようにする。base/local のマージ規則は設定リファレンスに従う。

```yaml
api:
  profiles:
    eval-reader:
      provider: gemini
      tools:
        repo_read: true
      deny:
        - docs/evaluation/repo-understanding/**
      limits:
        max_turns: 30
        max_duration_sec: 900
        timeout_sec: 120
```

評価用の正解と出力を探索に混ぜない。対象方式では上の deny で問題セットを除外し、
両方式の prompt でも評価資料を読まないように指示する。基準方式のパス単位の除外は強制していないため、
正解を参照した形跡は抜き取り時に確認し、比較の限界として記録する。実行出力はリポジトリ外に置く。

## 実行方式

- **baseline**: 対象リポジトリを cwd として `claude -p --tools Read,Grep,Glob --output-format stream-json --verbose` を実行する。
  hook を無効化し、MCP 設定を限定し、セッションを永続化しない。正解要点は探索 prompt へ渡さない。
- **agent**: 同じ問題を `llm agent --profile … --project … --output …` へ渡す。
  スクリプト内では worktree の取り違えを避けるため、この checkout の Python と `scripts/orchestra-manager.py` を使う。
  これは `orchex llm agent` と同じ CLI の入口である。実効プロファイルの許可・deny・上限を変更しない。
- **採点**: 道具なしの `claude -p` と JSON Schema を使い、問題・正解要点・回答を渡す。
  `correct` と短い `reason` を受け取る。採点失敗と結果欠落は不正答として理由を残す。partial も回答があれば採点する。

```bash
# OUT は毎回新しく作る。人の了承後にだけ実行する。
OUT=$(mktemp -d "${TMPDIR:-/tmp}/repo-eval.XXXXXX")
AI_ORCHESTRA_DIR="$PWD" uv run --no-project --python 3.12 --with-editable ".[dev,llm]" python tests/experiments/repo_understanding_eval.py run --profile eval-reader --out "$OUT" --confirm-real-api
```

`--project` と `--questions` で対象を明示できる。新規または空の `--out` だけを受け付け、
`agent/<問題ID>/` を問題ごとに作る。使用済みの結果を消さずに拒否する。
各探索・採点コマンドの時間上限は `--timeout-sec`（既定 1800 秒）。
実効プロファイルの `max_duration_sec + 2 × timeout_sec` より大きい値を設定する。
既定値を超える長時間プロファイルでは、この CLI オプションも増やす。

## 集計と根拠の扱い

- 正答率は正答数 / 全問題数。実行・採点の失敗を分母から除かない。
- 未確認割合は verified が false の根拠数 / 根拠総数。問題ごとの割合の平均ではない。根拠ゼロは値なし。
- agent は `citations[].verified` を使う。baseline は成功した Read の tool_use と tool_result を対応付け、
  offset（既定 1）・limit（既定 2000）を実ファイルの総行数で切って `ReadLedger` に記録する。
  既存の `verify_citations` と `RepoReadTools.check_citation` で同じ 3 条件（実在・範囲・読取記録）を確認する。
  Grep・Glob の結果は記録しない。Read の実際の表示切詰めと完全に一致することまでは保証しない近似である。
- baseline の入力トークンは input + cache_creation + cache_read、出力は output。usage の欠落フィールドは 0 とする。
- 時間は探索開始から結果の読込み・根拠確認までの壁時計。採点時間・採点トークンを含めない。全体と種類別の数字を出す。
- 採用は「対象の正答率 ≥ 基準の正答率 × 0.9」かつ「対象の未確認割合 ≤ 0.1」。境界は整数比で比較する。
  対象の根拠がゼロなら採用しない。verified は主張の意味的な正しさを保証しない。

## 出力と人による確認

`results.json` に問題・正解・両方式の回答・採点・status・stop_reason・終了コード・集計を保存する。
`spot-check.md` は抜き取り確認の一覧、`report-draft.md` は `report-template.md` から作る報告書の下書き。
モデル・プロファイル・ソースのコミット・実行条件を記入し、各種類と partial / 失敗を含めて抜き取り、採点との一致を確認する。

```bash
# 保存済み結果からの再生成。API は呼ばない。
AI_ORCHESTRA_DIR="$PWD" uv run --no-project --python 3.12 --with-editable ".[dev]" python tests/experiments/repo_understanding_eval.py report --results "$OUT/results.json" --out "$OUT/report"
```

実評価の報告書は `docs/evaluation/` の下に置く。未実行の数値を埋めたり、機械判定だけで人の採用判断を代替したりしない。
