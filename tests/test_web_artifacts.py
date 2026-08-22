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
        self.fail_delete = False

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

    def delete(self, key: str) -> bool:
        if self.fail_delete:
            raise ArtifactStoreError("削除に失敗しました")
        return self.contents.pop(key, None) is not None

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
            "social/cards/a-1.png": b"png-bytes",
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


# --------------------------------------------------------------------------
# 生成物の配信（プレビュー用）
# --------------------------------------------------------------------------
#
# 画面で動画を再生し画像を出すには、生成物を HTTP で返す経路が必要になる。
# ここが唯一「保存先の中身をブラウザに渡す」場所なので、何を配信して
# よいかと Range の扱いを検査する。


def test_a_video_is_served_from_the_store(client: tuple[TestClient, RecordingUploader]) -> None:
    """ローカルに実体が無くても再生用に配信できること。"""
    test_client, _ = client
    response = test_client.get("/artifacts/videos/20260814_010000_en.mp4")
    assert response.status_code == 200
    assert response.content == b"new-video"
    assert response.headers["content-type"] == "video/mp4"
    # Range に対応していることを `<video>` に伝える。生成した mp4 には
    # faststart が付いておらず moov が末尾にあるため、これが無いと
    # 全部落とすまで再生が始まらずシークも効かない。
    assert response.headers["accept-ranges"] == "bytes"


def test_a_card_image_is_served_from_the_store(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    test_client, _ = client
    response = test_client.get("/artifacts/social/cards/a-1.png")
    assert response.status_code == 200
    assert response.content == b"png-bytes"
    assert response.headers["content-type"] == "image/png"


def test_a_range_request_returns_only_that_span(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    test_client, _ = client
    response = test_client.get(
        "/artifacts/videos/20260814_010000_en.mp4", headers={"Range": "bytes=2-5"}
    )
    assert response.status_code == 206
    assert response.content == b"w-vi"
    assert response.headers["content-range"] == "bytes 2-5/9"


def test_a_suffix_range_reads_from_the_tail(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    """`bytes=-4` に対応すること。

    faststart が無い mp4 では、ブラウザは moov atom を探すために
    まず末尾を要求する。ここが 200（全体）になると、その1回で
    ファイル全部を運ぶことになる。
    """
    test_client, _ = client
    response = test_client.get(
        "/artifacts/videos/20260814_010000_en.mp4", headers={"Range": "bytes=-4"}
    )
    assert response.status_code == 206
    assert response.content == b"ideo"
    assert response.headers["content-range"] == "bytes 5-8/9"


def test_an_open_ended_range_is_clamped(
    client: tuple[TestClient, RecordingUploader], monkeypatch: pytest.MonkeyPatch
) -> None:
    """1レスポンスの大きさに上限を置くこと。

    Blob 構成では返すバイト列をメモリに載せる。長尺（実測で数十MB）に
    `bytes=0-` が来たときに全部を載せないよう切る。Range は要求より
    少なく返してよいので、ブラウザは続きを取りに来る。
    """
    monkeypatch.setattr(routes, "MAX_ARTIFACT_CHUNK_BYTES", 4)
    test_client, _ = client
    response = test_client.get(
        "/artifacts/videos/20260814_010000_en.mp4", headers={"Range": "bytes=0-"}
    )
    assert response.status_code == 206
    assert response.content == b"new-"
    assert response.headers["content-range"] == "bytes 0-3/9"


def test_an_unsatisfiable_range_is_rejected(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    test_client, _ = client
    response = test_client.get(
        "/artifacts/videos/20260814_010000_en.mp4", headers={"Range": "bytes=100-"}
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */9"


def test_a_broken_range_header_falls_back_to_the_whole_file(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    """読めない Range は無視して全体を返すこと（エラーにしない）。"""
    test_client, _ = client
    response = test_client.get(
        "/artifacts/videos/20260814_010000_en.mp4", headers={"Range": "furlongs=1-2"}
    )
    assert response.status_code == 200
    assert response.content == b"new-video"


@pytest.mark.parametrize(
    "key",
    [
        # 台本・音声・トークンは配信しない。プレビューに要らないものを
        # 出せる状態にしておく理由が無い。
        "scripts/20260814_000000_ja.json",
        "audio/20260814_000000_ja.mp3",
        # 許可プレフィックスの下でも、拡張子が違えば配信しない
        "videos/20260814_000000_ja.json",
        # 親をたどる形
        "videos/../scripts/20260814_000000_ja.json",
        # 絶対パス
        "/etc/passwd",
    ],
)
def test_only_allowlisted_artifacts_are_served(
    client: tuple[TestClient, RecordingUploader], key: str
) -> None:
    test_client, _ = client
    response = test_client.get(f"/artifacts/{key}")
    assert response.status_code == 404


def test_an_unknown_key_is_not_found(client: tuple[TestClient, RecordingUploader]) -> None:
    test_client, _ = client
    assert test_client.get("/artifacts/videos/nope.mp4").status_code == 404


def test_the_borrowed_file_is_released_after_serving(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """配信が終わったら借用した一時ファイルを消すこと。

    `StreamingResponse` で本文を後から流す実装に変えると、`fetch` の
    `with` を抜けた後に読むことになり、Blob 構成でだけ壊れる。
    そのときこのテストは「消えていない」ではなく配信の失敗で落ちる。
    """
    test_client, _ = client
    assert test_client.get("/artifacts/videos/20260814_010000_en.mp4").content == b"new-video"
    assert store.lent_paths
    assert not any(p.exists() for p in store.lent_paths)


def test_video_list_offers_a_preview_url(client: tuple[TestClient, RecordingUploader]) -> None:
    test_client, _ = client
    body = test_client.get("/videos").text
    assert 'data-preview-url="/artifacts/videos/20260814_010000_en.mp4"' in body


# --------------------------------------------------------------------------
# 削除
#
# 生成物は消えない限り溜まり続ける。一覧は新しい20件しか出さないので、
# 古い失敗作は画面から見えないまま Blob の課金だけが増えていく。
# --------------------------------------------------------------------------


def test_deleting_a_video_removes_it_from_the_store_and_the_list(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    test_client, _ = client

    response = test_client.delete("/videos/videos/20260814_010000_en.mp4")

    assert response.status_code == 200
    assert "videos/20260814_010000_en.mp4" not in store.contents
    assert "20260814_010000_en.mp4" not in response.text


def test_deleting_a_video_also_removes_its_script_and_audio(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """孤児になった台本と音声を残さない（同じ stem のものだけ）。"""
    test_client, _ = client

    test_client.delete("/videos/videos/20260814_000000_ja.mp4")

    assert "scripts/20260814_000000_ja.json" not in store.contents
    assert "audio/20260814_000000_ja.mp3" not in store.contents


def test_deleting_a_video_keeps_the_images(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """画像は言語をまたいで共有されるので消さない。"""
    store.contents["images/20260814_000000_1.png"] = b"png"
    test_client, _ = client

    test_client.delete("/videos/videos/20260814_000000_ja.mp4")

    assert "images/20260814_000000_1.png" in store.contents


def test_a_missing_companion_does_not_fail_the_delete(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """台本の無い動画（手で置いたもの）も消せること。"""
    store.contents["videos/20260814_020000_ja.mp4"] = b"orphan"
    test_client, _ = client

    response = test_client.delete("/videos/videos/20260814_020000_ja.mp4")

    assert response.status_code == 200
    assert "videos/20260814_020000_ja.mp4" not in store.contents


@pytest.mark.parametrize(
    "key",
    [
        "scripts/20260814_000000_ja.json",  # 台本は消させない
        "audio/20260814_000000_ja.mp3",  # 音声も消させない
        "social/cards/a-1.png",
        "videos/../scripts/20260814_000000_ja.json",
        "videos/20260814_000000_ja.txt",  # 拡張子が違う
    ],
)
def test_only_videos_can_be_deleted(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore, key: str
) -> None:
    """「保存先の中身なら何でも消せる」形にしないこと。

    キーは HTML 経由でフォームから戻ってくる値なので、プレフィックスと
    拡張子で縛る。台本と音声は動画を消したときに**付随物として**消える
    だけで、直接指名して消せてはいけない。
    """
    test_client, _ = client
    before = dict(store.contents)

    response = test_client.delete(f"/videos/{key}")

    assert response.status_code == 404
    assert store.contents == before


def test_a_store_failure_is_reported_not_swallowed(
    client: tuple[TestClient, RecordingUploader], store: FakeRemoteStore
) -> None:
    """消えていないのに一覧が更新されると、消したつもりの動画が残る。"""
    store.fail_delete = True
    test_client, _ = client

    response = test_client.delete("/videos/videos/20260814_010000_en.mp4")

    assert response.status_code == 502


def test_the_list_offers_a_delete_that_asks_first(
    client: tuple[TestClient, RecordingUploader],
) -> None:
    """取り消せない操作なので確認を挟む。"""
    test_client, _ = client
    body = test_client.get("/videos").text

    assert "hx-delete=" in body
    assert "hx-confirm=" in body
