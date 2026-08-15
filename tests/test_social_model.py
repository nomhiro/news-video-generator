"""投稿のドメインモデル。"""

import pytest
from src.models.social import (
    InvalidPostTransition,
    PostStatus,
    check_post_transition,
    weighted_length,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", 5),
        ("こんにちは", 10),  # CJK は1文字2カウント
        ("AIとLLM", 7),  # "AI"=2, "と"=2, "LLM"=3 -> 2+2+3
        ("", 0),
    ],
)
def test_weighted_length(text: str, expected: int) -> None:
    """X の文字数は weighted length で、CJK は2カウント。

    日本語140字が上限になるのはこの規則から来る。素の len() で
    数えると140字の投稿が280カウントで弾かれる。
    """
    assert weighted_length(text) == expected


def test_url_は_23カウント固定():
    """t.co で短縮されるため、実際の長さに関係なく23。"""
    short = weighted_length("https://a.co/x")
    long = weighted_length("https://example.com/very/long/path/to/an/article/page")

    assert short == long == 23


def test_許可された遷移は通る():
    check_post_transition(PostStatus.SCHEDULED, PostStatus.POSTING)
    check_post_transition(PostStatus.POSTING, PostStatus.POSTED)
    check_post_transition(PostStatus.POSTING, PostStatus.NEEDS_REVIEW)


def test_POSTED_からは_どこにも遷移できない():
    """二重投稿を型で防ぐ。終端に来た行はワーカーが二度と触らない。"""
    with pytest.raises(InvalidPostTransition):
        check_post_transition(PostStatus.POSTED, PostStatus.SCHEDULED)


def test_POSTING_から_SCHEDULED_に戻せない():
    """送信が届いたか分からない行を、キューに戻して再送してはいけない。"""
    with pytest.raises(InvalidPostTransition):
        check_post_transition(PostStatus.POSTING, PostStatus.SCHEDULED)
