"""YouTube video uploader module."""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .youtube_auth import YouTubeAuth, YouTubeAuthError

# Retry settings for resumable uploads
MAX_RETRIES = 10
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
RETRIABLE_EXCEPTIONS = (IOError,)

# YouTube category IDs
# See: https://developers.google.com/youtube/v3/docs/videoCategories/list
CATEGORY_NEWS_POLITICS = "25"
CATEGORY_ENTERTAINMENT = "24"
CATEGORY_PEOPLE_BLOGS = "22"
CATEGORY_EDUCATION = "27"
CATEGORY_SCIENCE_TECH = "28"


@dataclass
class UploadResult:
    """Result of a video upload operation."""

    success: bool
    video_id: str | None = None
    video_url: str | None = None
    error_message: str | None = None


class YouTubeUploadError(Exception):
    """Exception raised for YouTube upload errors."""

    pass


class YouTubeUploader:
    """Handles uploading videos to YouTube."""

    def __init__(
        self,
        client_secrets_file: str = "client_secrets.json",
        token_file: str = "youtube_token.json",
    ):
        """Initialize YouTube uploader.

        Args:
            client_secrets_file: Path to the OAuth2 client secrets JSON file.
            token_file: Path where the authenticated token will be saved.
        """
        self.auth = YouTubeAuth(client_secrets_file, token_file)
        self._youtube = None

    def _get_youtube_service(self):
        """Get authenticated YouTube API service."""
        if self._youtube is None:
            credentials = self.auth.get_credentials()
            self._youtube = build("youtube", "v3", credentials=credentials)
        return self._youtube

    def upload(
        self,
        video_path: str,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        category_id: str = CATEGORY_NEWS_POLITICS,
        privacy_status: str = "public",
        made_for_kids: bool = False,
        notify_subscribers: bool = True,
        progress_callback: Callable[[float], None] | None = None,
    ) -> UploadResult:
        """Upload a video to YouTube.

        Args:
            video_path: Path to the video file (MP4).
            title: Video title (max 100 characters).
            description: Video description (max 5000 characters).
            tags: List of tags for the video.
            category_id: YouTube category ID.
            privacy_status: One of "public", "private", or "unlisted".
            made_for_kids: Whether the video is made for kids.
            notify_subscribers: Whether to notify channel subscribers.
            progress_callback: Optional callback function for upload progress (0.0-1.0).

        Returns:
            UploadResult with success status and video details.
        """
        video_file = Path(video_path)
        if not video_file.exists():
            return UploadResult(success=False, error_message=f"Video file not found: {video_path}")

        # Ensure tags include #Shorts for vertical videos
        if tags is None:
            tags = []
        if "Shorts" not in tags:
            tags = ["Shorts", *tags]

        # Truncate title and description if needed
        title = title[:100]
        description = description[:5000]

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }

        # Use resumable upload for reliability
        media = MediaFileUpload(
            str(video_file),
            mimetype="video/mp4",
            resumable=True,
            chunksize=1024 * 1024,  # 1MB chunks
        )

        try:
            youtube = self._get_youtube_service()
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
                notifySubscribers=notify_subscribers,
            )

            response = self._resumable_upload(request, progress_callback)

            if response:
                video_id = response["id"]
                return UploadResult(
                    success=True,
                    video_id=video_id,
                    video_url=f"https://www.youtube.com/watch?v={video_id}",
                )
            else:
                return UploadResult(
                    success=False, error_message="Upload failed: no response received"
                )

        except YouTubeAuthError as e:
            return UploadResult(success=False, error_message=str(e))
        except HttpError as e:
            error_content = e.content.decode("utf-8") if e.content else str(e)
            return UploadResult(success=False, error_message=f"YouTube API error: {error_content}")
        except Exception as e:
            return UploadResult(success=False, error_message=f"Upload failed: {e}")

    def _resumable_upload(
        self,
        request,
        progress_callback: Callable[[float], None] | None = None,
    ):
        """Execute a resumable upload with retry logic.

        Args:
            request: The YouTube API insert request.
            progress_callback: Optional callback for progress updates.

        Returns:
            The API response on success, None on failure.
        """
        response = None
        error = None
        retry = 0

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    progress = status.progress()
                    if progress_callback:
                        progress_callback(progress)
                if response:
                    if progress_callback:
                        progress_callback(1.0)
                    return response

            except HttpError as e:
                if e.resp.status in RETRIABLE_STATUS_CODES:
                    error = f"Retriable HTTP error {e.resp.status}: {e.content}"
                else:
                    raise
            except RETRIABLE_EXCEPTIONS as e:
                error = f"Retriable error: {e}"

            if error:
                retry += 1
                if retry > MAX_RETRIES:
                    raise YouTubeUploadError(f"Max retries exceeded: {error}")

                # Exponential backoff with jitter
                sleep_seconds = random.random() * (2**retry)
                time.sleep(sleep_seconds)

        return response

    def is_authenticated(self) -> bool:
        """Check if YouTube authentication is valid.

        Returns:
            True if authenticated, False otherwise.
        """
        return self.auth.is_authenticated()

    def authenticate(self) -> None:
        """Trigger the authentication flow.

        Raises:
            YouTubeAuthError: If authentication fails.
        """
        self.auth.get_credentials()

    def authenticate_safe(self) -> bool:
        """Trigger the authentication flow without raising exceptions.

        Returns:
            True if authentication was successful, False otherwise.
        """
        try:
            self.auth.get_credentials()
            return True
        except YouTubeAuthError:
            return False
