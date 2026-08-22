"""再取得で既存の状態が消えないことの検証。

フィードは新しい順に数件しか返さないので、記事は数時間で入れ替わる。
`_merge_preserving_state` が取得できた記事だけを返していた間、
「記事を選ぶ → 最新ニュースを取得を押す → 選択が消えている」が普通に起きていた。
`consumed`（もう投稿したの権威）も同じ経路で失われ、二重投稿の余地があった。
"""

from datetime import UTC, datetime, timedelta

from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.news.aggregator import CONSUMED_RETENTION, NewsAggregator


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


def _merge(new: list[NewsArticle], existing: list[NewsArticle]) -> dict[str, NewsArticle]:
    merged = NewsAggregator._merge_preserving_state(new, {a.id: a for a in existing})
    return {a.id: a for a in merged}


def test_選択中の記事はフィードから消えても残る() -> None:
    selected = _article("selected")
    selected.is_selected = True
    fresh = _article("fresh")

    result = _merge([fresh], [selected])

    assert selected.id in result
    assert result[selected.id].is_selected is True


def test_消費済みの記事はフィードから消えても残る() -> None:
    """`consumed` を失うと同じ記事の投稿が作り直される。"""
    consumed = _article("consumed")
    consumed.mark_consumed(CHANNEL_X)
    fresh = _article("fresh")

    result = _merge([fresh], [consumed])

    assert consumed.id in result
    assert result[consumed.id].is_consumed_by(CHANNEL_X) is True


def test_選択も消費もされていない古い記事は入れ替わる() -> None:
    """無条件に残すと記事プールが単調増加して読めなくなる。"""
    stale = _article("stale")
    fresh = _article("fresh")

    result = _merge([fresh], [stale])

    assert stale.id not in result
    assert fresh.id in result


def test_保持期間を過ぎた消費済み記事は落ちる() -> None:
    old = _article("old")
    old.mark_consumed(CHANNEL_VIDEO, at=datetime.now(UTC) - CONSUMED_RETENTION - timedelta(days=1))

    assert old.id not in _merge([_article("fresh")], [old])


def test_保持期間の内側なら残る() -> None:
    recent = _article("recent")
    recent.mark_consumed(
        CHANNEL_VIDEO, at=datetime.now(UTC) - CONSUMED_RETENTION + timedelta(days=1)
    )

    assert recent.id in _merge([_article("fresh")], [recent])


def test_消費時刻が読めない記事は残す() -> None:
    """判断できないときは二重投稿を避ける方向に倒す。"""
    broken = _article("broken")
    broken.consumed = {CHANNEL_X: "not-a-timestamp"}

    assert broken.id in _merge([_article("fresh")], [broken])


def test_naive_な消費時刻でも比較できる() -> None:
    """記事データの時刻は tz 付きと naive が混ざる（JSON から復元した古い行）。"""
    naive = _article("naive")
    naive.consumed = {CHANNEL_X: datetime.now().replace(tzinfo=None).isoformat()}

    assert naive.id in _merge([_article("fresh")], [naive])


def test_再取得できた記事は状態を引き継ぐ() -> None:
    """従来の引き継ぎを壊していないこと。"""
    existing = _article("same")
    existing.is_selected = True
    existing.mark_consumed(CHANNEL_X)
    existing.content = "既にスクレイピングした本文"
    existing.thumbnail_url = "https://example.com/thumb.png"

    refetched = _article("same")

    result = _merge([refetched], [existing])

    assert len(result) == 1
    kept = result[existing.id]
    assert kept.is_selected is True
    assert kept.is_consumed_by(CHANNEL_X) is True
    assert kept.content == "既にスクレイピングした本文"
    assert kept.thumbnail_url == "https://example.com/thumb.png"


def test_選択が再取得をまたいで画面に残る(tmp_path) -> None:
    """ストアを経由した往復で確認する（症状が出ていたのはこの経路）。"""
    aggregator = NewsAggregator(data_dir=tmp_path)
    selected = _article("selected")
    selected.is_selected = True
    aggregator._save_category(NewsCategory.AI, [selected])

    # フィードが入れ替わった状況を再現する
    merged = NewsAggregator._merge_preserving_state(
        [_article("fresh")], {a.id: a for a in aggregator._load_category(NewsCategory.AI)}
    )
    aggregator._save_category(NewsCategory.AI, merged)

    assert [a.id for a in aggregator.get_selected_articles()] == [selected.id]
