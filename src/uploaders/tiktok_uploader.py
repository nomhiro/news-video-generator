"""TikTok video uploader module."""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

import httpx

from .tiktok_auth import TikTokAuth, TikTokAuthError


# TikTok Content Posting API endpoints
TIKTOK_UPLOAD_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_UPLOAD_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

# Retry settings
MAX_RETRIES = 5
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks (TikTok recommends 5-10MB)

# Privacy levels
PrivacyLevel = Literal[
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
]


@dataclass
class TikTokUploadResult:
    """Result of a TikTok video upload operation."""

    success: bool
    publish_id: Optional[str] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None


class TikTokUploadError(Exception):
    """Exception raised for TikTok upload errors."""

    pass


class TikTokUploader:
    """Handles uploading videos to TikTok."""

    def __init__(
        self,
        client_key: str,
        client_secret: str,
        token_file: str = "tiktok_token.json",
        redirect_uri: str = "http://127.0.0.1:8090/callback",
    ):
        """Initialize TikTok uploader.

        Args:
            client_key: TikTok app client key
            client_secret: TikTok app client secret
            token_file: Path where the authenticated token will be saved
            redirect_uri: OAuth redirect URI
        """
        self.auth = TikTokAuth(
            client_key=client_key,
            client_secret=client_secret,
            token_file=token_file,
            redirect_uri=redirect_uri,
        )

    def _get_auth_headers(self) -> dict:
        """Get authorization headers with current access token."""
        credentials = self.auth.get_credentials()
        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def upload(
        self,
        video_path: str,
        title: str,
        privacy_level: PrivacyLevel = "SELF_ONLY",
        disable_duet: bool = False,
        disable_stitch: bool = False,
        disable_comment: bool = False,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> TikTokUploadResult:
        """Upload a video to TikTok.

        Args:
            video_path: Path to the video file (MP4, max 4GB).
            title: Video title/caption (max 2200 UTF-16 characters).
            privacy_level: Video privacy setting.
            disable_duet: Disable duet feature.
            disable_stitch: Disable stitch feature.
            disable_comment: Disable comments.
            progress_callback: Optional callback for upload progress (0.0-1.0).

        Returns:
            TikTokUploadResult with success status and details.
        """
        video_file = Path(video_path)
        if not video_file.exists():
            return TikTokUploadResult(
                success=False,
                error_message=f"Video file not found: {video_path}",
            )

        # Validate file size (max 4GB)
        file_size = video_file.stat().st_size
        if file_size > 4 * 1024 * 1024 * 1024:  # 4GB
            return TikTokUploadResult(
                success=False,
                error_message="Video file exceeds 4GB limit",
            )

        # Truncate title if needed (2200 UTF-16 characters)
        title = title[:2200]

        try:
            # Step 1: Initialize upload
            upload_info = self._init_upload(
                file_size=file_size,
                title=title,
                privacy_level=privacy_level,
                disable_duet=disable_duet,
                disable_stitch=disable_stitch,
                disable_comment=disable_comment,
            )

            if progress_callback:
                progress_callback(0.1)  # 10% for initialization

            # Step 2: Upload video chunks
            self._upload_video_chunks(
                video_path=str(video_file),
                upload_url=upload_info["upload_url"],
                file_size=file_size,
                progress_callback=progress_callback,
            )

            # Step 3: Wait for processing and get result
            result = self._wait_for_processing(upload_info["publish_id"])

            if progress_callback:
                progress_callback(1.0)

            return result

        except TikTokAuthError as e:
            return TikTokUploadResult(
                success=False,
                error_message=str(e),
                error_code="auth_error",
            )
        except TikTokUploadError as e:
            return TikTokUploadResult(
                success=False,
                error_message=str(e),
                error_code="upload_error",
            )
        except Exception as e:
            return TikTokUploadResult(
                success=False,
                error_message=f"Upload failed: {e}",
                error_code="unknown_error",
            )

    def _init_upload(
        self,
        file_size: int,
        title: str,
        privacy_level: PrivacyLevel,
        disable_duet: bool,
        disable_stitch: bool,
        disable_comment: bool,
    ) -> dict:
        """Initialize the upload and get upload URL.

        Returns:
            Dict with 'upload_url' and 'publish_id'.
        """
        # Determine chunk count
        chunk_count = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

        payload = {
            "post_info": {
                "title": title,
                "privacy_level": privacy_level,
                "disable_duet": disable_duet,
                "disable_stitch": disable_stitch,
                "disable_comment": disable_comment,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": CHUNK_SIZE,
                "total_chunk_count": chunk_count,
            },
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                TIKTOK_UPLOAD_INIT_URL,
                json=payload,
                headers=self._get_auth_headers(),
            )
            result = response.json()

        # Check for errors
        error = result.get("error", {})
        if error.get("code") and error.get("code") != "ok":
            raise TikTokUploadError(
                f"Upload initialization failed: {error.get('message', 'Unknown error')}"
            )

        data = result.get("data", {})
        if not data.get("upload_url") or not data.get("publish_id"):
            raise TikTokUploadError(
                f"Upload initialization failed: Missing upload_url or publish_id"
            )

        return {
            "upload_url": data["upload_url"],
            "publish_id": data["publish_id"],
        }

    def _upload_video_chunks(
        self,
        video_path: str,
        upload_url: str,
        file_size: int,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Upload video in chunks to TikTok.

        Args:
            video_path: Path to video file.
            upload_url: URL from upload initialization.
            file_size: Total file size in bytes.
            progress_callback: Optional progress callback (0.0-1.0).
        """
        uploaded_bytes = 0

        with open(video_path, "rb") as f:
            while uploaded_bytes < file_size:
                # Read chunk
                chunk_data = f.read(CHUNK_SIZE)
                chunk_size = len(chunk_data)

                # Calculate byte range
                start_byte = uploaded_bytes
                end_byte = uploaded_bytes + chunk_size - 1

                # Upload chunk with retry
                self._upload_chunk_with_retry(
                    upload_url=upload_url,
                    chunk_data=chunk_data,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    total_size=file_size,
                )

                uploaded_bytes += chunk_size

                # Update progress (10% to 90% range for upload phase)
                if progress_callback:
                    upload_progress = uploaded_bytes / file_size
                    overall_progress = 0.1 + (upload_progress * 0.8)  # 10-90%
                    progress_callback(overall_progress)

    def _upload_chunk_with_retry(
        self,
        upload_url: str,
        chunk_data: bytes,
        start_byte: int,
        end_byte: int,
        total_size: int,
    ) -> None:
        """Upload a single chunk with retry logic."""
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(chunk_data)),
            "Content-Range": f"bytes {start_byte}-{end_byte}/{total_size}",
        }

        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=120.0) as client:
                    response = client.put(
                        upload_url,
                        content=chunk_data,
                        headers=headers,
                    )

                    if response.status_code in (200, 201, 206):
                        return  # Success

                    if response.status_code >= 500:
                        # Server error, retry
                        time.sleep(2**attempt)  # Exponential backoff
                        continue

                    # Client error, don't retry
                    raise TikTokUploadError(
                        f"Chunk upload failed: HTTP {response.status_code}"
                    )

            except httpx.TimeoutException:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise TikTokUploadError("Chunk upload timed out")

            except httpx.RequestError as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise TikTokUploadError(f"Chunk upload failed: {e}")

        raise TikTokUploadError("Max retries exceeded for chunk upload")

    def _wait_for_processing(
        self,
        publish_id: str,
        max_wait_seconds: int = 300,  # 5 minutes
        poll_interval: int = 5,
    ) -> TikTokUploadResult:
        """Wait for video processing to complete.

        Args:
            publish_id: The publish ID from upload initialization.
            max_wait_seconds: Maximum time to wait for processing.
            poll_interval: Seconds between status checks.

        Returns:
            TikTokUploadResult with final status.
        """
        start_time = time.time()

        while time.time() - start_time < max_wait_seconds:
            payload = {"publish_id": publish_id}

            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    TIKTOK_UPLOAD_STATUS_URL,
                    json=payload,
                    headers=self._get_auth_headers(),
                )
                result = response.json()

            error = result.get("error", {})
            if error.get("code") and error.get("code") != "ok":
                return TikTokUploadResult(
                    success=False,
                    publish_id=publish_id,
                    error_message=error.get("message", "Status check failed"),
                    error_code=str(error.get("code")),
                )

            data = result.get("data", {})
            status = data.get("status")

            if status == "PUBLISH_COMPLETE":
                # TikTokは直接動画URLを返さないため、ユーザーのプロフィールへ誘導
                return TikTokUploadResult(
                    success=True,
                    publish_id=publish_id,
                    video_url="https://www.tiktok.com",  # TikTokアプリ/サイトで確認
                )

            elif status == "FAILED":
                fail_reason = data.get("fail_reason", "Unknown failure")
                return TikTokUploadResult(
                    success=False,
                    publish_id=publish_id,
                    error_message=f"Video processing failed: {fail_reason}",
                    error_code="processing_failed",
                )

            # Still processing, wait and retry
            time.sleep(poll_interval)

        return TikTokUploadResult(
            success=False,
            publish_id=publish_id,
            error_message="Video processing timed out",
            error_code="timeout",
        )

    def is_authenticated(self) -> bool:
        """Check if TikTok authentication is valid."""
        return self.auth.is_authenticated()

    def authenticate(self) -> None:
        """Trigger the authentication flow.

        Raises:
            TikTokAuthError: If authentication fails.
        """
        self.auth.get_credentials()

    def authenticate_safe(self) -> bool:
        """Trigger authentication without raising exceptions.

        Returns:
            True if successful, False otherwise.
        """
        try:
            self.auth.get_credentials()
            return True
        except TikTokAuthError:
            return False
