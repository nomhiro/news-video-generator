"""Web 層が生成物を保存先経由で扱うことの検証。

以前は `output_dir/videos` を直接 glob し、アップロードにはローカルの
絶対パスをフォームで往復させていた。コンテナで動かすと生成物は
Blob 上にあるので、その形では一覧に何も出ず、アップロードもできない。

ここでは「ローカルにファイルが無くても一覧とアップロードが成立する」ことを、
リモート保存を模したフェイクで確かめる。
"""

import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.storage.artifacts import ArtifactInfo, ArtifactStoreError
from src.web import routes
from src.web.dependencies import (
    get_artifact_store,
    get_config,
    get_tiktok_uploader,
    get_youtube_uploader,
)


class FakeRemoteStore:
    """Blob のように、実体がローカルに無い保存先。

    `fetch` は一時ファイルにコピーして貸し、抜けたら消す。
    ローカル実装との違い（借り物である）をここで再現する。
    """

    def __init__(self, contents: dict[str, bytes]):
        self.contents = contents
        self.lent_paths: list[Path] = []
        self.fail_list = False

    def publish(self, local_path: Path, key: str) -> str:
        self.contents[key] = local_path.read_bytes()
        return f"https://fake.blob.core.windows.net/artifacts/{key}"

    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        if self.fail_list:
            raise ArtifactStoreError("一覧の取得に失敗しました")
        base = datetime(2026, 8, 14, tzinfo=UTC)
        return sorted(
            (
                ArtifactInfo(
                    key=key,
                    size_bytes=len(data),
                    # キー順に新しくなるよう時刻をずらす（順序を検証するため）
                    modified_at=base + timedelta(minutes=index),
                )
                for index, (key, data) in enumerate(self.contents.items())
                if key.startswith(prefix)
            ),
            key=lambda a: a.modified_at,
            reverse=True,
        )

    def exists(self, key: str) -> bool:
        return key in self.contents

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        if key not in self.contents:
            raise ArtifactStoreError(f"生成物が見つかりません: {key}")
        temp_dir = Path(tempfile.mkdtemp(prefix="fake-artifact-"))
        path = temp_dir / Path(key).name
        path.write_bytes(self.contents[key])
        self.lent_paths.append(path)
        try:
            yield path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class RecordingUploader:
    """アップロード先に渡されたパスを記録する差し替え。"""

    def __init__(self) -> None:
        self.received_paths: list[str] = []

    def is_authenticated(self) -> bool:
        return True

    def upload(self, video_path: str, **kwargs: Any) -> Any:
        self.received_paths.append(video_path)
        # アップロード時点でファイルが読めることを確かめる。
        # fetch の外で upload を呼ぶ実装に戻ると、ここで落ちる。
        assert Path(video_path).is_file(), "アップロード時にファイルが存在しない"

        class Result:
            success = True
            video_id = "abc123"
            video_url = "https://youtu.be/abc123"
            publish_id = "pub123"
            error_message = None

        return Result()


SCRIPT_JSON = json.dumps(
    {"title": "台本のタイトル", "description": "台本の説明"}, ensure_ascii=False
).encode("utf-8")


@pytest.fixture
def store() -> FakeRemoteStore:
    return FakeRemoteStore(
        {
            "scripts/20260814_000000_ja.json": SCRIPT_JSON,
            "videos/20260814_000000_ja.mp4": b"old-video",
            "videos/20260814_010000_en.mp4": b"new-video",
            "audio/20260814_000000_ja.mp3": b"audio",
        }
    )


@pytest.fixture
def client(store: FakeRemoteStore) -> Iterator[tuple[TestClient, RecordingUploader]]:
    uploader = RecordingUploader()

    class FakeConfig:
        youtube_default_privacy = "public"
        tiktok_default_privacy = "SELF_ONLY"

        def is_tiktok_configured(self) -> bool:
            return True

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_youtube_uploader] = lambda: uploader
    app.dependency_overrides[get_tiktok_uploader] = lambda: uploader
    app.dependency_overrides[get_config] = lambda: FakeConfig()

    with TestClient(app) as test_client:
        yield test_client, uploader


# --------------------------------------------------------------------------
# 動画一覧
# --------------------------------------------------------------------------


def test_video_list_comes_from_the_store(client: tuple[TestClient, RecordingUploader]) -> None:
    """ローカルに実体が無くても一覧に出ること。"""
    test_client, _ = client
    response = test_client.get("/videos")
    assert response.status_code == 200
    body = response.text
    assert "20260814_010000_en.mp4" in body
    assert "20260814_000000_ja.mp4" in body
    # 動画以外は一覧に混ぜない
    assert ".mp3" not in body


def test_video_list_is_newest_first(client: tuple[TestClient, RecordingUploader]) -> None:
    test_client, _ = client
    body = test_client.get("/videos").text
    assert body.index("20260814_010000_en.mp4") < body.index("20260814_000000_ja.mp4")


def test_video_list_carries_the_key_not_a_local_path(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    """ボタンが持つのは保存先のキーであること。

    以前はローカルの絶対パスを HTML に埋めてフォームで送り返していた。
    Blob 保存ではそのパスは存在しないうえ、任意パスを受け取る形になる。
    """
    test_client, _ = client
    body = test_client.get("/videos").text
    assert 'data-key="videos/20260814_010000_en.mp4"' in body
    assert "data-path=" not in body


def test_video_list_reads_title_from_the_script(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    """対応する台本 JSON からタイトルを引くこと。"""
    test_client, _ = client
    assert "台本のタイトル" in test_client.get("/videos").text


def test_video_list_survives_a_store_failure(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """保存先が落ちていても画面は出ること（500 にしない）。"""
    store.fail_list = True
    test_client, _ = client
    response = test_client.get("/videos")
    assert response.status_code == 200
    assert "まだ動画がありません" in response.text


def test_language_is_taken_from_the_key_suffix() -> None:
    assert routes._language_from_key("videos/20260814_000000_ja.mp4") == "ja"
    assert routes._language_from_key("videos/noseparator.mp4") == "unknown"


# --------------------------------------------------------------------------
# アップロード
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        (
            "/youtube/upload",
            {"video_key": "videos/20260814_010000_en.mp4", "title": "t", "description": "d"},
        ),
        ("/tiktok/upload", {"video_key": "videos/20260814_010000_en.mp4", "title": "t"}),
    ],
)
def test_upload_borrows_a_local_copy_from_the_store(
    client: tuple[TestClient, RecordingUploader],
    store: FakeRemoteStore,
    endpoint: str,
    payload: dict[str, str],
) -> None:
    """保存先から借りたローカルパスをアップローダに渡すこと。"""
    test_client, uploader = client
    response = test_client.post(endpoint, data=payload)
    assert response.status_code == 200
    assert uploader.received_paths, "アップローダが呼ばれていない"
    # 借り物なので output_dir ではなく一時ディレクトリのパスになる
    assert Path(uploader.received_paths[-1]) in store.lent_paths


def test_borrowed_file_is_cleaned_up(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """借用が終わったら一時ファイルを消すこと。

    動画は数MB〜数十MBあり、放置するとコンテナのディスクを埋める。
    """
    test_client, _ = client
    test_client.post(
        "/youtube/upload",
        data={"video_key": "videos/20260814_010000_en.mp4", "title": "t", "description": ""},
    )
    assert store.lent_paths
    assert not any(p.exists() for p in store.lent_paths)


@pytest.mark.parametrize("endpoint", ["/youtube/upload", "/tiktok/upload"])
def test_uploading_an_unknown_key_reports_not_found(
    client: tuple[TestClient, RecordingUploader], endpoint: str
) -> None:
    """存在しないキーはアップロードを試みる前に弾くこと。"""
    test_client, uploader = client
    response = test_client.post(
        endpoint, data={"video_key": "videos/nope.mp4", "title": "t", "description": ""}
    )
    assert response.status_code == 200
    assert "見つかりません" in response.text
    assert uploader.received_paths == []
