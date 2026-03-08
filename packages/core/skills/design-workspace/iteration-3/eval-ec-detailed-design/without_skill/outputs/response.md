# ECサイト レビュー機能 詳細設計 — 初回回答

## 状況整理

基本設計ドキュメントが以下の3点あることを確認しました:

- `docs/api/api-list.md` — エンドポイント一覧
- `docs/screens/screen-list.md` — 画面一覧
- `docs/database/er-design.md` — ER設計

これらを起点に、各エンドポイントと各画面の**個別設計書**を作成していきます。

---

## 進め方の提案

### ステップ1: 既存ドキュメントの内容確認

まず3つの基本設計書を読み込み、レビュー機能に関係する:

- APIエンドポイント一覧（CRUD操作の洗い出し）
- 画面一覧（レビュー投稿・閲覧・管理画面）
- テーブル構造（`reviews` テーブルとリレーション）

を把握します。内容を共有していただければ、より具体的な設計書を作成できます。

### ステップ2: 個別設計書の構成

以下の構成で各設計書を作成することを推奨します。

---

## 推奨ドキュメント構成

```
docs/
  api/
    api-list.md              （既存）
    endpoints/
      POST_reviews.md        — レビュー投稿
      GET_reviews.md         — レビュー一覧取得
      GET_reviews_{id}.md    — レビュー詳細取得
      PUT_reviews_{id}.md    — レビュー編集
      DELETE_reviews_{id}.md — レビュー削除
      POST_reviews_{id}_helpful.md  — 参考になったフラグ
  screens/
    screen-list.md           （既存）
    pages/
      review-form.md         — レビュー投稿・編集画面
      review-list.md         — レビュー一覧（商品詳細内）
      review-management.md   — 管理者レビュー管理画面
  database/
    er-design.md             （既存）
```

---

## APIエンドポイント個別設計書テンプレート

各エンドポイントに以下の項目を記載します:

```markdown
# POST /api/v1/reviews — レビュー投稿

## 概要
| 項目 | 内容 |
|------|------|
| メソッド | POST |
| パス | /api/v1/reviews |
| 認証 | 必須（購入済みユーザーのみ） |
| 概要 | 商品に対するレビューを投稿する |

## リクエスト

### ヘッダー
| ヘッダー | 必須 | 説明 |
|---------|------|------|
| Authorization | 必須 | Bearer {JWT token} |
| Content-Type | 必須 | application/json |

### ボディ
```json
{
  "product_id": "string（必須）",
  "rating": "integer（必須, 1-5）",
  "title": "string（必須, 最大100文字）",
  "body": "string（任意, 最大2000文字）",
  "images": ["string（URL, 任意, 最大5枚）"]
}
```

### バリデーション
| フィールド | ルール |
|-----------|--------|
| product_id | 存在するproduct_idであること、かつ購入履歴があること |
| rating | 1〜5の整数 |
| title | 1〜100文字 |
| body | 0〜2000文字 |
| images | 0〜5件、各URLは有効な形式 |

## レスポンス

### 成功（201 Created）
```json
{
  "id": "string",
  "product_id": "string",
  "user_id": "string",
  "rating": 4,
  "title": "良い商品でした",
  "body": "...",
  "images": [],
  "created_at": "2026-03-08T00:00:00Z"
}
```

### エラー
| ステータス | コード | 説明 |
|-----------|--------|------|
| 400 | VALIDATION_ERROR | バリデーション失敗 |
| 401 | UNAUTHORIZED | 未認証 |
| 403 | NOT_PURCHASED | 購入履歴なし |
| 409 | ALREADY_REVIEWED | 同商品に既にレビュー済み |

## ビジネスロジック
1. JWTからuser_idを取得
2. 購入履歴テーブルで `product_id` と `user_id` の組み合わせを確認
3. 同一ユーザーの同一商品レビューが存在しないことを確認
4. レビューをDBに保存
5. 商品の平均評価を非同期で再計算（またはDBトリガー）

## 関連テーブル
- `reviews`（主テーブル）
- `orders` / `order_items`（購入履歴確認）
- `products`（平均評価更新）
```

---

## 画面個別設計書テンプレート

```markdown
# レビュー投稿・編集画面

## 概要
| 項目 | 内容 |
|------|------|
| 画面ID | SCR-REV-001 |
| 画面名 | レビュー投稿・編集 |
| URL | /products/{product_id}/reviews/new |
| 認証 | 必須 |

## 表示条件
- 対象商品を購入済みのログインユーザーのみアクセス可
- 既にレビュー済みの場合は編集フォームを表示

## UI要素
| 要素 | 種別 | 説明 |
|------|------|------|
| 評価（星） | Radio（星5段階） | 必須 |
| タイトル | TextInput | 必須、最大100文字 |
| 本文 | Textarea | 任意、最大2000文字 |
| 画像アップロード | FileInput | 任意、最大5枚 |
| 送信ボタン | Button | バリデーション後にAPI呼び出し |

## イベント・ユーザーアクション
| アクション | 処理 |
|-----------|------|
| 星評価クリック | `rating` ステート更新 |
| 送信ボタン押下 | フロントバリデーション → POST /api/v1/reviews |
| 送信成功 | 商品詳細ページへリダイレクト（レビュー一覧にスクロール） |
| 送信失敗 | エラーメッセージをフォーム内に表示 |

## 状態管理
```typescript
type ReviewFormState = {
  productId: string;
  rating: number | null;      // 1-5
  title: string;
  body: string;
  images: File[];
  isSubmitting: boolean;
  errors: Record<string, string>;
};
```

## 呼び出すAPI
- `GET /api/v1/products/{id}` — 商品情報表示用
- `POST /api/v1/reviews` — レビュー投稿
- `PUT /api/v1/reviews/{id}` — レビュー編集（編集時）

## エラー表示
| エラー | 表示場所 | メッセージ例 |
|--------|---------|------------|
| rating未選択 | 星の下 | 「評価を選択してください」 |
| title空 | titleフィールド下 | 「タイトルを入力してください」 |
| 未購入 | ページトップ | 「この商品を購入した方のみレビューできます」 |
```

---

## 次のアクション候補

以下のいずれかを選んで進めることができます:

1. **既存ドキュメントを共有** → 内容に合わせた具体的な設計書を生成
2. **エンドポイント一覧から着手** → APIエンドポイントの個別設計書を順番に作成
3. **画面一覧から着手** → 画面設計書を先に整備
4. **テンプレートの確定** → チームで合意したフォーマットに調整してから量産

---

## 確認したいこと

設計書作成をスムーズに進めるため、以下を教えてください:

- **技術スタック**: バックエンド言語（Python/Go等）、フロントエンドフレームワーク（React/Next.js等）
- **認証方式**: JWT / セッション等
- **設計書の保存形式**: Markdown / Confluence / Notion等
- **レビュー機能の範囲**: 購入者限定か、画像投稿の有無、管理者承認フローの有無
- **`api-list.md` に記載されているエンドポイント数**: 規模感の把握のため
