"""OAuth トークンの保存先。

守りたい性質
------------
- ローカルと blob で**同じ名前**を使う（行き来しても同じ値を指す）
- 壊れた値で認証そのものが不可能にならない（再認証に進める）
- 書き込みが原子的（更新中に落ちても壊れた JSON を残さない）
- 保存先が読めないときに「未認証」として扱い、UI から復帰できる
"""

import json
from pathlib import Path

import pytest

from src.storage.tokens import (
    TIKTOK_TOKEN,
    YOUTUBE_CLIENT_SECRETS,
    YOUTUBE_TOKEN,
    LocalFileTokenStore,
    TokenStore,
    TokenStoreError,
    build_token_store,
    read_json,
    write_json,
)


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        YOUTUBE_TOKEN: tmp_path / "youtube_token.json",
        YOUTUBE_CLIENT_SECRETS: tmp_path / "client_secrets.json",
        TIKTOK_TOKEN: tmp_path / "tiktok_token.json",
    }


@pytest.fixture
def store(paths: dict[str, Path]) -> LocalFileTokenStore:
    return LocalFileTokenStore(paths)


# --------------------------------------------------------------------------
# ローカル保存
# --------------------------------------------------------------------------


def test_round_trip(store: LocalFileTokenStore) -> None:
    store.write(YOUTUBE_TOKEN, '{"refresh_token": "abc"}')
    assert store.exists(YOUTUBE_TOKEN) is True
    assert store.read(YOUTUBE_TOKEN) == '{"refresh_token": "abc"}'


def test_reading_a_missing_value_returns_none(store: LocalFileTokenStore) -> None:
    """未認証は「無い」で表す（例外にしない）。

    例外にすると、認証していない状態で画面を開くだけで落ちる。
    """
    assert store.read(TIKTOK_TOKEN) is None
    assert store.exists(TIKTOK_TOKEN) is False


def test_write_creates_the_parent_directory(tmp_path: Path) -> None:
    store = LocalFileTokenStore({YOUTUBE_TOKEN: tmp_path / "nested" / "dir" / "token.json"})
    store.write(YOUTUBE_TOKEN, "{}")
    assert (tmp_path / "nested" / "dir" / "token.json").is_file()


def test_write_leaves_no_temporary_file(store: LocalFileTokenStore, paths: dict[str, Path]) -> None:
    """一時ファイルを残さないこと（原子的な書き込みの副産物）。"""
    store.write(YOUTUBE_TOKEN, "{}")
    leftovers = list(paths[YOUTUBE_TOKEN].parent.glob("*.tmp"))
    assert leftovers == []


def test_write_overwrites(store: LocalFileTokenStore) -> None:
    """トークンの更新は上書き（アクセストークンは頻繁に変わる）。"""
    store.write(TIKTOK_TOKEN, '{"access_token": "old"}')
    store.write(TIKTOK_TOKEN, '{"access_token": "new"}')
    assert store.read(TIKTOK_TOKEN) == '{"access_token": "new"}'


def test_delete_is_idempotent(store: LocalFileTokenStore) -> None:
    """revoke を2回押しても落ちないこと。"""
    store.write(TIKTOK_TOKEN, "{}")
    store.delete(TIKTOK_TOKEN)
    store.delete(TIKTOK_TOKEN)
    assert store.exists(TIKTOK_TOKEN) is False


def test_an_unconfigured_name_is_an_error(store: LocalFileTokenStore) -> None:
    """設定に無い名前は、黙って別の場所に書かないこと。"""
    with pytest.raises(TokenStoreError):
        store.read("unknown_token")


def test_local_store_satisfies_the_protocol(store: LocalFileTokenStore) -> None:
    assert isinstance(store, TokenStore)


# --------------------------------------------------------------------------
# JSON の読み書き
# --------------------------------------------------------------------------


def test_json_round_trip(store: LocalFileTokenStore) -> None:
    write_json(store, TIKTOK_TOKEN, {"access_token": "a", "open_id": "o"})
    assert read_json(store, TIKTOK_TOKEN) == {"access_token": "a", "open_id": "o"}


def test_corrupt_json_reads_as_missing(store: LocalFileTokenStore) -> None:
    """壊れた値は「無い」として扱うこと。

    トークン更新が中断されて壊れた JSON が残った場合、例外にすると
    認証フローにも入れず、画面から復帰できなくなる。
    """
    store.write(YOUTUBE_TOKEN, "{壊れている")
    assert read_json(store, YOUTUBE_TOKEN) is None


def test_non_object_json_reads_as_missing(store: LocalFileTokenStore) -> None:
    store.write(YOUTUBE_TOKEN, '["配列は想定外"]')
    assert read_json(store, YOUTUBE_TOKEN) is None


def test_written_json_is_readable_by_humans(store: LocalFileTokenStore) -> None:
    """ローカルのファイルは人が読める形にしておく（調査のため）。"""
    write_json(store, TIKTOK_TOKEN, {"scope": "video.publish"})
    raw = store.read(TIKTOK_TOKEN)
    assert raw is not None
    assert "\n" in raw
    assert json.loads(raw)["scope"] == "video.publish"


# --------------------------------------------------------------------------
# 設定からの組み立て
# --------------------------------------------------------------------------


def test_build_local_store(paths: dict[str, Path]) -> None:
    built = build_token_store("local", local_paths=paths)
    assert isinstance(built, LocalFileTokenStore)


def test_build_blob_store_without_a_url_raises(paths: dict[str, Path]) -> None:
    with pytest.raises(TokenStoreError, match="AZURE_STORAGE_ACCOUNT_URL"):
        build_token_store("blob", local_paths=paths)


def test_build_unknown_store_raises(paths: dict[str, Path]) -> None:
    with pytest.raises(TokenStoreError, match="local"):
        build_token_store("keyvault", local_paths=paths)


# --------------------------------------------------------------------------
# 認証アダプタとの結合
# --------------------------------------------------------------------------


class MemoryTokenStore:
    """メモリ上のトークンストア（blob の代役）。"""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.unavailable = False

    def _guard(self) -> None:
        if self.unavailable:
            raise TokenStoreError("保存先に到達できません")

    def read(self, name: str) -> str | None:
        self._guard()
        return self.values.get(name)

    def write(self, name: str, payload: str) -> None:
        self._guard()
        self.values[name] = payload

    def delete(self, name: str) -> None:
        self._guard()
        self.values.pop(name, None)

    def exists(self, name: str) -> bool:
        self._guard()
        return name in self.values


def test_youtube_reads_the_token_from_the_store() -> None:
    """ローカルにファイルが無くても認証済みと判定できること。

    以前は `token_file` のパスに実体が必要だった。コンテナでは
    そのファイルが存在しないため、必ず未認証になっていた。
    """
    from src.uploaders.youtube_auth import YouTubeAuth

    # refresh_token だけの資格情報は「期限切れだが更新可能」になる。
    # 実際のリフレッシュ（ネットワーク）は行わせないので、
    # ここでは読み出しが成立することだけを見る。
    store = MemoryTokenStore(
        {
            YOUTUBE_TOKEN: json.dumps(
                {
                    "token": "access",
                    "refresh_token": "refresh",
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
                }
            )
        }
    )
    auth = YouTubeAuth(store)
    credentials = auth._load_credentials()
    assert credentials is not None
    assert credentials.refresh_token == "refresh"


def test_youtube_treats_an_unreachable_store_as_unauthenticated() -> None:
    """保存先に到達できないときは未認証として扱うこと。

    例外を投げると、画面を開くだけで 500 になる。
    未認証として出せば、利用者は認証ボタンを押せる。
    """
    from src.uploaders.youtube_auth import YouTubeAuth

    store = MemoryTokenStore()
    store.unavailable = True
    assert YouTubeAuth(store).is_authenticated() is False


def test_youtube_revoke_deletes_from_the_store() -> None:
    from src.uploaders.youtube_auth import YouTubeAuth

    store = MemoryTokenStore({YOUTUBE_TOKEN: "{}"})
    assert YouTubeAuth(store).revoke() is True
    assert store.exists(YOUTUBE_TOKEN) is False


def test_youtube_reports_a_missing_client_secrets() -> None:
    """client_secrets が無いときに、何をすればよいか分かる失敗にすること。"""
    from src.uploaders.youtube_auth import YouTubeAuth, YouTubeAuthError

    with pytest.raises(YouTubeAuthError, match="client_secrets"):
        YouTubeAuth(MemoryTokenStore()).get_credentials()


def test_tiktok_reads_and_writes_through_the_store() -> None:
    from src.uploaders.tiktok_auth import TikTokAuth

    store = MemoryTokenStore()
    auth = TikTokAuth("key", "secret", store)

    write_json(
        store,
        TIKTOK_TOKEN,
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 9_999_999_999.0,
            "open_id": "o",
            "scope": "video.publish",
        },
    )

    assert auth.is_authenticated() is True
    assert auth.revoke() is True
    assert store.exists(TIKTOK_TOKEN) is False


def test_tiktok_survives_a_corrupt_token() -> None:
    """壊れたトークンで認証状態の確認が落ちないこと。"""
    from src.uploaders.tiktok_auth import TikTokAuth

    store = MemoryTokenStore({TIKTOK_TOKEN: "{壊れている"})
    assert TikTokAuth("key", "secret", store).is_authenticated() is False


def test_tiktok_survives_a_token_missing_fields() -> None:
    """項目が足りないトークン（古い形式）でも落ちないこと。"""
    from src.uploaders.tiktok_auth import TikTokAuth

    store = MemoryTokenStore({TIKTOK_TOKEN: '{"access_token": "a"}'})
    assert TikTokAuth("key", "secret", store).is_authenticated() is False
