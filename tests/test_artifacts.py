"""生成物の保存先（ArtifactStore）。

Blob の実装は実APIを叩かないと意味のある検証にならないため、
ここではローカル実装とキーの正規化を見る。Blob は
`tests/test_artifacts_blob.py`（`-m live`）で往復を確認する。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    LocalArtifactStore,
    build_artifact_store,
    normalize_key,
)


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "output")


def _make_file(path: Path, content: bytes = b"video-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# --------------------------------------------------------------------------
# キーの正規化
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("videos/a.mp4", "videos/a.mp4"),
        # Windows のパス区切りが混ざったキーをそのまま Blob 名にすると、
        # ローカルとキーが一致しなくなる
        ("videos\\a.mp4", "videos/a.mp4"),
        ("/videos/a.mp4", "videos/a.mp4"),
        ("videos/a.mp4/", "videos/a.mp4"),
    ],
)
def test_keys_are_normalized_to_posix(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


@pytest.mark.parametrize("raw", ["", "/", "../secrets.env", "videos/../../x", "C:/Windows/x"])
def test_keys_escaping_the_root_are_rejected(raw: str) -> None:
    """ルートの外を指すキーを弾くこと。

    ローカル保存では `..` を含むキーで `output_dir` の外に書き出せてしまう。
    キーは動画一覧の HTML 経由でフォームに戻ってくる値なので、
    入口で弾く必要がある。
    """
    with pytest.raises(ValueError):
        normalize_key(raw)


# --------------------------------------------------------------------------
# ローカル保存
# --------------------------------------------------------------------------


def test_publish_copies_into_the_root(store: LocalArtifactStore, tmp_path: Path) -> None:
    source = _make_file(tmp_path / "elsewhere" / "a.mp4")
    store.publish(source, "videos/a.mp4")
    assert (store.root / "videos" / "a.mp4").read_bytes() == b"video-bytes"


def test_publishing_a_file_already_in_place_keeps_its_content(store: LocalArtifactStore) -> None:
    """生成した場所が保存先そのものの場合に内容を壊さないこと。

    通常の経路（output_dir 内で生成 → 同じ output_dir に publish）が
    これにあたる。同一ファイルへの copy は内容を失う。
    """
    source = _make_file(store.root / "videos" / "a.mp4")
    store.publish(source, "videos/a.mp4")
    assert source.read_bytes() == b"video-bytes"


def test_publishing_a_missing_file_raises(store: LocalArtifactStore, tmp_path: Path) -> None:
    with pytest.raises(ArtifactStoreError):
        store.publish(tmp_path / "nope.mp4", "videos/nope.mp4")


def test_list_returns_newest_first(store: LocalArtifactStore) -> None:
    """新しい順に返すこと（動画一覧が最新を先頭に出す前提）。"""
    old = _make_file(store.root / "videos" / "old.mp4")
    new = _make_file(store.root / "videos" / "new.mp4")
    # mtime を明示的にずらす。同一秒に作られると順序が不定になる
    import os

    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))

    keys = [a.key for a in store.list("videos/")]
    assert keys == ["videos/new.mp4", "videos/old.mp4"]
    assert new.exists()


def test_list_filters_by_prefix(store: LocalArtifactStore) -> None:
    _make_file(store.root / "videos" / "a.mp4")
    _make_file(store.root / "audio" / "a.mp3")
    assert [a.key for a in store.list("videos/")] == ["videos/a.mp4"]


def test_list_on_a_missing_root_is_empty(store: LocalArtifactStore) -> None:
    """まだ何も生成していない状態でも一覧が落ちないこと。"""
    assert store.list() == []


def test_list_reports_size_and_utc_time(store: LocalArtifactStore) -> None:
    _make_file(store.root / "videos" / "a.mp4", b"12345")
    (info,) = store.list("videos/")
    assert info.size_bytes == 5
    assert info.modified_at.tzinfo is not None
    assert info.modified_at.astimezone(UTC) <= datetime.now(UTC)
    assert info.name == "a.mp4"


def test_exists(store: LocalArtifactStore) -> None:
    _make_file(store.root / "videos" / "a.mp4")
    assert store.exists("videos/a.mp4") is True
    assert store.exists("videos/b.mp4") is False


def test_fetch_yields_the_real_path_without_copying(store: LocalArtifactStore) -> None:
    """ローカル保存では実体をそのまま貸すこと（無駄なコピーをしない）。"""
    source = _make_file(store.root / "videos" / "a.mp4")
    with store.fetch("videos/a.mp4") as path:
        assert path == source
    # 借用を終えても消えない
    assert source.exists()


def test_fetching_a_missing_key_raises(store: LocalArtifactStore) -> None:
    with pytest.raises(ArtifactStoreError):
        with store.fetch("videos/nope.mp4"):
            pass


def test_local_store_satisfies_the_protocol(store: LocalArtifactStore) -> None:
    """Protocol を満たすこと（フェイクを差し替えられる前提）。"""
    assert isinstance(store, ArtifactStore)


# --------------------------------------------------------------------------
# 設定からの組み立て
# --------------------------------------------------------------------------


def test_build_local_store(tmp_path: Path) -> None:
    built = build_artifact_store("local", local_root=tmp_path)
    assert isinstance(built, LocalArtifactStore)
    assert built.root == tmp_path


def test_build_blob_store_without_a_url_raises(tmp_path: Path) -> None:
    """blob 指定でアカウント URL が無ければ組み立て時点で落ちること。

    生成が終わってから保存先が無いと分かるのが最悪
    （画像6枚ぶんのクォータと数分を捨てることになる）。
    """
    with pytest.raises(ArtifactStoreError, match="AZURE_STORAGE_ACCOUNT_URL"):
        build_artifact_store("blob", local_root=tmp_path)


def test_build_unknown_store_raises(tmp_path: Path) -> None:
    with pytest.raises(ArtifactStoreError, match="local"):
        build_artifact_store("s3", local_root=tmp_path)
