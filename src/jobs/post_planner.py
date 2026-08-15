"""1日ぶんの投稿計画を立てる。

やること
--------
1. その日の投稿時刻を決める
2. X でまだ使っていない記事を選ぶ
3. 型を割り当てて下書きを作り、予定時刻を入れて積む

**コストの上限を超えていたら何も積まない。** 積んでから止めると、
上限が戻った月初に古い投稿が一斉に出る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from src.models.news import CHANNEL_X, NewsArticle
from src.models.social import NewPost, PostKind
from src.social.cost import estimate_month_cost, is_over_budget
from src.utils.logger import log_error, log_step, log_success


class SupportsArticlePicking(Protocol):
    """ニュースストアの必要な部分だけ。

    `planner.py` の `SupportsNewsFetching` と違い、この計画は取得を
    行わない（定期実行で動画側が既に取得済みのストアを読むだけ）ので、
    `pick_unconsumed` の1メソッドだけに絞る。
    """

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を返す。"""
        ...


class SupportsPostEnqueue(Protocol):
    """投稿表の必要な部分だけ。"""

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        """当月の投稿数を（リンク無し, リンク有り）で返す。"""
        ...

    def enqueue(self, posts: list[NewPost], scheduled_at_by_position: dict[int, datetime]) -> str:
        """投稿を1つのまとまりとして積む。"""
        ...


class SupportsPostGeneration(Protocol):
    """下書き生成器の必要な部分だけ。"""

    def generate(self, article: NewsArticle, kind: PostKind, hashtags: list[str]) -> list[NewPost]:
        """記事から投稿の下書きを生成する。"""
        ...


# 型の割り当て。固定の並びを posts_per_day に合わせて循環させる。
#
# LLM に「今日はどの型がよいか」を決めさせない。判断材料（過去の反応）が
# 数十件しかない時期に選ばせると、理由の説明できないばらつきが出るだけ。
KIND_ROTATION: tuple[PostKind, ...] = (
    PostKind.SINGLE,
    PostKind.SINGLE,
    PostKind.SINGLE,
    PostKind.CARD,
)


@dataclass(frozen=True)
class DailyPostPlan:
    """1回の投稿計画の結果。

    Attributes:
        group_ids: 積んだまとまりのID
        skipped_reason: 何も積まなかった理由（積んだ場合は None）
    """

    group_ids: list[str]
    skipped_reason: str | None = None

    @property
    def enqueued(self) -> bool:
        """1件でも積んだか。"""
        return bool(self.group_ids)


def plan_daily_posts(
    news: SupportsArticlePicking,
    posts: SupportsPostEnqueue,
    generator: SupportsPostGeneration,
    *,
    times: list[str],
    posts_per_day: int,
    hashtags: list[str],
    budget_usd: float,
    unit_usd: float,
    unit_with_link_usd: float,
    now: datetime,
    timezone: str = "Asia/Tokyo",
    card_generator: object | None = None,
    image_generator: object | None = None,
    artifacts: object | None = None,
) -> DailyPostPlan:
    """記事を選び、下書きを作り、予定時刻を入れて積む。

    Args:
        news: 記事ストア
        posts: 投稿表
        generator: 下書きの生成器
        times: 投稿時刻（HH:MM、timezone のローカル時刻）
        posts_per_day: 1日のテーマ数
        hashtags: 固定のハッシュタグ
        budget_usd: 概算コストの上限
        unit_usd: リンク無しの単価
        unit_with_link_usd: リンク有りの単価
        now: 現在時刻（UTC aware）
        timezone: times を解釈するタイムゾーン
        card_generator: 画像カードのキャプション生成器。このタスクでは
            未使用（Task 6 が実装する）。無ければ画像無しの CARD を作る
        image_generator: 画像カードの生成器。同上
        artifacts: 生成した画像の保存先。同上

    Returns:
        DailyPostPlan: 積んだまとまり、または積まなかった理由
    """
    # 上限を超えていたら積まない。
    #
    # 積んでから投稿側で止めると、上限が戻った月初に古い投稿が
    # 一斉に出る（そのときにはもうニュースとして古い）。
    plain, with_link = posts.monthly_post_counts(now.year, now.month)
    spent = estimate_month_cost(plain, with_link, unit_usd, unit_with_link_usd)
    if is_over_budget(spent, budget_usd):
        log_step(f"概算コストが上限に達しています（${spent:.2f} / ${budget_usd:.2f}）", "⏭️")
        return DailyPostPlan(group_ids=[], skipped_reason=f"予算上限（概算 ${spent:.2f}）")

    articles = news.pick_unconsumed(CHANNEL_X, posts_per_day)
    if not articles:
        return DailyPostPlan(group_ids=[], skipped_reason="X で未使用の記事がありません")

    schedule = _resolve_schedule(times, now, timezone)
    group_ids: list[str] = []

    for index, article in enumerate(articles):
        if index >= len(schedule):
            # 時刻の数より記事が多い。出せる分だけ出す
            log_error(f"投稿時刻が足りないため {len(articles) - index}件を見送ります")
            break

        kind = KIND_ROTATION[index % len(KIND_ROTATION)]
        try:
            drafts = generator.generate(article, kind, hashtags)
        # 1件の生成失敗で1日を落とさない。残りの記事は積む
        except Exception as e:
            log_error(f"下書きの生成に失敗しました（{article.title[:24]}）: {e}")
            continue

        at = schedule[index]
        group_ids.append(posts.enqueue(drafts, {d.position: at for d in drafts}))
        log_success(f"{at:%H:%M} に {kind} を積みました（{article.title[:24]}）")

    if not group_ids:
        return DailyPostPlan(group_ids=[], skipped_reason="積める下書きがありませんでした")
    return DailyPostPlan(group_ids=group_ids)


def _resolve_schedule(times: list[str], now: datetime, timezone: str) -> list[datetime]:
    """HH:MM の並びを、その日の UTC aware な時刻に変換する。

    既に過ぎた時刻は飛ばす。計画を朝に立てるので、当日の 08:00 を
    過ぎていれば最初の枠は 12:30 になる。過去の時刻を入れると
    `discard_stale` に即座に捨てられる。

    Args:
        times: HH:MM の並び（検証済み）
        now: 現在時刻（UTC aware）
        timezone: times を解釈するタイムゾーン

    Returns:
        list[datetime]: UTC aware な予定時刻（昇順）
    """
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)

    resolved: list[datetime] = []
    for value in times:
        hour, minute = (int(part) for part in value.split(":", 1))
        at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local_now:
            continue
        resolved.append(at.astimezone(UTC))
    return resolved
