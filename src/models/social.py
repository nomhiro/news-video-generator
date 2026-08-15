"""X 投稿のドメインモデル。

なぜ動画ジョブ（src/models/job.py）と分けたか
---------------------------------------------
失敗の意味が違う。ワーカーが掴んだあとに落ちたとき、動画は再生成で
画像クォータを食うだけだが、投稿は**同じ内容が2回公開される**。
X API に冪等キーが無いため、「不明なら送らない」という状態
（NEEDS_REVIEW）を状態機械に持つ必要がある。動画側には無い概念で、
共有すると両方に相手の都合が入る。

このモジュールは外部依存を持たない（SQLAlchemy を import しない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# X の投稿の上限（weighted length）。
X_MAX_WEIGHTED_LENGTH = 280

# t.co で短縮された URL の固定カウント。実際の長さは関係ない。
URL_WEIGHTED_LENGTH = 23

# 「URL を含むか」の唯一の定義。公開しているのは `weighted_length` と
# `PostGenerator._assemble` の `has_link` 判定が同じ定義を共有する必要が
# あるため。`has_link` はコスト単価（$0.015 と $0.20、13倍差）を選ぶ
# フラグなので、ここで数えた URL と `has_link` が指す URL がずれると、
# 文字数の予算計算と実際の課金階層が食い違う（"http" という文字列だけを
# 含み `://` を欠くボディが、リンク無しとして課金される "https://..." と
# 同じにカウントされてしまう、等）。
URL_PATTERN = re.compile(r"https?://\S+")


def weighted_length(text: str) -> int:
    """X の数え方で文字数を返す。

    X は CJK を1文字2カウントで数え、上限は 280。つまり日本語は実質140字。
    素の `len()` で予算を組むと、140字の投稿が実際には280カウントで
    上限ぴったりになり、出典表記を足した瞬間に投稿が弾かれる。

    URL は t.co で短縮されるため、長さに関係なく 23 カウント。

    Args:
        text: 投稿本文

    Returns:
        int: weighted length
    """
    without_urls = URL_PATTERN.sub("", text)
    url_count = len(URL_PATTERN.findall(text))

    total = url_count * URL_WEIGHTED_LENGTH
    for char in without_urls:
        total += 2 if _is_wide(char) else 1
    return total


def _is_wide(char: str) -> bool:
    """CJK など2カウントで数える文字か。

    X の weighted length は Unicode の範囲表で定義されている。
    ここでは日本語の運用に必要な範囲（CJK 統合漢字・かな・全角記号）を
    見る。範囲を厳密に写すより、予算を安全側に見積もることを優先する。
    """
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF  # ハングル字母
        or 0x2E80 <= code <= 0xA4CF  # CJK 部首〜かな〜漢字
        or 0xAC00 <= code <= 0xD7A3  # ハングル音節
        or 0xF900 <= code <= 0xFAFF  # CJK 互換漢字
        or 0xFE30 <= code <= 0xFE4F  # CJK 互換形
        or 0xFF00 <= code <= 0xFF60  # 全角英数・記号
        or 0xFFE0 <= code <= 0xFFE6  # 全角記号
    )


class PostKind(StrEnum):
    """投稿の型。型ごとに生成スキーマと字数予算が違う。"""

    SINGLE = "single"
    THREAD = "thread"
    CARD = "card"
    PROMO = "promo"


class PostStatus(StrEnum):
    """投稿の状態。"""

    DRAFTED = "drafted"
    SCHEDULED = "scheduled"
    POSTING = "posting"
    POSTED = "posted"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


# 許可される遷移。
#
# POSTING -> SCHEDULED が**無い**のが最も重要な点。送信の直前に
# POSTING にしてから API を呼ぶので、POSTING で残った行は
# 「届いたか分からない」行。キューに戻すと同じ内容が2回公開される。
# 取りこぼし（NEEDS_REVIEW に落として人が見る）のほうが安全。
_ALLOWED_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    PostStatus.DRAFTED: frozenset({PostStatus.SCHEDULED, PostStatus.FAILED}),
    PostStatus.SCHEDULED: frozenset(
        {PostStatus.POSTING, PostStatus.FAILED, PostStatus.NEEDS_REVIEW}
    ),
    PostStatus.POSTING: frozenset({PostStatus.POSTED, PostStatus.FAILED, PostStatus.NEEDS_REVIEW}),
    PostStatus.POSTED: frozenset(),
    PostStatus.FAILED: frozenset({PostStatus.SCHEDULED}),  # 手動での再実行
    PostStatus.NEEDS_REVIEW: frozenset({PostStatus.SCHEDULED, PostStatus.FAILED}),
}

TERMINAL_STATUSES = frozenset({PostStatus.POSTED, PostStatus.FAILED})


class InvalidPostTransition(Exception):
    """許可されていない状態遷移。"""


def check_post_transition(current: PostStatus, new: PostStatus) -> None:
    """状態遷移が許可されているか検証する。

    Args:
        current: 現在の状態
        new: 遷移先

    Raises:
        InvalidPostTransition: 許可されていない遷移
    """
    if new not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidPostTransition(f"{current} -> {new} は許可されていません")


@dataclass(frozen=True)
class NewPost:
    """これから積む投稿1件。

    Attributes:
        article_id: 元記事のID
        article_title: 元記事のタイトル（表示用。記事が消えても残す）
        kind: 投稿の型
        body: 投稿本文（出典表記を含む完成形）
        has_link: URL を含むか。コスト概算に使う（単価が13倍違う）
        position: スレッド内の順序。単発は 0
        image_key: 画像カードの保存先キー
    """

    article_id: str
    article_title: str
    kind: PostKind
    body: str
    has_link: bool
    position: int = 0
    image_key: str | None = None

    @property
    def weighted_length(self) -> int:
        """X の数え方での文字数。"""
        return weighted_length(self.body)


@dataclass(frozen=True)
class SocialPost:
    """投稿1件の読み取り用の写し。

    DB の行をそのまま渡さない理由: セッションを閉じた後に属性を触ると
    SQLAlchemy が `DetachedInstanceError` を投げる。
    """

    id: int
    group_id: str
    position: int
    article_id: str
    article_title: str
    kind: PostKind
    body: str
    weighted_length: int
    has_link: bool
    image_key: str | None
    status: PostStatus
    scheduled_at: datetime | None
    posted_at: datetime | None
    tweet_id: str | None
    reply_to_tweet_id: str | None
    attempts: int
    error_message: str | None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        """もう変化しない状態か。"""
        return self.status in TERMINAL_STATUSES
