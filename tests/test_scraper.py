"""記事本文抽出の検証。

ネットワークには出ない。HTTP 部分をモックし、抽出とエラー処理の
振る舞いだけを見る。実サイトからの抽出は `-m live` のテストで確認する。
"""

from collections.abc import Iterator

import httpx
import pytest

from src.news.sources.scraper import MIN_CONTENT_CHARS, ArticleScraper

ARTICLE_HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <title>OpenAIが新しい画像モデルを発表</title>
    <meta property="og:image" content="https://example.com/thumb.jpg">
    <meta property="og:title" content="OpenAIが新しい画像モデルを発表">
</head>
<body>
    <nav>ホーム | ニュース | 検索</nav>
    <article>
        <h1>OpenAIが新しい画像モデルを発表</h1>
        <p>OpenAIは本日、新しい画像生成モデルを一般提供したと発表しました。
        任意の解像度に対応し、4K相当の高精細な画像も生成できます。</p>
        <p>従来モデルと比べて指示への追従性が向上しており、
        デザイナーやクリエイターの制作フローに影響を与えると見られています。</p>
        <p>開発者向けのAPIも同時に公開され、アプリケーションへの
        組み込みも可能になりました。料金体系は解像度と品質設定に応じて変わります。</p>
    </article>
    <footer>広告 | 利用規約 | プライバシーポリシー</footer>
</body>
</html>
"""


def _response(status: int, body: str) -> httpx.Response:
    """request を紐付けた Response を作る。

    httpx.Response は request が設定されていないと raise_for_status() で
    RuntimeError になるため、モックでも必ず紐付ける。
    """
    return httpx.Response(
        status, text=body, request=httpx.Request("GET", "https://example.com/article")
    )


@pytest.fixture
def scraper() -> Iterator[ArticleScraper]:
    instance = ArticleScraper(max_workers=2, timeout=5)
    yield instance
    instance.close()


def test_extracts_article_body(monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper) -> None:
    """本文が抽出されること。"""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, ARTICLE_HTML))
    content, _ = scraper._extract_content("https://example.com/article")
    assert "新しい画像生成モデル" in content
    assert "任意の解像度" in content


def test_drops_navigation_and_footer(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """ナビゲーションやフッターを本文に含めないこと。

    含まれると台本の材料に「利用規約」などが混ざる。
    favor_precision=True にしている理由がこれ。
    """
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, ARTICLE_HTML))
    content, _ = scraper._extract_content("https://example.com/article")
    assert "利用規約" not in content
    assert "プライバシーポリシー" not in content
    assert "ホーム | ニュース | 検索" not in content


def test_extracts_thumbnail_from_og_image(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, ARTICLE_HTML))
    _, thumbnail = scraper._extract_content("https://example.com/article")
    assert thumbnail == "https://example.com/thumb.jpg"


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_http_errors_return_empty_without_raising(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper, status: int
) -> None:
    """取得に失敗しても例外を投げず空を返すこと。

    1件のサイトが 403 を返しただけでバッチ全体を止めたくない。
    実際に NHK と Wikipedia は 403 を返す。
    """

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(status, ""))
    content, thumbnail = scraper._extract_content("https://example.com/article")
    assert content == ""
    assert thumbnail is None


def test_connection_errors_return_empty(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    def raising_get(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("接続できません")

    monkeypatch.setattr(httpx, "get", raising_get)
    assert scraper._extract_content("https://example.com/article") == ("", None)


def test_timeout_returns_empty(monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper) -> None:
    def timing_out_get(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("時間切れ")

    monkeypatch.setattr(httpx, "get", timing_out_get)
    assert scraper._extract_content("https://example.com/article") == ("", None)


def test_page_without_article_body_returns_empty(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """本文が無いページ（一覧ページなど）では空を返すこと。

    trafilatura は記事本文が無いページでもナビゲーションの断片を
    返すことがある（実測で "a | b" が返った）。呼び出し側は
    `if article.content` で真偽を見るだけなので、断片を許すと
    それが台本生成の材料になってしまう。
    """
    listing = "<html><body><nav>a | b</nav><ul><li>見出し1</li><li>見出し2</li></ul></body></html>"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, listing))
    content, _ = scraper._extract_content("https://example.com/list")
    assert content == ""


def test_content_just_below_the_minimum_is_discarded(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """下限を下回る本文は破棄すること。"""
    short_body = "あ" * (MIN_CONTENT_CHARS - 20)
    html = f"<html><body><article><p>{short_body}</p></article></body></html>"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, html))
    content, _ = scraper._extract_content("https://example.com/short")
    assert content == ""


def test_content_above_the_minimum_is_kept(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """下限を超える本文は残すこと。"""
    body = "これはニュース記事の本文です。" * 20
    html = f"<html><body><article><p>{body}</p></article></body></html>"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, html))
    content, _ = scraper._extract_content("https://example.com/long")
    assert len(content) >= MIN_CONTENT_CHARS


def test_extracted_content_is_stripped(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """前後の空白を落として返すこと。"""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, ARTICLE_HTML))
    content, _ = scraper._extract_content("https://example.com/article")
    assert content == content.strip()


def test_non_google_news_urls_are_left_alone(scraper: ArticleScraper) -> None:
    """Google News 以外のURLはデコードを試みないこと。"""
    url = "https://example.com/article"
    assert scraper._resolve_google_news_url(url) == url


def test_google_news_url_decode_failure_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    """デコードに失敗したら元のURLを返すこと（例外を漏らさない）。"""
    import googlenewsdecoder

    def raising_decoder(url: str) -> dict[str, object]:
        raise RuntimeError("デコード失敗")

    monkeypatch.setattr(googlenewsdecoder, "new_decoderv1", raising_decoder)
    url = "https://news.google.com/rss/articles/ABC123"
    assert scraper._resolve_google_news_url(url) == url


def test_google_news_url_is_decoded(
    monkeypatch: pytest.MonkeyPatch, scraper: ArticleScraper
) -> None:
    import googlenewsdecoder

    monkeypatch.setattr(
        googlenewsdecoder,
        "new_decoderv1",
        lambda url: {"status": True, "decoded_url": "https://real.example.com/a"},
    )
    resolved = scraper._resolve_google_news_url("https://news.google.com/rss/articles/ABC123")
    assert resolved == "https://real.example.com/a"


@pytest.mark.asyncio
async def test_scrape_content_skips_articles_that_already_have_content(
    scraper: ArticleScraper,
) -> None:
    """既に本文を持つ記事は再取得しないこと。

    ネットワークをモックしていないので、取得しようとすれば失敗する。
    """
    from src.models.news import NewsArticle, NewsCategory

    article = NewsArticle(
        id="x",
        title="t",
        url="https://example.invalid/never-fetched",
        source="s",
        category=NewsCategory.AI,
        content="既にある本文",
    )
    result = await scraper.scrape_content(article)
    assert result.content == "既にある本文"
