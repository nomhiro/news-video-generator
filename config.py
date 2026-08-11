"""Configuration management for News Video Generator."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    """アプリケーション設定。

    Attributes:
        azure_openai_endpoint: Azure OpenAI endpoint URL
        azure_openai_api_key: Azure OpenAI API key
        azure_openai_deployment: 台本生成モデルのデプロイ名
        azure_openai_image_deployment: 画像生成モデルのデプロイ名 (gpt-image-2)
        image_max_concurrency: 画像生成の同時リクエスト数
        google_credentials_path: Google Cloud認証情報JSONファイルのパス (optional)
        google_cloud_project: Google Cloud project ID (for TTS)
        google_cloud_location: Google Cloud region (default: us-central1)
        voice_name_ja: Japanese voice name for Google Cloud TTS
        voice_name_en: English voice name for Google Cloud TTS
        output_dir: Output directory path
        news_data_dir: News data storage directory
        web_host: Web server host
        web_port: Web server port
        news_fetch_limit: Max articles per category
    """

    # Azure OpenAI Settings (台本生成)
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_deployment: str

    # Azure OpenAI Settings (画像生成 - gpt-image-2)
    # 台本生成と同じエンドポイント・APIキーを使い、デプロイ名だけが異なる。
    # 既定値は置かない。デプロイ名はモデル名と一致しないことが多く
    # (例: モデル gpt-image-2 のデプロイ名が "gpt-image-2-1")、
    # 推測した既定値は unknown_model という分かりにくい 400 を招く。
    azure_openai_image_deployment: str = ""

    # 画像生成の同時リクエスト数。
    # gpt-image-2 の既定クォータは 5 images/min per deployment のため、
    # 引き上げ申請が通るまでは小さく保つ。
    image_max_concurrency: int = 3

    # Google Cloud TTS Settings
    google_credentials_path: str | None = None  # Uses ADC if not set

    # Google Cloud Project Settings (for TTS)
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"

    # Voice Settings (Google Cloud TTS Chirp 3 HD)
    voice_name_ja: str = "ja-JP-Chirp3-HD-Zephyr"
    voice_name_en: str = "en-US-Chirp3-HD-Zephyr"

    # Output Settings
    output_dir: Path = field(default_factory=lambda: Path("./output"))

    # News Aggregation Settings
    news_data_dir: Path = field(default_factory=lambda: Path("./data/news"))
    news_fetch_limit: int = 10  # Articles per category

    # AI News Settings
    ai_search_queries: list[str] = field(
        default_factory=lambda: [
            "生成AI",
            "ChatGPT",
            "Claude AI",
            "Claude Code",
            "Gemini AI",
            "GitHub Copilot",
            "大規模言語モデル LLM",
            "OpenAI",
            "Anthropic",
            "Stable Diffusion",
            "Midjourney",
            "画像生成AI",
        ]
    )
    ai_news_limit_per_query: int = 5  # Articles per search query

    # Web Server Settings
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # YouTube Upload Settings
    youtube_client_secrets_file: str = "client_secrets.json"
    youtube_token_file: str = "youtube_token.json"
    youtube_default_privacy: str = "public"  # "public", "private", or "unlisted"

    # TikTok Upload Settings
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_token_file: str = "tiktok_token.json"
    tiktok_redirect_uri: str = "http://127.0.0.1:8090/callback"
    tiktok_default_privacy: str = "SELF_ONLY"  # "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"

    @classmethod
    def _parse_ai_search_queries(cls, env_value: str) -> list[str]:
        """環境変数からAI検索クエリをパースする。

        Args:
            env_value: カンマ区切りのクエリ文字列

        Returns:
            List[str]: クエリのリスト（空の場合はデフォルト値）
        """
        if not env_value:
            return [
                "生成AI",
                "ChatGPT",
                "Claude AI",
                "Claude Code",
                "Gemini AI",
                "GitHub Copilot",
                "大規模言語モデル LLM",
                "OpenAI",
                "Anthropic",
                "Stable Diffusion",
                "Midjourney",
                "画像生成AI",
            ]
        return [q.strip() for q in env_value.split(",") if q.strip()]

    @classmethod
    def from_env(cls) -> "Config":
        """環境変数から設定を読み込む。

        Returns:
            Config: 設定オブジェクト
        """
        load_dotenv()

        return cls(
            azure_openai_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            azure_openai_api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            # 既定値は置かない。Azure のデプロイ名は環境固有であり、
            # コード側の既定値は「動くはず」という誤解を生む。
            azure_openai_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
            azure_openai_image_deployment=os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", ""),
            image_max_concurrency=int(os.getenv("IMAGE_MAX_CONCURRENCY", "3")),
            google_credentials_path=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            voice_name_ja=os.getenv("GOOGLE_TTS_VOICE_JA", "ja-JP-Chirp3-HD-Zephyr"),
            voice_name_en=os.getenv("GOOGLE_TTS_VOICE_EN", "en-US-Chirp3-HD-Zephyr"),
            news_data_dir=Path(os.getenv("NEWS_DATA_DIR", "./data/news")),
            news_fetch_limit=int(os.getenv("NEWS_FETCH_LIMIT", "10")),
            ai_search_queries=cls._parse_ai_search_queries(os.getenv("AI_SEARCH_QUERIES", "")),
            ai_news_limit_per_query=int(os.getenv("AI_NEWS_LIMIT_PER_QUERY", "5")),
            web_host=os.getenv("WEB_HOST", "127.0.0.1"),
            web_port=int(os.getenv("WEB_PORT", "8000")),
            youtube_client_secrets_file=os.getenv(
                "YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json"
            ),
            youtube_token_file=os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token.json"),
            youtube_default_privacy=os.getenv("YOUTUBE_DEFAULT_PRIVACY", "public"),
            # TikTok settings
            tiktok_client_key=os.getenv("TIKTOK_CLIENT_KEY", ""),
            tiktok_client_secret=os.getenv("TIKTOK_CLIENT_SECRET", ""),
            tiktok_token_file=os.getenv("TIKTOK_TOKEN_FILE", "tiktok_token.json"),
            tiktok_redirect_uri=os.getenv("TIKTOK_REDIRECT_URI", "http://127.0.0.1:8090/callback"),
            tiktok_default_privacy=os.getenv("TIKTOK_DEFAULT_PRIVACY", "SELF_ONLY"),
        )

    def validate(self) -> list[str]:
        """設定の検証。エラーメッセージのリストを返す。

        Returns:
            List[str]: エラーメッセージのリスト（空なら検証成功）
        """
        errors = []
        if not self.azure_openai_endpoint:
            errors.append("AZURE_OPENAI_ENDPOINT が設定されていません")
        if not self.azure_openai_api_key:
            errors.append("AZURE_OPENAI_API_KEY が設定されていません")
        if not self.azure_openai_deployment:
            errors.append(
                "AZURE_OPENAI_DEPLOYMENT が設定されていません "
                "(台本生成モデルのデプロイ名。例: gpt-5.1)"
            )
        if not self.azure_openai_image_deployment:
            errors.append(
                "AZURE_OPENAI_IMAGE_DEPLOYMENT が設定されていません "
                "(画像生成モデルのデプロイ名。例: gpt-image-2)"
            )
        if self.image_max_concurrency < 1:
            errors.append("IMAGE_MAX_CONCURRENCY は1以上でなければなりません")
        # Note: google_credentials_path is optional (uses ADC if not set)
        if not self.google_cloud_project:
            errors.append("GOOGLE_CLOUD_PROJECT が設定されていません")
        return errors

    def is_tiktok_configured(self) -> bool:
        """TikTok APIキーが設定されているかチェック。

        Returns:
            bool: TikTokのclient_keyとclient_secretが設定されている場合True
        """
        return bool(self.tiktok_client_key and self.tiktok_client_secret)

    def ensure_output_dirs(self) -> None:
        """出力ディレクトリを作成する。"""
        subdirs = ["audio", "images", "videos", "scripts"]
        for subdir in subdirs:
            (self.output_dir / subdir).mkdir(parents=True, exist_ok=True)

    def ensure_news_dirs(self) -> None:
        """ニュースデータディレクトリを作成する。"""
        self.news_data_dir.mkdir(parents=True, exist_ok=True)
