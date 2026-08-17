"""X の OAuth トークンの扱い。

X の refresh token は**単回使用でローテートする**。更新のたびに新しい
refresh token が返り、古いものは無効になる。書き戻しに失敗すると
次回の更新ができず、ブラウザでの再認証が必要になる。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.social.x_auth import (
    XTokenExpiredError,
    ensure_fresh,
    load_credentials,
)
from src.storage.tokens import X_TOKEN, read_json, write_json


class FakeStore:
    """メモリ上の TokenStore。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def read(self, name: str) -> str | None:
        return self.data.get(name)

    def write(self, name: str, payload: str) -> None:
        self.data[name] = payload

    def delete(self, name: str) -> None:
        self.data.pop(name, None)

    def exists(self, name: str) -> bool:
        return name in self.data


class FakeExchange:
    """refresh を1回だけ成功させる交換器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.calls.append(refresh_token)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def _save(store: FakeStore, expires_at: datetime) -> None:
    write_json(
        store,
        X_TOKEN,
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": expires_at.isoformat(),
        },
    )


def test_期限内なら_refresh_しない(store: FakeStore) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now + timedelta(hours=1))
    creds = load_credentials(store)
    assert creds is not None
    exchange = FakeExchange()

    fresh = ensure_fresh(store, creds, exchange, now=now)

    assert exchange.calls == []
    assert fresh.access_token == "old-access"


def test_期限が近ければ_refresh_して_書き戻す(store: FakeStore) -> None:
    """書き戻しを忘れると、単回使用の refresh token を失う。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now + timedelta(seconds=30))
    creds = load_credentials(store)
    assert creds is not None
    exchange = FakeExchange()

    fresh = ensure_fresh(store, creds, exchange, now=now)

    assert exchange.calls == ["old-refresh"]
    assert fresh.access_token == "new-access"

    persisted = read_json(store, X_TOKEN)
    assert persisted is not None
    assert persisted["refresh_token"] == "new-refresh"


def test_書き戻しに失敗したら_再認証が必要だと伝える(store: FakeStore) -> None:
    """生の例外を投げると、原因の分からない停止になる。

    書き戻しに失敗した時点で古い refresh token は既に使い切られている
    （単回使用）ので、自動での回復は不可能。呼び出し元が捕まえるのは
    `XTokenExpiredError` だけなので、この型に変換しないと
    「再認証が必要」という唯一の復旧手段が誰にも伝わらない。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now)
    creds = load_credentials(store)
    assert creds is not None

    class UnwritableStore(FakeStore):
        def write(self, name: str, payload: str) -> None:
            raise OSError("Blob に到達できません")

    unwritable = UnwritableStore()
    unwritable.data = dict(store.data)

    with pytest.raises(XTokenExpiredError, match="再認証"):
        ensure_fresh(unwritable, creds, FakeExchange(), now=now)


def test_保存先が空なら_None(store: FakeStore) -> None:
    """未認証を例外にすると、画面を開くだけで 500 になる。"""
    assert load_credentials(store) is None


def test_壊れた値は_無いものとして扱う(store: FakeStore) -> None:
    """更新が中断して壊れた JSON が残ると、認証フローにも入れなくなる。"""
    store.write(X_TOKEN, "{not json")

    assert load_credentials(store) is None


def test_refresh_が失敗したら_XTokenExpiredError(store: FakeStore) -> None:
    """理由不明の失効が実際に多く報告されている。

    例外で落とさず、この型で受けて画面に再認証ボタンを出す。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now)
    creds = load_credentials(store)
    assert creds is not None

    class Failing:
        def refresh(self, refresh_token: str) -> dict[str, Any]:
            raise RuntimeError("invalid_grant")

    with pytest.raises(XTokenExpiredError):
        ensure_fresh(store, creds, Failing(), now=now)


def test_refresh_の応答に_access_token_が無ければ_XTokenExpiredError(
    store: FakeStore,
) -> None:
    """200 で返ってきても中身が壊れていることがある。

    ここを素通りさせると KeyError が再認証ボタンを経由せず
    そのまま外に漏れる。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now)
    creds = load_credentials(store)
    assert creds is not None

    class MissingAccessToken:
        def refresh(self, refresh_token: str) -> dict[str, Any]:
            return {"expires_in": 7200}

    with pytest.raises(XTokenExpiredError):
        ensure_fresh(store, creds, MissingAccessToken(), now=now)


def test_refresh_の応答の_expires_in_が数値でなければ_XTokenExpiredError(
    store: FakeStore,
) -> None:
    """同上。数値でない expires_in も壊れた応答の一種。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _save(store, now)
    creds = load_credentials(store)
    assert creds is not None

    class NonNumericExpiresIn:
        def refresh(self, refresh_token: str) -> dict[str, Any]:
            return {"access_token": "new-access", "expires_in": "soon"}

    with pytest.raises(XTokenExpiredError):
        ensure_fresh(store, creds, NonNumericExpiresIn(), now=now)
