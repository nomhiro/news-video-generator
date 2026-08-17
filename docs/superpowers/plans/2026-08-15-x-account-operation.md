# X アカウント運用 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI・IT ニュースを1日4テーマ、X アカウントへ完全自動で投稿する仕組みを作る。

**Architecture:** 動画生成の `jobs` 表には載せず、`social_posts` テーブルと `PostWorker` を別に立てる。動画ジョブと投稿は失敗の意味が違う（再生成でクォータを食うだけか、同じ内容が2回公開されるか）ため、状態機械を分ける。「もう投稿した」の記録だけは、リビジョン更新で消えない Azure Files 上の記事データに置く。

**Tech Stack:** Python 3.12 / FastAPI / HTMX / SQLAlchemy 2.0 + Alembic / pydantic-settings / Azure OpenAI (gpt-5.1, gpt-image-2) / X API v2 / Tailwind v4

**Spec:** `docs/superpowers/specs/2026-08-15-x-account-operation-design.md`

## Global Constraints

これらは全タスクの要件に含まれる。

- コメントと docstring は**日本語**。「何をしているか」ではなく**なぜそうしたか**を書く
- 例外を包むときは `from e` を付ける（ruff B904）
- `zip()` は長さが一致するはずの場所では `strict=True`
- 設定を足すときは `config.py` と `.env.example` の**両方**を更新する（`tests/test_config.py` が双方向に突き合わせる）
- API キー・シークレットは `SecretStr`。使うときは `.get_secret_value()`
- リスト型の設定は `CommaSeparated`（`Annotated[list[str], NoDecode]`）を使い、`mode="before"` のバリデータで分割する
- SQLite から読んだ `datetime` は `_as_utc` で UTC を付け直す
- テンプレートに新しい Tailwind クラスを使ったら `npm run build:css`
- 検査は `.githooks/pre-push` で走る（`uv run ruff check . && uv run ruff format --check .`、`uv run mypy`、`uv run pytest -m "not live"`）
- ログの絵文字は `src/utils/logger.py` の `log_step` / `prefix()` を通す。新しい絵文字は `_EMOJI_PROBE` にも足す
- コミットは各タスクの中で細かく行う。`main` へは PR 経由

## 前提となる外部条件（コードに埋めない）

- X API は従量課金。投稿 $0.015/件、**URL を含む投稿は $0.20/件**。単価は設定に出す
- X の文字数は weighted length。**CJK は1文字 = 2カウント、上限 280**（日本語 140字）
- X の refresh token は**単回使用でローテートする**。更新したら必ず保存先へ書き戻す
- `images.generate` に system prompt は無い。固定の指示はプロンプトへの前置

---

## File Structure

| ファイル | 責務 |
|---|---|
| `src/models/news.py`（変更） | `consumed` によるチャネル別の消費記録 |
| `src/news/aggregator.py`（変更） | `mark_consumed` / `pick_unconsumed` |
| `src/jobs/planner.py`（変更） | `_pick_candidates` を `pick_unconsumed` に寄せる |
| `src/models/social.py`（新規） | 投稿のドメインモデル・状態遷移・weighted length |
| `src/storage/tables.py`（変更） | `SocialPostRecord` |
| `src/storage/social.py`（新規） | `social_posts` の読み書き |
| `migrations/versions/*_create_social_posts_table.py`（新規） | スキーマ |
| `src/social/grounding.py`（新規） | 数値の grounding 検証 |
| `src/social/post_generator.py`（新規） | 投稿の下書き生成（Structured Outputs） |
| `src/social/x_auth.py`（新規） | OAuth 2.0 + PKCE、トークンの読み書き |
| `src/storage/tokens.py`（変更） | `X_TOKEN` の名前を足す |
| `src/social/x_client.py`（新規） | X API v2 のラッパ（Protocol + 実装） |
| `src/social/switch.py`（新規） | 自動投稿の有効/無効（Azure Files 上の JSON） |
| `src/social/cost.py`（新規） | 概算コストと上限判定 |
| `src/jobs/post_planner.py`（新規） | 記事を選び下書きを積む |
| `src/jobs/post_worker.py`（新規） | 予定時刻を過ぎた行を投稿する |
| `src/social/card_visual.py`（新規） | 画像カードの視覚指示と固定スタイル文 |
| `src/social/metrics.py`（新規） | 指標の取得と Blob への日次記録 |
| `templates/*`（変更）/ `static/css/input.css`（変更） | 見張る卓としての画面 |

---

## Task 1: 記事のチャネル別消費記録

既存コードに触る唯一のタスク。単独で入れて回帰を確認する。

**Files:**
- Modify: `src/models/news.py:45-118`
- Modify: `src/news/aggregator.py:347-365`
- Modify: `src/jobs/planner.py:154-180`
- Test: `tests/test_news_model.py`, `tests/test_aggregator_consumed.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `src.models.news.CHANNEL_VIDEO: str = "video"`, `CHANNEL_X: str = "x"`
  - `NewsArticle.consumed: dict[str, str]`（チャネル → ISO 文字列）
  - `NewsArticle.is_consumed_by(channel: str) -> bool`
  - `NewsArticle.mark_consumed(channel: str, at: datetime | None = None) -> None`
  - `NewsArticle.video_generated -> bool`（読み取り専用 property）
  - `NewsAggregator.mark_consumed(article_id: str, channel: str) -> bool`
  - `NewsAggregator.pick_unconsumed(channel: str, needed: int) -> list[NewsArticle]`

- [ ] **Step 1: 旧データを読めることのテストを書く**

`tests/test_news_model.py` に追記する。

```python
def test_旧形式の_video_generated_を_consumed_として読む():
    """既存の data/news/*.json を移行スクリプトなしで読めること。

    クラウドの Azure Files 上には旧形式の JSON が既に存在する。
    読めなくなると記事一覧が空になり、生成対象を全て見失う。
    """
    data = {
        "id": "abc123",
        "title": "テスト記事",
        "url": "https://example.com/a",
        "source": "Example",
        "category": "ai",
        "fetched_at": "2026-08-01T10:00:00",
        "video_generated": True,
    }

    article = NewsArticle.from_dict(data)

    assert article.is_consumed_by(CHANNEL_VIDEO) is True
    assert article.video_generated is True
    assert article.is_consumed_by(CHANNEL_X) is False
    assert article.consumed[CHANNEL_VIDEO] == "2026-08-01T10:00:00"


def test_未消費の記事は_どのチャネルでも未消費():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/b", source="s", category=NewsCategory.AI
    )

    assert article.consumed == {}
    assert article.video_generated is False
    assert article.is_consumed_by(CHANNEL_X) is False


def test_mark_consumed_は_他のチャネルを消さない():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/c", source="s", category=NewsCategory.AI
    )

    article.mark_consumed(CHANNEL_VIDEO)
    article.mark_consumed(CHANNEL_X)

    assert article.is_consumed_by(CHANNEL_VIDEO) is True
    assert article.is_consumed_by(CHANNEL_X) is True


def test_to_dict_は_video_generated_を_出力しない():
    """出力に旧フィールドを残すと、権威が2つになって食い違う。"""
    article = NewsArticle(
        id="x", title="t", url="https://example.com/d", source="s", category=NewsCategory.AI
    )
    article.mark_consumed(CHANNEL_VIDEO)

    data = article.to_dict()

    assert "video_generated" not in data
    assert data["consumed"] == article.consumed


def test_from_dict_は_to_dict_の_出力を_復元できる():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/e", source="s", category=NewsCategory.AI
    )
    article.mark_consumed(CHANNEL_X)

    restored = NewsArticle.from_dict(article.to_dict())

    assert restored.consumed == article.consumed
```

ファイル冒頭の import に `CHANNEL_VIDEO`, `CHANNEL_X` を足す。

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_news_model.py -v`
Expected: FAIL（`ImportError: cannot import name 'CHANNEL_VIDEO'`）

- [ ] **Step 3: `NewsArticle` を変更する**

`src/models/news.py`。`video_generated: bool = False` フィールドを削除し、以下を入れる。

```python
# 記事を消費したチャネルの名前。
#
# 動画1本ぶんの `video_generated: bool` から一般化した。X が動画への
# 導線ではなく独立した発信の柱になったため、フラグ1本では
# 「動画にはしたが X には出していない」を表せない。
CHANNEL_VIDEO = "video"
CHANNEL_X = "x"


@dataclass
class NewsArticle:
    ...
    is_selected: bool = False
    # チャネル名 -> 消費した時刻（ISO 文字列）。
    #
    # **これが「もう投稿した」の権威。** ジョブ表の SQLite はコンテナの
    # ローカルディスクにあってリビジョン更新で消えるため、そこに置くと
    # デプロイ直後に同じ記事が再投稿される。記事データは Azure Files に
    # あるので残る。
    consumed: dict[str, str] = field(default_factory=dict)

    @property
    def video_generated(self) -> bool:
        """動画を作り終えているか。

        `consumed` に一般化する前のフィールド名。テンプレートと
        `planner._pick_candidates` が参照しているため property で受ける。
        書き込みは `mark_consumed` を使う（権威を1箇所に保つ）。
        """
        return self.is_consumed_by(CHANNEL_VIDEO)

    def is_consumed_by(self, channel: str) -> bool:
        """そのチャネルで既に使ったか。"""
        return channel in self.consumed

    def mark_consumed(self, channel: str, at: datetime | None = None) -> None:
        """そのチャネルで使ったと記録する。

        他のチャネルの記録は消さない（動画と X の両方で使う運用なので、
        上書きすると片方の記録を失う）。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            at: 消費時刻。省略時は現在時刻
        """
        self.consumed[channel] = (at or datetime.now()).isoformat()
```

`from_dict` に旧形式の読み替えを入れる。

```python
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsArticle":
        data = data.copy()
        data["category"] = NewsCategory(data["category"])
        if data.get("published_at"):
            data["published_at"] = datetime.fromisoformat(data["published_at"])
        if data.get("fetched_at"):
            data["fetched_at"] = datetime.fromisoformat(data["fetched_at"])

        # 旧形式（video_generated: bool）を consumed に読み替える。
        #
        # 移行スクリプトを書かない理由: クラウドの Azure Files 上の JSON を
        # 書き換える手順が必要になり、その手順を実行し忘れた状態で
        # デプロイすると記事を全部読めなくなる。読み込み時に変換すれば、
        # 次回の保存で自然に新形式になる。
        legacy = data.pop("video_generated", None)
        if legacy and not data.get("consumed"):
            fetched = data.get("fetched_at")
            stamp = fetched.isoformat() if isinstance(fetched, datetime) else ""
            data["consumed"] = {CHANNEL_VIDEO: stamp or datetime.now().isoformat()}

        return cls(**data)
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_news_model.py -v`
Expected: PASS

- [ ] **Step 5: aggregator のテストを書く**

`tests/test_aggregator_consumed.py` を新規作成する。

```python
"""記事のチャネル別消費記録の読み書き。"""

from pathlib import Path

import pytest

from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator


@pytest.fixture
def aggregator(tmp_path: Path) -> NewsAggregator:
    return NewsAggregator(data_dir=tmp_path)


def _store(aggregator: NewsAggregator, *articles: NewsArticle) -> None:
    aggregator._save_category(NewsCategory.AI, list(articles))


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


def test_mark_consumed_は_保存される(aggregator: NewsAggregator) -> None:
    article = _article("a")
    _store(aggregator, article)

    assert aggregator.mark_consumed(article.id, CHANNEL_X) is True

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.is_consumed_by(CHANNEL_X) is True
    assert reloaded.is_consumed_by(CHANNEL_VIDEO) is False


def test_mark_as_generated_は_video_チャネルを記録する(aggregator: NewsAggregator) -> None:
    """既存の呼び出し元（PipelineJobRunner）を壊さないこと。"""
    article = _article("b")
    _store(aggregator, article)

    assert aggregator.mark_as_generated(article.id) is True

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.video_generated is True
    assert reloaded.is_selected is False


def test_pick_unconsumed_は_そのチャネルで未使用の記事だけ返す(
    aggregator: NewsAggregator,
) -> None:
    used_by_x, used_by_video, fresh = _article("c"), _article("d"), _article("e")
    used_by_x.mark_consumed(CHANNEL_X)
    used_by_video.mark_consumed(CHANNEL_VIDEO)
    _store(aggregator, used_by_x, used_by_video, fresh)

    picked = aggregator.pick_unconsumed(CHANNEL_X, needed=10)

    ids = {a.id for a in picked}
    assert used_by_x.id not in ids
    # 動画で使った記事は X には出せる（両チャネルで使う運用）
    assert used_by_video.id in ids
    assert fresh.id in ids


def test_pick_unconsumed_は_needed_件で打ち切る(aggregator: NewsAggregator) -> None:
    _store(aggregator, _article("f"), _article("g"), _article("h"))

    assert len(aggregator.pick_unconsumed(CHANNEL_X, needed=2)) == 2
```

- [ ] **Step 6: 失敗を確認する**

Run: `uv run pytest tests/test_aggregator_consumed.py -v`
Expected: FAIL（`AttributeError: 'NewsAggregator' object has no attribute 'mark_consumed'`）

- [ ] **Step 7: aggregator に追加する**

`src/news/aggregator.py`。`mark_as_generated` を置き換える。

```python
    def mark_consumed(self, article_id: str, channel: str) -> bool:
        """記事をそのチャネルで消費済みとしてマークする。

        動画生成は threadpool のスレッドから、投稿は PostWorker の
        スレッドから呼ばれ、イベントループ側の toggle_selection と
        同時に走りうる。`_update_article` がロックで直列化する。

        Args:
            article_id: 記事ID
            channel: CHANNEL_VIDEO / CHANNEL_X

        Returns:
            bool: 記事が見つかって更新できたか
        """

        def mark(article: NewsArticle) -> None:
            article.mark_consumed(channel)

        return self._update_article(article_id, mark) is not None

    def mark_as_generated(self, article_id: str) -> bool:
        """記事を動画生成済みとしてマークし、選択を外す。

        選択を外すのは動画だけの都合（画面の「選択した記事」から消す）。
        X の投稿では選択状態に触らないため、mark_consumed とは分けている。

        Args:
            article_id: 記事ID

        Returns:
            bool: 成功したかどうか
        """

        def mark(article: NewsArticle) -> None:
            article.mark_consumed(CHANNEL_VIDEO)
            article.is_selected = False

        return self._update_article(article_id, mark) is not None

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を選ぶ。

        AI カテゴリを優先する。このアカウントの主題が AI・技術ニュースで、
        独自解説を載せやすいのがこの分野だから。足りなければ
        technology で補う。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            needed: 必要な件数

        Returns:
            list[NewsArticle]: 選んだ記事（足りなければ少なく返す）
        """
        picked: list[NewsArticle] = []
        seen: set[str] = set()

        for category in (NewsCategory.AI, NewsCategory.TECHNOLOGY):
            for article in self.get_articles_by_category(category):
                if len(picked) >= needed:
                    return picked
                if article.is_consumed_by(channel) or article.id in seen:
                    continue
                seen.add(article.id)
                picked.append(article)

        return picked
```

import に `CHANNEL_VIDEO` を足す。

- [ ] **Step 8: テストが通ることを確認する**

Run: `uv run pytest tests/test_aggregator_consumed.py tests/test_news_model.py -v`
Expected: PASS

- [ ] **Step 9: planner を `pick_unconsumed` に寄せる**

`src/jobs/planner.py`。`_pick_candidates` を削除し、`SupportsNewsFetching` Protocol に
`pick_unconsumed` を足して呼び出しを差し替える。

```python
class SupportsNewsFetching(Protocol):
    ...
    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を返す。"""
        ...
```

```python
    needed = articles_per_format * len(formats)
    candidates = news.pick_unconsumed(CHANNEL_VIDEO, needed)
```

`get_articles_by_category` は Protocol から外す（planner が使わなくなる）。

- [ ] **Step 10: 全体の回帰を確認する**

Run: `uv run pytest -m "not live" -q`
Expected: PASS。落ちるならフェイクの `SupportsNewsFetching` 実装に
`pick_unconsumed` を足す（`tests/test_schedule.py` にある）

- [ ] **Step 11: 型と lint**

Run: `uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: エラーなし

- [ ] **Step 12: コミット**

```bash
git add src/models/news.py src/news/aggregator.py src/jobs/planner.py tests/
git commit -m "Record which channel consumed each article"
```

---

## Task 2: `social_posts` テーブルと状態機械

投稿はまだしない。行を作って読める状態まで。

**Files:**
- Create: `src/models/social.py`
- Create: `src/storage/social.py`
- Create: `migrations/versions/<rev>_create_social_posts_table.py`
- Modify: `src/storage/tables.py`
- Test: `tests/test_social_model.py`, `tests/test_social_repository.py`

**Interfaces:**
- Consumes: Task 1 の `CHANNEL_X`
- Produces:
  - `PostKind(StrEnum)`: `SINGLE="single"` / `THREAD="thread"` / `CARD="card"` / `PROMO="promo"`
  - `PostStatus(StrEnum)`: `DRAFTED` / `SCHEDULED` / `POSTING` / `POSTED` / `FAILED` / `NEEDS_REVIEW`
  - `check_post_transition(current: PostStatus, new: PostStatus) -> None` / `InvalidPostTransition`
  - `weighted_length(text: str) -> int`
  - `SocialPost`（frozen dataclass。列と同名の属性）
  - `NewPost`（frozen dataclass: `article_id` / `article_title` / `kind` / `body` / `has_link` / `position` / `image_key`）
  - `SocialPostRepository`: `enqueue(posts, scheduled_at_by_position) -> str`,
    `claim_due(now) -> SocialPost | None`, `mark_posted(post_id, tweet_id)`,
    `mark_failed(post_id, reason)`, `mark_needs_review(post_id, reason)`,
    `recover_stuck_posting(reason) -> int`, `discard_stale(now, max_delay_minutes) -> int`,
    `list_upcoming(limit) -> list[SocialPost]`, `list_needs_review() -> list[SocialPost]`,
    `monthly_post_counts(year, month) -> tuple[int, int]`,
    `group_posted_tweet_id(group_id, position) -> str | None`

- [ ] **Step 1: weighted length と状態遷移のテストを書く**

`tests/test_social_model.py` を新規作成する。

```python
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
        ("AIとLLM", 4 + 2 + 2),  # "AI"=2, "と"=2, "LLM"=3 -> 2+2+3
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
```

`"AIとLLM"` の期待値は `2 + 2 + 3 = 7`。パラメータを `7` に直して書く。

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_social_model.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.models.social'`）

- [ ] **Step 3: `src/models/social.py` を書く**

```python
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

_URL_PATTERN = re.compile(r"https?://\S+")


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
    without_urls = _URL_PATTERN.sub("", text)
    url_count = len(_URL_PATTERN.findall(text))

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
    PostStatus.POSTING: frozenset(
        {PostStatus.POSTED, PostStatus.FAILED, PostStatus.NEEDS_REVIEW}
    ),
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
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_social_model.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/models/social.py tests/test_social_model.py
git commit -m "Model X posts with their own state machine"
```

- [ ] **Step 6: リポジトリのテストを書く**

`tests/test_social_repository.py` を新規作成する。`tests/test_jobs.py` の
fixture の作り方に合わせる（in-memory ではなく `tmp_path` の SQLite）。

```python
"""social_posts の読み書き。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.social import NewPost, PostKind, PostStatus
from src.storage.db import create_db_engine, create_session_factory
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository


@pytest.fixture
def repository(tmp_path: Path) -> SocialPostRepository:
    """既存 tests/test_jobs.py と同じ作り方。

    `create_all` ではなく `upgrade_to_head` を使う。マイグレーションを
    通しておかないと、Alembic の当て漏れをテストが検出できない。
    """
    url = f"sqlite:///{(tmp_path / 'social.db').as_posix()}"
    upgrade_to_head(url)
    return SocialPostRepository(create_session_factory(create_db_engine(url)))


def _post(position: int = 0, has_link: bool = False) -> NewPost:
    return NewPost(
        article_id="a1",
        article_title="テスト記事",
        kind=PostKind.SINGLE,
        body="本文",
        has_link=has_link,
        position=position,
    )


def test_claim_due_は_予定時刻を過ぎた行だけ返す(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=1)})
    repository.enqueue([_post()], {0: now + timedelta(hours=1)})

    claimed = repository.claim_due(now)

    assert claimed is not None
    assert claimed.status is PostStatus.POSTING
    # 2件目はまだ来ていない
    assert repository.claim_due(now) is None


def test_claim_due_は_同じ行を二度返さない(repository: SocialPostRepository) -> None:
    """POSTING にした行を再び掴むと、同じ内容が2回公開される。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})

    assert repository.claim_due(now) is not None
    assert repository.claim_due(now) is None


def test_recover_stuck_posting_は_NEEDS_REVIEW_にする(
    repository: SocialPostRepository,
) -> None:
    """これがこの計画で最も重要な回帰テスト。

    POSTING で残った行を SCHEDULED に戻すと、送信が届いていた場合に
    同じ投稿が2つ並ぶ。自動では再送しない。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})
    claimed = repository.claim_due(now)
    assert claimed is not None

    recovered = repository.recover_stuck_posting("プロセスが再起動しました")

    assert recovered == 1
    reviewed = repository.list_needs_review()
    assert [p.id for p in reviewed] == [claimed.id]
    # 掴み直せないこと（再送されない）
    assert repository.claim_due(now) is None


def test_discard_stale_は_遅れすぎた行を捨てる(repository: SocialPostRepository) -> None:
    """復帰した瞬間に溜まった投稿が連投されるとスパムに見える。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=90)})
    repository.enqueue([_post()], {0: now - timedelta(minutes=10)})

    discarded = repository.discard_stale(now, max_delay_minutes=60)

    assert discarded == 1
    claimed = repository.claim_due(now)
    assert claimed is not None
    assert repository.claim_due(now) is None


def test_monthly_post_counts_は_リンク有無で分ける(repository: SocialPostRepository) -> None:
    """単価が13倍違うので、混ぜて数えるとコスト概算が意味を失う。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    for has_link in (False, False, True):
        repository.enqueue([_post(has_link=has_link)], {0: now})
        claimed = repository.claim_due(now)
        assert claimed is not None
        repository.mark_posted(claimed.id, tweet_id="1", posted_at=now)

    plain, with_link = repository.monthly_post_counts(2026, 8)

    assert (plain, with_link) == (2, 1)


def test_スレッドは_group_id_でまとまる(repository: SocialPostRepository) -> None:
    group_id = repository.enqueue(
        [_post(position=0), _post(position=1)],
        {0: datetime(2026, 8, 15, 12, 0, tzinfo=UTC), 1: datetime(2026, 8, 15, 12, 0, tzinfo=UTC)},
    )

    upcoming = repository.list_upcoming(limit=10)

    assert {p.group_id for p in upcoming} == {group_id}
    assert sorted(p.position for p in upcoming) == [0, 1]
```

`build_session_factory` / `create_all` の実際の名前は `src/storage/db.py` を読んで合わせる。
違う場合はそちらの名前を使う（`tests/test_jobs.py` が既に使っている形に倣う）。

- [ ] **Step 7: 失敗を確認する**

Run: `uv run pytest tests/test_social_repository.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.storage.social'`）

- [ ] **Step 8: `SocialPostRecord` を足す**

`src/storage/tables.py` に追記する。

```python
class SocialPostRecord(Base):
    """X 投稿1件。

    スレッドは `group_id` でまとめ、`position` で順序を持つ。
    1行 = 1テーマ（下書き全体を JSON）にしなかった理由: スレッドの途中で
    失敗したときに「どこまで出せたか」が行として観測できない。
    """

    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    group_id: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    article_id: Mapped[str] = mapped_column(String(64), index=True)
    article_title: Mapped[str] = mapped_column(Text)

    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    # 生成時に計算した weighted length。画面に「118/140」と出す。
    weighted_length: Mapped[int] = mapped_column(Integer, default=0)
    # URL を含むか。単価が $0.015 と $0.20 で13倍違うため、
    # コスト概算にはこの区別が必須。
    has_link: Mapped[bool] = mapped_column(Boolean, default=False)
    image_key: Mapped[str | None] = mapped_column(Text, default=None)

    status: Mapped[str] = mapped_column(String(16), index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    tweet_id: Mapped[str | None] = mapped_column(String(32), default=None)
    reply_to_tweet_id: Mapped[str | None] = mapped_column(String(32), default=None)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # ワーカーの検索は「SCHEDULED で予定時刻を過ぎた最古の1件」。
        Index("ix_social_posts_status_scheduled_at", "status", "scheduled_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<SocialPostRecord id={self.id} status={self.status} kind={self.kind}>"
```

import に `Boolean` を足す。

- [ ] **Step 9: `src/storage/social.py` を書く**

`src/storage/jobs.py` の `_as_utc` / `session_scope` / 「UPDATE の影響行数で競合を検出」の
方式をそのまま踏襲する。リースと heartbeat は**持たない**（投稿は数秒で終わる）。

```python
"""social_posts の読み書き。

ジョブ表（src/storage/jobs.py）との違い
--------------------------------------
リースと heartbeat を持たない。投稿は数秒で終わるので、15分のリースを
延ばし続ける仕組みは意味を持たない。

代わりに `recover_stuck_posting()` を持つ。POSTING で残った行を
**SCHEDULED に戻さず NEEDS_REVIEW にする**のがジョブ表との決定的な違い。
X API に冪等キーが無く、送信が届いたか分からない行を再送すると
同じ内容が2回公開される。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from src.models.social import (
    NewPost,
    PostKind,
    PostStatus,
    SocialPost,
    check_post_transition,
)
from src.storage.db import session_scope
from src.storage.tables import SocialPostRecord, utcnow


def _as_utc(value: datetime | None) -> datetime | None:
    """naive な datetime に UTC を補う。

    SQLite は `DateTime(timezone=True)` でもタイムゾーンを保存しない。
    付け直さないと `scheduled_at <= now` の比較が
    `can't compare offset-naive and offset-aware datetimes` で落ちる。
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_domain(record: SocialPostRecord) -> SocialPost:
    """行をドメインの写しに変換する。"""
    created_at = _as_utc(record.created_at)
    assert created_at is not None  # NOT NULL
    return SocialPost(
        id=record.id,
        group_id=record.group_id,
        position=record.position,
        article_id=record.article_id,
        article_title=record.article_title,
        kind=PostKind(record.kind),
        body=record.body,
        weighted_length=record.weighted_length,
        has_link=record.has_link,
        image_key=record.image_key,
        status=PostStatus(record.status),
        scheduled_at=_as_utc(record.scheduled_at),
        posted_at=_as_utc(record.posted_at),
        tweet_id=record.tweet_id,
        reply_to_tweet_id=record.reply_to_tweet_id,
        attempts=record.attempts,
        error_message=record.error_message,
        created_at=created_at,
    )


class SocialPostRepository:
    """social_posts への操作。

    セッションは呼び出しごとに開いて閉じる（SQLAlchemy の Session は
    スレッドセーフでなく、ワーカースレッドとイベントループが
    同じリポジトリを共有する）。
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def enqueue(
        self, posts: list[NewPost], scheduled_at_by_position: dict[int, datetime]
    ) -> str:
        """投稿を1つのまとまりとして積む。

        Args:
            posts: 積む投稿
            scheduled_at_by_position: position -> 予定時刻

        Returns:
            str: group_id

        Raises:
            ValueError: 投稿が空、または予定時刻の無い position がある
        """
        if not posts:
            raise ValueError("積む投稿がありません")
        missing = [p.position for p in posts if p.position not in scheduled_at_by_position]
        if missing:
            raise ValueError(f"予定時刻の無い position があります: {missing}")

        group_id = str(uuid.uuid4())
        with session_scope(self._sessions) as session:
            session.add_all(
                SocialPostRecord(
                    group_id=group_id,
                    position=post.position,
                    article_id=post.article_id,
                    article_title=post.article_title,
                    kind=post.kind,
                    body=post.body,
                    weighted_length=post.weighted_length,
                    has_link=post.has_link,
                    image_key=post.image_key,
                    status=PostStatus.SCHEDULED,
                    scheduled_at=scheduled_at_by_position[post.position],
                )
                for post in posts
            )
        return group_id

```

`claim_due` は `src/storage/jobs.py:claim_next` と同じ構造で書く。

```python
    def claim_due(self, now: datetime) -> SocialPost | None:
        """予定時刻を過ぎた SCHEDULED を1件掴んで POSTING にする。

        スレッドは position 順に出す必要があるため、
        `(scheduled_at, group_id, position)` の順で最古を取る。

        Args:
            now: 現在時刻（UTC aware）

        Returns:
            SocialPost | None: 掴んだ投稿。無ければ None
        """
        with session_scope(self._sessions) as session:
            candidate = session.scalars(
                select(SocialPostRecord)
                .where(
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                    SocialPostRecord.scheduled_at.is_not(None),
                    SocialPostRecord.scheduled_at <= now,
                )
                .order_by(
                    SocialPostRecord.scheduled_at,
                    SocialPostRecord.group_id,
                    SocialPostRecord.position,
                )
                .limit(1)
            ).first()
            if candidate is None:
                return None

            # 「status が SCHEDULED のままである」ことを条件にした UPDATE。
            # SQLite には SKIP LOCKED が無いため、影響行数で競合を検出する。
            claimed_rows = session.execute(
                update(SocialPostRecord)
                .where(
                    SocialPostRecord.id == candidate.id,
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                )
                .values(status=PostStatus.POSTING, attempts=candidate.attempts + 1)
            ).rowcount  # type: ignore[attr-defined]
            if claimed_rows != 1:
                return None

            session.flush()
            claimed = session.get(SocialPostRecord, candidate.id)
            assert claimed is not None
            return _to_domain(claimed)
```

残りのメソッド。

```python
    def mark_posted(self, post_id: int, tweet_id: str, posted_at: datetime | None = None) -> None:
        """投稿できたと記録する。"""
        self._transition(
            post_id, PostStatus.POSTED, tweet_id=tweet_id, posted_at=posted_at or utcnow()
        )

    def mark_failed(self, post_id: int, reason: str) -> None:
        """送信前に失敗したと記録する（再実行できる）。"""
        self._transition(post_id, PostStatus.FAILED, error_message=reason)

    def mark_needs_review(self, post_id: int, reason: str) -> None:
        """人が見るまで触らない状態にする。"""
        self._transition(post_id, PostStatus.NEEDS_REVIEW, error_message=reason)

    def _transition(
        self,
        post_id: int,
        new_status: PostStatus,
        error_message: str | None = None,
        tweet_id: str | None = None,
        posted_at: datetime | None = None,
    ) -> None:
        with session_scope(self._sessions) as session:
            record = session.get(SocialPostRecord, post_id)
            if record is None:
                return
            check_post_transition(PostStatus(record.status), new_status)
            record.status = new_status
            if error_message is not None:
                record.error_message = error_message
            if tweet_id is not None:
                record.tweet_id = tweet_id
            if posted_at is not None:
                record.posted_at = posted_at

    def recover_stuck_posting(self, reason: str) -> int:
        """POSTING で残った行を NEEDS_REVIEW にする。

        **SCHEDULED に戻さない。** 送信が届いたか分からない行なので、
        再送すると同じ内容が2つ並ぶ。取りこぼしのほうが安全。

        起動時に1回呼ぶ（前回のプロセスが送信中に落ちた分を拾う）。

        Args:
            reason: 画面に出す理由

        Returns:
            int: NEEDS_REVIEW にした件数
        """
        with session_scope(self._sessions) as session:
            stuck = session.scalars(
                select(SocialPostRecord).where(SocialPostRecord.status == PostStatus.POSTING)
            ).all()
            for record in stuck:
                record.status = PostStatus.NEEDS_REVIEW
                record.error_message = reason
            return len(stuck)

    def discard_stale(self, now: datetime, max_delay_minutes: int) -> int:
        """予定時刻から遅れすぎた行を捨てる。

        デプロイやプロセス停止で数時間止まったあと、復帰した瞬間に
        4件が連投されるとスパムに見える。

        Args:
            now: 現在時刻
            max_delay_minutes: これ以上遅れたら捨てる

        Returns:
            int: 捨てた件数
        """
        limit = now - timedelta(minutes=max_delay_minutes)
        with session_scope(self._sessions) as session:
            stale = session.scalars(
                select(SocialPostRecord).where(
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                    SocialPostRecord.scheduled_at.is_not(None),
                    SocialPostRecord.scheduled_at < limit,
                )
            ).all()
            for record in stale:
                record.status = PostStatus.FAILED
                record.error_message = (
                    f"予定時刻から{max_delay_minutes}分以上遅れたため投稿しませんでした"
                )
            return len(stale)

    def list_upcoming(self, limit: int = 20) -> list[SocialPost]:
        """これから出る投稿を予定時刻順に返す。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord)
                .where(
                    SocialPostRecord.status.in_(
                        [PostStatus.DRAFTED, PostStatus.SCHEDULED, PostStatus.POSTING]
                    )
                )
                .order_by(SocialPostRecord.scheduled_at, SocialPostRecord.position)
                .limit(limit)
            ).all()
            return [_to_domain(r) for r in records]

    def list_needs_review(self) -> list[SocialPost]:
        """人が見る必要のある投稿。画面の上の帯にも出す。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord)
                .where(SocialPostRecord.status == PostStatus.NEEDS_REVIEW)
                .order_by(SocialPostRecord.scheduled_at)
            ).all()
            return [_to_domain(r) for r in records]

    def list_posted_between(self, start: datetime, end: datetime) -> list[SocialPost]:
        """期間内に投稿できたものを返す（計測の対象を選ぶのに使う）。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord).where(
                    SocialPostRecord.status == PostStatus.POSTED,
                    SocialPostRecord.posted_at.is_not(None),
                    SocialPostRecord.posted_at >= start,
                    SocialPostRecord.posted_at < end,
                )
            ).all()
            return [_to_domain(r) for r in records]

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        """当月の投稿数を（リンク無し, リンク有り）で返す。

        単価が $0.015 と $0.20 で13倍違うため、混ぜて数えると
        コスト概算が意味を失う。

        Returns:
            tuple[int, int]: (リンク無しの件数, リンク有りの件数)
        """
        start = datetime(year, month, 1, tzinfo=UTC)
        end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
        posts = self.list_posted_between(start, end)
        with_link = sum(1 for p in posts if p.has_link)
        return len(posts) - with_link, with_link

    def group_posted_tweet_id(self, group_id: str, position: int) -> str | None:
        """同じまとまりの指定 position の tweet_id を返す。

        スレッドの返信先（`reply_to_tweet_id`）を決めるのに使う。
        """
        with session_scope(self._sessions) as session:
            return session.scalars(
                select(SocialPostRecord.tweet_id).where(
                    SocialPostRecord.group_id == group_id,
                    SocialPostRecord.position == position,
                )
            ).first()
```

- [ ] **Step 10: テストが通ることを確認する**

Run: `uv run pytest tests/test_social_repository.py -v`
Expected: PASS

- [ ] **Step 11: Alembic マイグレーションを作る**

```bash
uv run alembic revision --autogenerate -m "create social_posts table"
```

**`alembic.ini` と生成されたファイルに日本語コメントを書かない。**
`alembic.ini` はロケールの encoding で読まれるので、cp932 で
`UnicodeDecodeError` になる（一度踏んだ）。マイグレーションファイル本体は
UTF-8 で読まれるが、揃えて英語にする。

- [ ] **Step 12: マイグレーションを当てて確認する**

Run: `uv run alembic upgrade head && uv run pytest -m "not live" -q`
Expected: PASS

- [ ] **Step 13: lint と型、コミット**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/storage/social.py src/storage/tables.py migrations/ tests/test_social_repository.py
git commit -m "Persist X posts in their own table"
```

---

## Task 3: 投稿の下書き生成

生成した下書きを積めるところまで。投稿はまだしない。

**Files:**
- Create: `src/social/__init__.py`, `src/social/grounding.py`, `src/social/post_generator.py`
- Modify: `config.py`, `.env.example`
- Test: `tests/test_grounding.py`, `tests/test_post_generator.py`

**Interfaces:**
- Consumes: `src.models.social.{PostKind, NewPost, weighted_length}`, `src.models.news.NewsArticle`
- Produces:
  - `src.social.grounding.ungrounded_numbers(text: str, source: str) -> set[str]`
  - `src.social.post_generator.PostGenerator(endpoint: str, api_key: str, deployment: str)`
  - `PostGenerator.generate(article: NewsArticle, kind: PostKind, hashtags: list[str]) -> list[NewPost]`
  - `PostGenerationError` / `GroundingError(PostGenerationError)`
  - `BUDGETS: dict[PostKind, tuple[int, int]]`（型 → (下限, 上限) 日本語文字数）

- [ ] **Step 1: grounding のテストを書く**

`tests/test_grounding.py` を新規作成する。

```python
"""生成文の数値が記事に根拠を持つかの検証。

自動投稿なので、機械的な検証だけが捏造への防衛線になる。
"""

from src.social.grounding import ungrounded_numbers


def test_記事にある数値は通る():
    source = "OpenAI は推論コストを 40% 削減したと発表した。"
    text = "推論コストが40%下がった。"

    assert ungrounded_numbers(text, source) == set()


def test_記事に無い数値は検出する():
    source = "OpenAI は推論コストを削減したと発表した。"
    text = "推論コストが40%下がった。"

    assert ungrounded_numbers(text, source) == {"40"}


def test_列挙表現は除外する():
    """「3つのポイント」は投稿の構成であって記事の数値ではない。"""
    source = "新しいモデルが公開された。"
    text = "ポイントは3つある。1つ目は速度だ。2点目はコスト。"

    assert ungrounded_numbers(text, source) == set()


def test_桁区切りのある数値も突き合わせる():
    source = "調達額は 1,200 万ドルだった。"
    text = "1200万ドルを調達した。"

    assert ungrounded_numbers(text, source) == set()


def test_複数の未根拠な数値をすべて返す():
    source = "モデルが公開された。"
    text = "3倍速く、コストは80%減、対応言語は95。"

    assert ungrounded_numbers(text, source) == {"3", "80", "95"}
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: `src/social/grounding.py` を書く**

```python
"""生成した投稿文の数値が、記事本文に根拠を持つかを検証する。

なぜ機械的に検証するか
----------------------
投稿は完全自動で公開される。人が読む工程が無いので、捏造を止める手段は
コードしかない。数値は捏造されると最も害が大きく（誤った統計が拡散する）、
かつ機械的に検証できる唯一の要素なので、ここだけは必ず突き合わせる。

固有名詞は同じ方法では検証できない（記事の言い換えを許す必要がある）。
そちらは「モデルに URL と媒体名を渡さない」ことで防いでいる。
"""

from __future__ import annotations

import re

# 数値の抽出。桁区切りと小数を含む。
_NUMBER_PATTERN = re.compile(r"\d[\d,\.]*")

# 投稿の構成を表す数え上げ。記事の数値ではないので検証から外す。
#
# 「ポイントは3つ」「1つ目は」といった書き方は投稿として自然だが、
# 記事本文には現れない。除外しないと、まともな投稿が毎回破棄される。
_ENUMERATION_SUFFIXES = ("つ", "つ目", "点", "点目", "番目", "個", "回", "度目")


def _normalize(value: str) -> str:
    """比較用に桁区切りと末尾のドットを落とす。

    記事が「1,200」、投稿が「1200」と書くのは正常な言い換えなので、
    そのままでは一致しない。
    """
    return value.replace(",", "").rstrip(".")


def ungrounded_numbers(text: str, source: str) -> set[str]:
    """記事本文に根拠が無い数値を返す。

    Args:
        text: 生成した投稿文
        source: 記事本文（タイトルを含めてよい）

    Returns:
        set[str]: 根拠の無い数値（正規化済み）。空なら合格
    """
    source_numbers = {_normalize(m) for m in _NUMBER_PATTERN.findall(source)}

    ungrounded: set[str] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        raw = match.group()
        # 数え上げ表現は投稿の構成なので見ない
        tail = text[match.end() : match.end() + 3]
        if any(tail.startswith(suffix) for suffix in _ENUMERATION_SUFFIXES):
            continue
        normalized = _normalize(raw)
        if normalized not in source_numbers:
            ungrounded.add(normalized)
    return ungrounded
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: PASS。`_ENUMERATION_SUFFIXES` の並び順で
「1つ目」が「つ」に先にマッチしても結果は同じ（どちらも除外）

- [ ] **Step 5: コミット**

```bash
git add src/social/__init__.py src/social/grounding.py tests/test_grounding.py
git commit -m "Reject numbers the source article does not support"
```

- [ ] **Step 6: 生成のテストを書く**

`tests/test_post_generator.py` を新規作成する。LLM は呼ばず、
`PostGenerator._complete` を差し替えて検証だけを見る。

```python
"""投稿の下書き生成。字数予算と検証の規則を確かめる。"""

import json

import pytest

from src.models.news import NewsArticle, NewsCategory
from src.models.social import PostKind
from src.social.post_generator import (
    BUDGETS,
    GroundingError,
    PostGenerationError,
    PostGenerator,
)


@pytest.fixture
def article() -> NewsArticle:
    return NewsArticle(
        id="a1",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="TechCrunch",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを 40% 削減したと発表した。"
        "開発者は同じ入力を繰り返す用途で恩恵を受ける。",
    )


@pytest.fixture
def generator(monkeypatch: pytest.MonkeyPatch) -> PostGenerator:
    gen = PostGenerator(
        endpoint="https://example.openai.azure.com",
        api_key="dummy",
        deployment="gpt-5.1",
    )
    return gen


def _reply(gen: PostGenerator, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """LLM の応答を固定する。"""
    monkeypatch.setattr(gen, "_complete", lambda *a, **k: json.dumps(payload))


def test_単発ポストに_出典が付く(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出典の媒体名はコード側が差し込む（モデルに渡すと捏造する）。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])

    assert len(posts) == 1
    assert "出典: TechCrunch" in posts[0].body
    assert posts[0].has_link is False
    assert posts[0].kind is PostKind.SINGLE


def test_記事に無い数値があれば_GroundingError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI が推論コストを85%削減。" + "あ" * 95,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(GroundingError) as excinfo:
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])

    assert "85" in str(excinfo.value)


def test_字数が下限を割ったら_PostGenerationError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限だけを見ると下振れする。実測で145字まで縮んだ事例がある。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "推論コストが40%下がった。",
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])


def test_practical_use_が短いと_PostGenerationError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ニュースをなぞるだけの投稿を出さないための必須フィールド。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "便利",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])


def test_スレッドは_投稿ごとに_position_が付く(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "OpenAI がキャッシュ方式で推論コストを40%削減した理由を説明する。" + "あ" * 70
    _reply(
        generator,
        monkeypatch,
        {
            "posts": [body, body, body],
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.THREAD, hashtags=["#AI"])

    assert [p.position for p in posts] == [0, 1, 2]
    # 出典は先頭にだけ付ける（毎投稿に付けると字数を食う）
    assert "出典: TechCrunch" in posts[0].body
    assert "出典" not in posts[1].body


def test_全ての型に予算が定義されている():
    """型を足したときに予算の定義漏れを防ぐ。"""
    assert set(BUDGETS) == set(PostKind)
    for kind, (low, high) in BUDGETS.items():
        assert 0 < low < high, kind
```

- [ ] **Step 7: 失敗を確認する**

Run: `uv run pytest tests/test_post_generator.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.social.post_generator'`）

- [ ] **Step 8: `src/social/post_generator.py` を書く**

`src/generators/script_generator.py` のクライアント組み立て（Azure v1 エンドポイント、
`base_url` に `/openai/v1` を付ける）と Structured Outputs の使い方をそのまま踏襲する。

要点だけ示す。

```python
"""記事から X 投稿の下書きを作る。

なぜ ScriptGenerator に相乗りしないか
------------------------------------
台本は「音声のタイミング同期」という制約を背負っている
（segment_narrations と image_prompts と text_overlays の要素数が
一致しなければ動画合成が失敗する）。投稿にその制約は無く、代わりに
weighted length という別の制約がある。混ぜると両方のスキーマに
相手の都合が入る。

独自解説を必須フィールドで強制する理由
--------------------------------------
ニュースをなぞるだけの投稿は伸びず、引用元の価値を横取りするだけになる。
Structured Outputs では必須フィールドをモデルが省略できないので、
プロンプトでお願いするより保証が強い（台本の technical_insight /
practical_impact と同じ理屈）。
"""

from __future__ import annotations

# 型ごとの本文の字数予算（日本語の文字数、下限と上限）。
#
# **上限だけでなく下限も持つ。** 上限だけを指示すると下振れし、
# 実測では145字まで縮んで文の断片になった（台本での経験）。
#
# 上限が140より小さいのは、出典表記とハッシュタグを足す余地を残すため。
# weighted length では日本語1字が2カウントなので、140字で上限ぴったり。
BUDGETS: dict[PostKind, tuple[int, int]] = {
    PostKind.SINGLE: (105, 125),
    PostKind.THREAD: (100, 130),  # 1投稿あたり
    PostKind.CARD: (60, 90),
    PostKind.PROMO: (70, 100),
}

# スレッドの投稿数。
THREAD_MIN_POSTS = 3
THREAD_MAX_POSTS = 5

# 独自解説の最低文字数。言語非依存にしている
# （スキーマが language を持たないため、言語別の閾値を選べない）。
MIN_INSIGHT_CHARS = 30


class PostGenerationError(Exception):
    """下書きの生成または検証に失敗した。"""


class GroundingError(PostGenerationError):
    """記事本文に根拠の無い数値が含まれていた。"""
```

`PostGenerator` の構造。

- `__init__(endpoint, api_key, deployment)` — `ScriptGenerator` と同じ形で
  `OpenAI(api_key=..., base_url=f"{endpoint}/openai/v1")` を作る
- `_complete(system_prompt: str, user_prompt: str) -> str` — 1回の補完。
  テストがここを差し替えるので、**必ずこの名前で切り出す**
- `generate(article, kind, hashtags) -> list[NewPost]`:
  1. 型に応じたシステムプロンプトを組む（予算を文字列で埋め込む）
  2. `user_prompt` は**記事のタイトルと本文だけ**。`url` と `source` は渡さない
  3. `_complete` を呼び、JSON を読む
  4. `_validate(...)` を通す。落ちたら**1回だけ**再生成し、それでも落ちたら
     例外を投げる
  5. `出典: {article.source}` とハッシュタグを付けて `NewPost` を作る。
     `has_link = "http" in body`

```python
    def _validate(self, body: str, insights: dict[str, str], kind: PostKind, source_text: str) -> None:
        """字数・独自解説・数値の根拠を検証する。

        Raises:
            GroundingError: 記事本文に無い数値があった
            PostGenerationError: 字数または独自解説が要件を満たさない
        """
        low, high = BUDGETS[kind]
        length = len(body.strip())
        if not low <= length <= high:
            raise PostGenerationError(
                f"{kind} の本文が予算外です（{length}字、期待 {low}〜{high}字）"
            )

        weighted = weighted_length(body)
        if weighted > X_MAX_WEIGHTED_LENGTH:
            raise PostGenerationError(
                f"weighted length が上限を超えています（{weighted}/{X_MAX_WEIGHTED_LENGTH}）"
            )

        for name in ("practical_use", "why_now"):
            value = insights.get(name, "").strip()
            if len(value) < MIN_INSIGHT_CHARS:
                raise PostGenerationError(
                    f"{name} が短すぎます（{len(value)}字、最低 {MIN_INSIGHT_CHARS}字）"
                )

        ungrounded = ungrounded_numbers(body, source_text)
        if ungrounded:
            raise GroundingError(
                f"記事本文に根拠の無い数値が含まれています: {sorted(ungrounded)}"
            )
```

システムプロンプトは `SYSTEM_PROMPT_SINGLE` / `_THREAD` / `_CARD` / `_PROMO` の
4定数。`<<BUDGET>>` を予算で置換する（台本の `<<STRUCTURE_SPEC>>` と同じ方式）。
本文には次を必ず書く。

- 各投稿は単独で文として言い切ること（断片にしない）
- 記事本文に無い数値・固有名詞を書かないこと
- URL を書かないこと（`promo` を除く）
- 出典表記とハッシュタグはこちらで付けるので書かないこと

- [ ] **Step 9: テストが通ることを確認する**

Run: `uv run pytest tests/test_post_generator.py -v`
Expected: PASS

- [ ] **Step 10: 設定を足す**

`config.py` に追加する（`.env.example` にも同じ項目と説明を書く）。

```python
    # --- X（旧 Twitter）投稿 ---
    #
    # 既定は無効。完全自動投稿なので、開発中に勝手に公開されると
    # 取り返しがつかない。有効化は画面から行う（下記のスイッチ）。
    x_posting_enabled: bool = Field(default=False)

    x_client_id: str = Field(default="")
    x_client_secret: SecretStr = Field(default=SecretStr(""))
    x_token_file: str = Field(default="x_token.json")
    x_redirect_uri: str = Field(default="http://127.0.0.1:8091/callback")

    # 投稿時刻（SCHEDULE_TIMEZONE のローカル時刻、HH:MM）。
    x_post_times: CommaSeparated = Field(default=["08:00", "12:30", "19:00", "21:30"])

    # 1日のテーマ数（宣伝投稿は含まない）。
    x_posts_per_day: int = Field(default=4, ge=1, le=20)

    # 予定時刻からこれ以上遅れた投稿は捨てる。
    # 止まっていたあと復帰した瞬間の連投を防ぐ。
    x_max_post_delay_minutes: int = Field(default=60, ge=1)

    # 概算コストの上限（USD/月）と単価。
    # 単価を設定に出しているのは、X の料金改定に追随するため。
    x_monthly_budget_usd: float = Field(default=20.0, gt=0)
    x_cost_per_post_usd: float = Field(default=0.015, ge=0)
    x_cost_per_post_with_link_usd: float = Field(default=0.20, ge=0)

    # 固定のハッシュタグ。モデルに作らせない
    # （無関係なタグはスパム判定を受ける）。
    x_hashtags: CommaSeparated = Field(default=["#AI", "#生成AI"])
```

`x_post_times` と `x_hashtags` に `mode="before"` のバリデータを足す
（`_parse_schedule_formats` と同じ形）。`x_post_times` には HH:MM の検証も足す
（`_check_schedule_time` と同じ理由: スケジューラ起動時に落ちるより設定読み込みで弾く）。

`token_paths` に `"x_token": Path(self.x_token_file)` を足す。

- [ ] **Step 11: 設定のテストが通ることを確認する**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS（落ちたら `.env.example` の記載漏れ）

- [ ] **Step 12: lint と型、コミット**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/social/post_generator.py config.py .env.example tests/test_post_generator.py
git commit -m "Generate X post drafts with enforced insight and budget"
```

---

## Task 4: X の認証

**Files:**
- Create: `src/social/x_auth.py`
- Modify: `src/storage/tokens.py`, `scripts/push_tokens.py`
- Test: `tests/test_x_auth.py`

**Interfaces:**
- Consumes: `src.storage.tokens.{TokenStore, read_json, write_json}`
- Produces:
  - `src.storage.tokens.X_TOKEN: str = "x_token"`
  - `XCredentials`（frozen dataclass: `access_token: str`, `refresh_token: str`, `expires_at: datetime`）
  - `XAuthError` / `XTokenExpiredError(XAuthError)`
  - `load_credentials(store: TokenStore) -> XCredentials | None`
  - `ensure_fresh(store: TokenStore, creds: XCredentials, exchange: TokenExchange, now: datetime) -> XCredentials`
  - `TokenExchange` Protocol: `refresh(refresh_token: str) -> dict[str, Any]`
  - `build_authorization_url(client_id, redirect_uri, verifier) -> tuple[str, str]`（URL と state）

- [ ] **Step 1: refresh の書き戻しのテストを書く**

`tests/test_x_auth.py` を新規作成する。

```python
"""X の OAuth トークンの扱い。

X の refresh token は**単回使用でローテートする**。更新のたびに新しい
refresh token が返り、古いものは無効になる。書き戻しに失敗すると
次回の更新ができず、ブラウザでの再認証が必要になる。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.social.x_auth import (
    XCredentials,
    XTokenExpiredError,
    ensure_fresh,
    load_credentials,
)
from src.storage.tokens import X_TOKEN, read_json, write_json


class FakeStore:
    """メモリ上の TokenStore。"""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def read(self, name: str) -> bytes | None:
        return self.data.get(name)

    def write(self, name: str, payload: bytes) -> None:
        self.data[name] = payload

    def exists(self, name: str) -> bool:
        return name in self.data


class FakeExchange:
    """refresh を1回だけ成功させる交換器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.calls.append(refresh_token)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def _save(store: FakeStore, expires_at: datetime) -> None:
    write_json(
        store,
        X_TOKEN,
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": expires_at.isoformat(),
        },
    )


def test_期限内なら_refresh_しない(store: FakeStore) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now + timedelta(hours=1))
    creds = load_credentials(store)
    assert creds is not None
    exchange = FakeExchange()

    fresh = ensure_fresh(store, creds, exchange, now=now)

    assert exchange.calls == []
    assert fresh.access_token == "old-access"


def test_期限が近ければ_refresh_して_書き戻す(store: FakeStore) -> None:
    """書き戻しを忘れると、単回使用の refresh token を失う。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now + timedelta(seconds=30))
    creds = load_credentials(store)
    assert creds is not None
    exchange = FakeExchange()

    fresh = ensure_fresh(store, creds, exchange, now=now)

    assert exchange.calls == ["old-refresh"]
    assert fresh.access_token == "new-access"

    persisted = read_json(store, X_TOKEN)
    assert persisted is not None
    assert persisted["refresh_token"] == "new-refresh"


def test_保存先が空なら_None(store: FakeStore) -> None:
    """未認証を例外にすると、画面を開くだけで 500 になる。"""
    assert load_credentials(store) is None


def test_壊れた値は_無いものとして扱う(store: FakeStore) -> None:
    """更新が中断して壊れた JSON が残ると、認証フローにも入れなくなる。"""
    store.write(X_TOKEN, b"{not json")

    assert load_credentials(store) is None


def test_refresh_が失敗したら_XTokenExpiredError(store: FakeStore) -> None:
    """理由不明の失効が実際に多く報告されている。

    例外で落とさず、この型で受けて画面に再認証ボタンを出す。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now)
    creds = load_credentials(store)
    assert creds is not None

    class Failing:
        def refresh(self, refresh_token: str) -> dict[str, Any]:
            raise RuntimeError("invalid_grant")

    with pytest.raises(XTokenExpiredError):
        ensure_fresh(store, creds, Failing(), now=now)
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_x_auth.py -v`
Expected: FAIL（`ImportError: cannot import name 'X_TOKEN'`）

- [ ] **Step 3: `X_TOKEN` を足す**

`src/storage/tokens.py` の定数群に追記する。

```python
# X（旧 Twitter）の OAuth 2.0 トークン。
# ローカルと Blob で同じ名前を使う（config.token_paths と揃える）。
X_TOKEN = "x_token"
```

- [ ] **Step 4: `src/social/x_auth.py` を書く**

```python
"""X の OAuth 2.0（Authorization Code + PKCE）。

なぜローカルで1回認証して送る運用にするか
----------------------------------------
PKCE フローはリダイレクト先を必要とし、コンテナの中では実質完了できない
（YouTube の InstalledAppFlow と同じ理由）。ローカルで認証し、
`uv run python -m scripts.push_tokens` で保存先へ送る。

YouTube と違う点: refresh token が**単回使用でローテートする**。
更新したら必ず保存先へ書き戻す必要があり、書き戻しは投稿より先に行う
（投稿が失敗しても、トークンだけは前に進んだ状態を保つ）。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.storage.tokens import X_TOKEN, TokenStore, read_json, write_json
from src.utils.logger import log_error

# 期限のこれだけ前になったら更新する。
# 投稿の直前に期限が切れると、その回の投稿を落とすことになる。
REFRESH_MARGIN_SECONDS = 120

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
SCOPES = ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access")


class XAuthError(Exception):
    """認証に失敗した。"""


class XTokenExpiredError(XAuthError):
    """トークンが失効しており、再認証が必要。

    X では理由不明の失効が実際に起きる。これを異常終了として扱わず、
    投稿を NEEDS_REVIEW にして画面に再認証ボタンを出すために型で分ける。
    """


@dataclass(frozen=True)
class XCredentials:
    """アクセストークンと更新用トークン。"""

    access_token: str
    refresh_token: str
    expires_at: datetime

    def needs_refresh(self, now: datetime) -> bool:
        """更新すべきか。"""
        return now >= self.expires_at - timedelta(seconds=REFRESH_MARGIN_SECONDS)


class TokenExchange(Protocol):
    """トークンエンドポイントとの通信。

    Protocol にする理由: テストで実際の HTTP を張らずに、
    ローテーションの書き戻しだけを検証したい。
    """

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """refresh token を使って新しいトークン一式を得る。"""
        ...


def load_credentials(store: TokenStore) -> XCredentials | None:
    """保存先からトークンを読む。

    未認証・壊れた値・保存先に到達できない場合はいずれも None を返す。
    例外を投げると、画面を開くだけで 500 になる（未認証なら
    認証ボタンを出せばよい）。

    Args:
        store: トークンの保存先

    Returns:
        XCredentials | None: 読めたトークン
    """
    data = read_json(store, X_TOKEN)
    if not data:
        return None
    try:
        return XCredentials(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )
    except (KeyError, TypeError, ValueError) as e:
        log_error(f"X のトークンを読めませんでした（未認証として扱います）: {e}")
        return None


def ensure_fresh(
    store: TokenStore,
    credentials: XCredentials,
    exchange: TokenExchange,
    now: datetime | None = None,
) -> XCredentials:
    """必要なら更新し、**保存先へ書き戻してから**返す。

    書き戻しを先に行うのが重要。X の refresh token は単回使用なので、
    更新に成功したあと書き戻す前に落ちると、手元の refresh token は
    既に無効で、保存先の値も無効。再認証しか道が無くなる。

    Args:
        store: トークンの保存先
        credentials: 現在のトークン
        exchange: トークンエンドポイント
        now: 現在時刻（省略時は UTC の現在）

    Returns:
        XCredentials: 有効なトークン

    Raises:
        XTokenExpiredError: 更新に失敗した（再認証が必要）
    """
    moment = now or datetime.now(UTC)
    if not credentials.needs_refresh(moment):
        return credentials

    try:
        payload = exchange.refresh(credentials.refresh_token)
    except Exception as e:
        raise XTokenExpiredError(f"X のトークンを更新できませんでした: {e}") from e

    refreshed = XCredentials(
        access_token=str(payload["access_token"]),
        # 新しい refresh token が返らない実装もありうるので、
        # 無ければ現在のものを維持する
        refresh_token=str(payload.get("refresh_token") or credentials.refresh_token),
        expires_at=moment + timedelta(seconds=int(payload.get("expires_in", 7200))),
    )
    write_json(
        store,
        X_TOKEN,
        {
            "access_token": refreshed.access_token,
            "refresh_token": refreshed.refresh_token,
            "expires_at": refreshed.expires_at.isoformat(),
        },
    )
    return refreshed


def build_authorization_url(
    client_id: str, redirect_uri: str, verifier: str | None = None
) -> tuple[str, str, str]:
    """認可 URL を組む。

    Args:
        client_id: アプリのクライアントID
        redirect_uri: 登録済みのリダイレクト先
        verifier: PKCE の code_verifier。省略時は生成する

    Returns:
        tuple[str, str, str]: (認可URL, state, code_verifier)
    """
    from urllib.parse import urlencode

    code_verifier = verifier or secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}", state, code_verifier
```

- [ ] **Step 5: テストが通ることを確認する**

Run: `uv run pytest tests/test_x_auth.py -v`
Expected: PASS

- [ ] **Step 6: `push_tokens` を対応させる**

`scripts/push_tokens.py` の対象一覧に `X_TOKEN` を足す。既存の
YouTube / TikTok の扱いと同じ形にする（ファイルが無ければ黙って飛ばす）。

- [ ] **Step 7: lint と型、コミット**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/social/x_auth.py src/storage/tokens.py scripts/push_tokens.py tests/test_x_auth.py
git commit -m "Authenticate to X and survive rotating refresh tokens"
```

---

## Task 5: 投稿の実行

**ここで初めて実際に投稿する。** `X_POSTING_ENABLED` は既定 false のままなので、
画面で有効にするまで公開はされない。

**Files:**
- Create: `src/social/x_client.py`, `src/social/switch.py`, `src/social/cost.py`,
  `src/jobs/post_planner.py`, `src/jobs/post_worker.py`
- Modify: `src/jobs/scheduler.py`, `src/web/dependencies.py`
- Test: `tests/test_posting_switch.py`, `tests/test_post_cost.py`,
  `tests/test_post_planner.py`, `tests/test_post_worker.py`

**Interfaces:**
- Consumes: Task 1〜4 のすべて
- Produces:
  - `XClient` Protocol: `create_post(text: str, reply_to: str | None = None, media_ids: list[str] | None = None) -> str`,
    `upload_media(path: Path) -> str`, `fetch_metrics(tweet_ids: list[str]) -> dict[str, dict[str, int]]`
  - `PostingSwitch(path: Path, default_enabled: bool)`: `is_enabled() -> bool`, `set_enabled(value: bool) -> None`
  - `estimate_month_cost(plain: int, with_link: int, unit: float, unit_with_link: float) -> float`
  - `is_over_budget(spent: float, budget: float) -> bool`
  - `plan_daily_posts(...) -> DailyPostPlan`
  - `PostWorker(repository, client_factory, switch, ...)`: `start()`, `stop(timeout)`, `is_running`

- [ ] **Step 1: スイッチとコストのテストを書く**

`tests/test_posting_switch.py`。

```python
"""自動投稿の有効/無効。"""

from pathlib import Path

from src.social.switch import PostingSwitch


def test_ファイルが無ければ既定値を返す(tmp_path: Path) -> None:
    switch = PostingSwitch(tmp_path / "x_posting.json", default_enabled=False)

    assert switch.is_enabled() is False


def test_画面から有効にできる(tmp_path: Path) -> None:
    path = tmp_path / "x_posting.json"
    switch = PostingSwitch(path, default_enabled=False)

    switch.set_enabled(True)

    assert switch.is_enabled() is True
    # 別のインスタンス（= 別プロセス）からも見える
    assert PostingSwitch(path, default_enabled=False).is_enabled() is True


def test_一度切り替えたら_既定値より_ファイルが優先される(tmp_path: Path) -> None:
    """既定値は「ファイルが無いときの初期値」でしかない。

    デプロイのたびに既定値へ戻ると、画面で有効にした翌日に
    黙って投稿が止まる。
    """
    path = tmp_path / "x_posting.json"
    PostingSwitch(path, default_enabled=False).set_enabled(True)

    assert PostingSwitch(path, default_enabled=False).is_enabled() is True


def test_壊れたファイルは既定値として扱う(tmp_path: Path) -> None:
    """壊れた JSON で画面が 500 になると、止めることも直すこともできない。"""
    path = tmp_path / "x_posting.json"
    path.write_text("{broken", encoding="utf-8")

    assert PostingSwitch(path, default_enabled=False).is_enabled() is False
```

`tests/test_post_cost.py`。

```python
"""概算コストと上限判定。"""

from src.social.cost import estimate_month_cost, is_over_budget


def test_リンク付きは13倍で数える():
    """$0.015 と $0.20 の差を無視すると、上限が意味を失う。"""
    cost = estimate_month_cost(plain=200, with_link=30, unit=0.015, unit_with_link=0.20)

    assert cost == 200 * 0.015 + 30 * 0.20


def test_上限を超えたら_True():
    assert is_over_budget(spent=20.5, budget=20.0) is True


def test_上限ちょうどは_超えていない():
    assert is_over_budget(spent=20.0, budget=20.0) is False
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_posting_switch.py tests/test_post_cost.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: `switch.py` と `cost.py` を書く**

```python
"""自動投稿の有効/無効を持つスイッチ。

なぜ SQLite ではなくファイルか
------------------------------
ジョブ表の SQLite はコンテナのローカルディスクにあり、リビジョン更新で
消える。スイッチをそこに置くと、画面で有効にした翌日にマージした時点で
**黙って投稿が止まる**。実体は Azure Files 上のファイルにする
（記事の選択状態と同じ場所）。

環境変数 X_POSTING_ENABLED は「ファイルが無いときの初期値」でしかない。
一度画面で切り替えたら、以降はファイルが権威。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.utils.logger import log_error


class PostingSwitch:
    """自動投稿の有効/無効。"""

    def __init__(self, path: Path, default_enabled: bool):
        """初期化する。

        Args:
            path: スイッチの実体（Azure Files 上を想定）
            default_enabled: ファイルが無いときの値
        """
        self._path = path
        self._default = default_enabled

    def is_enabled(self) -> bool:
        """投稿してよいか。

        読めない・壊れている場合は既定値として扱う。例外にすると、
        壊れたファイルのせいで画面が開かず、止めることも直すことも
        できなくなる。
        """
        if not self._path.exists():
            return self._default
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return bool(data["enabled"])
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            log_error(f"投稿スイッチを読めませんでした（既定値 {self._default} を使います）: {e}")
            return self._default

    def set_enabled(self, value: bool) -> None:
        """切り替えて保存する。

        一時ファイル + replace で原子的に書く。書き込み中に落ちると
        壊れた JSON が残り、次回の判定が既定値に戻る。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump({"enabled": value}, f)
            temp_path.replace(self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
```

```python
"""投稿の概算コスト。

単価をコードに埋めない理由: X の料金は 2026-02 に体系ごと変わった。
設定に出しておけば、改定時にデプロイだけで追随できる。
"""

from __future__ import annotations


def estimate_month_cost(
    plain: int, with_link: int, unit: float, unit_with_link: float
) -> float:
    """当月の概算コストを返す。

    リンク付きを分けて数えるのは、単価が $0.015 と $0.20 で13倍違うため。
    混ぜて数えると上限判定が意味を失う。

    Args:
        plain: リンクを含まない投稿の件数
        with_link: リンクを含む投稿の件数
        unit: リンク無しの単価
        unit_with_link: リンク有りの単価

    Returns:
        float: 概算コスト（USD）
    """
    return plain * unit + with_link * unit_with_link


def is_over_budget(spent: float, budget: float) -> bool:
    """上限を超えているか。

    Args:
        spent: 概算の使用額
        budget: 上限

    Returns:
        bool: 超えていれば True
    """
    return spent > budget
```

- [ ] **Step 4: テストが通ることを確認してコミット**

```bash
uv run pytest tests/test_posting_switch.py tests/test_post_cost.py -v
git add src/social/switch.py src/social/cost.py tests/test_posting_switch.py tests/test_post_cost.py
git commit -m "Hold the posting switch outside the ephemeral database"
```

- [ ] **Step 5: `x_client.py` を書く**

Protocol と HTTP 実装を分ける。テストは Protocol のフェイクを使う。

```python
"""X API v2 の薄いラッパ。

Protocol にしている理由: ワーカーのループ（掴む・状態を進める・停止する）と
「実際に X を叩く」処理を分けたい。テストではフェイクを差し込んで、
課金も公開もせずにループの挙動を検証する（既存 JobRunner と同じ方針）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class XClientError(Exception):
    """X API の呼び出しに失敗した。"""


class XSendUncertainError(XClientError):
    """送信したが結果が分からない（タイムアウトなど）。

    再送してはいけない種類の失敗。呼び出し側は NEEDS_REVIEW にする。
    """


class XClient(Protocol):
    """投稿とメディアと指標。"""

    def create_post(
        self,
        text: str,
        reply_to: str | None = None,
        media_ids: list[str] | None = None,
    ) -> str:
        """投稿して tweet_id を返す。"""
        ...

    def upload_media(self, path: Path) -> str:
        """画像をアップロードして media_id を返す。"""
        ...

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
        """投稿の指標を返す（tweet_id -> 指標）。"""
        ...
```

`HttpXClient` は `httpx` で実装する（既存のニュース取得が httpx を使っている）。

```python
API_BASE = "https://api.x.com/2"
UPLOAD_URL = "https://api.x.com/2/media/upload"


class HttpXClient:
    """httpx による XClient の実装。

    **create_post は一切再試行しない。** タイムアウトや 429 の応答が
    届く前に投稿自体は通っている可能性があり、それを排除できない。
    再試行すると同じ内容が2つ並ぶ。再試行の判断は人に委ねる
    （呼び出し側が NEEDS_REVIEW にして画面に出す）。

    Attributes:
        access_token: 有効なアクセストークン（呼び出し側が ensure_fresh 済み）
    """

    def __init__(self, access_token: str, timeout: float = 30.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """接続を閉じる。"""
        self._client.close()

    def create_post(
        self,
        text: str,
        reply_to: str | None = None,
        media_ids: list[str] | None = None,
    ) -> str:
        """投稿して tweet_id を返す。

        Args:
            text: 本文
            reply_to: 返信先の tweet_id（スレッドの2件目以降）
            media_ids: 添付するメディアのID

        Returns:
            str: 作成された投稿の ID

        Raises:
            XSendUncertainError: 届いたか分からない（再送してはいけない）
            XTokenExpiredError: 再認証が必要
            XClientError: 拒否された
        """
        payload: dict[str, Any] = {"text": text}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}

        try:
            response = self._client.post(f"{API_BASE}/tweets", json=payload)
        # 送ったが応答を受け取れなかった。投稿が通っている可能性がある
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise XSendUncertainError(f"応答を受け取れませんでした: {e}") from e

        if response.status_code == 401:
            raise XTokenExpiredError(f"認証されませんでした: {response.text}")
        # 429 も再試行しない。応答が届く前に投稿が通った場合と区別できない
        if response.status_code == 429:
            raise XSendUncertainError(f"レート制限に達しました: {response.text}")
        if response.status_code >= 500:
            # 5xx はサーバー側の状態が不明。届いた可能性を排除できない
            raise XSendUncertainError(f"サーバーエラー: {response.status_code}")
        if response.status_code >= 400:
            raise XClientError(f"投稿を拒否されました（{response.status_code}）: {response.text}")

        try:
            return str(response.json()["data"]["id"])
        except (KeyError, TypeError, ValueError) as e:
            # 2xx なのに ID が読めない。投稿は通っている
            raise XSendUncertainError(f"応答から ID を読めませんでした: {response.text}") from e

    def upload_media(self, path: Path) -> str:
        """画像をアップロードして media_id を返す。

        投稿と違い、**失敗しても再試行してよい**（アップロードは公開されない。
        重複しても未使用のメディアが残るだけ）。

        Args:
            path: PNG のパス

        Returns:
            str: media_id

        Raises:
            XClientError: アップロードに失敗した
        """
        try:
            with path.open("rb") as f:
                response = self._client.post(
                    UPLOAD_URL,
                    files={"media": (path.name, f, "image/png")},
                    # multipart なので Content-Type をヘッダーから外す
                    headers={"Content-Type": None},  # type: ignore[dict-item]
                )
            response.raise_for_status()
            return str(response.json()["data"]["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            raise XClientError(f"メディアのアップロードに失敗しました: {e}") from e

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
        """投稿の指標を返す。

        Args:
            tweet_ids: 最大100件

        Returns:
            dict[str, dict[str, int]]: tweet_id -> 指標

        Raises:
            XClientError: 取得に失敗した
        """
        if len(tweet_ids) > 100:
            raise XClientError(f"1回に問い合わせられるのは100件までです: {len(tweet_ids)}件")
        try:
            response = self._client.get(
                f"{API_BASE}/tweets",
                params={"ids": ",".join(tweet_ids), "tweet.fields": "public_metrics"},
            )
            response.raise_for_status()
            data = response.json().get("data", [])
        except (httpx.HTTPError, ValueError) as e:
            raise XClientError(f"指標の取得に失敗しました: {e}") from e

        return {str(item["id"]): dict(item.get("public_metrics", {})) for item in data}
```

**メディアアップロードのエンドポイントは実装前に確認する**（`UPLOAD_URL` と
multipart のフィールド名。v1.1 の `media/upload` から移行が進んでおり、
本計画の値は二次情報）。

- [ ] **Step 6: ワーカーのテストを書く**

`tests/test_post_worker.py` を新規作成する。ここがこのタスクの本体。

```python
"""投稿ワーカーの挙動。実際の X は叩かない。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.jobs.post_worker import post_due_once
from src.models.social import NewPost, PostKind, PostStatus
from src.social.x_client import XSendUncertainError
from src.storage.db import create_db_engine, create_session_factory
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository


class FakeClient:
    """投稿を記録するだけのクライアント。"""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.posted: list[tuple[str, str | None]] = []
        self.uploaded: list[Path] = []
        self._fail_with = fail_with

    def create_post(self, text, reply_to=None, media_ids=None) -> str:
        if self._fail_with is not None:
            raise self._fail_with
        self.posted.append((text, reply_to))
        return f"tw{len(self.posted)}"

    def upload_media(self, path: Path) -> str:
        self.uploaded.append(path)
        return "media1"

    def fetch_metrics(self, tweet_ids):
        return {}


class EnabledSwitch:
    def is_enabled(self) -> bool:
        return True


class DisabledSwitch:
    def is_enabled(self) -> bool:
        return False


@pytest.fixture
def repository(tmp_path: Path) -> SocialPostRepository:
    url = f"sqlite:///{(tmp_path / 'social.db').as_posix()}"
    upgrade_to_head(url)
    return SocialPostRepository(create_session_factory(create_db_engine(url)))


def _enqueue(repo: SocialPostRepository, when: datetime, count: int = 1) -> str:
    posts = [
        NewPost(
            article_id="a1",
            article_title="記事",
            kind=PostKind.THREAD if count > 1 else PostKind.SINGLE,
            body=f"本文{i}",
            has_link=False,
            position=i,
        )
        for i in range(count)
    ]
    return repo.enqueue(posts, {i: when for i in range(count)})


def test_予定時刻を過ぎた投稿を出す(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient()

    assert post_due_once(repository, client, EnabledSwitch(), now=now) is True

    assert client.posted == [("本文0", None)]
    assert repository.list_upcoming() == []


def test_スイッチが無効なら_出さない(repository: SocialPostRepository) -> None:
    """暴走時に止める手段。行は残して、有効にしたら出せるようにする。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient()

    assert post_due_once(repository, client, DisabledSwitch(), now=now) is False

    assert client.posted == []
    upcoming = repository.list_upcoming()
    assert [p.status for p in upcoming] == [PostStatus.SCHEDULED]


def test_スレッドは_直前の投稿への返信になる(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now, count=3)
    client = FakeClient()

    for _ in range(3):
        post_due_once(repository, client, EnabledSwitch(), now=now)

    assert client.posted == [("本文0", None), ("本文1", "tw1"), ("本文2", "tw2")]


def test_送信結果が不明なら_NEEDS_REVIEW(repository: SocialPostRepository) -> None:
    """再送すると同じ内容が2つ並ぶ。取りこぼしのほうが安全。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient(fail_with=XSendUncertainError("timeout"))

    post_due_once(repository, client, EnabledSwitch(), now=now)

    reviewed = repository.list_needs_review()
    assert len(reviewed) == 1
    assert "timeout" in (reviewed[0].error_message or "")


def test_スレッドの途中で失敗したら_残りも_NEEDS_REVIEW(
    repository: SocialPostRepository,
) -> None:
    """半端なスレッドを、時間をおいて自動で続けると文脈が切れる。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now, count=3)

    post_due_once(repository, FakeClient(), EnabledSwitch(), now=now)
    post_due_once(
        repository,
        FakeClient(fail_with=XSendUncertainError("timeout")),
        EnabledSwitch(),
        now=now,
    )

    statuses = {p.position: p.status for p in repository.list_needs_review()}
    assert statuses == {1: PostStatus.NEEDS_REVIEW, 2: PostStatus.NEEDS_REVIEW}


def test_画像カードは_メディアを先に上げる(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    image = tmp_path / "card.png"
    image.write_bytes(b"png")
    repository.enqueue(
        [
            NewPost(
                article_id="a1",
                article_title="記事",
                kind=PostKind.CARD,
                body="本文",
                has_link=False,
                image_key="social/cards/card.png",
            )
        ],
        {0: now},
    )
    client = FakeClient()

    post_due_once(
        repository, client, EnabledSwitch(), now=now, fetch_image=lambda key: image
    )

    assert client.uploaded == [image]
```

- [ ] **Step 7: 失敗を確認する**

Run: `uv run pytest tests/test_post_worker.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.jobs.post_worker'`）

- [ ] **Step 8: `post_worker.py` を書く**

`post_due_once` を独立した関数として切り出し、`PostWorker` はそれを回すだけにする
（テストがループを起動せずに1回ぶんを検証できる）。

```python
"""投稿を実行するワーカー。

ジョブワーカー（src/jobs/worker.py）との違い
-------------------------------------------
リースと heartbeat を持たない。投稿は数秒で終わるので、15分のリースを
延ばし続ける仕組みは意味を持たない。

代わりに、送信結果が不明なときの扱いが厳しい。X API に冪等キーが無いため、
「届いたか分からない」行は再送せず NEEDS_REVIEW にする。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from src.models.social import PostKind, PostStatus
from src.social.x_auth import XTokenExpiredError
from src.social.x_client import XClient, XSendUncertainError
from src.storage.social import SocialPostRepository
from src.utils.logger import log_error, log_step, log_success

# キューが空のときに次を見に行くまでの間隔。
# 投稿時刻の精度は分単位で十分なので、30秒で足りる。
POLL_INTERVAL_SEC = 30.0


class SupportsSwitch(Protocol):
    """自動投稿の有効/無効の判定だけ。"""

    def is_enabled(self) -> bool: ...


def post_due_once(
    repository: SocialPostRepository,
    client: XClient,
    switch: SupportsSwitch,
    now: datetime | None = None,
    fetch_image: Callable[[str], Path] | None = None,
    on_posted: Callable[[str], None] | None = None,
) -> bool:
    """予定時刻を過ぎた投稿を1件だけ出す。

    Args:
        repository: 投稿表
        client: X クライアント
        switch: 自動投稿の有効/無効
        now: 現在時刻（UTC aware）
        fetch_image: 画像キー -> ローカルパス（画像カードのとき必要）
        on_posted: 投稿できたときに article_id を渡す（消費記録の更新用）

    Returns:
        bool: 投稿を試みたら True、何もしなかったら False
    """
    moment = now or datetime.now(UTC)

    # 無効なら掴まない。掴んでから止めると POSTING の行が残り、
    # 次の起動で NEEDS_REVIEW に落ちてしまう。
    if not switch.is_enabled():
        return False

    post = repository.claim_due(moment)
    if post is None:
        return False

    # スレッドの2件目以降は、直前の投稿への返信にする。
    reply_to: str | None = None
    if post.position > 0:
        reply_to = repository.group_posted_tweet_id(post.group_id, post.position - 1)
        if reply_to is None:
            # 直前が出ていない。時間をおいて続けると文脈が切れるので、
            # このまとまりは人が見る。
            repository.mark_needs_review(
                post.id, "スレッドの直前の投稿が出ていないため中断しました"
            )
            return True

    try:
        media_ids: list[str] | None = None
        if post.kind is PostKind.CARD and post.image_key and fetch_image is not None:
            media_ids = [client.upload_media(fetch_image(post.image_key))]

        tweet_id = client.create_post(post.body, reply_to=reply_to, media_ids=media_ids)
    except XSendUncertainError as e:
        # 届いたか分からない。**再送しない。**
        repository.mark_needs_review(post.id, f"送信結果が不明です: {e}")
        log_error(f"投稿 {post.id}: 送信結果が不明のため要確認にしました - {e}")
        return True
    except XTokenExpiredError as e:
        repository.mark_needs_review(post.id, f"再認証が必要です: {e}")
        log_error(f"投稿 {post.id}: トークンが失効しています - {e}")
        return True
    except Exception as e:
        repository.mark_failed(post.id, str(e))
        log_error(f"投稿 {post.id} 失敗: {e}")
        return True

    repository.mark_posted(post.id, tweet_id=tweet_id, posted_at=moment)
    if on_posted is not None:
        on_posted(post.article_id)
    log_success(f"投稿 {post.id} 完了: {post.article_title[:30]}（{post.kind}）")
    return True
```

`PostWorker` は `JobWorker` と同じ形（`threading.Thread`、`daemon=False`、
`stop()` で join、ループは絶対に落とさない）。加えて **`start()` の中で
`recover_stuck_posting()` を1回呼ぶ**（前回のプロセスが送信中に落ちた分を拾う）。

スレッドの途中失敗で「残りも NEEDS_REVIEW」になるのは、`position > 0` の分岐が
`group_posted_tweet_id` を見て自然にそうなるため、追加の処理は不要。

- [ ] **Step 9: テストが通ることを確認する**

Run: `uv run pytest tests/test_post_worker.py -v`
Expected: PASS

- [ ] **Step 10: `post_planner.py` を書く**

`src/jobs/planner.py` と同じ構造（Protocol で依存を絞る、`DailyPostPlan` を返す）。

```python
"""1日ぶんの投稿計画を立てる。

やること
--------
1. その日の投稿時刻を決める
2. X でまだ使っていない記事を選ぶ
3. 型を割り当てて下書きを作り、予定時刻を入れて積む

**コストの上限を超えていたら何も積まない。** 積んでから止めると、
上限が戻った月初に古い投稿が一斉に出る。
"""
```

```python
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
```

**積んだ時点では消費記録を書かない。** 投稿できた時点で `PostWorker` の
`on_posted` から `aggregator.mark_consumed(article_id, CHANNEL_X)` を呼ぶ。
積んだだけで消費扱いにすると、出せなかった記事を二度と使えなくなる。

- [ ] **Step 10b: planner のテストを書いて通す**

`tests/test_post_planner.py`。

```python
"""1日ぶんの投稿計画。"""

from datetime import UTC, datetime

import pytest

from src.jobs.post_planner import plan_daily_posts
from src.models.news import CHANNEL_X, NewsArticle, NewsCategory
from src.models.social import NewPost, PostKind


class FakeNews:
    def __init__(self, articles: list[NewsArticle]) -> None:
        self._articles = articles

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        assert channel == CHANNEL_X
        return self._articles[:needed]


class FakePosts:
    def __init__(self, plain: int = 0, with_link: int = 0) -> None:
        self.enqueued: list[tuple[list[NewPost], dict[int, datetime]]] = []
        self._counts = (plain, with_link)

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        return self._counts

    def enqueue(self, posts, scheduled_at_by_position) -> str:
        self.enqueued.append((posts, scheduled_at_by_position))
        return f"g{len(self.enqueued)}"


class FakeGenerator:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self._fail_for = fail_for or set()

    def generate(self, article, kind, hashtags) -> list[NewPost]:
        if article.id in self._fail_for:
            raise RuntimeError("生成に失敗しました")
        return [
            NewPost(
                article_id=article.id,
                article_title=article.title,
                kind=kind,
                body="本文",
                has_link=False,
            )
        ]


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=suffix,
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


# 2026-08-15 00:00 JST = 2026-08-14 15:00 UTC。全ての枠がまだ先。
MORNING = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)

TIMES = ["08:00", "12:30", "19:00", "21:30"]


def _plan(news, posts, generator, **overrides):
    kwargs = {
        "times": TIMES,
        "posts_per_day": 4,
        "hashtags": ["#AI"],
        "budget_usd": 20.0,
        "unit_usd": 0.015,
        "unit_with_link_usd": 0.20,
        "now": MORNING,
    }
    kwargs.update(overrides)
    return plan_daily_posts(news, posts, generator, **kwargs)


def test_記事ごとに時刻順で積む() -> None:
    posts = FakePosts()
    plan = _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    assert len(plan.group_ids) == 4
    scheduled = [next(iter(times.values())) for _, times in posts.enqueued]
    assert scheduled == sorted(scheduled)


def test_予算上限を超えていたら何も積まない() -> None:
    """積んでから止めると、月初に古い投稿が一斉に出る。"""
    posts = FakePosts(plain=0, with_link=200)  # 200 * 0.20 = $40
    plan = _plan(FakeNews([_article("a")]), posts, FakeGenerator())

    assert plan.enqueued is False
    assert plan.skipped_reason is not None
    assert "予算上限" in plan.skipped_reason
    assert posts.enqueued == []


def test_生成が1件失敗しても残りを積む() -> None:
    posts = FakePosts()
    plan = _plan(
        FakeNews([_article("a"), _article("b")]), posts, FakeGenerator(fail_for={"a"})
    )

    assert len(plan.group_ids) == 1
    assert posts.enqueued[0][0][0].article_id == "b"


def test_過ぎた時刻は使わない() -> None:
    """過去の時刻を入れると discard_stale に即座に捨てられる。"""
    posts = FakePosts()
    # 2026-08-15 20:00 JST。残る枠は 21:30 だけ
    evening = datetime(2026, 8, 15, 11, 0, tzinfo=UTC)

    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator(), now=evening)

    assert len(posts.enqueued) == 1


def test_記事が無ければ理由を返す() -> None:
    plan = _plan(FakeNews([]), FakePosts(), FakeGenerator())

    assert plan.enqueued is False
    assert plan.skipped_reason == "X で未使用の記事がありません"


def test_4件目はカードになる() -> None:
    posts = FakePosts()
    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    kinds = [batch[0].kind for batch, _ in posts.enqueued]
    assert kinds == [PostKind.SINGLE, PostKind.SINGLE, PostKind.SINGLE, PostKind.CARD]
```

Run: `uv run pytest tests/test_post_planner.py -v`
Expected: PASS

- [ ] **Step 11: スケジューラと依存の組み立てに繋ぐ**

`src/jobs/scheduler.py` の日次実行に `plan_daily_posts` を足す。
`src/web/dependencies.py` の `AppContext` に `SocialPostRepository` /
`PostingSwitch` / `PostWorker` を持たせ、`lifespan` で起動・停止する
（`JobWorker` と同じ形）。

**`generate_videos_task` と同じ罠に注意。** `PostWorker` はスレッドで回す。
`async def` の中で同期の投稿処理を await すると Web が固まる。

- [ ] **Step 12: 回帰と lint、コミット**

```bash
uv run pytest -m "not live" -q
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/social/x_client.py src/jobs/post_planner.py src/jobs/post_worker.py \
        src/jobs/scheduler.py src/web/dependencies.py tests/
git commit -m "Post to X on schedule, holding back what we cannot confirm"
```

---

## Task 6: 画像カード

**Files:**
- Create: `src/social/card_visual.py`
- Modify: `src/jobs/post_planner.py`
- Test: `tests/test_card_visual.py`, `tests/test_card_visual_live.py`

**Interfaces:**
- Consumes: `src.generators.image_generator.ImageGenerator`, `src.models.news.NewsArticle`
- Produces:
  - `CARD_STYLE_PROMPT: str`（固定のスタイル指定文）
  - `CardVisual`（pydantic: `subject: str`, `key_details: list[str]`, `labels: list[str]`, `caption_ja: str`）
  - `build_card_prompt(visual: CardVisual) -> str`
  - `CardVisualGenerator.generate(article: NewsArticle) -> CardVisual`
  - `CARD_IMAGE_SIZE: str = "1024x1024"`

- [ ] **Step 1: プロンプト組み立てのテストを書く**

`tests/test_card_visual.py`。

```python
"""画像カードの視覚指示とプロンプト。"""

import pytest

from src.social.card_visual import (
    CARD_IMAGE_SIZE,
    CARD_STYLE_PROMPT,
    CardVisual,
    build_card_prompt,
)
from src.generators.image_generator import validate_size


def _visual(**overrides) -> CardVisual:
    data = {
        "subject": "A cache that reuses previous model inputs to cut cost.",
        "key_details": ["a funnel narrowing", "two arrows returning to a store"],
        "labels": ["CACHE", "REUSED"],
        "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
    }
    data.update(overrides)
    return CardVisual(**data)


def test_固定のスタイル文が先頭に来る():
    """順序は background/scene -> subject -> key details -> constraints。

    OpenAI のガイドがこの順を推奨しており、順序を崩すと
    毎回違う絵が出る。
    """
    prompt = build_card_prompt(_visual())

    assert prompt.startswith(CARD_STYLE_PROMPT)


def test_ラベルは引用符で囲む():
    """ガイドは literal text を引用符か ALL CAPS で示すよう指示している。"""
    prompt = build_card_prompt(_visual())

    assert '"CACHE"' in prompt
    assert '"REUSED"' in prompt


def test_日本語を描かせない指示が入っている():
    """gpt-image-2 の CJK 描画は保証されていない。

    日本語は投稿本文に持たせれば確実に読めるので、画像に賭けない。
    """
    assert "NO Japanese or CJK characters" in CARD_STYLE_PROMPT


def test_caption_ja_はプロンプトに入らない():
    """画像に日本語を入れないという方針と矛盾する。"""
    visual = _visual()
    prompt = build_card_prompt(visual)

    assert visual.caption_ja not in prompt


def test_ラベルが英大文字でなければ弾く():
    with pytest.raises(ValueError):
        _visual(labels=["キャッシュ"])


def test_ラベルは4個まで():
    with pytest.raises(ValueError):
        _visual(labels=["A", "B", "C", "D", "E"])


def test_カードのサイズは_gpt_image_2_の制約を満たす():
    """両辺が16の倍数、総ピクセル数の範囲内。"""
    assert validate_size(CARD_IMAGE_SIZE) == (1024, 1024)
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_card_visual.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: `src/social/card_visual.py` を書く**

```python
"""画像カードの視覚指示。

なぜ記事本文をそのままプロンプトに入れないか
------------------------------------------
OpenAI のガイドは `background/scene -> subject -> key details ->
constraints` の順と、長文1段落ではなくラベル付きの短いセグメントを
推奨している。数千字の日本語記事を投げると、モデルが「何を1枚に描くか」を
自分で決めることになり、毎回違うものが出る。

既存の動画パイプラインと同じ2段構えにする。LLM が英語の視覚指示を作り、
コード側が固定のスタイル文を前置して gpt-image-2 に渡す
（`images.generate` に system prompt は無いため、固定の指示は前置しかない）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# X のタイムラインで 16:9 より縦の面積を取れる。
# 両辺が16の倍数という gpt-image-2 の制約も満たす。
CARD_IMAGE_SIZE = "1024x1024"

# 固定のスタイル指定文。**ここが単一の情報源。**
#
# ガイドの推奨順（scene -> subject -> details -> constraints）に沿って
# ラベルで区切っている。1段落に流すと、モデルが指示を取りこぼす。
CARD_STYLE_PROMPT = """\
Medium: hand-drawn illustrated sketch — an engineer's whiteboard explainer
  redrawn cleanly. Loose ink linework with visible stroke ends, light marker
  fills, faint paper grain.
Palette: off-white paper ground, near-black ink, one accent (deep teal), one
  highlight (warm amber). Flat fills only — no gradients, no glossy 3D render.
Composition: a single explanatory diagram, centred, front-on flat view,
  generous margins. One idea only — no comic panels, no multi-step timeline.
Intended use: an explanatory illustration for a technology news post.
Constraints: render NO Japanese or CJK characters. No watermark, no logos, no
  UI chrome, no photorealism. Do not depict any real, identifiable person; use
  simple silhouettes if a figure is needed."""


class CardVisual(BaseModel):
    """1枚の概念図に描くもの。

    LLM への出力契約そのもの。フィールドを増やすと生成される
    JSON スキーマが変わる。
    """

    subject: str = Field(description="1枚で説明する概念を英語1文で")
    key_details: list[str] = Field(
        min_length=2, max_length=4, description="描く視覚要素とその関係（英語）"
    )
    labels: list[str] = Field(
        default_factory=list, max_length=4, description="画像に入れる短いラベル（英大文字）"
    )
    caption_ja: str = Field(description="投稿本文に載せる日本語の1文。画像には入れない")

    @field_validator("labels")
    @classmethod
    def _labels_must_be_ascii_upper(cls, value: list[str]) -> list[str]:
        """ラベルは英大文字だけに限る。

        gpt-image-2 の CJK 描画は保証されていない。日本語は投稿本文に
        持たせれば確実に読めるので、画像に賭ける必要がない。
        """
        for label in value:
            if not label.isascii() or label != label.upper():
                raise ValueError(f"ラベルは英大文字のみ: {label!r}")
        return value


def build_card_prompt(visual: CardVisual) -> str:
    """gpt-image-2 に渡すプロンプトを組む。

    Args:
        visual: LLM が作った視覚指示

    Returns:
        str: 固定のスタイル文を前置したプロンプト
    """
    parts = [CARD_STYLE_PROMPT, f"Subject: {visual.subject}"]
    parts.append("Key details: " + "; ".join(visual.key_details))
    if visual.labels:
        quoted = ", ".join(f'"{label}"' for label in visual.labels)
        parts.append(
            f"Labels: render exactly these words, {quoted}, in a small hand-lettered "
            "sans-serif placed beside the element each one names."
        )
    else:
        parts.append("Labels: none. Render no words at all.")
    return "\n".join(parts)
```

`CardVisualGenerator` は `PostGenerator` と同じ形（Structured Outputs、
`_complete` を切り出す）。

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_card_visual.py -v`
Expected: PASS

- [ ] **Step 5: planner に画像生成を繋ぐ**

`src/jobs/post_planner.py` で `PostKind.CARD` のとき、

1. `CardVisualGenerator.generate(article)` で視覚指示を得る
2. `build_card_prompt(visual)` で1枚生成する（`ImageGenerator.generate_batch` に
   プロンプト1件を渡す。`video_format` ではなくサイズを直接渡せるよう
   `ImageGenerator` にサイズ引数を足す）
3. `ArtifactStore.publish` で `social/cards/<group_id>.png` として保存し、
   `NewPost.image_key` に入れる
4. `visual.caption_ja` を投稿本文の生成に渡す

**動画生成中は画像カードを作らない。** `jobs.has_active_jobs()` が True なら
その日は `CARD` を `SINGLE` に降格する。`gpt-image-2` のクォータは
リージョン単位で上限4という律速のまま。

**`ContentFilterError` は `SINGLE` に降格する。** 再試行しても結果は変わらない
設計になっており、画像カードが落ちてもその日の投稿は出したい。

- [ ] **Step 6: live テストを書く（既定では走らない）**

`tests/test_card_visual_live.py` に `@pytest.mark.live` で1枚だけ生成し、
PNG として開けることと解像度が 1024x1024 であることを確認する。
**画像クォータを消費する**ので既定では走らせない。

- [ ] **Step 7: 回帰と lint、コミット**

```bash
uv run pytest -m "not live" -q
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/social/card_visual.py src/jobs/post_planner.py src/generators/image_generator.py tests/
git commit -m "Draw the concept as an ink sketch instead of a text card"
```

---

## Task 7: 画面

**Files:**
- Modify: `static/css/input.css`, `templates/base.html`, `templates/index.html`, `src/web/routes.py`
- Create: `static/fonts/*.woff2`, `templates/partials/day_band.html`,
  `templates/partials/post_queue.html`, `templates/partials/x_status.html`
- Test: `tests/test_web_social.py`

**Interfaces:**
- Consumes: `SocialPostRepository.{list_upcoming, list_needs_review, monthly_post_counts}`, `PostingSwitch`
- Produces: ルート `GET /x/status`, `POST /x/enabled`, `GET /x/queue`, `GET /x/band`, `POST /x/posts/{id}/cancel`

- [ ] **Step 1: 設計トークンを入れる**

`static/css/input.css` の `@import "tailwindcss";` の直後に足す。

```css
/*
 * 設計トークン。
 *
 * 配色は生成する画像カード（インク線 + deep teal + warm amber）と
 * 同じにしている。運用者は一日中その絵を見るので、画面がそれと
 * 喧嘩しないほうがよい。以前の青のグラデーションは、何のアプリでも
 * あり得る既定値だった。
 */
@theme {
    --color-ink: #16232B;
    --color-paper: #EEF1F0;
    --color-raised: #FBFCFC;
    --color-teal: #0F6E6B;
    --color-amber: #C97A16;
    --color-vermilion: #B23B2E;

    /*
     * 欧文だけをベンダリングする。CDN を使わない方針なので
     * Google Fonts は使えず、和文 webfont は数 MB になる。
     * 時刻・件数・金額は等幅の tabular figures で読ませたいので、
     * そこだけ Plex Mono を当てる。
     */
    --font-display: "IBM Plex Sans Condensed", "Hiragino Kaku Gothic ProN",
        "Yu Gothic", "Noto Sans JP", sans-serif;
    --font-body: "Hiragino Kaku Gothic ProN", "Yu Gothic", "Noto Sans JP",
        sans-serif;
    --font-mono: "IBM Plex Mono", ui-monospace, monospace;
}

@font-face {
    font-family: "IBM Plex Mono";
    src: url("/static/fonts/IBMPlexMono-Regular-latin.woff2") format("woff2");
    font-weight: 400;
    font-display: swap;
    /* Latin サブセットのみ。和文はシステムスタックに任せる */
    unicode-range: U+0000-00FF, U+2000-206F, U+2190-21BB;
}

@font-face {
    font-family: "IBM Plex Sans Condensed";
    src: url("/static/fonts/IBMPlexSansCondensed-SemiBold-latin.woff2") format("woff2");
    font-weight: 600;
    font-display: swap;
    unicode-range: U+0000-00FF, U+2000-206F;
}

/* 数字を等幅で揃える。時刻の桁がずれると帯が読めない */
.tnum {
    font-variant-numeric: tabular-nums;
}

/* 「いま」の線。動くのはここだけ */
@media (prefers-reduced-motion: reduce) {
    .now-marker {
        transition: none;
    }
}
```

woff2 は IBM Plex の配布物（SIL Open Font License）から Latin サブセットを
`static/fonts/` に置く。`static/vendor/htmx.min.js` と同じ扱い。

- [ ] **Step 2: 帯のテストを書く**

`tests/test_web_social.py`。`tests/test_web_artifacts.py` の
`dependency_overrides` の作法に合わせる。

```python
"""X 運用の画面。"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient


def test_帯に予定と要確認が出る(client: TestClient, fake_posts) -> None:
    response = client.get("/x/band")

    assert response.status_code == 200
    body = response.text
    assert "19:00" in body
    # 状態は色だけでなく語でも示す
    assert "予約" in body
    assert "要確認" in body


def test_キューに本文が全文出る(client: TestClient, fake_posts) -> None:
    """畳むと誰も読まない。読んで気付くことが運用者の唯一の仕事。"""
    response = client.get("/x/queue")

    assert "本文がそのまま全部出ている" in response.text


def test_文字数が出る(client: TestClient, fake_posts) -> None:
    response = client.get("/x/queue")

    assert "/280" in response.text


def test_スイッチを画面から有効にできる(client: TestClient, fake_switch) -> None:
    response = client.post("/x/enabled", data={"enabled": "true"})

    assert response.status_code == 200
    assert fake_switch.is_enabled() is True


def test_概算コストに概算と明示する(client: TestClient, fake_posts) -> None:
    """実際の課金は X 側の集計なので、一致を保証できない。"""
    response = client.get("/x/status")

    assert "概算" in response.text
```

- [ ] **Step 3: 失敗を確認する**

Run: `uv run pytest tests/test_web_social.py -v`
Expected: FAIL（404）

- [ ] **Step 4: ルートとテンプレートを書く**

`src/web/routes.py` に5つのルートを足す。既存の `Depends(get_aggregator)` の
作法に合わせ、`get_social_posts` / `get_posting_switch` / `get_config` を
`src/web/dependencies.py` に足す。

```python
@router.get("/x/band", response_class=HTMLResponse)
async def x_band(
    request: Request,
    posts: SocialPostRepository = Depends(get_social_posts),
    config: Config = Depends(get_config),
) -> HTMLResponse:
    """今日の時間割。過ぎた枠・いま・これから出るものを1本の軸に並べる。

    このプロダクトの本質は「決めた時刻に、見ていない間に出る」こと。
    数字のタイルではなく時間軸を主役にしているのはそのため。
    """
    zone = ZoneInfo(config.schedule_timezone)
    local_now = datetime.now(UTC).astimezone(zone)
    slots = [
        _to_slot(post, zone)
        for post in posts.list_upcoming(limit=40) + _today_posted(posts, local_now)
    ]
    return templates.TemplateResponse(
        request,
        "partials/day_band.html",
        {
            "slots": sorted(slots, key=lambda s: s["at"]),
            "now": local_now,
            # 1日を 00:00〜24:00 の帯として描くので、いまの位置は割合で渡す
            "now_ratio": (local_now.hour * 60 + local_now.minute) / (24 * 60),
            "needs_review": posts.list_needs_review(),
        },
    )


@router.get("/x/queue", response_class=HTMLResponse)
async def x_queue(
    request: Request,
    posts: SocialPostRepository = Depends(get_social_posts),
) -> HTMLResponse:
    """投稿キュー。要確認を先に、次にこれから出るものを並べる。

    本文は畳まない。自動投稿なので運用者の唯一の仕事が「読んで気付く」
    ことで、開かないと読めない UI では誰も読まない。
    """
    return templates.TemplateResponse(
        request,
        "partials/post_queue.html",
        {
            "needs_review": posts.list_needs_review(),
            "upcoming": posts.list_upcoming(limit=20),
            "max_weighted": X_MAX_WEIGHTED_LENGTH,
        },
    )


@router.get("/x/status", response_class=HTMLResponse)
async def x_status(
    request: Request,
    posts: SocialPostRepository = Depends(get_social_posts),
    switch: PostingSwitch = Depends(get_posting_switch),
    config: Config = Depends(get_config),
    tokens: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """自動投稿の状態、概算コスト、認証の有無。"""
    now = datetime.now(UTC)
    plain, with_link = posts.monthly_post_counts(now.year, now.month)
    spent = estimate_month_cost(
        plain, with_link, config.x_cost_per_post_usd, config.x_cost_per_post_with_link_usd
    )
    return templates.TemplateResponse(
        request,
        "partials/x_status.html",
        {
            "enabled": switch.is_enabled(),
            # 「概算」と明示する。実際の課金は X 側の集計なので一致を保証できない
            "spent_usd": spent,
            "budget_usd": config.x_monthly_budget_usd,
            "authenticated": load_credentials(tokens) is not None,
        },
    )


@router.post("/x/enabled", response_class=HTMLResponse)
async def x_set_enabled(
    request: Request,
    enabled: bool = Form(...),
    switch: PostingSwitch = Depends(get_posting_switch),
    posts: SocialPostRepository = Depends(get_social_posts),
    config: Config = Depends(get_config),
    tokens: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """自動投稿を開始・停止する。

    実体は Azure Files 上のファイル。SQLite に置くとリビジョン更新で
    消え、画面で有効にした翌日にマージした時点で黙って止まる。
    """
    switch.set_enabled(enabled)
    return await x_status(request, posts, switch, config, tokens)


@router.post("/x/posts/{post_id}/cancel", response_class=HTMLResponse)
async def x_cancel_post(
    request: Request,
    post_id: int,
    posts: SocialPostRepository = Depends(get_social_posts),
) -> HTMLResponse:
    """予約を取り消す。

    操作名を通す。ボタンが「取り消す」なら結果の文言も「取り消しました」。
    """
    posts.mark_failed(post_id, "取り消しました")
    return await x_queue(request, posts)
```

`_to_slot` は `SocialPost` を帯の1枠に変換する小さな関数
（`{"at": ローカル時刻, "kind": 型, "status": 状態, "label": 表示名}`）。
`_today_posted` は `list_posted_between(その日の00:00, 24:00)`。

`templates/base.html` を書き換える。

- ヘッダーは `bg-ink text-paper`。左にアカウント名、右にスイッチの状態と概算コスト
- 絵文字を外す。状態は色と語で示す
- `<main>` の先頭に帯（`hx-get="/x/band" hx-trigger="load, every 60s"`）
- ポーリングは帯とキューだけ

`templates/index.html` を2カラムに組み替える。左が投稿キュー、右が記事プールと
生成済み動画。記事プールのカードには**チャネル別の消費を丸2つ**で出す
（`article.is_consumed_by('video')` / `('x')`）。

`sm:` より下では1カラム。ボタンには `focus-visible:outline-2` を付ける。
投稿完了の通知は `aria-live="polite"` の領域に出す。

- [ ] **Step 5: CSS を再生成する**

Run: `npm run build:css`
Expected: `static/css/app.css` が更新される。**これを忘れるとスタイルが効かない**

- [ ] **Step 6: テストが通ることを確認する**

Run: `uv run pytest tests/test_web_social.py -v`
Expected: PASS

- [ ] **Step 7: 実際に見る**

Run: `uv run python web_app.py`
確認: 帯が出る、`いま` の線が正しい位置にある、幅を狭めて1カラムになる、
Tab キーでフォーカスが見える

- [ ] **Step 8: 回帰と lint、コミット**

```bash
uv run pytest -m "not live" -q
uv run ruff check . && uv run ruff format . && uv run mypy
git add static/ templates/ src/web/routes.py tests/test_web_social.py
git commit -m "Rebuild the console around today's timeline"
```

---

## Task 8: 計測

**Files:**
- Create: `src/social/metrics.py`
- Modify: `src/jobs/scheduler.py`
- Test: `tests/test_social_metrics.py`

**Interfaces:**
- Consumes: `SocialPostRepository.list_posted_between`, `XClient.fetch_metrics`, `ArtifactStore`
- Produces:
  - `MEASUREMENT_OFFSETS: tuple[timedelta, timedelta]`（24時間後と7日後）
  - `collect_metrics(repository, client, store, now) -> int`（記録した件数）
  - Blob キー `metrics/x/YYYY-MM-DD.json`

- [ ] **Step 1: テストを書く**

`tests/test_social_metrics.py` を新規作成する。

```python
"""投稿の指標の取得と記録。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.social import NewPost, PostKind
from src.social.metrics import collect_metrics
from src.storage.db import create_db_engine, create_session_factory
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


class FakeMetricsClient:
    """問い合わせを記録するクライアント。"""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
        self.batches.append(list(tweet_ids))
        return {tid: {"impression_count": 100, "like_count": 3} for tid in tweet_ids}


class FakeStore:
    """publish されたキーと内容を覚えるだけの保存先。"""

    def __init__(self) -> None:
        self.published: dict[str, bytes] = {}

    def publish(self, path: Path, key: str) -> None:
        self.published[key] = path.read_bytes()


@pytest.fixture
def repository(tmp_path: Path) -> SocialPostRepository:
    url = f"sqlite:///{(tmp_path / 'social.db').as_posix()}"
    upgrade_to_head(url)
    return SocialPostRepository(create_session_factory(create_db_engine(url)))


def _posted(repo: SocialPostRepository, tweet_id: str, posted_at: datetime) -> None:
    """投稿済みの行を1件作る。"""
    repo.enqueue(
        [
            NewPost(
                article_id=f"a{tweet_id}",
                article_title="記事",
                kind=PostKind.SINGLE,
                body="本文",
                has_link=False,
            )
        ],
        {0: posted_at},
    )
    claimed = repo.claim_due(posted_at)
    assert claimed is not None
    repo.mark_posted(claimed.id, tweet_id=tweet_id, posted_at=posted_at)


def test_24時間前と7日前の投稿だけ測る(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    """毎日全件を追うと読み取り課金が月 $8 を超える。2回なら約 $2。"""
    _posted(repository, "day1", NOW - timedelta(hours=24))
    _posted(repository, "week1", NOW - timedelta(days=7))
    _posted(repository, "day3", NOW - timedelta(days=3))  # 対象外
    client = FakeMetricsClient()
    store = FakeStore()

    measured = collect_metrics(repository, client, store, tmp_path, now=NOW)

    assert measured == 2
    assert sorted(sum(client.batches, [])) == ["day1", "week1"]


def test_100件ずつまとめて問い合わせる(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    """GET /2/tweets?ids= は最大100件。1件ずつ引くと課金も時間も増える。"""
    for index in range(150):
        _posted(repository, f"t{index}", NOW - timedelta(hours=24, seconds=index))
    client = FakeMetricsClient()

    collect_metrics(repository, client, FakeStore(), tmp_path, now=NOW)

    assert [len(batch) for batch in client.batches] == [100, 50]


def test_結果は日次ファイルとして保存先に書く(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    """SQLite はデプロイで消えるので、蓄積が要るデータを置けない。"""
    _posted(repository, "day1", NOW - timedelta(hours=24))
    store = FakeStore()

    collect_metrics(repository, FakeMetricsClient(), store, tmp_path, now=NOW)

    assert "metrics/x/2026-08-15.json" in store.published
    saved = json.loads(store.published["metrics/x/2026-08-15.json"])
    assert saved["metrics"]["day1"]["impression_count"] == 100


def test_対象が無ければ何も書かない(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    """空のファイルを毎日置くと、Blob にごみが積もる。"""
    store = FakeStore()

    measured = collect_metrics(repository, FakeMetricsClient(), store, tmp_path, now=NOW)

    assert measured == 0
    assert store.published == {}
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_social_metrics.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.social.metrics'`）

- [ ] **Step 3: `src/social/metrics.py` を書く**

```python
"""投稿の指標を取り、保存先に日次ファイルとして記録する。

なぜ2回だけ測るか
-----------------
読み取りも従量課金（$0.005/投稿）。月240投稿を毎日追うと月 $8 を超えるが、
1投稿につき2回なら約 $2 で済む。24時間で初速、7日で最終的な伸びが分かる。

なぜ Blob に置くか
------------------
ジョブ表の SQLite はコンテナのローカルディスクにあってリビジョン更新で
消える。指標は蓄積してこそ意味を持つデータなので、そこには置けない。

**自動最適化はしない。** 数十件のデータで型や時間帯を自動調整すると、
ノイズに追従して安定しない。伸びたテーマは人が見て判断する。
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from src.storage.social import SocialPostRepository
from src.utils.logger import log_step, log_success

# 投稿からどれだけ経ったものを測るか。
MEASUREMENT_OFFSETS: tuple[timedelta, timedelta] = (timedelta(hours=24), timedelta(days=7))

# 1回の問い合わせで測る件数の上限（GET /2/tweets?ids= の制約）。
BATCH_SIZE = 100

# 対象を選ぶときの時刻の許容幅。
# 計測は1日1回なので、offset ぴったりの投稿は存在しない。
WINDOW = timedelta(hours=12)


class SupportsMetrics(Protocol):
    """指標の取得だけ。"""

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]: ...


class SupportsPublish(Protocol):
    """保存先への publish だけ。"""

    def publish(self, path: Path, key: str) -> None: ...


def collect_metrics(
    repository: SocialPostRepository,
    client: SupportsMetrics,
    store: SupportsPublish,
    work_dir: Path,
    now: datetime | None = None,
) -> int:
    """対象の投稿の指標を取り、日次ファイルとして保存する。

    Args:
        repository: 投稿表
        client: 指標を取れるクライアント
        store: 保存先
        work_dir: 一時ファイルを置く場所（ローカル）
        now: 現在時刻（UTC aware）

    Returns:
        int: 測った件数
    """
    moment = now or datetime.now(UTC)

    tweet_ids: list[str] = []
    for offset in MEASUREMENT_OFFSETS:
        target = moment - offset
        for post in repository.list_posted_between(target - WINDOW, target + WINDOW):
            if post.tweet_id and post.tweet_id not in tweet_ids:
                tweet_ids.append(post.tweet_id)

    if not tweet_ids:
        # 空のファイルを毎日置くと Blob にごみが積もる
        return 0

    log_step(f"{len(tweet_ids)}件の投稿の指標を取得します", "📈")
    metrics: dict[str, dict[str, int]] = {}
    for start in range(0, len(tweet_ids), BATCH_SIZE):
        metrics.update(client.fetch_metrics(tweet_ids[start : start + BATCH_SIZE]))

    key = f"metrics/x/{moment:%Y-%m-%d}.json"
    payload = {"measured_at": moment.isoformat(), "metrics": metrics}

    work_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=work_dir, suffix=".json")
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        store.publish(temp_path, key)
    finally:
        temp_path.unlink(missing_ok=True)

    log_success(f"指標を保存しました（{key}、{len(metrics)}件）")
    return len(metrics)
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_social_metrics.py -v`
Expected: PASS

- [ ] **Step 5: スケジューラに繋いでコミット**

`src/jobs/scheduler.py` の日次実行に足す。**投稿計画とは別の時刻に回す**
（同時に回すと、その日の計画で使う記事の選定と読み取り課金が同じ枠で重なる）。

```bash
uv run pytest -m "not live" -q
uv run ruff check . && uv run ruff format . && uv run mypy
git add src/social/metrics.py src/jobs/scheduler.py tests/test_social_metrics.py
git commit -m "Measure each post twice and keep the record where it survives"
```

---

## 実装前に一次情報で確認する（Task 4 の前に済ませる）

これらは spec の根拠が二次情報なので、**コードを書く前に確認する**。
値が違えば設定の既定値と `BUDGETS` を直す。

- [ ] X 開発者ポータルで従量課金の実際の単価（投稿・リンク付き投稿・読み取り）
- [ ] weighted length の仕様（CJK = 2 カウント、上限 280）
- [ ] `media.write` スコープとメディアアップロードのエンドポイント・課金の有無
- [ ] refresh token のローテーション挙動を実際のトークンで1往復

## 最後に

- [ ] `CLAUDE.md` に追記する。特に次の判断は、知らないと善意で元に戻される
  - 「もう投稿した」の権威は Azure Files 上の記事データ（SQLite は消える）
  - `POSTING` の行は自動で再投稿しない
  - 投稿スイッチは SQLite ではなくファイル
  - 画像カードのラベルは英語のみ
- [ ] `.githooks/pre-push` が通ることを確認して PR を作る
