"""生成物を保存先へ送る工程の検証。

守っている性質は2つ。

- キーが `output_dir` からの相対パス（posix）になること。
  Windows の絶対パスをそのまま Blob 名にすると、ローカルと
  リモートでキーが一致しなくなる。
- 保存が失敗しても生成を失敗させないこと。動画はローカルに残っており、
  ここで例外を投げると成功した生成物ごと失敗扱いになる。
"""

from pathlib import Path

import pytest

from config import Config
from src.pipeline import Pipeline
from src.storage.artifacts import ArtifactStoreError, LocalArtifactStore

DUMMY_ENV: dict[str, object] = {
    "azure_openai_endpoint": "https://example.openai.azure.com",
    "azure_openai_api_key": "dummy",
    "azure_openai_deployment": "gpt-5.1",
    "azure_openai_image_deployment": "gpt-image-2",
    "azure_speech_api_key": "dummy",
}


class BrokenStore:
    """publish が必ず失敗する保存先。"""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def publish(self, local_path: Path, key: str) -> str:
        self.attempts.append(key)
        raise ArtifactStoreError("保存先に到達できません")

    def list(self, prefix: str = "") -> list[object]:
        return []

    def exists(self, key: str) -> bool:
        return False

    def fetch(self, key: str) -> object:  # pragma: no cover - このテストでは使わない
        raise ArtifactStoreError(key)


def _pipeline(tmp_path: Path, store: object) -> Pipeline:
    """外部サービスを呼ばない範囲で Pipeline を組み立てる。

    各ジェネレータのコンストラクタはクライアントを作るだけで
    ネットワークに出ないため、ダミーの資格情報で構築できる。
    """
    config = Config(_env_file=None, output_dir=tmp_path / "output", **DUMMY_ENV)  # type: ignore[arg-type,call-arg]
    return Pipeline(config, artifact_store=store)  # type: ignore[arg-type]


def _make_video(root: Path) -> Path:
    path = root / "output" / "videos" / "20260814_000000_ja.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def test_key_is_relative_to_the_output_dir(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path, LocalArtifactStore(tmp_path / "output"))
    video = _make_video(tmp_path)
    assert pipeline._artifact_key(video) == "videos/20260814_000000_ja.mp4"


def test_key_uses_forward_slashes(tmp_path: Path) -> None:
    """Windows でもキーに `\\` が入らないこと。

    Blob 名に `\\` が混ざるとローカルとキーが一致せず、
    アップロードした動画を一覧から引けなくなる。
    """
    pipeline = _pipeline(tmp_path, LocalArtifactStore(tmp_path / "output"))
    key = pipeline._artifact_key(_make_video(tmp_path))
    assert "\\" not in key


def test_publish_reports_the_keys_it_saved(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "elsewhere")
    pipeline = _pipeline(tmp_path, store)
    video = _make_video(tmp_path)

    published = pipeline._publish_artifacts([video])

    assert published == ["videos/20260814_000000_ja.mp4"]
    assert store.exists("videos/20260814_000000_ja.mp4")


def test_a_failing_store_does_not_abort_the_run(tmp_path: Path) -> None:
    """保存の失敗を例外にしないこと（生成物はローカルに残る）。"""
    store = BrokenStore()
    pipeline = _pipeline(tmp_path, store)
    video = _make_video(tmp_path)

    published = pipeline._publish_artifacts([video])

    assert published == []
    assert store.attempts == ["videos/20260814_000000_ja.mp4"]
    assert video.exists(), "ローカルの生成物は残っていること"


def test_one_failure_does_not_stop_the_others(tmp_path: Path) -> None:
    """1件失敗しても残りの保存を続けること。"""
    calls: list[str] = []

    class FlakyStore(BrokenStore):
        def publish(self, local_path: Path, key: str) -> str:
            calls.append(key)
            if key.endswith("_ja.mp4"):
                raise ArtifactStoreError("この1件だけ失敗")
            return key

    pipeline = _pipeline(tmp_path, FlakyStore())
    ja = _make_video(tmp_path)
    en = ja.with_name("20260814_000000_en.mp4")
    en.write_bytes(b"video")

    published = pipeline._publish_artifacts([ja, en])

    assert published == ["videos/20260814_000000_en.mp4"]
    assert len(calls) == 2


def test_default_store_is_local(tmp_path: Path) -> None:
    """既定はローカル保存であること（開発時に Azure を要求しない）。"""
    config = Config(_env_file=None, output_dir=tmp_path / "output", **DUMMY_ENV)  # type: ignore[arg-type,call-arg]
    pipeline = Pipeline(config)
    assert isinstance(pipeline.artifact_store, LocalArtifactStore)


def test_blob_store_without_a_url_fails_at_startup(tmp_path: Path) -> None:
    """blob 指定でアカウント URL が無ければ設定の検証で落ちること。

    生成が終わってから保存先が無いと分かるのが最悪
    （画像6枚ぶんのクォータと数分を無駄にする）。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="AZURE_STORAGE_ACCOUNT_URL"):
        Config(_env_file=None, output_dir=tmp_path, artifact_store="blob", **DUMMY_ENV)  # type: ignore[arg-type,call-arg]
