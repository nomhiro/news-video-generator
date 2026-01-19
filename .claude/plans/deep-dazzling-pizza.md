# 生成AIフォーカスのニュース収集機能追加

## 概要
既存のGoogle News RSSベースのニュース取得機能に、生成AI（Generative AI）にフォーカスした検索機能を追加する。既存の8カテゴリを維持しつつ、新しい「AI・生成AI」カテゴリを追加する。

## 実装方針
Google News RSSの検索クエリ機能（`/search?q=...`）を使用し、AI関連のキーワードで日本語・英語両方のニュースを取得する。

## 変更対象ファイル

### 1. [src/models/news.py](src/models/news.py) - NewsCategoryにAIカテゴリ追加
**変更内容:**
- `NewsCategory` enumに `AI = "ai"` を追加
- `display_name` プロパティに日本語名「AI・生成AI」を追加

```python
class NewsCategory(str, Enum):
    AI = "ai"  # 新規追加
    POLITICS = "politics"
    # ... (既存のカテゴリ)

    @property
    def display_name(self) -> str:
        names = {
            "ai": "AI・生成AI",  # 新規追加
            "politics": "政治",
            # ...
        }
```

### 2. [config.py](config.py) - AI検索設定の追加
**変更内容:**
- AIニュース用の検索クエリリストを設定に追加
- クエリごとの取得件数制限を追加

```python
@dataclass
class Config:
    # ... 既存の設定 ...

    # AI News Settings
    ai_search_queries: List[str] = field(default_factory=lambda: [
        "生成AI",
        "ChatGPT",
        "Claude AI",
        "Claude Code",
        "Gemini AI",
        "GitHub Copilot",
        "大規模言語モデル LLM",
        "OpenAI",
        "Anthropic",
        "Stable Diffusion",
        "Midjourney",
        "画像生成AI",
    ])
    ai_news_limit_per_query: int = 5  # クエリごとに5件取得
```

環境変数からの読み込みも追加:
```python
# .envから読み込み
AI_SEARCH_QUERIES=生成AI,ChatGPT,Claude AI,Gemini AI,LLM
AI_NEWS_LIMIT_PER_QUERY=5
```

### 3. [src/news/sources/google_news.py](src/news/sources/google_news.py) - 検索クエリ対応
**変更内容:**
- `fetch_by_search()` メソッドを追加（検索クエリでニュース取得）
- `fetch_ai_news()` メソッドを追加（複数クエリからAIニュースを取得）
- `CATEGORY_TOPICS` に `NewsCategory.AI` のマッピングを追加

```python
async def fetch_by_search(
    self, query: str, category: NewsCategory, limit: int = 10
) -> List[NewsArticle]:
    """検索クエリでニュース記事を取得する。"""
    from urllib.parse import quote
    encoded_query = quote(query)
    url = f"{self.BASE_URL}/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"
    # ... HTTPリクエスト処理 ...

async def fetch_ai_news(
    self, search_queries: List[str], limit_per_query: int = 5
) -> List[NewsArticle]:
    """AI関連ニュースを複数の検索クエリから取得する。"""
    # 全クエリを並行実行し、重複を除去して返す
```

### 4. [src/news/aggregator.py](src/news/aggregator.py) - AIニュース取得メソッド追加
**変更内容:**
- `fetch_ai_news_and_store()` メソッドを追加
- AIカテゴリ用のJSON保存ロジック追加

```python
async def fetch_ai_news_and_store(
    self, search_queries: List[str], limit_per_query: int = 5
) -> List[NewsArticle]:
    """AI関連ニュースを取得してJSONに保存する。"""
    # fetch_ai_news() を呼び出し、ai.json に保存
```

### 5. [src/web/routes.py](src/web/routes.py) - `/news/fetch` エンドポイント修正
**変更内容:**
- `fetch_news()` 関数で、通常カテゴリ取得に加えてAIニュースも取得

```python
@router.post("/news/fetch", response_class=HTMLResponse)
async def fetch_news(request: Request, aggregator: NewsAggregator = Depends(get_aggregator)):
    config = get_config()

    # 通常カテゴリを取得
    await aggregator.fetch_and_store()

    # AIニュースも取得（追加）
    await aggregator.fetch_ai_news_and_store(
        config.ai_search_queries,
        config.ai_news_limit_per_query
    )
    # ...
```

### 6. [.env.example](.env.example) - 設定例の追加
**変更内容:**
- AI検索設定の例を追加

```bash
# AI News Settings
AI_SEARCH_QUERIES=生成AI,ChatGPT,Claude AI,Claude Code,Gemini AI,GitHub Copilot,LLM,OpenAI,Anthropic
AI_NEWS_LIMIT_PER_QUERY=5
```

## 検索クエリ（デフォルト）

| クエリ | 目的 |
|--------|------|
| 生成AI | 日本語での生成AI全般 |
| ChatGPT | OpenAIのChatGPT関連 |
| Claude AI | AnthropicのClaude関連 |
| Claude Code | Anthropicのコーディングアシスタント |
| Gemini AI | GoogleのGemini関連 |
| GitHub Copilot | GitHubのAIコーディングアシスタント |
| 大規模言語モデル LLM | LLM全般 |
| OpenAI | OpenAI社関連 |
| Anthropic | Anthropic社関連 |
| Stable Diffusion | 画像生成AI |
| Midjourney | 画像生成AI |
| 画像生成AI | 日本語での画像生成AI全般 |

## 実装順序

1. **config.py** - AI検索設定を追加（リスクなし）
2. **src/models/news.py** - AIカテゴリをenumに追加
3. **src/news/sources/google_news.py** - 検索クエリ対応メソッド追加
4. **src/news/aggregator.py** - AIニュース取得・保存メソッド追加
5. **src/web/routes.py** - fetchエンドポイントでAIニュースも取得
6. **.env.example** - 設定例を更新

## 検証方法

1. **Web UIでの動作確認**
   ```bash
   python web_app.py --port 8000
   ```
   - ブラウザで http://localhost:8000 にアクセス
   - カテゴリタブに「AI・生成AI」が表示されることを確認
   - 「ニュース取得」ボタンをクリック
   - AI関連のニュースが取得されることを確認

2. **保存データの確認**
   - `data/news/ai.json` が作成されることを確認
   - 記事の内容がAI関連であることを確認

3. **記事選択と動画生成**
   - AIカテゴリの記事を選択
   - 動画生成が正常に動作することを確認

## 備考
- 既存の8カテゴリ（政治、テクノロジー等）の動作には影響しない
- 検索クエリは環境変数でカスタマイズ可能
- 重複記事は自動的に除去される（URLベースのID）
