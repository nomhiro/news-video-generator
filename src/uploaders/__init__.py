"""YouTube and TikTok social media uploaders."""

from .tiktok_auth import TikTokAuth, TikTokAuthError, TikTokCredentials
from .tiktok_uploader import TikTokUploader, TikTokUploadError, TikTokUploadResult
from .youtube_auth import YouTubeAuth, get_youtube_credentials
from .youtube_uploader import UploadResult, YouTubeUploader

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
