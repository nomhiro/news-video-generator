"""生成物（台本・画像・音声・動画）の保存先。

なぜ抽象化するか
----------------
生成は必ずローカルのファイルシステム上で行う。ffmpeg は
subprocess で起動する外部プロセスで、パスしか受け取れないため
これは変えられない。

一方で**保存先**はローカルに固定できない。コンテナのファイルシステムは
再起動で消え、レプリカ間で共有されない。生成した動画が消えるのは
単なる不便ではなく、YouTube にアップロードする前に成果物を失うということ。

そこで「ローカルで作る」と「どこかに保存する」を分ける。
生成は `output_dir` で行い、終わったものを `ArtifactStore` に publish する。
ストアがローカルなら publish は同一ファイルシステム内の移動で済み、
Blob Storage なら実際のアップロードになる。

読み出し側（動画一覧・アップローダ）は `fetch()` でローカルパスを借りる。
ローカルストアなら実体をそのまま渡し、Blob なら一時ファイルに落として
使い終わったら消す。呼び出し側はどちらか知らなくてよい。

キーの形
--------
キーは `videos/20260814_005245_ja.mp4` のような**posix 形式の相対パス**。
`output_dir` 以下のレイアウトをそのまま使う。Blob の名前も同じにするので、
ストレージエクスプローラで見たときにローカルと同じ構造で並ぶ。

Windows の `\\` は使わない。Blob 名に含めると別階層として扱われるうえ、
ローカルと Blob でキーが一致しなくなる。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.utils.logger import log_step, log_success

if TYPE_CHECKING:  # pragma: no cover - 型のためだけの import
    from azure.storage.blob import ContainerClient


class ArtifactStoreError(Exception):
    """生成物の保存・取得に失敗した。"""


@dataclass(frozen=True)
class ArtifactInfo:
    """保存済みの生成物1件。

    Attributes:
        key: ストア内のキー（posix 形式の相対パス）
        size_bytes: サイズ
        modified_at: 最終更新時刻（UTC aware）
    """

    key: str
    size_bytes: int
    modified_at: datetime

    @property
    def name(self) -> str:
        """キーの末尾（ファイル名）。"""
        return PurePosixPath(self.key).name


def normalize_key(key: str) -> str:
    """キーを posix 形式に正規化する。

    Windows のパス区切りが混ざったキーで publish すると、
    Blob 名に `\\` が入ってローカルとキーが一致しなくなる。

    Args:
        key: 正規化前のキー

    Returns:
        str: `videos/xxx.mp4` の形

    Raises:
        ValueError: 絶対パスや `..` を含む場合
    """
    normalized = key.replace("\\", "/").strip("/")
    if not normalized:
        raise ValueError("キーが空です")
    parts = PurePosixPath(normalized).parts
    if ".." in parts or normalized.startswith("/") or ":" in parts[0]:
        # ローカルストアでは root の外に書き出せてしまうため入口で弾く
        raise ValueError(f"キーに使えない形式です: {key!r}")
    return normalized


@runtime_checkable
class ArtifactStore(Protocol):
    """生成物の保存先。

    実装は `LocalArtifactStore` と `BlobArtifactStore`。
    テストではフェイクを差し込める（Protocol にしている理由）。
    """

    def publish(self, local_path: Path, key: str) -> str:
        """ローカルのファイルをストアに保存し、参照用の URI を返す。"""
        ...

    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        """prefix に一致する生成物を新しい順に返す。"""
        ...

    def exists(self, key: str) -> bool:
        """キーが存在するか。"""
        ...

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        """ローカルパスを借りる。ブロックを抜けたら一時ファイルは消える。"""
        ...


class LocalArtifactStore:
    """ローカルファイルシステムに保存する。

    開発時の既定。生成した場所がそのまま保存先になるので、
    publish は多くの場合何もしない（同一パスの検出）。
    """

    def __init__(self, root: Path):
        """初期化する。

        Args:
            root: 保存先のルート（`output_dir` を渡す想定）
        """
        self.root = root

    def _path_for(self, key: str) -> Path:
        return self.root / normalize_key(key)

    def publish(self, local_path: Path, key: str) -> str:
        """ルート配下へコピーする。

        生成物が既に所定の位置にある場合（`output_dir` 内で生成した通常の
        経路）は何もしない。同じファイルへの copy は内容を失う。

        Args:
            local_path: 保存するファイル
            key: ストア内のキー

        Returns:
            str: 保存先の絶対パス

        Raises:
            ArtifactStoreError: 元のファイルが無い場合
        """
        if not local_path.is_file():
            raise ArtifactStoreError(f"保存元のファイルがありません: {local_path}")

        destination = self._path_for(key)
        if destination.resolve() == local_path.resolve():
            return str(destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, destination)
        return str(destination)

    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        """ルート配下のファイルを新しい順に返す。

        Args:
            prefix: キーの前方一致（`"videos/"` など）

        Returns:
            list[ArtifactInfo]: 更新時刻の降順
        """
        if not self.root.exists():
            return []

        found: list[ArtifactInfo] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if not key.startswith(prefix):
                continue
            stat = path.stat()
            found.append(
                ArtifactInfo(
                    key=key,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return sorted(found, key=lambda a: a.modified_at, reverse=True)

    def exists(self, key: str) -> bool:
        """キーが存在するか。"""
        return self._path_for(key).is_file()

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        """実体のパスをそのまま渡す（コピーしない）。

        Args:
            key: ストア内のキー

        Yields:
            Path: ローカルパス

        Raises:
            ArtifactStoreError: キーが無い場合
        """
        path = self._path_for(key)
        if not path.is_file():
            raise ArtifactStoreError(f"生成物が見つかりません: {key}")
        yield path


class BlobArtifactStore:
    """Azure Blob Storage に保存する。

    認証はキーではなく Entra ID（`DefaultAzureCredential`）で行う。
    ローカルでは `az login` の資格情報、Container Apps ではマネージド ID が
    使われる。接続文字列やアカウントキーを `.env` に置かずに済むので、
    漏洩する対象がそもそも無くなる。

    ストレージアカウント側は共有キー認証を無効にしてある
    （`infra/core/storage.bicep`）ので、キーを使う経路は存在しない。
    """

    def __init__(self, container_client: ContainerClient):
        """初期化する。

        通常は `from_account_url` を使う。コンストラクタが
        `ContainerClient` を受けるのは、テストで差し替えるため。

        Args:
            container_client: 保存先コンテナのクライアント
        """
        self._container = container_client

    @classmethod
    def from_account_url(
        cls, account_url: str, container_name: str, *, create_container: bool = False
    ) -> BlobArtifactStore:
        """アカウント URL とコンテナ名から組み立てる。

        Args:
            account_url: `https://<account>.blob.core.windows.net`
            container_name: コンテナ名
            create_container: 無ければ作る（IaC で作る前提なので既定は False）

        Returns:
            BlobArtifactStore: 組み立てたストア

        Raises:
            ArtifactStoreError: 認証や接続に失敗した場合
        """
        # import をメソッド内に置く。Blob を使わない構成（既定のローカル）で
        # azure-identity の初期化コストを払わないため。
        from azure.core.exceptions import AzureError, ResourceExistsError
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        try:
            client = ContainerClient(
                account_url=account_url,
                container_name=container_name,
                credential=DefaultAzureCredential(),
            )
            if create_container:
                try:
                    client.create_container()
                except ResourceExistsError:
                    pass
        except AzureError as e:
            raise ArtifactStoreError(f"Blob ストレージに接続できません: {e}") from e
        return cls(client)

    def publish(self, local_path: Path, key: str) -> str:
        """Blob にアップロードする。

        同じキーは上書きする。生成物のキーはタイムスタンプを含むので
        衝突は再実行時だけで、そのときは新しい方が正しい。

        Args:
            local_path: 保存するファイル
            key: Blob 名

        Returns:
            str: Blob の URL

        Raises:
            ArtifactStoreError: 元のファイルが無い、または転送に失敗した場合
        """
        from azure.core.exceptions import AzureError

        if not local_path.is_file():
            raise ArtifactStoreError(f"保存元のファイルがありません: {local_path}")

        blob_name = normalize_key(key)
        size_mb = local_path.stat().st_size / (1024 * 1024)
        log_step(f"生成物をアップロード中... ({blob_name}, {size_mb:.1f}MB)", "☁️")
        try:
            with local_path.open("rb") as stream:
                blob = self._container.upload_blob(name=blob_name, data=stream, overwrite=True)
        except AzureError as e:
            raise ArtifactStoreError(f"アップロードに失敗しました ({blob_name}): {e}") from e
        log_success(f"アップロードしました: {blob_name}")
        return str(blob.url)

    def list(self, prefix: str = "") -> list[ArtifactInfo]:
        """Blob を新しい順に返す。

        Args:
            prefix: Blob 名の前方一致

        Returns:
            list[ArtifactInfo]: 更新時刻の降順

        Raises:
            ArtifactStoreError: 一覧の取得に失敗した場合
        """
        from azure.core.exceptions import AzureError

        try:
            blobs = list(self._container.list_blobs(name_starts_with=prefix or None))
        except AzureError as e:
            raise ArtifactStoreError(f"生成物の一覧を取得できません: {e}") from e

        found = [
            ArtifactInfo(
                key=blob.name,
                size_bytes=blob.size or 0,
                # last_modified は tz aware で返るが、念のため UTC を補う
                modified_at=(blob.last_modified or datetime.now(UTC)).astimezone(UTC),
            )
            for blob in blobs
        ]
        return sorted(found, key=lambda a: a.modified_at, reverse=True)

    def exists(self, key: str) -> bool:
        """Blob が存在するか。"""
        from azure.core.exceptions import AzureError

        try:
            return bool(self._container.get_blob_client(normalize_key(key)).exists())
        except AzureError as e:
            raise ArtifactStoreError(f"存在確認に失敗しました ({key}): {e}") from e

    @contextmanager
    def fetch(self, key: str) -> Iterator[Path]:
        """一時ファイルにダウンロードして貸す。

        アップローダは動画ファイルのパスを要求するため、Blob 上の
        生成物を一度ローカルに落とす必要がある。ブロックを抜けたら消す
        （動画は数MB〜数十MBあり、放置するとコンテナのディスクを埋める）。

        Args:
            key: Blob 名

        Yields:
            Path: ダウンロードした一時ファイル

        Raises:
            ArtifactStoreError: ダウンロードに失敗した場合
        """
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        blob_name = normalize_key(key)
        # 一時ディレクトリを作り、その中に元のファイル名で置く。
        # アップローダが拡張子を見て形式を判断することがあるため名前を保つ。
        temp_dir = Path(tempfile.mkdtemp(prefix="newsvideo-artifact-"))
        local_path = temp_dir / PurePosixPath(blob_name).name
        try:
            try:
                with local_path.open("wb") as f:
                    self._container.download_blob(blob_name).readinto(f)
            except ResourceNotFoundError as e:
                raise ArtifactStoreError(f"生成物が見つかりません: {blob_name}") from e
            except AzureError as e:
                raise ArtifactStoreError(f"ダウンロードに失敗しました ({blob_name}): {e}") from e
            yield local_path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def build_artifact_store(
    kind: str,
    *,
    local_root: Path,
    account_url: str | None = None,
    container_name: str = "artifacts",
) -> ArtifactStore:
    """設定から保存先を組み立てる。

    Args:
        kind: `"local"` または `"blob"`
        local_root: ローカル保存のルート
        account_url: Blob のアカウント URL（kind が blob のとき必須）
        container_name: Blob のコンテナ名

    Returns:
        ArtifactStore: 保存先

    Raises:
        ArtifactStoreError: kind が未知、または blob なのに URL が無い場合
    """
    if kind == "local":
        return LocalArtifactStore(local_root)
    if kind == "blob":
        if not account_url:
            raise ArtifactStoreError(
                "ARTIFACT_STORE=blob には AZURE_STORAGE_ACCOUNT_URL が必要です"
            )
        return BlobArtifactStore.from_account_url(account_url, container_name)
    raise ArtifactStoreError(f"未知の保存先です: {kind!r}（local / blob のいずれか）")
