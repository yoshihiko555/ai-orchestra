# ECサイト レビュー機能 — 要件定義・設計

## 前提の整理

まず、既存ECサイトへの追加要件として以下を確認します。

- **対象ユーザー**: ログイン済みユーザーのみ投稿可能か、ゲストも可能か
- **購入者限定か**: 実際に購入した商品のみレビュー可能にするか
- **1商品1レビューか**: 同一ユーザーが同一商品に複数回投稿できるか
- **モデレーション**: 投稿前に管理者承認を挟むか、即時公開か
- **既存技術スタック**: バックエンド言語/フレームワーク、DBは何か

以下はよくある前提（購入者限定・1商品1レビュー・即時公開）で進めます。

---

## 1. 要件定義

### 機能要件

#### レビュー投稿

- 購入済み商品に限り、ログインユーザーがレビューを投稿できる
- 星評価（1〜5）とテキスト（任意）を入力できる
- 1ユーザー × 1商品 = 1レビューのみ（重複投稿不可）
- 投稿後は編集・削除が可能

#### レビュー表示

- 商品詳細ページに、その商品のレビュー一覧を表示する
- 平均評価（星）と件数を商品詳細ページのサマリに表示する
- 新着順・評価順などで並び替えできる
- ページネーションまたは無限スクロールで大量件数に対応する

#### 管理機能（オプション）

- 管理者が不適切なレビューを非表示にできる

### 非機能要件

- 表示レスポンス: 商品詳細ページのレビュー読み込みを 500ms 以内
- 可用性: 既存ECサイトの SLA に準拠
- セキュリティ: XSS対策（テキスト入力のサニタイズ）、CSRF対策

---

## 2. データモデル設計

```
users（既存）
  id, email, ...

products（既存）
  id, name, ...

orders（既存）
  id, user_id, ...

order_items（既存）
  id, order_id, product_id, ...

reviews（新規）
  id             : PK
  user_id        : FK → users.id
  product_id     : FK → products.id
  rating         : TINYINT (1-5)  NOT NULL
  body           : TEXT           NULL
  is_visible     : BOOLEAN        DEFAULT true
  created_at     : TIMESTAMP
  updated_at     : TIMESTAMP
  UNIQUE(user_id, product_id)
```

**インデックス**:
- `(product_id, is_visible, created_at)` — 商品別一覧取得
- `(user_id, product_id)` — 重複投稿チェック（UNIQUE制約で兼用）

**集計テーブル（任意・パフォーマンス最適化）**:

```
product_review_summaries（新規）
  product_id     : FK, PK
  review_count   : INT
  rating_sum     : INT
  average_rating : DECIMAL(3,2)
  updated_at     : TIMESTAMP
```

平均評価を毎回集計するとN件スキャンが発生するため、レビュー投稿/更新/削除時にサマリを更新する設計を推奨します。

---

## 3. API設計（REST）

| メソッド | パス | 説明 |
|---------|------|------|
| GET | `/api/products/{id}/reviews` | レビュー一覧取得（ページネーション付き） |
| POST | `/api/products/{id}/reviews` | レビュー投稿（要認証・購入済み確認） |
| PUT | `/api/products/{id}/reviews/{review_id}` | レビュー編集（投稿者本人のみ） |
| DELETE | `/api/products/{id}/reviews/{review_id}` | レビュー削除（投稿者本人 or 管理者） |

### GET /api/products/{id}/reviews

**クエリパラメータ**:
- `sort`: `newest` (default) / `highest` / `lowest`
- `page`: ページ番号
- `per_page`: 件数（デフォルト10）

**レスポンス例**:
```json
{
  "summary": {
    "average_rating": 4.2,
    "review_count": 128
  },
  "reviews": [
    {
      "id": 1,
      "rating": 5,
      "body": "とても良い商品でした。",
      "user_name": "田中 太郎",
      "created_at": "2026-03-01T12:00:00Z"
    }
  ],
  "pagination": {
    "current_page": 1,
    "total_pages": 13,
    "total_count": 128
  }
}
```

### POST /api/products/{id}/reviews

**リクエストボディ**:
```json
{
  "rating": 4,
  "body": "品質が良く満足しています。"
}
```

**バリデーション**:
- `rating`: 1〜5の整数（必須）
- `body`: 最大1000文字（任意）

**エラーレスポンス**:
- 401: 未認証
- 403: 購入履歴なし
- 409: 既にレビュー済み
- 422: バリデーションエラー

---

## 4. フロントエンド設計

### 商品詳細ページへの追加コンポーネント

```
<ProductDetailPage>
  ├── <ProductInfo> （既存）
  ├── <ReviewSummary>        ← 新規: 平均評価・件数の表示
  ├── <ReviewForm>           ← 新規: 投稿フォーム（購入済みの場合のみ表示）
  └── <ReviewList>           ← 新規: レビュー一覧
        ├── <ReviewSortControls>
        └── <ReviewItem> × n
```

### ReviewForm（投稿フォーム）

- 星評価: クリック/タップで選択できるインタラクティブな星UI
- テキスト: textarea（文字数カウンター表示）
- 送信ボタン: 送信中はローディング表示
- 購入未済のユーザーには「この商品を購入するとレビューできます」を表示

### ReviewSummary

- 大きな星アイコン + 平均評価数値（例: ★ 4.2）
- 件数（例: 128件のレビュー）
- 評価分布バー（1〜5星の割合）

---

## 5. 認可・セキュリティ設計

| チェック項目 | 実装方針 |
|------------|---------|
| 未認証ユーザー | 投稿APIに認証ミドルウェアを適用。閲覧は誰でも可 |
| 購入者確認 | `order_items` と `orders` を結合し user_id + product_id で確認 |
| 重複投稿防止 | DB の UNIQUE 制約 + APIで 409 を返す |
| XSS対策 | テキストはHTMLエスケープしてから保存/表示 |
| CSRF対策 | 既存サイトのCSRFトークン機構を流用 |
| 本人確認 | 編集/削除は `review.user_id == current_user.id` をサーバー側で確認 |

---

## 6. 実装フェーズ案

| フェーズ | 内容 | 目安工数 |
|---------|------|---------|
| Phase 1 | DB migration（reviews テーブル追加） | 0.5日 |
| Phase 2 | バックエンドAPI実装（CRUD + 認可） | 2〜3日 |
| Phase 3 | フロントエンド実装（一覧・フォーム） | 2〜3日 |
| Phase 4 | サマリ集計（summaryテーブル or SQLビュー） | 1日 |
| Phase 5 | テスト・QA | 1〜2日 |

---

## 7. 確認したい事項（次のアクション）

設計を進める前に確認させてください。

1. **購入者限定** にするか、ログインユーザー全員が投稿可能か
2. **モデレーション** は必要か（即時公開 or 管理者承認後公開）
3. **既存の技術スタック**（バックエンド言語・フレームワーク・DB）
4. **スター評価のUI**: 半星（0.5刻み）は必要か、整数のみか
5. **レビュー画像添付** は将来的に必要か（DB設計に影響）

これらが確定すれば、より詳細な設計・実装計画に進められます。
