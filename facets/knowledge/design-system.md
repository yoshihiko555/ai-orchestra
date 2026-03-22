# デザインシステム設計

このドキュメントは `/design` スキルの拡張設計トラックとして、ユーザーの要望または AI の提案時に参照される。

## 目的

UIの一貫性と開発効率を担保するために、デザインシステム（Design System）を定義する。
コンポーネントライブラリ、Design Tokens、スタイルガイドを体系化し、実装者が迷わず UI を構築できる状態を作る。

## 前提

- 画面設計（`screen-list.md`）が存在する、または並行して進行中であること
- Web アプリまたはネイティブアプリなど、UI を持つプロジェクトであること

## いつ実施するか

以下のいずれかに該当する場合に実施を検討する：

- ユーザーが「デザインシステムを作りたい」「UIの統一ルールを決めたい」と明示した場合
- 画面数が多く（目安: 10画面以上）、UI の一貫性が課題になりそうな場合
- 複数人で並行してフロントエンド開発を行う場合
- 既存プロジェクトで UI の不統一が発生している場合

## ステップ

### Step 1: デザイン原則の定義

プロジェクトの UI に対する基本方針を決める。

#### ヒアリング項目

- 参考にしたいサービス・UI はあるか？（例: Linear, Notion, Stripe）
- ブランドカラーやロゴは既に決まっているか？
- モバイルファースト or デスクトップファーストか？
- ダークモード対応は必要か？
- アクセシビリティ要件（WCAG レベル）は？

#### 出力

デザイン原則を `docs/design-system/principles.md` に記載する。

```markdown
# デザイン原則

## ビジュアル原則

- {原則1: 例「シンプルさ — 情報密度を下げ、余白を活用する」}
- {原則2: 例「一貫性 — 同じアクションには同じ UI パターンを使う」}
- {原則3}

## レスポンシブ方針

- {モバイルファースト / デスクトップファースト}
- ブレークポイント: {sm: 640px, md: 768px, lg: 1024px, xl: 1280px}

## アクセシビリティ

- WCAG {レベル} 準拠
- {その他の方針}
```

---

### Step 2: Design Tokens の定義

UI の基礎値（色、フォント、スペーシング等）を Design Tokens として定義する。

#### 設計内容

| カテゴリ | 定義する内容 |
|---------|------------|
| Color | プライマリ、セカンダリ、セマンティック（success, warning, error, info）、グレースケール |
| Typography | フォントファミリー、サイズスケール、ウェイト、行間 |
| Spacing | スペーシングスケール（4px ベース等） |
| Border Radius | 角丸のサイズスケール |
| Shadow | エレベーションレベル（shadow-sm, shadow-md 等） |
| Z-index | レイヤー管理のスケール |

#### 出力

`docs/design-system/tokens.md` に記載する。

```markdown
# Design Tokens

## Color

### Brand

| Token | Value | 用途 |
|-------|-------|------|
| color-primary | {#hex} | メインアクション、ブランドカラー |
| color-primary-hover | {#hex} | ホバー状態 |
| color-secondary | {#hex} | サブアクション |

### Semantic

| Token | Value | 用途 |
|-------|-------|------|
| color-success | {#hex} | 成功状態 |
| color-warning | {#hex} | 警告状態 |
| color-error | {#hex} | エラー状態 |
| color-info | {#hex} | 情報表示 |

### Neutral

| Token | Value | 用途 |
|-------|-------|------|
| color-bg-primary | {#hex} | メイン背景 |
| color-bg-secondary | {#hex} | セカンダリ背景 |
| color-text-primary | {#hex} | メインテキスト |
| color-text-secondary | {#hex} | サブテキスト |
| color-border | {#hex} | ボーダー |

## Typography

| Token | Value | 用途 |
|-------|-------|------|
| font-family-base | {フォント名} | 本文 |
| font-family-heading | {フォント名} | 見出し |
| font-family-mono | {フォント名} | コード |
| font-size-xs | {rem} | 補足テキスト |
| font-size-sm | {rem} | ラベル・キャプション |
| font-size-base | {rem} | 本文 |
| font-size-lg | {rem} | サブ見出し |
| font-size-xl | {rem} | セクション見出し |
| font-size-2xl | {rem} | ページタイトル |
| line-height-tight | {値} | 見出し |
| line-height-normal | {値} | 本文 |

## Spacing

{4px ベースのスケール}

| Token | Value |
|-------|-------|
| space-1 | 4px |
| space-2 | 8px |
| space-3 | 12px |
| space-4 | 16px |
| space-6 | 24px |
| space-8 | 32px |
| space-12 | 48px |
| space-16 | 64px |

## Border Radius

| Token | Value | 用途 |
|-------|-------|------|
| radius-sm | {px} | 入力フィールド |
| radius-md | {px} | カード・ボタン |
| radius-lg | {px} | モーダル |
| radius-full | 9999px | アバター・バッジ |

## Shadow

| Token | Value | 用途 |
|-------|-------|------|
| shadow-sm | {値} | カード |
| shadow-md | {値} | ドロップダウン |
| shadow-lg | {値} | モーダル |
```

---

### Step 3: コンポーネント一覧の作成

UI で使用する共通コンポーネントを一覧化する。

#### 設計内容

- コンポーネント分類（Atoms / Molecules / Organisms — Atomic Design を参考に）
- 各コンポーネントの概要と使用場面

#### 出力

`docs/design-system/component-list.md` に記載する。

```markdown
# コンポーネント一覧

## Atoms（基本要素）

| ID | コンポーネント | 概要 | バリアント |
|----|-------------|------|----------|
| CMP-001 | Button | アクションボタン | primary, secondary, ghost, danger |
| CMP-002 | Input | テキスト入力 | text, password, email, number |
| CMP-003 | Badge | ステータス表示 | success, warning, error, info |
| CMP-004 | Icon | アイコン表示 | {アイコンセット名} |

## Molecules（組み合わせ）

| ID | コンポーネント | 概要 | 構成要素 |
|----|-------------|------|---------|
| CMP-010 | FormField | ラベル付き入力 | Label + Input + ErrorMessage |
| CMP-011 | SearchBar | 検索バー | Input + Button |

## Organisms（複合要素）

| ID | コンポーネント | 概要 | 構成要素 |
|----|-------------|------|---------|
| CMP-020 | Header | グローバルヘッダー | Logo + Navigation + UserMenu |
| CMP-021 | DataTable | データテーブル | Table + Pagination + Sort |
```

---

### Step 4: コンポーネント詳細設計（必要に応じて）

主要コンポーネントの詳細仕様を個別ファイルで定義する。
全コンポーネントに対して行う必要はなく、以下のようなケースで実施する：

- 複数バリアントを持つ複雑なコンポーネント
- プロジェクト固有のカスタムコンポーネント
- 状態管理が複雑なコンポーネント

#### 出力

`docs/design-system/CMP-{ID}.md` として個別ファイルを作成する。

```markdown
# CMP-001: Button

## 概要

アクションを実行するためのボタンコンポーネント。

## バリアント

| バリアント | 用途 | 背景色 | テキスト色 |
|----------|------|--------|----------|
| primary | メインアクション | color-primary | white |
| secondary | サブアクション | transparent | color-primary |
| ghost | テキストリンク風 | transparent | color-text-primary |
| danger | 破壊的操作 | color-error | white |

## サイズ

| サイズ | padding | font-size | height |
|--------|---------|-----------|--------|
| sm | space-2 space-3 | font-size-sm | 32px |
| md | space-2 space-4 | font-size-base | 40px |
| lg | space-3 space-6 | font-size-lg | 48px |

## 状態

| 状態 | 変化 |
|------|------|
| default | 通常表示 |
| hover | 背景色を暗く / 明るく |
| active | さらに暗く |
| focus | フォーカスリング表示 |
| disabled | opacity: 0.5, cursor: not-allowed |
| loading | スピナー表示、テキスト非表示 |

## Props

| Prop | 型 | 必須 | デフォルト | 説明 |
|------|-----|------|----------|------|
| variant | 'primary' | 'secondary' | 'ghost' | 'danger' | No | 'primary' | ボタンの種類 |
| size | 'sm' | 'md' | 'lg' | No | 'md' | サイズ |
| disabled | boolean | No | false | 無効化 |
| loading | boolean | No | false | ローディング状態 |
| icon | ReactNode | No | - | 左側アイコン |
| onClick | () => void | No | - | クリックハンドラ |

## アクセシビリティ

- `role="button"` を付与（`<button>` 要素使用時は不要）
- `disabled` 時は `aria-disabled="true"` を設定
- loading 時は `aria-busy="true"` を設定
- アイコンのみのボタンには `aria-label` を必須とする
```

---

## 出力ディレクトリ

```
docs/design-system/
├── principles.md        ← デザイン原則
├── tokens.md            ← Design Tokens
├── component-list.md    ← コンポーネント一覧
├── CMP-001.md           ← 個別コンポーネント詳細（必要に応じて）
├── CMP-002.md
└── ...
```

## ネイティブアプリでの考慮

ネイティブアプリの場合、以下の追加考慮が必要：

- **プラットフォームガイドライン**: iOS（HIG）と Android（Material Design）の準拠レベルを決める
- **クロスプラットフォーム**: Flutter/React Native 等の場合、プラットフォーム差異の吸収方針
- **ナビゲーションパターン**: タブバー、ドロワー、スタック等の標準パターン選択
- **ジェスチャー**: スワイプ、ロングプレス等のジェスチャー操作の定義

## 注意事項

- デザインシステムは完璧を目指さず、プロジェクトに必要な範囲で作成する
- 小規模プロジェクト（画面数 5 以下）では、Design Tokens と主要コンポーネントのリストで十分
- 既存の CSS フレームワーク（Tailwind, shadcn/ui 等）を使用する場合は、フレームワークの Tokens をベースにカスタマイズする方が効率的
- コンポーネント詳細設計（Step 4）は、実装フレームワークに依存する部分が大きいため、技術スタック確定後に行う
