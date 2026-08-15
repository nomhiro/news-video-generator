"""`scripts/authorize_x.py` のテスト。

ソケットを実際に開かず、コールバックの判定ロジック（`evaluate_callback`）と
`main` の分岐（クライアント設定の欠落 / state 不一致 / access_denied /
交換成功時の書き込み）を検査する。`main` の HTTP 待ち受けは
`_wait_for_callback` をモンキーパッチして避ける。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from scripts import authorize_x
from src.social.x_auth import XAuthError, load_credentials
from src.storage.tokens import X_TOKEN, read_json


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


class FakeConfig:
    """`main` が触るフィールドだけを持つ `Config` の代わり。"""

    def __init__(
        self,
        *,
        x_client_id: str = "client-id",
        x_client_secret: str = "client-secret",
    ) -> None:
        self.x_client_id = x_client_id
        self.x_client_secret = _FakeSecret(x_client_secret)
        self.x_redirect_uri = "http://127.0.0.1:8091/callback"
        self.token_store = "local"
        self.token_paths: dict[str, Any] = {}
        self.azure_storage_account_url = None
        self.azure_token_container = "tokens"


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


# --- evaluate_callback: ソケットを介さない判定ロジックの検査 -----------


def test_evaluate_callback_state不一致は拒否する() -> None:
    result = authorize_x.evaluate_callback(
        {"state": ["wrong"], "code": ["abc"]}, expected_state="expected"
    )
    assert result.code is None
    assert result.error is not None
    assert "state" in result.error


def test_evaluate_callback_access_denied_を処理する() -> None:
    result = authorize_x.evaluate_callback(
        {"state": ["expected"], "error": ["access_denied"]}, expected_state="expected"
    )
    assert result.code is None
    assert result.error is not None
    assert "access_denied" in result.error


def test_evaluate_callback_code_が無ければ拒否する() -> None:
    result = authorize_x.evaluate_callback({"state": ["expected"]}, expected_state="expected")
    assert result.code is None
    assert result.error is not None


def test_evaluate_callback_成功時は_code_を返す() -> None:
    result = authorize_x.evaluate_callback(
        {"state": ["expected"], "code": ["auth-code"]}, expected_state="expected"
    )
    assert result.code == "auth-code"
    assert result.error is None


# --- main: クライアント設定の欠落は最初に止める --------------------------


def test_main_client_id_が無ければフローを開始せず終了する(monkeypatch: pytest.MonkeyPatch) -> None:
    config = FakeConfig(x_client_id="")
    monkeypatch.setattr(authorize_x.Config, "from_env", staticmethod(lambda: config))

    called = False

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(authorize_x, "build_authorization_url", _fail_if_called)

    assert authorize_x.main() != 0
    assert called is False


def test_main_client_secret_が無ければフローを開始せず終了する(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = FakeConfig(x_client_secret="")
    monkeypatch.setattr(authorize_x.Config, "from_env", staticmethod(lambda: config))

    called = False

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(authorize_x, "build_authorization_url", _fail_if_called)

    assert authorize_x.main() != 0
    assert called is False


# --- main: フロー全体（ブラウザとサーバーはモック化） ----------------------


def _patch_common(monkeypatch: pytest.MonkeyPatch, config: FakeConfig, store: FakeStore) -> None:
    monkeypatch.setattr(authorize_x.Config, "from_env", staticmethod(lambda: config))
    monkeypatch.setattr(authorize_x.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(authorize_x, "_build_store", lambda cfg: store)
    monkeypatch.setattr(
        authorize_x,
        "build_authorization_url",
        lambda client_id, redirect_uri: ("https://x.example/auth", "expected-state", "verifier"),
    )


def test_main_state_不一致なら保存しない(monkeypatch: pytest.MonkeyPatch) -> None:
    config = FakeConfig()
    store = FakeStore()
    _patch_common(monkeypatch, config, store)
    monkeypatch.setattr(
        authorize_x,
        "_wait_for_callback",
        lambda redirect_uri, expected_state: authorize_x.CallbackResult(
            code=None, error="state が一致しません"
        ),
    )

    assert authorize_x.main() != 0
    assert store.exists(X_TOKEN) is False


def test_main_access_denied_なら保存しない(monkeypatch: pytest.MonkeyPatch) -> None:
    config = FakeConfig()
    store = FakeStore()
    _patch_common(monkeypatch, config, store)
    monkeypatch.setattr(
        authorize_x,
        "_wait_for_callback",
        lambda redirect_uri, expected_state: authorize_x.CallbackResult(
            code=None, error="X が認可を拒否しました: access_denied"
        ),
    )

    assert authorize_x.main() != 0
    assert store.exists(X_TOKEN) is False


def test_main_トークン交換に失敗したら保存しない(monkeypatch: pytest.MonkeyPatch) -> None:
    config = FakeConfig()
    store = FakeStore()
    _patch_common(monkeypatch, config, store)
    monkeypatch.setattr(
        authorize_x,
        "_wait_for_callback",
        lambda redirect_uri, expected_state: authorize_x.CallbackResult(
            code="auth-code", error=None
        ),
    )

    def _raise(*args: object, **kwargs: object) -> dict[str, Any]:
        raise XAuthError("boom")

    monkeypatch.setattr(authorize_x, "exchange_authorization_code", _raise)

    assert authorize_x.main() != 0
    assert store.exists(X_TOKEN) is False


def test_main_成功時は_load_credentials_で読める形式で書き込む(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書き込みと読み出しの形式が一致することが最も重要な検査。"""
    config = FakeConfig()
    store = FakeStore()
    _patch_common(monkeypatch, config, store)
    monkeypatch.setattr(
        authorize_x,
        "_wait_for_callback",
        lambda redirect_uri, expected_state: authorize_x.CallbackResult(
            code="auth-code", error=None
        ),
    )
    monkeypatch.setattr(
        authorize_x,
        "exchange_authorization_code",
        lambda client_id, client_secret, code, redirect_uri, code_verifier: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        },
    )

    assert authorize_x.main() == 0

    persisted = read_json(store, X_TOKEN)
    assert persisted is not None
    assert set(persisted) == {"access_token", "refresh_token", "expires_at"}
    assert persisted["access_token"] == "new-access"
    assert persisted["refresh_token"] == "new-refresh"
    # フォーマットが `datetime.fromisoformat` で解釈できること。
    datetime.fromisoformat(persisted["expires_at"])

    # 書き込んだ形式を `load_credentials` がそのまま読めること
    # （このスクリプトと読み手が形式について合意していることの証拠）。
    credentials = load_credentials(store)
    assert credentials is not None
    assert credentials.access_token == "new-access"
    assert credentials.refresh_token == "new-refresh"
