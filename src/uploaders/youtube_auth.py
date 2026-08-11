"""YouTube OAuth2 authentication module."""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# YouTube Data API v3 scope for uploading videos
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubeAuthError(Exception):
    """Exception raised for YouTube authentication errors."""

    pass


class YouTubeAuth:
    """Handles YouTube OAuth2 authentication flow."""

    def __init__(
        self,
        client_secrets_file: str = "client_secrets.json",
        token_file: str = "youtube_token.json",
    ):
        """Initialize YouTube authentication.

        Args:
            client_secrets_file: Path to the OAuth2 client secrets JSON file
                downloaded from Google Cloud Console.
            token_file: Path where the authenticated token will be saved.
        """
        self.client_secrets_file = Path(client_secrets_file)
        self.token_file = Path(token_file)
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

        # Try to load from file
        if self.token_file.exists():
            self._credentials = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)

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
        if not self.client_secrets_file.exists():
            raise YouTubeAuthError(
                f"Client secrets file not found: {self.client_secrets_file}\n"
                "Please download it from Google Cloud Console:\n"
                "1. Go to https://console.cloud.google.com/\n"
                "2. Select your project\n"
                "3. Go to 'APIs & Services' > 'Credentials'\n"
                "4. Create OAuth 2.0 Client ID (Desktop app)\n"
                "5. Download JSON and save as 'client_secrets.json'"
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets_file), SCOPES)
            # This will open a browser for the user to authenticate
            # Use port 8089 to avoid conflicts with common services
            self._credentials = flow.run_local_server(
                host="127.0.0.1",
                port=8089,
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
                    "ポート8089が使用中です。他のアプリケーションを終了してから再試行してください。"
                ) from e
            raise YouTubeAuthError(f"OAuth2 authentication failed: {e}") from e
        except Exception as e:
            raise YouTubeAuthError(f"OAuth2 authentication failed: {e}") from e

    def _save_credentials(self) -> None:
        """Save credentials to the token file."""
        if self._credentials:
            with open(self.token_file, "w") as f:
                f.write(self._credentials.to_json())

    def is_authenticated(self) -> bool:
        """Check if we have valid credentials without triggering auth flow.

        Returns:
            True if valid credentials exist, False otherwise.
        """
        if self._credentials and self._credentials.valid:
            return True

        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
                if creds.valid:
                    self._credentials = creds
                    return True
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    self._credentials = creds
                    self._save_credentials()
                    return True
            except Exception:
                pass

        return False

    def revoke(self) -> bool:
        """Revoke the current credentials and delete the token file.

        Returns:
            True if revocation was successful, False otherwise.
        """
        try:
            if self.token_file.exists():
                self.token_file.unlink()
            self._credentials = None
            return True
        except Exception:
            return False


def get_youtube_credentials(
    client_secrets_file: str = "client_secrets.json",
    token_file: str = "youtube_token.json",
) -> Credentials:
    """Convenience function to get YouTube credentials.

    Args:
        client_secrets_file: Path to the OAuth2 client secrets JSON file.
        token_file: Path where the authenticated token will be saved.

    Returns:
        Valid Google OAuth2 credentials for YouTube API.
    """
    auth = YouTubeAuth(client_secrets_file, token_file)
    return auth.get_credentials()
