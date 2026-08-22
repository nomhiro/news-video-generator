"""記事の供給。動画と X の計画が共有する「選ぶ → 本文を取る」の1段。

なぜ共有するか
--------------
この1段は両方の計画がまったく同じ理由で必要としている。

- どちらもストア（`NewsAggregator`）から未消費の記事を選ぶ
- どちらも `content` が無ければ生成できない（台本も投稿も記事本文が入力）
- どちらも他人のサーバーからのスクレイピングに依存し、常に一部が落ちる

**片方だけに取りこぼし対策があった。** 2026-08-22 の実装で X 側に
`ARTICLE_OVERFETCH` を入れたが、動画側は必要数ぴったりを選んでいた。
同じ理由で同じ対策が要るのに、片方にしか無い状態は必ず片方だけ腐る。

**方針は共有しない。** 「本文が1件も取れなかったとき」の扱いは
計画ごとに違ってよい（`ArticleSupply` の docstring を参照）。共有するのは
機構（多めに選ぶ・本文を取る・分ける）で、そこから先の判断は呼び出し元。
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

from src.models.news import NewsArticle

# 本文の取得に失敗する分を見込んで、必要数の何倍を候補に取るか。
#
# スクレイピングは他人のサーバー相手なので常に一部が落ちる
# （実測 2026-08-22: Google News のリダイレクタ経由で12件中9件成功。
# 403 と 429 で2件、本文が短くて1件。発信元の実 URL に変えたあとの
# 実測は12/12だったが、フィードが増えれば取りにくい媒体も混ざる）。
# 3倍にしてあるのは、成功率75%で4件必要なときに候補12件なら
# 期待値9件で足りるため。
ARTICLE_OVERFETCH = 3


class SupportsArticleSupply(Protocol):
    """ニュースストアのうち、この1段が使う部分だけ。"""

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を返す。"""
        ...

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """指定した記事の本文を取得して保存する。"""
        ...


class ArticleSupply(NamedTuple):
    """供給の結果。

    **`with_content` と `candidates` を両方返す理由。** 「本文が1件も
    取れなかったとき」の扱いが計画ごとに違う。

    - 動画（`plan_daily_batch`）は `candidates` に落ちる。本文が無い記事でも
      ジョブに投入し、**画面に「なぜ作れなかったか」を残す**。黙って捨てると
      その日なにも起きなかった理由が分からなくなる
    - X（`plan_daily_posts`）は諦める。投稿は1日に4件あり、1件が
      出ないことは画面に残す価値が無い。本文なしの生成は実測73字で
      下限95を割り、引き直し3回ぶんの API 呼び出しを捨てるだけ

    Attributes:
        with_content: 本文が取れた記事（新しい順のまま）
        candidates: 本文の有無を問わない候補すべて
    """

    with_content: list[NewsArticle]
    candidates: list[NewsArticle]


async def supply_articles(
    news: SupportsArticleSupply,
    channel: str,
    needed: int,
    overfetch: int = ARTICLE_OVERFETCH,
) -> ArticleSupply:
    """未消費の記事を多めに選び、本文を取って分ける。

    **記事を消費済みにしない。** 消費の記録は「実際に出せた後」に呼び出し元が
    行う（出せなかった記事を二度と使えなくしないため）。ここで本文が取れずに
    落ちた記事も、次回また候補に入る。

    Args:
        news: ニュースストア
        channel: CHANNEL_VIDEO / CHANNEL_X
        needed: 使う予定の件数
        overfetch: 必要数の何倍を候補にするか

    Returns:
        ArticleSupply: 本文が取れた記事と、候補すべて
    """
    candidates = news.pick_unconsumed(channel, needed * overfetch)
    if not candidates:
        return ArticleSupply(with_content=[], candidates=[])

    scraped = await news.scrape_articles(candidates)
    by_id = {a.id: a for a in scraped}
    # `scrape_articles` は本文が取れた記事だけを返すとは限らない
    # （実物は取れなかったものも含めて返す契約）。取れた方で上書きする。
    merged = [by_id.get(a.id, a) for a in candidates]

    return ArticleSupply(
        with_content=[a for a in merged if a.content],
        candidates=merged,
    )
