## 共通参照順序

提案や判断を行う前に、次の順で確認してください。

1. `README.md`（プロジェクト範囲、パッケージ構成、利用コマンド）
2. `.claude/config/agent-routing/cli-tools.yaml` と任意の `.claude/config/agent-routing/cli-tools.local.yaml`（実効ルーティング/モデル設定）
3. `.claude/rules/`（`orchestra-usage.md` / `config-loading.md` / `coding-principles.md` などの運用制約）
4. `.claude/orchestra.json`（対象プロジェクトで有効化されているパッケージ状態）

運用メモ:

- `*.local.yaml` / `*.local.json` はベース設定より優先される上書きとして扱う
