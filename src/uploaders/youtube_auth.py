"""YouTube OAuth2 authentication module.

トークンと client_secrets の置き場所は `TokenStore` で差し替える。
以前はどちらもローカルのファイルパス固定だった。コンテナで動かすと
再起動でトークンが消えて毎回ブラウザ認証が必要になり、
`InstalledAppFlow`（localhost にリダイレクトする方式）はコンテナ内で
実質的に完了できない。保存先を外に出せば、認証はローカルで1回行い、
コンテナはそれを読むだけで済む。
"""

import json

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from src.storage.tokens import (
    YOUTUBE_CLIENT_SECRETS,
    YOUTUBE_TOKEN,
    TokenStore,
    TokenStoreError,
    read_json,
)

# YouTube Data API v3 scope for uploading videos
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# OAuth フローが待ち受けるポート。
# 8080 のような一般的なポートは他のアプリと衝突しやすい。
OAUTH_CALLBACK_PORT = 8089


class YouTubeAuthError(Exception):
    """Exception raised for YouTube authentication errors."""

    pass


class YouTubeAuth:
    """Handles YouTube OAuth2 authentication flow."""

    def __init__(self, token_store: TokenStore):
        """Initialize YouTube authentication.

        Args:
            token_store: トークンと client_secrets の保存先。
                ローカル実行では従来どおりファイル、コンテナでは
                Blob Storage を指す。
        """
        self._tokens = token_store
        self._credentials: Credentials | None = None

    def get_credentials(self) -> Credentials:
        """Get valid YouTube API credentials.

        This method handles the OAuth2 flow:
        1. If a valid token exists, use it
        2. If the token is expired but refreshable, refresh it
        3. If no valid token exists, initiate the OAuth2 flow

        Returns:
            Valid Google OAuth2 credentials for YouTube API.

        Raises:
            YouTubeAuthError: If authentication fails.
        """
        # Check for existing credentials
        if self._credentials and self._credentials.valid:
            return self._credentials

        self._credentials = self._load_credentials()

        # Check if credentials are valid or can be refreshed
        if self._credentials:
            if self._credentials.valid:
                return self._credentials
            if self._credentials.expired and self._credentials.refresh_token:
                try:
                    self._credentials.refresh(Request())
                    self._save_credentials()
                    return self._credentials
                except Exception:
                    # Token refresh failed, need to re-authenticate
                    pass

        # Need to run the OAuth2 flow
        client_config = read_json(self._tokens, YOUTUBE_CLIENT_SECRETS)
        if client_config is None:
            raise YouTubeAuthError(
                "client_secrets が見つかりません。\n"
                "Google Cloud Console から取得して保存してください:\n"
                "1. https://console.cloud.google.com/ を開く\n"
                "2. プロジェクトを選ぶ\n"
                "3. 'APIs & Services' > 'Credentials'\n"
                "4. OAuth 2.0 クライアント ID（デスクトップアプリ）を作る\n"
                "5. JSON をダウンロードして 'client_secrets.json' として置く"
            )

        try:
            # ファイルではなく dict から組み立てる。
            # 保存先が Blob の場合、ローカルにファイルが存在しない。
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            # This will open a browser for the user to authenticate
            self._credentials = flow.run_local_server(
                host="127.0.0.1",
                port=OAUTH_CALLBACK_PORT,
                prompt="consent",
                success_message="認証成功! このウィンドウを閉じてください。",
                open_browser=True,
                authorization_prompt_message="ブラウザで認証してください。自動で開かない場合は以下のURLを開いてください:",
            )
            self._save_credentials()
            return self._credentials
        except OSError as e:
            if "address already in use" in str(e).lower():
                raise YouTubeAuthError(
                    f"ポート{OAUTH_CALLBACK_PORT}が使用中です。"
                    "他のアプリケーションを終了してから再試行してください。"
                ) from e
            raise YouTubeAuthError(f"OAuth2 authentication failed: {e}") from e
        except Exception as e:
            raise YouTubeAuthError(f"OAuth2 authentication failed: {e}") from e

    def _load_credentials(self) -> Credentials | None:
        """保存先から資格情報を読む。

        Returns:
            Credentials | None: 読めなければ None（再認証に進む）
        """
        info = read_json(self._tokens, YOUTUBE_TOKEN)
        if info is None:
            return None
        try:
            # from_authorized_user_file ではなく dict から作る。
            # 保存先が Blob のときローカルにファイルが無い。
            credentials: Credentials = Credentials.from_authorized_user_info(info, SCOPES)
            return credentials
        except (ValueError, KeyError):
            # 形式が違う（スコープ変更前の古いトークン等）。再認証すれば直る。
            return None

    def _save_credentials(self) -> None:
        """Save credentials to the token store."""
        if self._credentials:
            # to_json() は refresh_token を含む JSON 文字列を返す。
            # そのまま保存すれば from_authorized_user_info で復元できる。
            self._tokens.write(YOUTUBE_TOKEN, self._credentials.to_json())

    def is_authenticated(self) -> bool:
        """Check if we have valid credentials without triggering auth flow.

        Returns:
            True if valid credentials exist, False otherwise.
        """
        if self._credentials and self._credentials.valid:
            return True

        try:
            creds = self._load_credentials()
        except TokenStoreError:
            # 保存先に到達できない。未認証として扱い、UI に認証ボタンを出す
            return False
        if creds is None:
            return False

        try:
            if creds.valid:
                self._credentials = creds
                return True
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._credentials = creds
                self._save_credentials()
                return True
        except Exception:
            return False

        return False

    def revoke(self) -> bool:
        """Revoke the current credentials and delete the stored token.

        Returns:
            True if revocation was successful, False otherwise.
        """
        try:
            self._tokens.delete(YOUTUBE_TOKEN)
            self._credentials = None
            return True
        except Exception:
            return False


def load_client_secrets_from_file(path: str) -> dict[str, object]:
    """ローカルの client_secrets.json を読む。

    保存先を Blob に切り替えるときの移行用
    （`scripts/` から呼ぶ想定の小さなヘルパ）。

    Args:
        path: ファイルパス

    Returns:
        dict: JSON の内容

    Raises:
        YouTubeAuthError: 読めない場合
    """
    from pathlib import Path

    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise YouTubeAuthError(f"client_secrets を読めません（{path}）: {e}") from e
    if not isinstance(parsed, dict):
        raise YouTubeAuthError(f"client_secrets の形式が不正です: {path}")
    return parsed
