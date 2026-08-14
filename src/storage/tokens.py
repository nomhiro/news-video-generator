"""OAuth トークンの保存先。

なぜ抽象化するか
----------------
YouTube と TikTok のトークンはローカルの JSON ファイルだった
（`youtube_token.json` / `tiktok_token.json`）。コンテナで動かすと
2つの問題が出る。

1. ファイルシステムが再起動で消えるので、**毎回ブラウザでの再認証が
   必要になる**。YouTube の OAuth は `InstalledAppFlow` で
   localhost にリダイレクトする方式なので、コンテナの中では実質的に
   完了できない
2. レプリカ間で共有されない。片方のレプリカで認証しても、もう片方は
   未認証のまま

`client_secrets.json` も同じ性質を持つ（イメージに焼き込みたくない
静的なシークレット）。同じ仕組みで扱う。

なぜ Key Vault ではなく Blob か
-------------------------------
Key Vault は本来この用途に向いている（監査ログ、シークレット単位の
アクセス制御）。それでも Blob にしたのは、生成物の保存先として
**すでに Entra ID 専用のストレージアカウントがある**ため。

- `allowSharedKeyAccess: false` なのでキーで読む経路が無い
- コンテナは `publicAccess: None`
- 7日間のソフトデリートが効くので、消してしまっても戻せる
- 認証経路（`DefaultAzureCredential`）とコードを生成物と共有できる

トークンは1組しかなく、頻繁に書き換わる（アクセストークンの更新ごと）。
利用者ごとに何十個も持つ、監査が要件になる、といった段階に来たら
Key Vault へ移す。そのときもこのモジュールの実装を1つ足すだけで済む。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.utils.logger import log_error, log_step

if TYPE_CHECKING:  # pragma: no cover - 型のためだけの import
    from azure.storage.blob import ContainerClient

# 保存する項目の名前。ファイル名やBlob名の元になる。
YOUTUBE_TOKEN = "youtube_token"
YOUTUBE_CLIENT_SECRETS = "youtube_client_secrets"
TIKTOK_TOKEN = "tiktok_token"


class TokenStoreError(Exception):
    """トークンの読み書きに失敗した。"""


@runtime_checkable
class TokenStore(Protocol):
    """OAuth トークンと、それに準ずるシークレットの保存先。

    値は JSON 文字列として扱う。中身の解釈は各アダプタ
    （`youtube_auth` / `tiktok_auth`）に任せる。
    """

    def read(self, name: str) -> str | None:
        """保存された値を返す。無ければ None。"""
        ...

    def write(self, name: str, payload: str) -> None:
        """値を保存する（上書き）。"""
        ...

    def delete(self, name: str) -> None:
        """値を削除する。無ければ何もしない。"""
        ...

    def exists(self, name: str) -> bool:
        """値があるか。"""
        ...


class LocalFileTokenStore:
    """ローカルのファイルに保存する（開発時の既定）。

    既存の `youtube_token.json` / `tiktok_token.json` /
    `client_secrets.json` をそのまま使えるように、名前からパスへの
    対応を明示的に受け取る。名前からパスを機械的に導くと、
    既存のファイル名（`client_secrets.json` は接頭辞が違う）と合わない。
    """

    def __init__(self, paths: dict[str, Path]):
        """初期化する。

        Args:
            paths: 名前 -> ファイルパス
        """
        self._paths = paths

    def _path_for(self, name: str) -> Path:
        try:
            return self._paths[name]
        except KeyError as e:
            raise TokenStoreError(f"保存先が設定されていません: {name}") from e

    def read(self, name: str) -> str | None:
        """ファイルを読む。無ければ None。"""
        path = self._path_for(name)
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise TokenStoreError(f"読み込みに失敗しました（{name}）: {e}") from e

    def write(self, name: str, payload: str) -> None:
        """ファイルに書く。

        一時ファイル + `replace` で原子的に書く。トークンの更新中に
        落ちると、壊れた JSON が残って次回の起動時に再認証になる。
        """
        path = self._path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            temp.replace(path)
        except OSError as e:
            temp.unlink(missing_ok=True)
            raise TokenStoreError(f"保存に失敗しました（{name}）: {e}") from e

    def delete(self, name: str) -> None:
        """ファイルを削除する。"""
        self._path_for(name).unlink(missing_ok=True)

    def exists(self, name: str) -> bool:
        """ファイルがあるか。"""
        return self._path_for(name).is_file()


class BlobTokenStore:
    """Azure Blob Storage に保存する。

    認証は Entra ID（`DefaultAzureCredential`）。アカウントキーは
    使わない（ストレージアカウント側で共有キー認証を無効にしている）。
    """

    def __init__(self, container_client: ContainerClient):
        """初期化する。

        Args:
            container_client: トークン用コンテナのクライアント
        """
        self._container = container_client

    @classmethod
    def from_account_url(cls, account_url: str, container_name: str) -> BlobTokenStore:
        """アカウント URL とコンテナ名から組み立てる。

        Args:
            account_url: `https://<account>.blob.core.windows.net`
            container_name: コンテナ名

        Returns:
            BlobTokenStore: 組み立てたストア

        Raises:
            TokenStoreError: 接続に失敗した場合
        """
        from azure.core.exceptions import AzureError
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        try:
            client = ContainerClient(
                account_url=account_url,
                container_name=container_name,
                credential=DefaultAzureCredential(),
            )
        except AzureError as e:
            raise TokenStoreError(f"トークンの保存先に接続できません: {e}") from e
        return cls(client)

    @staticmethod
    def _blob_name(name: str) -> str:
        return f"{name}.json"

    def read(self, name: str) -> str | None:
        """Blob を読む。無ければ None。"""
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            return str(
                self._container.download_blob(self._blob_name(name), encoding="utf-8").readall()
            )
        except ResourceNotFoundError:
            return None
        except AzureError as e:
            raise TokenStoreError(f"読み込みに失敗しました（{name}）: {e}") from e

    def write(self, name: str, payload: str) -> None:
        """Blob に書く（上書き）。"""
        from azure.core.exceptions import AzureError

        try:
            self._container.upload_blob(
                name=self._blob_name(name),
                data=payload.encode("utf-8"),
                overwrite=True,
            )
        except AzureError as e:
            raise TokenStoreError(f"保存に失敗しました（{name}）: {e}") from e
        log_step(f"トークンを保存しました: {name}", "🔐")

    def delete(self, name: str) -> None:
        """Blob を削除する。

        コンテナにはソフトデリート（7日）が効いているので、
        誤って消しても戻せる。
        """
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        try:
            self._container.delete_blob(self._blob_name(name))
        except ResourceNotFoundError:
            return
        except AzureError as e:
            raise TokenStoreError(f"削除に失敗しました（{name}）: {e}") from e

    def exists(self, name: str) -> bool:
        """Blob があるか。"""
        from azure.core.exceptions import AzureError

        try:
            return bool(self._container.get_blob_client(self._blob_name(name)).exists())
        except AzureError as e:
            raise TokenStoreError(f"存在確認に失敗しました（{name}）: {e}") from e


def read_json(store: TokenStore, name: str) -> dict[str, Any] | None:
    """JSON として読み出す。

    壊れた値は「無い」として扱う。中断されたトークン更新などで
    壊れた JSON が残っていた場合、例外にすると認証そのものが
    できなくなる（再認証すれば直る状況なので、None を返して
    フローに進ませる方がよい）。

    Args:
        store: 保存先
        name: 名前

    Returns:
        dict | None: 読み出した値
    """
    payload = store.read(name)
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        log_error(f"保存された {name} が壊れています。再認証が必要です")
        return None
    return parsed if isinstance(parsed, dict) else None


def write_json(store: TokenStore, name: str, value: dict[str, Any]) -> None:
    """JSON として書き込む。

    Args:
        store: 保存先
        name: 名前
        value: 保存する値
    """
    store.write(name, json.dumps(value, ensure_ascii=False, indent=2))


def build_token_store(
    kind: str,
    *,
    local_paths: dict[str, Path],
    account_url: str | None = None,
    container_name: str = "tokens",
) -> TokenStore:
    """設定から保存先を組み立てる。

    Args:
        kind: `"local"` または `"blob"`
        local_paths: ローカル保存時の 名前 -> パス
        account_url: Blob のアカウント URL（kind が blob のとき必須）
        container_name: Blob のコンテナ名

    Returns:
        TokenStore: 保存先

    Raises:
        TokenStoreError: kind が未知、または blob なのに URL が無い場合
    """
    if kind == "local":
        return LocalFileTokenStore(local_paths)
    if kind == "blob":
        if not account_url:
            raise TokenStoreError("TOKEN_STORE=blob には AZURE_STORAGE_ACCOUNT_URL が必要です")
        return BlobTokenStore.from_account_url(account_url, container_name)
    raise TokenStoreError(f"未知の保存先です: {kind!r}（local / blob のいずれか）")
