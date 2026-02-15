# AI Orchestra 包括的レビュー結果

**実施日**: 2026-02-15
**レビュアー**: Claude Opus 4.6 (Architecture / Code / Security / Performance)

---

## 総合評価

| 観点 | 評価 | 概要 |
|------|------|------|
| アーキテクチャ | **要改善** | Claude Code への依存リスクが高い |
| コード品質 | **良好（改善余地あり）** | 基盤は堅実だが重複・エラー処理に課題 |
| セキュリティ | **要注意** | パス検証・入力バリデーション不足 |
| パフォーマンス | **許容範囲（最適化可能）** | PostToolUse で累積 15-28ms |

---

## Critical / High の指摘事項

### 1. Claude Code API 依存リスク（Architecture: Critical）

システム全体が Claude Code の hook API（SessionStart, UserPromptSubmit, PreToolUse, PostToolUse 等）に完全依存。

- **問題**: バージョニングなし、互換性レイヤーなし、フォールバックなし
- **影響**: `tool_input` スキーマ変更や hook イベント廃止で全体が破損
- **推奨**: `hook_common.py` に互換性抽象化レイヤーを追加し、SessionStart でバージョンチェック

### 2. コード重複（Code Review: High）

JSON 読み書き関数が3箇所、`find_first_text` が2箇所、`sys.path` 操作が4箇所で重複。

- `orchestration-bootstrap.py` / `orchestration-expected-route.py` / `orchestration-route-audit.py` で `read_json()` が別々に実装
- **推奨**: `hook_common.py` に `read_json_safe()`, `find_first_text()` を集約

### 3. エラーハンドリングの不統一（Code Review: High / Architecture: High）

```python
# パターン1: エラー内容が失われる
except Exception:
    sys.exit(0)

# パターン2: 部分的に記録
except Exception as e:
    print(f"Hook error: {e}", file=sys.stderr)
    sys.exit(0)
```

- 24個の hook で例外処理パターンがバラバラ
- **推奨**: `hook_common.py` に `safe_hook_execution` デコレーターを実装

### 4. パストラバーサル脆弱性（Security: Critical）

`orchestra-manager.py` の `file_path` に `../` を含めることでプロジェクト外にファイルを書き込める可能性。

- **推奨**: `validate_safe_path()` 関数を実装し全ファイル操作に適用

### 5. ハードコードされたモデル名（Code Review: Critical）

`log-cli-tools.py` でデフォルトモデル名が直接埋め込み。

```python
model = extract_model(command) or "gpt-5.2-codex"  # ハードコード
```

- **推奨**: config ファイルから動的に取得

### 6. 同期メカニズムの脆弱性（Architecture: High）

`sync-orchestra.py` が mtime 比較のみで同期判定。ユーザーカスタマイズの誤削除リスクもあり。

- **推奨**: SHA256 チェックサム検証 + `.orchestra-ignore` による除外機能

### 7. `/tmp` への状態保存（Code Review + Security: Medium-High）

`post-implementation-review.py` が `/tmp/claude-impl-review-state.json`、`tmux_common.py` が `/tmp/claude-session-info` を使用。

- マルチユーザー環境での情報漏洩リスク
- **推奨**: `.claude/state/` に集約し、`mode=0o700` で保護

---

## Medium の指摘事項

| # | 観点 | 内容 |
|---|------|------|
| 8 | Performance | config ファイルの重複読み込み（同一イベントで2フックが同じJSON を読む） |
| 9 | Performance | `os.makedirs(exist_ok=True)` が毎回実行される（キャッシュ推奨） |
| 10 | Code | `orchestra-manager.py` が1264行で巨大（分割推奨） |
| 11 | Code | `detect_route()` 関数が複雑（サブ関数に分割推奨） |
| 12 | Security | ログファイルに機密情報（プロンプト全文）が記録される |
| 13 | Architecture | パッケージ間の暗黙的依存（manifest.json に `runtime_deps` なし） |
| 14 | Code | テストカバレッジ不足（lint-on-save, post-implementation-review 等にテストなし） |

---

## パフォーマンス影響の概算

| イベント | フック数 | 推定レイテンシ | 頻度 |
|---------|---------|--------------|------|
| UserPromptSubmit | 2 | 13-25ms | 毎プロンプト |
| PostToolUse (Bash) | 3-4 | 15-28ms | 高頻度 |
| PostToolUse (Edit/Write) | 3-4 | 20-40ms | 中頻度 |
| SessionStart | 3 | 83-278ms | セッション開始時 |

---

## 良い点

- **循環依存なし**: パッケージ間の依存が core → 各パッケージの一方向
- **型ヒントの一貫性**: 新しいコードで `from __future__ import annotations` を使用
- **テストの質**: コアロジックに対して適切なテストが存在
- **命名規則**: Python 慣習に従った snake_case
- **モジュール分離**: `hook_common.py` / `log_common.py` / `tmux_common.py` で共通ロジックを適切に分離
- **設定レイヤー化**: base + `.local.json` の上書き構造が適切
- **hook 失敗の安全性**: `sys.exit(0)` パターンでシステム全体を停止させない

---

## 推奨アクション（優先度順）

### 短期（1-2週間）
1. `hook_common.py` に `read_json_safe()`, `find_first_text()`, `safe_hook_execution` デコレーターを集約
2. ハードコードされたモデル名を config 参照に変更
3. `validate_safe_path()` をファイル操作に適用
4. `/tmp` の状態ファイルを `.claude/state/` に移動

### 中期（1-2ヶ月）
5. Claude Code バージョンチェック + 互換性レイヤー導入
6. 同期メカニズムのチェックサム検証化
7. config 読み込みキャッシュの導入（パフォーマンス改善）
8. テストカバレッジ向上

### 長期（3-6ヶ月）
9. パッケージ間イベントバスの導入
10. `orchestra-manager.py` の分割リファクタリング
11. hook パフォーマンス計測の仕組み導入

---

## セキュリティチェックリスト

- [x] Injection (コマンド/パスインジェクション) - Critical
- [ ] Broken Authentication - N/A
- [x] Sensitive Data Exposure - Medium
- [x] Broken Access Control - High
- [x] Security Misconfiguration - Medium
- [x] Insufficient Input Validation - Critical
- [x] Insufficient Logging & Monitoring - Low
- [ ] Secrets in code - OK
- [ ] Hardcoded credentials - OK
