"""毎日の生成計画を立てる。

やること
--------
1. ニュースを取得する
2. まだ動画にしていない記事を選ぶ
3. 形式ごとにジョブを投入する

**利用者の選択状態（`is_selected`）は触らない。** 画面で選んでいた記事が
定期実行で勝手に増減すると、何を選んだのか分からなくなる。
定期実行は自分で記事を決め、ジョブ表にだけ書く。

生成そのものはワーカーが担う（`src/jobs/worker.py`）。ここは
「何を作るか」を決めるところで、作る処理は持たない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory
from src.utils.logger import log_error, log_step, log_success


class SupportsNewsFetching(Protocol):
    """ニュースストアの必要な部分だけ。"""

    async def fetch_and_store(
        self, limit_per_category: int = ...
    ) -> dict[NewsCategory, list[NewsArticle]]:
        """カテゴリ別にニュースを取得して保存する。"""
        ...

    async def fetch_ai_news_and_store(
        self, search_queries: list[str], limit_per_query: int = ...
    ) -> list[NewsArticle]:
        """AI関連ニュースを取得して保存する。"""
        ...

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """指定した記事の本文を取得する。"""
        ...

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を返す。"""
        ...


class SupportsEnqueue(Protocol):
    """ジョブ表の必要な部分だけ。"""

    def has_active_jobs(self) -> bool:
        """実行待ち・実行中のジョブがあるか。"""
        ...

    def enqueue_batch(
        self, articles: list[tuple[str, str]], video_format: str, language: str = ...
    ) -> str:
        """ジョブを投入する。"""
        ...


@dataclass(frozen=True)
class DailyPlan:
    """1回の定期実行の結果。

    Attributes:
        batch_ids: 形式 -> 投入したバッチID
        skipped_reason: 何も投入しなかった理由（投入した場合は None）
    """

    batch_ids: dict[str, str]
    skipped_reason: str | None = None

    @property
    def enqueued(self) -> bool:
        """1件でも投入したか。"""
        return bool(self.batch_ids)


async def plan_daily_batch(
    news: SupportsNewsFetching,
    jobs: SupportsEnqueue,
    *,
    formats: list[str],
    search_queries: list[str],
    ai_limit_per_query: int = 5,
    articles_per_format: int = 1,
    language: str = "ja",
) -> DailyPlan:
    """ニュースを取得し、形式ごとにジョブを投入する。

    Args:
        news: ニュースストア
        jobs: ジョブ表
        formats: 作る形式（例: `["short", "long"]`）
        search_queries: AI ニュースの検索クエリ
        ai_limit_per_query: クエリごとの取得件数
        articles_per_format: 形式ごとに何件の記事を対象にするか
        language: 言語コード

    Returns:
        DailyPlan: 投入したバッチ、または投入しなかった理由
    """
    # 前回の生成がまだ終わっていないなら何もしない。
    # 積み増すと画像生成のクォータを食い合い、どちらも遅くなる。
    if jobs.has_active_jobs():
        log_step("実行中のジョブがあるため、今回の定期実行は見送ります", "⏭️")
        return DailyPlan(batch_ids={}, skipped_reason="実行中のジョブがあります")

    log_step(f"定期実行: ニュースを取得します（形式: {', '.join(formats)}）", "🗓️")
    try:
        await news.fetch_and_store()
        await news.fetch_ai_news_and_store(search_queries, ai_limit_per_query)
    except Exception as e:
        # 取得に失敗しても、既にストアにある記事で続行できる。
        # ここで諦めると、一時的なネットワーク障害で丸一日が飛ぶ。
        log_error(f"ニュースの取得に失敗しました（既存の記事で続行します）: {e}")

    needed = articles_per_format * len(formats)
    candidates = news.pick_unconsumed(CHANNEL_VIDEO, needed)
    if not candidates:
        log_error("未生成の記事が見つかりませんでした")
        return DailyPlan(batch_ids={}, skipped_reason="未生成の記事がありません")

    # 本文を取る。取れなかった記事も投入する（ジョブが理由付きで失敗し、
    # 画面に「なぜ作れなかったか」が残る。黙って捨てると分からなくなる）。
    scraped = await news.scrape_articles(candidates)
    by_id = {a.id: a for a in scraped}
    ordered = [by_id.get(a.id, a) for a in candidates]

    batch_ids: dict[str, str] = {}
    for index, video_format in enumerate(formats):
        start = index * articles_per_format
        chosen = ordered[start : start + articles_per_format]
        if not chosen:
            # 記事が足りない。作れる分だけ作る
            log_error(f"{video_format} に割り当てる記事が足りませんでした")
            continue
        batch_ids[video_format] = jobs.enqueue_batch(
            [(a.id, a.title) for a in chosen],
            video_format=video_format,
            language=language,
        )
        titles = ", ".join(a.title[:24] for a in chosen)
        log_success(f"{video_format}: {len(chosen)}件を投入しました（{titles}）")

    if not batch_ids:
        return DailyPlan(batch_ids={}, skipped_reason="投入できる記事がありませんでした")
    return DailyPlan(batch_ids=batch_ids)
