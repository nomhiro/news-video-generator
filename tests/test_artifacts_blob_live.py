"""Blob Storage への実際の往復（`-m live`）。

なぜ実APIで確かめるか
--------------------
Blob 実装で間違えやすいのは、モックでは再現できない部分に集中している。

- Entra ID 認証が通るか（共有キー認証を無効にしてあるので、
  RBAC が正しく割り当たっていないとここで落ちる）
- キーに `/` を含めたときに階層として扱われるか
- `download_blob().readinto()` が期待どおりバイト列を復元するか

いずれもフェイクでは「自分の思い込みどおりに動く」ことしか確認できない。

実行方法:
    uv run pytest -m live -k blob

環境変数 AZURE_STORAGE_ACCOUNT_URL が未設定ならスキップする。
"""

import os
import uuid
from pathlib import Path

import pytest

from src.storage.artifacts import ArtifactStoreError, BlobArtifactStore

pytestmark = pytest.mark.live

ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "artifacts")


@pytest.fixture
def store() -> BlobArtifactStore:
    if not ACCOUNT_URL:
        pytest.skip("AZURE_STORAGE_ACCOUNT_URL が未設定")
    return BlobArtifactStore.from_account_url(ACCOUNT_URL, CONTAINER)


@pytest.fixture
def prefix() -> str:
    """テストごとに固有の接頭辞。実際のコンテナを汚さないため。"""
    return f"_pytest/{uuid.uuid4().hex[:12]}"


def test_round_trip(store: BlobArtifactStore, prefix: str, tmp_path: Path) -> None:
    """publish -> list -> exists -> fetch が一貫していること。"""
    payload = b"\x00\x01video-bytes\xff" * 100
    source = tmp_path / "sample.mp4"
    source.write_bytes(payload)
    key = f"{prefix}/videos/sample.mp4"

    url = store.publish(source, key)
    assert key in url

    assert store.exists(key) is True
    listed = store.list(f"{prefix}/")
    assert [a.key for a in listed] == [key]
    assert listed[0].size_bytes == len(payload)
    assert listed[0].modified_at.tzinfo is not None

    with store.fetch(key) as local:
        assert local.read_bytes() == payload
        assert local.name == "sample.mp4"
        borrowed = local

    # 借用は一時ファイル。ブロックを抜けたら消えていること
    assert not borrowed.exists()


def test_windows_separators_land_on_the_same_key(
    store: BlobArtifactStore, prefix: str, tmp_path: Path
) -> None:
    """`\\` 区切りのキーでも posix のキーで引けること。

    Windows のローカルパスから作ったキーがそのまま Blob 名になると、
    ローカルとリモートでキーが一致しなくなる。
    """
    source = tmp_path / "a.mp3"
    source.write_bytes(b"audio")
    store.publish(source, f"{prefix}\\audio\\a.mp3")
    assert store.exists(f"{prefix}/audio/a.mp3") is True


def test_missing_key_raises(store: BlobArtifactStore, prefix: str) -> None:
    assert store.exists(f"{prefix}/nope.mp4") is False
    with pytest.raises(ArtifactStoreError, match="見つかりません"):
        with store.fetch(f"{prefix}/nope.mp4"):
            pass
