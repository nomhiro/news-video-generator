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

from src.jobs.article_supply import SupportsArticleSupply, supply_articles
from src.models.job import ORIGIN_SCHEDULE
from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory
from src.news.feeds import AI_FEEDS, Feed
from src.utils.logger import log_error, log_step, log_success


class SupportsNewsRefresh(Protocol):
    """取得だけを行うのに必要な部分。

    `fetch_daily_news` は記事を選ばないので、選択と本文取得
    （`SupportsArticleSupply`）は要らない。
    """

    async def fetch_and_store(
        self, limit_per_category: int = ...
    ) -> dict[NewsCategory, list[NewsArticle]]:
        """カテゴリ別にニュースを取得して保存する。"""
        ...

    async def fetch_ai_news_and_store(
        self, feeds: tuple[Feed, ...] | list[Feed] = ..., limit_per_feed: int = ...
    ) -> list[NewsArticle]:
        """AI関連の記事を発信元のフィードから取得して保存する。"""
        ...


class SupportsNewsFetching(SupportsNewsRefresh, SupportsArticleSupply, Protocol):
    """`plan_daily_batch` が使う部分。取得 + 供給。

    **選択と本文取得は `SupportsArticleSupply` から継承する。**
    以前は同じ2メソッドをここと `post_planner` に書き写していた。
    写しがあると、片方の実装だけが変わる（実際に
    `ARTICLE_OVERFETCH` が X 側にしか入らなかった）。
    """


async def fetch_daily_news(
    news: SupportsNewsRefresh,
    *,
    feeds: tuple[Feed, ...] | list[Feed] = AI_FEEDS,
    ai_limit_per_feed: int = 3,
) -> None:
    """その日のニュースを取得してストアに入れる。

    **`plan_daily_batch` から切り出してある。** 動画の計画の中に取得が
    埋まっていると、X の計画を動画より先に走らせた瞬間に
    「X はこのサイクルで取得した記事を見られない」状態になる
    （前回のサイクルで取得した記事しか選べず、毎日1日古いニュースを
    投稿することになる）。呼び出し順の全体像は
    `src/web/dependencies.py` の日次タスクを参照。

    **取得の失敗で例外を投げない。** 一時的なネットワーク障害で
    丸一日を落とさないため、ログに残して帰る。呼び出し元は既に
    ストアにある記事で計画を続けられる（動画も X も）。ここで投げると、
    後続の X の計画まで巻き込んで止まる。

    Args:
        news: ニュースストア
        feeds: AI 記事のフィード（既定は `AI_FEEDS`）
        ai_limit_per_feed: フィードごとの取得件数
    """
    log_step("定期実行: ニュースを取得します", "🗓️")
    try:
        await news.fetch_and_store()
        await news.fetch_ai_news_and_store(feeds, ai_limit_per_feed)
    except Exception as e:
        log_error(f"ニュースの取得に失敗しました（既存の記事で続行します）: {e}")


class SupportsEnqueue(Protocol):
    """ジョブ表の必要な部分だけ。"""

    def has_active_jobs(self) -> bool:
        """実行待ち・実行中のジョブがあるか。"""
        ...

    def enqueue_batch(
        self,
        articles: list[tuple[str, str]],
        video_format: str,
        language: str = ...,
        origin: str | None = ...,
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
    feeds: tuple[Feed, ...] | list[Feed] = AI_FEEDS,
    ai_limit_per_feed: int = 3,
    articles_per_format: int = 1,
    language: str = "ja",
    fetch: bool = True,
) -> DailyPlan:
    """ニュースを取得し、形式ごとにジョブを投入する。

    Args:
        news: ニュースストア
        jobs: ジョブ表
        formats: 作る形式（例: `["short", "long"]`）
        feeds: AI 記事のフィード
        ai_limit_per_feed: フィードごとの取得件数
        articles_per_format: 形式ごとに何件の記事を対象にするか
        language: 言語コード
        fetch: 自分でニュースを取得するか。**Web の日次タスクは False**
            （取得を先に1回だけ済ませ、X の計画にも同じ記事を見せるため。
            `fetch_daily_news` の docstring を参照）。既定を True に
            しているのは、CLI などの単独呼び出しがこの関数だけで
            完結できるようにするため

    Returns:
        DailyPlan: 投入したバッチ、または投入しなかった理由
    """
    # 前回の生成がまだ終わっていないなら何もしない。
    # 積み増すと画像生成のクォータを食い合い、どちらも遅くなる。
    if jobs.has_active_jobs():
        log_step("実行中のジョブがあるため、今回の定期実行は見送ります", "⏭️")
        return DailyPlan(batch_ids={}, skipped_reason="実行中のジョブがあります")

    if fetch:
        await fetch_daily_news(news, feeds=feeds, ai_limit_per_feed=ai_limit_per_feed)
    log_step(f"定期実行: 動画のジョブを組みます（形式: {', '.join(formats)}）", "🗓️")

    needed = articles_per_format * len(formats)
    # **選ぶのと本文を取るのは X と共通の1段**（`supply_articles`）。
    # 必要数の3倍を候補にするのもここ——以前は必要数ぴったりを選んでいたので、
    # スクレイピングが1件落ちるとその形式の動画が作れずに1日が終わっていた。
    supply = await supply_articles(news, CHANNEL_VIDEO, needed)
    if not supply.candidates:
        log_error("未生成の記事が見つかりませんでした")
        return DailyPlan(batch_ids={}, skipped_reason="未生成の記事がありません")

    # **本文が取れた記事を優先する。** ただし1件も取れなかったときは候補に
    # 落ちる（本文の無い記事でもジョブに投入し、画面に「なぜ作れなかったか」を
    # 残す。黙って捨てるとその日なにも起きなかった理由が分からなくなる）。
    # X 側は同じ状況で諦める——投稿は1日4件あり、1件の欠落を画面に残す
    # 価値が無いため。**機構は共通、この判断だけが計画ごとに違う。**
    ordered = supply.with_content or supply.candidates

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
            # 定期実行の印。コンテンツフィルタに拒否されたとき、この印がある
            # ジョブだけが別の記事で作り直される（手動は差し替えない）。
            origin=ORIGIN_SCHEDULE,
        )
        titles = ", ".join(a.title[:24] for a in chosen)
        log_success(f"{video_format}: {len(chosen)}件を投入しました（{titles}）")

    if not batch_ids:
        return DailyPlan(batch_ids={}, skipped_reason="投入できる記事がありませんでした")
    return DailyPlan(batch_ids=batch_ids)
