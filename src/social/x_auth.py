"""X の OAuth 2.0（Authorization Code + PKCE）。

なぜローカルで1回認証して送る運用にするか
----------------------------------------
PKCE フローはリダイレクト先を必要とし、コンテナの中では実質完了できない
（YouTube の InstalledAppFlow と同じ理由）。ローカルで認証し、
`uv run python -m scripts.push_tokens` で保存先へ送る。

YouTube と違う点: refresh token が**単回使用でローテートする**。
更新したら必ず保存先へ書き戻す必要があり、書き戻しは投稿より先に行う
（投稿が失敗しても、トークンだけは前に進んだ状態を保つ）。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.storage.tokens import X_TOKEN, TokenStore, read_json, write_json
from src.utils.logger import log_error

# 期限のこれだけ前になったら更新する。
# 投稿の直前に期限が切れると、その回の投稿を落とすことになる。
REFRESH_MARGIN_SECONDS = 120

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
SCOPES = ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access")


class XAuthError(Exception):
    """認証に失敗した。"""


class XTokenExpiredError(XAuthError):
    """トークンが失効しており、再認証が必要。

    X では理由不明の失効が実際に起きる。これを異常終了として扱わず、
    投稿を NEEDS_REVIEW にして画面に再認証ボタンを出すために型で分ける。
    """


@dataclass(frozen=True)
class XCredentials:
    """アクセストークンと更新用トークン。"""

    access_token: str
    refresh_token: str
    expires_at: datetime

    def needs_refresh(self, now: datetime) -> bool:
        """更新すべきか。"""
        return now >= self.expires_at - timedelta(seconds=REFRESH_MARGIN_SECONDS)


class TokenExchange(Protocol):
    """トークンエンドポイントとの通信。

    Protocol にする理由: テストで実際の HTTP を張らずに、
    ローテーションの書き戻しだけを検証したい。
    """

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """refresh token を使って新しいトークン一式を得る。"""
        ...


def load_credentials(store: TokenStore) -> XCredentials | None:
    """保存先からトークンを読む。

    未認証・壊れた値・保存先に到達できない場合はいずれも None を返す。
    例外を投げると、画面を開くだけで 500 になる（未認証なら
    認証ボタンを出せばよい）。

    Args:
        store: トークンの保存先

    Returns:
        XCredentials | None: 読めたトークン
    """
    data = read_json(store, X_TOKEN)
    if not data:
        return None
    try:
        return XCredentials(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )
    except (KeyError, TypeError, ValueError) as e:
        log_error(f"X のトークンを読めませんでした（未認証として扱います）: {e}")
        return None


def ensure_fresh(
    store: TokenStore,
    credentials: XCredentials,
    exchange: TokenExchange,
    now: datetime | None = None,
) -> XCredentials:
    """必要なら更新し、**保存先へ書き戻してから**返す。

    書き戻しを先に行うのが重要。X の refresh token は単回使用なので、
    更新に成功したあと書き戻す前に落ちると、手元の refresh token は
    既に無効で、保存先の値も無効。再認証しか道が無くなる。

    Args:
        store: トークンの保存先
        credentials: 現在のトークン
        exchange: トークンエンドポイント
        now: 現在時刻（省略時は UTC の現在）

    Returns:
        XCredentials: 有効なトークン

    Raises:
        XTokenExpiredError: 更新に失敗した（再認証が必要）
    """
    moment = now or datetime.now(UTC)
    if not credentials.needs_refresh(moment):
        return credentials

    # payload の組み立てまでこの try に含める。200 で返ってきても
    # access_token が欠けている・expires_in が数値でないといった
    # 壊れた応答が実際にありうる。ここで KeyError / ValueError を
    # 素通りさせると、再認証ボタンにつながる XTokenExpiredError を
    # 経由せずクラッシュする（呼び出し元は「更新が失敗した」という
    # 一種類の失敗しか想定していない）。narrow に戻さないこと。
    try:
        payload = exchange.refresh(credentials.refresh_token)
        refreshed = XCredentials(
            access_token=str(payload["access_token"]),
            # 新しい refresh token が返らない実装もありうるので、
            # 無ければ現在のものを維持する
            refresh_token=str(payload.get("refresh_token") or credentials.refresh_token),
            expires_at=moment + timedelta(seconds=int(payload.get("expires_in", 7200))),
        )
    except Exception as e:
        raise XTokenExpiredError(f"X のトークンを更新できませんでした: {e}") from e
    write_json(
        store,
        X_TOKEN,
        {
            "access_token": refreshed.access_token,
            "refresh_token": refreshed.refresh_token,
            "expires_at": refreshed.expires_at.isoformat(),
        },
    )
    return refreshed


def build_authorization_url(
    client_id: str, redirect_uri: str, verifier: str | None = None
) -> tuple[str, str, str]:
    """認可 URL を組む。

    タスクブリーフのインターフェース節は `tuple[str, str]`（URL, state）を
    挙げているが、それでは呼び出し元が code_verifier を保持できず PKCE を
    完結できない。ステップ4のサンプル実装が返す3要素
    （URL, state, code_verifier）をそのまま採用する。

    Args:
        client_id: アプリのクライアントID
        redirect_uri: 登録済みのリダイレクト先
        verifier: PKCE の code_verifier。省略時は生成する

    Returns:
        tuple[str, str, str]: (認可URL, state, code_verifier)
    """
    from urllib.parse import urlencode

    code_verifier = verifier or secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}", state, code_verifier
