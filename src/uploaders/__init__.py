"""YouTube and TikTok social media uploaders."""

from .youtube_auth import YouTubeAuth, get_youtube_credentials
from .youtube_uploader import YouTubeUploader, UploadResult
from .tiktok_auth import TikTokAuth, TikTokCredentials, TikTokAuthError
from .tiktok_uploader import TikTokUploader, TikTokUploadResult, TikTokUploadError

__all__ = [
    # YouTube
    "YouTubeAuth",
    "get_youtube_credentials",
    "YouTubeUploader",
    "UploadResult",
    # TikTok
    "TikTokAuth",
    "TikTokCredentials",
    "TikTokAuthError",
    "TikTokUploader",
    "TikTokUploadResult",
    "TikTokUploadError",
]
