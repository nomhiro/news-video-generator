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

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from src.generators.image_generator import ContentFilterError
from src.models.news import CHANNEL_X, NewsArticle
from src.models.social import NewPost, PostKind
from src.social.card_visual import CARD_IMAGE_SIZE, CardVisual, build_card_prompt
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

    def generate(
        self,
        article: NewsArticle,
        kind: PostKind,
        hashtags: list[str],
        caption: str | None = None,
    ) -> list[NewPost]:
        """記事から投稿の下書きを生成する。"""
        ...


class SupportsActiveJobsCheck(Protocol):
    """ジョブ表の必要な部分だけ。"""

    def has_active_jobs(self) -> bool:
        """実行待ち・実行中の動画生成ジョブがあるか。"""
        ...


class SupportsCardVisualGeneration(Protocol):
    """`CardVisualGenerator` の必要な部分だけ。"""

    def generate(self, article: NewsArticle) -> CardVisual:
        """記事から画像カードの視覚指示を生成する。"""
        ...


class SupportsCardImageGeneration(Protocol):
    """`ImageGenerator` の必要な部分だけ。"""

    def generate_batch(
        self,
        prompts: list[str],
        output_dir: Path,
        *,
        size: str | None = None,
        enhance: bool = True,
    ) -> list[Path]:
        """プロンプトから画像を生成する。"""
        ...


class SupportsArtifactPublish(Protocol):
    """`ArtifactStore` の必要な部分だけ。"""

    def publish(self, local_path: Path, key: str) -> str:
        """ローカルのファイルを保存先へ公開する。"""
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
    enabled: bool,
    times: list[str],
    posts_per_day: int,
    hashtags: list[str],
    budget_usd: float,
    unit_usd: float,
    unit_with_link_usd: float,
    unit_read_usd: float = 0.0,
    now: datetime,
    timezone: str = "Asia/Tokyo",
    card_generator: SupportsCardVisualGeneration | None = None,
    image_generator: SupportsCardImageGeneration | None = None,
    artifacts: SupportsArtifactPublish | None = None,
    jobs: SupportsActiveJobsCheck | None = None,
    output_dir: Path | None = None,
) -> DailyPostPlan:
    """記事を選び、下書きを作り、予定時刻を入れて積む。

    Args:
        news: 記事ストア
        posts: 投稿表
        generator: 下書きの生成器
        enabled: 自動投稿スイッチが有効か（`PostingSwitch.is_enabled()`）。
            False なら何も積まずに帰る（下の docstring 本文を参照）
        times: 投稿時刻（HH:MM、timezone のローカル時刻）
        posts_per_day: 1日のテーマ数
        hashtags: 固定のハッシュタグ
        budget_usd: 概算コストの上限
        unit_usd: リンク無しの単価
        unit_read_usd: 投稿1件の読み取り単価（計測が投稿ごとに2回読む）
        unit_with_link_usd: リンク有りの単価
        now: 現在時刻（UTC aware）
        timezone: times を解釈するタイムゾーン
        card_generator: 画像カードの視覚指示生成器。`image_generator` /
            `artifacts` / `output_dir` のいずれかと組にならない場合、
            または省略時は、画像無しの CARD を作る（Task 6 以前の動作）
        image_generator: 画像カードの生成器。同上
        artifacts: 生成した画像の保存先。同上
        jobs: 動画生成ジョブ表。`has_active_jobs()` が True の間はカードを
            作らない（`gpt-image-2` のクォータをリージョン単位で
            動画パイプラインと共有しているため、奪うと双方が遅くなる）
        output_dir: 画像カードを生成する作業ディレクトリ（ローカル）。
            生成後に `artifacts` へ publish する。省略時はカードを作らない

    Returns:
        DailyPostPlan: 積んだまとまり、または積まなかった理由
    """
    # スイッチは「送信するか」だけでなく「X チャネルが動いているか」を
    # 意味する。無効なのに下書きだけ作ると：
    #
    # 1. `PostWorker` の discard_stale が X_MAX_POST_DELAY_MINUTES 後に
    #    その下書きを黙って捨てる。見えるものが無いので誰も見直せない
    # 2. 記事は「投稿できた後」にしか消費済みにならないため、同じ記事が
    #    翌日以降も再ドラフトされ続け、無駄が収束しない
    # 3. 下書き生成は Azure OpenAI 呼び出しであり、Task 6 以降は CARD が
    #    画像も生成する。`gpt-image-2` のクォータはリージョン上限 4 で
    #    動画パイプラインと共有しているため、見られない出力のために
    #    奪うのは実害が大きい
    #
    # つまりスイッチは送信ステップだけでなく計画ステップも止める。
    if not enabled:
        log_step("自動投稿が無効なため、下書きを作成していません", "⏭️")
        return DailyPostPlan(
            group_ids=[],
            skipped_reason="自動投稿が無効（スイッチ off）なため、下書きを作成していません",
        )

    # 上限を超えていたら積まない。
    #
    # 積んでから投稿側で止めると、上限が戻った月初に古い投稿が
    # 一斉に出る（そのときにはもうニュースとして古い）。
    plain, with_link = posts.monthly_post_counts(now.year, now.month)
    spent = estimate_month_cost(plain, with_link, unit_usd, unit_with_link_usd, unit_read_usd)
    if is_over_budget(spent, budget_usd):
        log_step(f"概算コストが上限に達しています（${spent:.2f} / ${budget_usd:.2f}）", "⏭️")
        return DailyPostPlan(group_ids=[], skipped_reason=f"予算上限（概算 ${spent:.2f}）")

    articles = news.pick_unconsumed(CHANNEL_X, posts_per_day)
    if not articles:
        return DailyPostPlan(group_ids=[], skipped_reason="X で未使用の記事がありません")

    schedule = _resolve_schedule(times, now, timezone)
    group_ids: list[str] = []

    # **枠は「成功した下書き」に順に割り当てる。記事の添字に紐づけない。**
    # 実測（2026-08-17）: 3件のうち1件目と3件目の生成が失敗し、成功した2件目だけが
    # 2番目の枠（19:00）に積まれた。1番目（12:30）と3番目（21:30）の枠は誰にも
    # 使われず消えた。3件出す予定の日に1件しか出ないことになる。
    # 型の割り当ても同じ理由で成功順にする（失敗すると CARD の順番が飛ぶ）。
    filled = 0

    for article in articles:
        if filled >= len(schedule):
            # 埋められる枠が無くなった。残りは翌日に回る（消費済みにしていない）
            log_error(f"投稿時刻が埋まったため {len(articles) - filled}件を見送ります")
            break

        kind = KIND_ROTATION[filled % len(KIND_ROTATION)]
        image_key: str | None = None
        caption: str | None = None
        # card_generator / image_generator / artifacts が揃っていない場合は
        # 画像生成そのものを試みず、画像無しの CARD のまま進む
        # （Task 6 以前からの動作。呼び出し元が未設定でも壊れない）。
        if kind is PostKind.CARD and card_generator and image_generator and artifacts:
            kind, image_key, caption = _build_card_image(
                article, card_generator, image_generator, artifacts, jobs, output_dir
            )

        try:
            if caption:
                drafts = generator.generate(article, kind, hashtags, caption=caption)
            else:
                drafts = generator.generate(article, kind, hashtags)
        # 1件の生成失敗で1日を落とさない。残りの記事は積む
        except Exception as e:
            log_error(f"下書きの生成に失敗しました（{article.title[:24]}）: {e}")
            continue

        if image_key:
            drafts = [dataclasses.replace(drafts[0], image_key=image_key), *drafts[1:]]

        at = schedule[filled]
        group_ids.append(posts.enqueue(drafts, {d.position: at for d in drafts}))
        log_success(f"{at:%H:%M} に {kind} を積みました（{article.title[:24]}）")
        filled += 1

    if not group_ids:
        return DailyPostPlan(group_ids=[], skipped_reason="積める下書きがありませんでした")
    return DailyPostPlan(group_ids=group_ids)


def _build_card_image(
    article: NewsArticle,
    card_generator: SupportsCardVisualGeneration,
    image_generator: SupportsCardImageGeneration,
    artifacts: SupportsArtifactPublish,
    jobs: SupportsActiveJobsCheck | None,
    output_dir: Path | None,
) -> tuple[PostKind, str | None, str | None]:
    """画像カードの視覚指示・画像を生成し、保存先へ公開する。

    失敗したら例外を投げず SINGLE への降格を返す。カード1枚を諦めても
    その日の投稿は出したいため（呼び出し元の `plan_daily_posts` が
    1件の生成失敗で1日を落とさない、という方針と同じ）。

    Args:
        article: 元記事
        card_generator: 視覚指示の生成器
        image_generator: 画像の生成器
        artifacts: 生成した画像の保存先
        jobs: 動画生成ジョブ表（None なら判定を省略する）
        output_dir: 画像を生成する作業ディレクトリ（None なら降格する）

    Returns:
        tuple[PostKind, str | None, str | None]:
            (最終的な型, 画像の保存先キー, 投稿本文に渡すキャプション)
    """
    if jobs is not None and jobs.has_active_jobs():
        # gpt-image-2 のクォータはリージョン単位で上限4という律速で、
        # 動画パイプラインと共有している。動画生成中にカードを作ると
        # 動画側の完了を遅らせるので、その日は SINGLE に降格する。
        log_step("動画生成が進行中のため画像カードを SINGLE に降格します", "⏭️")
        return PostKind.SINGLE, None, None

    if output_dir is None:
        log_error("画像カードの作業ディレクトリが未設定のため SINGLE に降格します")
        return PostKind.SINGLE, None, None

    try:
        visual = card_generator.generate(article)
        prompt = build_card_prompt(visual)
        # enhance=False: `CARD_STYLE_PROMPT` は medium / palette / composition /
        # constraints を自分で書き切った完結済みの指示を持つ。
        # `_enhance_prompt` は動画用の1行シーン記述を飾るためのもので、
        # 重ねると矛盾した指示が混ざる（縦長構図 vs 1024x1024、「ラベルの
        # 文字を描け」vs「テキストは描くな」）。
        paths = image_generator.generate_batch(
            [prompt], output_dir, size=CARD_IMAGE_SIZE, enhance=False
        )
        # キーに group_id は使えない（group_id は NewPost 構築後に
        # SocialPostRepository.enqueue が振るため）。article_id は URL の
        # 16文字ハッシュで安定しているので、同じ記事の再ドラフトはキーを
        # 上書きするだけで、孤立ファイルが積み重ならない。
        key = f"social/cards/{article.id}.png"
        artifacts.publish(paths[0], key)
    except ContentFilterError as e:
        # コンテンツフィルタの拒否は再試行しても結果が変わらない設計
        # （ImageGenerator 側の判断）なので、ここでも再試行せず即座に降格する。
        log_error(f"画像カードがコンテンツフィルタに拒否されました。SINGLE に降格します: {e}")
        return PostKind.SINGLE, None, None
    except Exception as e:
        log_error(f"画像カードの生成に失敗しました。SINGLE に降格します: {e}")
        return PostKind.SINGLE, None, None

    return PostKind.CARD, key, visual.caption_ja


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
