"""TikTok OAuth2 authentication module."""

import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# TikTok OAuth2 endpoints
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Required scopes for video upload
SCOPES = ["video.publish", "video.upload"]


@dataclass
class TikTokCredentials:
    """TikTok OAuth2 credentials."""

    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    open_id: str
    scope: str

    @property
    def is_expired(self) -> bool:
        """Check if access token is expired (with 5 min buffer)."""
        return time.time() >= (self.expires_at - 300)

    @property
    def is_valid(self) -> bool:
        """Check if credentials are valid and not expired."""
        return bool(self.access_token) and not self.is_expired

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "open_id": self.open_id,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TikTokCredentials":
        """Create from dictionary."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
            open_id=data["open_id"],
            scope=data["scope"],
        )


class TikTokAuthError(Exception):
    """Exception raised for TikTok authentication errors."""

    pass


class TikTokAuth:
    """Handles TikTok OAuth2 authentication flow."""

    def __init__(
        self,
        client_key: str,
        client_secret: str,
        token_file: str = "tiktok_token.json",
        redirect_uri: str = "http://127.0.0.1:8090/callback",
    ):
        """Initialize TikTok authentication.

        Args:
            client_key: TikTok app client key
            client_secret: TikTok app client secret
            token_file: Path where the authenticated token will be saved
            redirect_uri: OAuth redirect URI (must match app settings)
        """
        self.client_key = client_key
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self.redirect_uri = redirect_uri
        self._credentials: TikTokCredentials | None = None

    def get_credentials(self) -> TikTokCredentials:
        """Get valid TikTok API credentials.

        This method handles the OAuth2 flow:
        1. If a valid token exists, use it
        2. If the token is expired but refreshable, refresh it
        3. If no valid token exists, initiate the OAuth2 flow

        Returns:
            Valid TikTok OAuth2 credentials.

        Raises:
            TikTokAuthError: If authentication fails.
        """
        # Check cached credentials
        if self._credentials and self._credentials.is_valid:
            return self._credentials

        # Try to load from file
        if self.token_file.exists():
            self._load_credentials()

        # Check if credentials are valid or need refresh
        if self._credentials:
            if self._credentials.is_valid:
                return self._credentials
            # Try to refresh
            if self._credentials.refresh_token:
                try:
                    self._refresh_token()
                    return self._credentials
                except TikTokAuthError:
                    pass  # Fall through to new auth flow

        # Run OAuth2 flow
        self._run_oauth_flow()
        if self._credentials is None:
            # None を返すと呼び出し側で access_token 参照時に
            # AttributeError になり原因が分かりにくいため、ここで失敗させる
            raise TikTokAuthError("OAuth2 フローが完了しましたが認証情報を取得できませんでした")
        return self._credentials

    def _load_credentials(self) -> None:
        """Load credentials from token file."""
        try:
            with open(self.token_file, encoding="utf-8") as f:
                data = json.load(f)
            self._credentials = TikTokCredentials.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            self._credentials = None

    def _save_credentials(self) -> None:
        """Save credentials to token file."""
        if self._credentials:
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(self._credentials.to_dict(), f, indent=2)

    def _refresh_token(self) -> None:
        """Refresh the access token using refresh token."""
        if not self._credentials or not self._credentials.refresh_token:
            raise TikTokAuthError("No refresh token available")

        data = {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._credentials.refresh_token,
        }

        with httpx.Client() as client:
            response = client.post(
                TIKTOK_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            result = response.json()

        if "error" in result or result.get("data", {}).get("error_code"):
            error_msg = result.get("error_description") or result.get("data", {}).get(
                "description", "Token refresh failed"
            )
            raise TikTokAuthError(f"Token refresh failed: {error_msg}")

        token_data = result.get("data", result)
        self._credentials = TikTokCredentials(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", self._credentials.refresh_token),
            expires_at=time.time() + token_data["expires_in"],
            open_id=token_data.get("open_id", self._credentials.open_id),
            scope=token_data.get("scope", self._credentials.scope),
        )
        self._save_credentials()

    def _run_oauth_flow(self) -> None:
        """Run the OAuth2 authorization flow."""
        if not self.client_key or not self.client_secret:
            raise TikTokAuthError(
                "TikTok API credentials not configured.\n"
                "Please set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in your .env file."
            )

        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Build authorization URL
        params = {
            "client_key": self.client_key,
            "redirect_uri": self.redirect_uri,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "state": state,
        }
        auth_url = f"{TIKTOK_AUTH_URL}?{urlencode(params)}"

        # Parse redirect URI to get port
        parsed = urlparse(self.redirect_uri)
        port = parsed.port or 8090
        callback_path = parsed.path or "/callback"

        # Create handler class with state
        handler = _create_callback_handler(state, callback_path)

        try:
            server = HTTPServer(("127.0.0.1", port), handler)
        except OSError as e:
            if "address already in use" in str(e).lower():
                raise TikTokAuthError(
                    f"ポート{port}が使用中です。他のアプリケーションを終了してから再試行してください。"
                ) from e
            raise TikTokAuthError(f"OAuth2 server failed to start: {e}") from e

        # Open browser for user authentication
        print("ブラウザでTikTok認証ページを開いています...")
        print(f"自動で開かない場合は以下のURLを開いてください:\n{auth_url}")
        webbrowser.open(auth_url)

        # Wait for callback (with timeout handling)
        server.handle_request()

        if handler.error:
            raise TikTokAuthError(f"OAuth flow failed: {handler.error}")

        if not handler.code:
            raise TikTokAuthError("No authorization code received")

        # Exchange code for tokens
        self._exchange_code(handler.code)

    def _exchange_code(self, code: str) -> None:
        """Exchange authorization code for access token."""
        data = {
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

        with httpx.Client() as client:
            response = client.post(
                TIKTOK_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            result = response.json()

        if "error" in result or result.get("data", {}).get("error_code"):
            error_msg = result.get("error_description") or result.get("data", {}).get(
                "description", "Token exchange failed"
            )
            raise TikTokAuthError(f"Token exchange failed: {error_msg}")

        token_data = result.get("data", result)
        self._credentials = TikTokCredentials(
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=time.time() + token_data["expires_in"],
            open_id=token_data["open_id"],
            scope=token_data["scope"],
        )
        self._save_credentials()

    def is_authenticated(self) -> bool:
        """Check if we have valid credentials without triggering auth flow."""
        if self._credentials and self._credentials.is_valid:
            return True

        if self.token_file.exists():
            try:
                self._load_credentials()
                if self._credentials and self._credentials.is_valid:
                    return True
                # Try refresh if expired
                if self._credentials and self._credentials.refresh_token:
                    self._refresh_token()
                    return self._credentials.is_valid
            except Exception:
                pass

        return False

    def revoke(self) -> bool:
        """Revoke credentials and delete token file."""
        try:
            if self.token_file.exists():
                self.token_file.unlink()
            self._credentials = None
            return True
        except Exception:
            return False


def _create_callback_handler(expected_state: str, callback_path: str):
    """Create a callback handler class with the expected state."""

    class CallbackHandler(BaseHTTPRequestHandler):
        """HTTP handler for OAuth callback."""

        code: str | None = None
        error: str | None = None

        def do_GET(self):
            """Handle OAuth callback GET request."""
            parsed = urlparse(self.path)

            # Check if this is the callback path
            if not parsed.path.rstrip("/").endswith(callback_path.rstrip("/")):
                self.send_error(404, "Not Found")
                return

            params = parse_qs(parsed.query)

            # Verify state
            state = params.get("state", [""])[0]
            if state != expected_state:
                CallbackHandler.error = "State mismatch - possible CSRF attack"
                self._send_response("認証に失敗しました。再度お試しください。", success=False)
                return

            # Check for error
            if "error" in params:
                CallbackHandler.error = params.get("error_description", ["Unknown error"])[0]
                self._send_response(f"認証エラー: {CallbackHandler.error}", success=False)
                return

            # Get authorization code
            CallbackHandler.code = params.get("code", [""])[0]
            if CallbackHandler.code:
                self._send_response(
                    "TikTok認証成功! このウィンドウを閉じてください。", success=True
                )
            else:
                CallbackHandler.error = "No authorization code received"
                self._send_response("認証コードを取得できませんでした。", success=False)

        def _send_response(self, message: str, success: bool = True):
            """Send HTML response."""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

            color = "#10B981" if success else "#EF4444"
            icon = "✅" if success else "❌"

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>TikTok認証</title>
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        min-height: 100vh;
                        margin: 0;
                        background: linear-gradient(135deg, #000 0%, #25F4EE 50%, #FE2C55 100%);
                    }}
                    .card {{
                        background: white;
                        padding: 40px 60px;
                        border-radius: 16px;
                        text-align: center;
                        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                    }}
                    .icon {{ font-size: 48px; margin-bottom: 16px; }}
                    h1 {{ color: {color}; margin: 0 0 8px 0; font-size: 24px; }}
                    p {{ color: #666; margin: 0; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="icon">{icon}</div>
                    <h1>{message}</h1>
                    <p>このウィンドウは閉じても構いません</p>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, format, *args):
            """Suppress default logging."""
            pass

    return CallbackHandler
