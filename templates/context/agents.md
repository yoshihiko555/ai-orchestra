# <YOUR_PROJECT_NAME>

**概要**: <YOUR_PROJECT_DESCRIPTION>

<!-- この AGENTS.md は Claude Code / Codex CLI / Antigravity CLI が共通で読む指示書です。末尾の ai-orchestra ブロックより上がプロジェクト固有の記述欄です（orchex がここを書き換えるのは初回作成時と --force 実行時だけです）。CLAUDE.md を置くと Claude Code はこのファイルを読まなくなるため、指示はこのファイルに集約してください。 -->

---

## 目的

<!-- TODO: プロジェクトの目的をここに記載してください -->

- <YOUR_GOAL_1>
- <YOUR_GOAL_2>
- <YOUR_GOAL_3>

---

## 技術スタック

<!-- TODO: プロジェクトの技術スタックをここに記載してください -->

- **Language**: <YOUR_LANGUAGE>
- **Framework**: <YOUR_FRAMEWORK>
- **Quality**: <YOUR_TEST_AND_LINT_TOOLS>
- **Config**: <YOUR_CONFIG_FORMAT>

---

## 主要コマンド

<!-- TODO: プロジェクト固有のコマンドをここに記載してください（変更後の検証に使われます）-->

```bash
# 依存インストール
<YOUR_INSTALL_COMMAND>

# テスト
<YOUR_TEST_COMMAND>

# Lint / Format
<YOUR_LINT_COMMAND>
```

---

## ディレクトリ構成

<!-- TODO: プロジェクトのディレクトリ構成をここに記載してください -->

```text
<your-src>/          # メインソースコード
<your-tests>/        # テストコード
.claude/             # AI Orchestra の実行コンテキスト（agents/config は sync、skills/rules は facet build）
```

---

## プロジェクト固有のルール

<!-- TODO: 変更ガードレールやパス別のレビュー観点など、プロジェクト固有のルールをここに記載してください -->

- <YOUR_RULE_1>
