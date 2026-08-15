"""投稿の指標の取得と記録。"""

import json
from datetime import UTC, datetime, timedelta
from itertools import chain
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
    """publish されたキーと内容を覚えるだけの保存先。

    `ArtifactStore.publish` の実シグネチャ（`src/storage/artifacts.py`）は
    引数名 `local_path`、戻り値 `str`（参照用 URI）。ここで `path: Path -> None`
    にすると型検査は通ってもプロトコルとして不正なフェイクになる。
    """

    def __init__(self) -> None:
        self.published: dict[str, bytes] = {}

    def publish(self, local_path: Path, key: str) -> str:
        self.published[key] = local_path.read_bytes()
        return f"fake://{key}"


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


def test_24時間前と7日前の投稿だけ測る(repository: SocialPostRepository, tmp_path: Path) -> None:
    """毎日全件を追うと読み取り課金が月 $8 を超える。2回なら約 $2。"""
    _posted(repository, "day1", NOW - timedelta(hours=24))
    _posted(repository, "week1", NOW - timedelta(days=7))
    _posted(repository, "day3", NOW - timedelta(days=3))  # 対象外
    client = FakeMetricsClient()
    store = FakeStore()

    measured = collect_metrics(repository, client, store, tmp_path, now=NOW)

    assert measured == 2
    assert sorted(chain.from_iterable(client.batches)) == ["day1", "week1"]


def test_100件ずつまとめて問い合わせる(repository: SocialPostRepository, tmp_path: Path) -> None:
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
    assert saved["records"][0]["metrics"]["impression_count"] == 100


def test_レコードは_それ自体で解釈できる(repository: SocialPostRepository, tmp_path: Path) -> None:
    """平坦な `tweet_id -> metrics` だと、あとから読めない。

    24時間後の値と7日後の値が同じ形で同じ辞書に入るため、どちらの測定かは
    `posted_at` と突き合わせないと分からない。その `posted_at` は SQLite に
    しか無く**デプロイごとに消える**ので、Blob に蓄積する意味が1回の
    マージで失われていた。1レコードに投稿の識別と時刻を持たせる。
    """
    posted_at = NOW - timedelta(hours=24)
    _posted(repository, "day1", posted_at)
    _posted(repository, "week1", NOW - timedelta(days=7))
    store = FakeStore()

    collect_metrics(repository, FakeMetricsClient(), store, tmp_path, now=NOW)

    saved = json.loads(store.published["metrics/x/2026-08-15.json"])
    by_id = {r["tweet_id"]: r for r in saved["records"]}
    assert by_id["day1"]["offset_hours"] == 24
    assert by_id["week1"]["offset_hours"] == 168
    assert by_id["day1"]["posted_at"] == posted_at.isoformat()
    assert by_id["day1"]["kind"] == "single"
    assert by_id["day1"]["article_title"] == "記事"
    assert by_id["day1"]["metrics"]["like_count"] == 3


def test_応答に無い投稿もレコードとして残す(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    """落とすと「測ろうとして取れなかった」ことが記録から消える。"""

    class EmptyClient:
        def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]:
            return {}

    _posted(repository, "gone", NOW - timedelta(hours=24))
    store = FakeStore()

    measured = collect_metrics(repository, EmptyClient(), store, tmp_path, now=NOW)

    assert measured == 0  # 取れたものは無い
    saved = json.loads(store.published["metrics/x/2026-08-15.json"])
    assert saved["records"][0]["tweet_id"] == "gone"
    assert saved["records"][0]["metrics"] == {}


def test_対象が無ければ何も書かない(repository: SocialPostRepository, tmp_path: Path) -> None:
    """空のファイルを毎日置くと、Blob にごみが積もる。"""
    store = FakeStore()

    measured = collect_metrics(repository, FakeMetricsClient(), store, tmp_path, now=NOW)

    assert measured == 0
    assert store.published == {}
