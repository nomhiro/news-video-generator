"""アプリケーション設定。

pydantic-settings で環境変数を読み、型と必須項目を検証する。
以前は `os.getenv` を手書きし、`validate()` がエラー文字列のリストを
返す独自方式だった。差し替えた理由:

- 必須項目の欠落や型の誤りを、使う直前ではなく起動時に検出できる
- APIキーを `SecretStr` にすると、ログや例外・`repr()` に平文が出ない。
  設定オブジェクトはエラー時に丸ごと出力されることがあるため実害がある
- 検証ロジックを自分で書かなくて済む

環境変数名はフラットなまま維持している。`env_nested_delimiter` で
グループ化すると `AZURE_OPENAI__API_KEY` のような改名が必要になり、
既存の `.env` が動かなくなるため。
"""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# AI関連ニュースの既定の検索クエリ。
# 環境変数 AI_SEARCH_QUERIES で上書きできる。
DEFAULT_AI_SEARCH_QUERIES: tuple[str, ...] = (
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
)


class Config(BaseSettings):
    """アプリケーション設定。

    環境変数（および `.env`）から読み込む。フィールド名の大文字が
    そのまま環境変数名になる（`azure_openai_endpoint` ←
    `AZURE_OPENAI_ENDPOINT`）。

    必須項目が欠けていると `ValidationError` を投げる。
    使う直前に None を踏むより、起動時に落ちた方が原因が分かりやすい。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # .env には他のツール向けの変数が混ざることがあるため、
        # 知らないキーはエラーにしない
        extra="ignore",
    )

    # --- Azure OpenAI（台本生成と画像生成で共用） ---
    azure_openai_endpoint: str = Field(description="Azure OpenAI のエンドポイント URL")
    azure_openai_api_key: SecretStr = Field(description="Azure OpenAI の API キー")
    azure_openai_deployment: str = Field(description="台本生成モデルのデプロイ名（例: gpt-5.1）")
    # 既定値を置かない。デプロイ名はモデル名と一致しないことが多く
    # （以前は モデル gpt-image-2 のデプロイ名が "gpt-image-2-1" だった）、
    # 推測した既定値は unknown_model という分かりにくい 400 を招く。
    azure_openai_image_deployment: str = Field(
        description="画像生成モデルのデプロイ名（例: gpt-image-2）"
    )

    # --- Azure OpenAI（画像生成のリソースが台本生成と別の場合） ---
    #
    # 画像生成は専用の Foundry プロジェクト（infra/ の azd で払い出す）に
    # 置いており、台本生成とは別リージョン・別リソースになる。
    # 理由: gpt-image-2 のクォータはサブスクリプション単位・リージョン単位で
    # 上限 4 で、台本生成のある eastus2 は既存デプロイで使い切っていた。
    #
    # 未設定なら台本生成と同じエンドポイント・キーを使う。
    # 単一リソースに両方のデプロイを置く構成も引き続き有効なので、
    # その場合に設定を増やさなくて済むようにしている。
    azure_openai_image_endpoint: str | None = Field(
        default=None,
        description="画像生成リソースのエンドポイント。未指定なら台本生成と同じものを使う",
    )
    azure_openai_image_api_key: SecretStr | None = Field(
        default=None,
        description="画像生成リソースの API キー。未指定なら台本生成と同じものを使う",
    )

    # 画像生成の同時リクエスト数。
    # gpt-image-2 の既定クォータは 5 images/min 程度なので小さく保つ。
    image_max_concurrency: int = Field(default=3, ge=1, le=20)

    # --- Google Cloud（音声合成） ---
    google_application_credentials: str | None = Field(
        default=None,
        description="サービスアカウント JSON のパス。未指定なら ADC を使う",
    )
    google_cloud_project: str = Field(description="Google Cloud プロジェクト ID")
    google_cloud_location: str = Field(default="us-central1")

    google_tts_voice_ja: str = Field(default="ja-JP-Chirp3-HD-Zephyr")
    google_tts_voice_en: str = Field(default="en-US-Chirp3-HD-Zephyr")

    # --- 出力 ---
    output_dir: Path = Field(default=Path("./output"))

    # --- ニュース取得 ---
    news_data_dir: Path = Field(default=Path("./data/news"))
    news_fetch_limit: int = Field(default=10, ge=1)
    ai_search_queries: list[str] = Field(default=list(DEFAULT_AI_SEARCH_QUERIES))
    ai_news_limit_per_query: int = Field(default=5, ge=1)

    # --- Web サーバー ---
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8000, ge=1, le=65535)

    # --- YouTube アップロード（任意） ---
    youtube_client_secrets_file: str = Field(default="client_secrets.json")
    youtube_token_file: str = Field(default="youtube_token.json")
    youtube_default_privacy: str = Field(default="public")

    # --- TikTok アップロード（任意） ---
    tiktok_client_key: SecretStr = Field(default=SecretStr(""))
    tiktok_client_secret: SecretStr = Field(default=SecretStr(""))
    tiktok_token_file: str = Field(default="tiktok_token.json")
    tiktok_redirect_uri: str = Field(default="http://127.0.0.1:8090/callback")
    tiktok_default_privacy: str = Field(default="SELF_ONLY")

    @field_validator("ai_search_queries", mode="before")
    @classmethod
    def _parse_ai_search_queries(cls, value: object) -> object:
        """カンマ区切りの文字列をリストに変換する。

        pydantic は list 型の環境変数を JSON として解釈しようとするため、
        `AI_SEARCH_QUERIES=生成AI,ChatGPT` のような素直な書き方を
        受け付けるにはここで変換する必要がある。

        Args:
            value: 環境変数の生の値、またはすでにリスト

        Returns:
            リスト（空文字列なら既定値）
        """
        if isinstance(value, str):
            queries = [q.strip() for q in value.split(",") if q.strip()]
            return queries or list(DEFAULT_AI_SEARCH_QUERIES)
        return value

    @field_validator("youtube_default_privacy")
    @classmethod
    def _check_youtube_privacy(cls, value: str) -> str:
        """YouTube の公開設定が API の受け付ける値であること。

        不正な値はアップロード時に初めて弾かれ、そこまでの生成が
        無駄になるため起動時に検証する。
        """
        allowed = {"public", "private", "unlisted"}
        if value not in allowed:
            raise ValueError(f"YOUTUBE_DEFAULT_PRIVACY は {sorted(allowed)} のいずれか: {value!r}")
        return value

    @field_validator("tiktok_default_privacy")
    @classmethod
    def _check_tiktok_privacy(cls, value: str) -> str:
        """TikTok の公開設定が API の受け付ける値であること。"""
        from src.uploaders.tiktok_uploader import PRIVACY_LEVELS

        if value not in PRIVACY_LEVELS:
            raise ValueError(
                f"TIKTOK_DEFAULT_PRIVACY は {sorted(PRIVACY_LEVELS)} のいずれか: {value!r}"
            )
        return value

    @field_validator("azure_openai_endpoint", "azure_openai_image_endpoint")
    @classmethod
    def _check_endpoint_looks_like_a_url(cls, value: str | None) -> str | None:
        """エンドポイントが URL の形をしていること。

        リソース名だけを入れる間違いが起きやすく、その場合の
        エラーメッセージが分かりにくい。
        """
        if value is None or value == "":
            # 画像生成用は任意。空文字は未指定として扱う
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"エンドポイントは http(s):// で始まる URL: {value!r}")
        return value.rstrip("/")

    # --- 呼び出し側の名前に合わせるプロパティ ---
    # フィールド名は環境変数名に合わせている（google_tts_voice_ja）。
    # 既存コードは意味に沿った名前（voice_name_ja）で参照しているので、
    # ここで受ける。

    @property
    def image_endpoint(self) -> str:
        """画像生成に使うエンドポイント。

        専用リソースが設定されていればそれを、なければ台本生成と
        同じものを返す。

        Returns:
            str: エンドポイント URL
        """
        return self.azure_openai_image_endpoint or self.azure_openai_endpoint

    @property
    def image_api_key(self) -> SecretStr:
        """画像生成に使う API キー。

        専用リソースが設定されていればそれを、なければ台本生成と
        同じものを返す。

        Returns:
            SecretStr: API キー
        """
        return self.azure_openai_image_api_key or self.azure_openai_api_key

    @property
    def uses_dedicated_image_resource(self) -> bool:
        """画像生成が台本生成と別リソースかどうか。

        ログに出して、どちらの構成で動いているか分かるようにする。

        Returns:
            bool: 別リソースなら True
        """
        return self.azure_openai_image_endpoint is not None

    @property
    def voice_name_ja(self) -> str:
        """日本語ナレーションのボイス名。"""
        return self.google_tts_voice_ja

    @property
    def voice_name_en(self) -> str:
        """英語ナレーションのボイス名。"""
        return self.google_tts_voice_en

    @property
    def google_credentials_path(self) -> str | None:
        """サービスアカウント JSON のパス（未設定なら None）。"""
        return self.google_application_credentials

    @classmethod
    def from_env(cls) -> "Config":
        """環境変数から設定を読み込む。

        Returns:
            Config: 検証済みの設定

        Raises:
            pydantic.ValidationError: 必須項目の欠落や不正な値
        """
        return cls()  # type: ignore[call-arg]  # pydantic-settings が env から埋める

    def is_tiktok_configured(self) -> bool:
        """TikTok の資格情報が揃っているか。

        Returns:
            bool: client_key と client_secret の両方が設定されていれば True
        """
        return bool(
            self.tiktok_client_key.get_secret_value()
            and self.tiktok_client_secret.get_secret_value()
        )

    def ensure_output_dirs(self) -> None:
        """出力ディレクトリを作成する。"""
        for subdir in ("audio", "images", "videos", "scripts"):
            (self.output_dir / subdir).mkdir(parents=True, exist_ok=True)

    def ensure_news_dirs(self) -> None:
        """ニュースデータディレクトリを作成する。"""
        self.news_data_dir.mkdir(parents=True, exist_ok=True)
