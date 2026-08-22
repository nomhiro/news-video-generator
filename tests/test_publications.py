"""公開の記録（`data/publications.json`）の読み書き。

ここで見張っているのは「二重公開のガードが働くための最低条件」——
**記録が残ること**と、**読めない記録で画面を落とさないこと**の2つ。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.publications import (
    CHANNEL_TIKTOK,
    CHANNEL_YOUTUBE,
    PublicationStore,
)


@pytest.fixture
def store(tmp_path: Path) -> PublicationStore:
    return PublicationStore(tmp_path / "publications.json")


def test_記録した公開が読み出せる(store: PublicationStore) -> None:
    store.record(
        "videos/a_ja.mp4",
        CHANNEL_YOUTUBE,
        external_id="abc123",
        url="https://www.youtube.com/watch?v=abc123",
        article_id="art1",
        at=datetime(2026, 8, 22, 9, 10, tzinfo=UTC),
    )

    found = store.for_video("videos/a_ja.mp4")

    assert [p.channel for p in found] == [CHANNEL_YOUTUBE]
    assert found[0].external_id == "abc123"
    assert found[0].at == datetime(2026, 8, 22, 9, 10, tzinfo=UTC)
    assert found[0].label == "YouTube"


def test_ファイルが無ければ空(store: PublicationStore) -> None:
    assert store.for_video("videos/a_ja.mp4") == []
    assert store.by_article() == {}


def test_チャネルごとに別の記録になる(store: PublicationStore) -> None:
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="y", url="u1")
    store.record("videos/a_ja.mp4", CHANNEL_TIKTOK, external_id="t", url="u2")

    assert [p.channel for p in store.for_video("videos/a_ja.mp4")] == [
        CHANNEL_YOUTUBE,
        CHANNEL_TIKTOK,
    ]


def test_同じチャネルの再公開は上書きする(store: PublicationStore) -> None:
    """限定公開からやり直す経路があるので、2回目を拒否はしない。

    記録の役割は「1回目があったことを画面に出す」ことで、押させないこと
    ではない（`record` の docstring 参照）。
    """
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="old", url="u")
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="new", url="u")

    found = store.for_video("videos/a_ja.mp4")

    assert len(found) == 1
    assert found[0].external_id == "new"


def test_記事のIDで逆引きできる(store: PublicationStore) -> None:
    """1つの記事から複数の動画ができるので、値はリストになる。"""
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="1", url="u", article_id="art1")
    store.record("videos/a_en.mp4", CHANNEL_YOUTUBE, external_id="2", url="u", article_id="art1")
    store.record("videos/b_ja.mp4", CHANNEL_YOUTUBE, external_id="3", url="u", article_id="art2")

    by_article = store.by_article()

    assert sorted(by_article) == ["art1", "art2"]
    assert len(by_article["art1"]) == 2


def test_記事に辿れない動画も記録される(store: PublicationStore) -> None:
    """CLI の自由テキストから作った動画は台本の `source_url` が空。

    **ガードは記事と無関係に働かなければならない。** 欠けるのは記事カードの
    バッジだけで、二重公開を止める側は article_id を要求しない。
    """
    store.record("videos/manual.mp4", CHANNEL_YOUTUBE, external_id="1", url="u")

    assert len(store.for_video("videos/manual.mp4")) == 1
    assert store.by_article() == {}


def test_後の記録が記事のIDを消さない(store: PublicationStore) -> None:
    """台本を消したあとに別チャネルへ公開しても、逆引きを失わない。"""
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="1", url="u", article_id="art1")
    store.record("videos/a_ja.mp4", CHANNEL_TIKTOK, external_id="2", url="u", article_id=None)

    assert "art1" in store.by_article()
    assert len(store.by_article()["art1"]) == 2


def test_Windowsの区切りでも同じキーになる(store: PublicationStore) -> None:
    """キーは HTML のフォーム経由で戻ってくる。`normalize_key` を通す。

    通さないと、記録したのに引けない（＝ガードが黙って効かない）。
    """
    store.record("videos\\a_ja.mp4", CHANNEL_YOUTUBE, external_id="1", url="u")

    assert len(store.for_video("videos/a_ja.mp4")) == 1


def test_使えないキーは空を返す(store: PublicationStore) -> None:
    """`..` を含むキーで画面を落とさない。"""
    assert store.for_video("../secrets.mp4") == []


def test_壊れたJSONは無いものとして扱う(tmp_path: Path) -> None:
    """例外にすると、書き込みが中断されただけで画面が 500 になる。"""
    path = tmp_path / "publications.json"
    path.write_text("{壊れている", encoding="utf-8")
    store = PublicationStore(path)

    assert store.for_video("videos/a_ja.mp4") == []
    assert store.by_article() == {}

    # 壊れた状態からでも記録できる（上書きして復帰する）。
    store.record("videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="1", url="u")
    assert len(store.for_video("videos/a_ja.mp4")) == 1


def test_辞書でない中身も無いものとして扱う(tmp_path: Path) -> None:
    path = tmp_path / "publications.json"
    path.write_text("[]", encoding="utf-8")

    assert PublicationStore(path).by_article() == {}


def test_時刻が読めなくても公開の事実は残る(tmp_path: Path) -> None:
    """時刻は画面の補足で、公開したという事実の方が重要。"""
    path = tmp_path / "publications.json"
    path.write_text(
        json.dumps({"videos/a.mp4": {"youtube": {"id": "1", "url": "u", "at": "きのう"}}}),
        encoding="utf-8",
    )

    found = PublicationStore(path).for_video("videos/a.mp4")

    assert len(found) == 1
    assert found[0].at is None


def test_naiveな時刻はUTCとして読む(tmp_path: Path) -> None:
    """古い記録や手で書いた記録でも比較・表示ができるようにする。"""
    path = tmp_path / "publications.json"
    path.write_text(
        json.dumps(
            {"videos/a.mp4": {"youtube": {"id": "1", "url": "u", "at": "2026-08-22T09:10:00"}}}
        ),
        encoding="utf-8",
    )

    found = PublicationStore(path).for_video("videos/a.mp4")

    assert found[0].at == datetime(2026, 8, 22, 9, 10, tzinfo=UTC)


def test_書き込みは原子的で中間ファイルを残さない(tmp_path: Path) -> None:
    """置換の途中で落ちても壊れた JSON が残らないこと（の名残を見る）。

    実際の中断は再現できないので、成功時に `.tmp` が残らないことを見る。
    残ると Azure Files に無限にゴミが溜まる。
    """
    store = PublicationStore(tmp_path / "publications.json")
    store.record("videos/a.mp4", CHANNEL_YOUTUBE, external_id="1", url="u")

    assert list(tmp_path.glob("*.tmp")) == []
