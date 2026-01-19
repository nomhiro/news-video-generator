# 推奨コマンド

## 開発環境セットアップ
```bash
# 仮想環境作成・有効化
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Mac/Linux

# 依存関係インストール
pip install -r requirements.txt
```

## 実行コマンド

### Web UI起動
```bash
python web_app.py
# または開発モード（自動リロード）
python web_app.py --reload
```

### CLI実行
```bash
python main.py "ニュース内容" -l ja
python main.py "ニュース内容" -l ja en  # 日英両方
```

## テスト

### ニュース取得テスト
```python
import asyncio
from src.news.sources.google_news import GoogleNewsSource
from src.models.news import NewsCategory

async def test():
    source = GoogleNewsSource()
    articles = await source.fetch_category(NewsCategory.TECHNOLOGY, limit=5)
    for a in articles:
        print(f"- {a.title}")

asyncio.run(test())
```

## Windows固有コマンド
```bash
# FFmpegインストール
winget install FFmpeg

# 環境変数確認
echo %GOOGLE_CLOUD_PROJECT%
```
