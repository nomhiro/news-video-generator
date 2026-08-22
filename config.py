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

from datetime import time as dt_time
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# リスト型の設定は `NoDecode` を付ける。
#
# pydantic-settings は list 型のフィールドを「複雑な型」として扱い、
# 環境変数の値を **field_validator より前に json.loads する**。
# そのため `SCHEDULE_FORMATS=short,long` のような素直な書き方は
# `SettingsError: error parsing value for field ...` で落ちる。
#
# 厄介なのは、`.env` 経由（DotEnvSettingsSource）では通り、
# **実際の環境変数のときだけ落ちる**こと。ローカルは .env で動くので
# 気付かず、Container Apps に env として渡した時点で起動しなくなった
# （一度踏んだ）。NoDecode を付けると JSON 解釈を飛ばし、
# 下の mode="before" バリデータが分割を担当する。
CommaSeparated = Annotated[list[str], NoDecode]


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

    # --- Azure AI Speech（音声合成） ---
    #
    # Google Cloud TTS から移行した。Chirp 3 HD が SSML の <mark> を
    # サポートせず、セグメント境界のタイミングを取得できなかったのが理由。
    # 詳細は src/generators/voice_generator.py の docstring を参照。
    #
    # 副産物として Google Cloud のサービスアカウント JSON が不要になり、
    # コンテナ実行時にシークレットをマウントする必要も消えた。
    azure_speech_api_key: SecretStr = Field(description="Azure AI Speech の API キー")
    azure_speech_region: str = Field(
        default="japaneast",
        description="Azure AI Speech のリージョン（例: japaneast）",
    )

    # 標準の Neural ボイスを使う。Dragon HD 系（*:MAI-Voice-*）は
    # <prosody> 非対応で、形式別の話速を指定できない。
    azure_speech_voice_ja: str = Field(default="ja-JP-NanamiNeural")
    azure_speech_voice_en: str = Field(default="en-US-AvaNeural")

    # --- 動画のレンダラ ---
    #
    # ffmpeg: 静止画（gpt-image-2）を並べる現行の方式。
    # remotion: React で図解を描く方式。画像生成 API は挿絵1枚だけに使う
    #           （旧方式はショート1本で6枚。クォータの消費が6分の1になる）。
    #
    # **既定は remotion**（2026-08-20 に ffmpeg から切り替えた）。
    #
    # 切り替えの理由は「既定が ffmpeg のままだと意味が無い」ため。本番の
    # Container App には `VIDEO_RENDERER` の env を置いていないので、
    # **この既定値がそのまま毎朝の自動生成の見た目を決める**。ここを
    # ffmpeg に残したままレンダラを作り込んでも、毎朝の生成は旧方式
    # （静止画のスライドショー）で回り続ける。CD が無かった頃と同じ形の
    # 「気付かないまま古いものが動き続ける」失敗になる。
    #
    # **自動フォールバックは無い。** remotion が失敗したらジョブを失敗させ、
    # リースと再試行に任せる（理由は
    # src/generators/video_renderer.py の docstring）。つまり本番イメージに
    # Node 22 と Chrome Headless Shell が無ければ、毎朝の生成は丸ごと
    # 失敗する。ffmpeg へ黙って落ちて旧見た目で回り続けるより、
    # 失敗して気付く方を選んでいる。
    #
    # ローカルでは `cd remotion && npm install` を一度実行する。
    # ffmpeg 方式は退路として残してあり、`VIDEO_RENDERER=ffmpeg` で戻せる。
    video_renderer: Literal["ffmpeg", "remotion"] = Field(default="remotion")

    # --- 出力 ---
    #
    # output_dir は「生成の作業場所」。ffmpeg は subprocess で動く外部
    # プロセスなので、生成そのものは必ずローカルのファイルシステムで行う。
    output_dir: Path = Field(default=Path("./output"))

    # 生成物の保存先。local はローカルのファイルシステム、
    # blob は Azure Blob Storage。
    #
    # コンテナで動かすときに local だと、再起動で生成物が消え、
    # レプリカ間でも共有されない（YouTube に上げる前に成果物を失う）。
    artifact_store: Literal["local", "blob"] = Field(default="local")

    # Blob の認証はアカウントキーではなく Entra ID（DefaultAzureCredential）。
    # ローカルは az login、Container Apps はマネージド ID を使う。
    # ストレージアカウント側で共有キー認証を無効にしてあるので、
    # キーを使う経路はそもそも存在しない。
    azure_storage_account_url: str | None = Field(
        default=None,
        description="https://<account>.blob.core.windows.net。ARTIFACT_STORE=blob のとき必須",
    )
    azure_storage_container: str = Field(
        default="artifacts",
        description="生成物を入れる Blob コンテナ名",
    )

    # --- データベース（ジョブ表） ---
    #
    # 進捗をプロセスメモリではなく行として持つための DB。
    # SQLite のファイルは1台のファイルシステム上にしかないので、
    # レプリカを2つ以上にするなら共有できる DB（PostgreSQL）に
    # 差し替える。SQLAlchemy を挟んでいるのはそのため。
    database_url: str = Field(
        default="sqlite:///./data/newsvideo.db",
        description="SQLAlchemy の接続 URL",
    )

    # SQLite の journal mode。
    #
    # 既定は WAL。書き込み中でも読めるので、ワーカーが書いている最中に
    # /status が読む構成に合っている。
    #
    # ただし **Azure Files（SMB）の上では WAL が使えない**。WAL は
    # 共有メモリ（-shm の mmap）を要求し、SMB はそれを提供しないため
    # "disk I/O error" や "database is locked" になる。
    # クラウドでファイル共有にマウントするときは DELETE にする
    # （同時読み書きは弱くなるが、レプリカ1・ワーカー1なので実害は小さい）。
    sqlite_journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"] = Field(
        default="WAL",
        description="SQLite の journal_mode。Azure Files 上では DELETE",
    )

    # --- ニュース取得 ---
    news_data_dir: Path = Field(default=Path("./data/news"))
    news_fetch_limit: int = Field(default=10, ge=1)
    # AI カテゴリはフィードから埋める（一覧と理由は src/news/feeds.py）。
    # 検索クエリ（旧 AI_SEARCH_QUERIES）は廃止した——語が一致するだけの
    # 記事を一次情報と区別できず、実物の投稿が芸能ニュースになった。
    ai_news_limit_per_feed: int = Field(default=3, ge=1)

    # --- 定期実行 ---
    #
    # 既定は無効。ローカルで開発しているだけのときに、勝手にニュースを
    # 取得して動画を作り始めると課金が発生する。
    # クラウド側は infra/ の Bicep が有効にする。
    schedule_enabled: bool = Field(default=False)

    # 実行時刻（SCHEDULE_TIMEZONE のローカル時刻、HH:MM）。
    # 朝に回すと、出社前に前日夜〜当日朝のニュースで作れる。
    schedule_time: str = Field(default="06:30")
    schedule_timezone: str = Field(default="Asia/Tokyo")

    # 作る形式。既定はショートのみ（長尺は当面作らない。CLAUDE.md 参照）。
    schedule_formats: CommaSeparated = Field(default=["short"])

    # 形式ごとに何件の記事を対象にするか。
    # 画像生成のクォータが律速なので、増やす前にクォータを上げる。
    schedule_articles_per_format: int = Field(default=1, ge=1, le=10)

    # --- Web サーバー ---
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8000, ge=1, le=65535)

    # --- OAuth トークンの保存先 ---
    #
    # local はローカルのファイル（従来どおり）、blob は Azure Blob Storage。
    #
    # コンテナで local だと、再起動でトークンが消えて毎回ブラウザ認証が
    # 必要になる。YouTube の OAuth は localhost にリダイレクトする方式
    # （InstalledAppFlow）なので、コンテナの中では実質的に完了できない。
    # blob にすれば、認証はローカルで1回行い、コンテナは読むだけで済む。
    token_store: Literal["local", "blob"] = Field(default="local")

    # トークン用の Blob コンテナ。生成物とは分けている
    # （トークンは長期の資格情報で、生成物より扱いが重い）。
    azure_token_container: str = Field(default="tokens")

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

    # --- X（旧 Twitter）投稿 ---
    #
    # 既定は無効。完全自動投稿なので、開発中に勝手に公開されると
    # 取り返しがつかない。有効化は画面から行う（下記のスイッチ）。
    x_posting_enabled: bool = Field(default=False)

    x_client_id: str = Field(default="")
    x_client_secret: SecretStr = Field(default=SecretStr(""))
    x_token_file: str = Field(default="x_token.json")
    x_redirect_uri: str = Field(default="http://127.0.0.1:8091/callback")

    # 投稿時刻（SCHEDULE_TIMEZONE のローカル時刻、HH:MM）。
    x_post_times: CommaSeparated = Field(default=["08:00", "12:30", "19:00", "21:30"])

    # 1日のテーマ数（宣伝投稿は含まない）。
    x_posts_per_day: int = Field(default=4, ge=1, le=20)

    # 予定時刻からこれ以上遅れた投稿は捨てる。
    # 止まっていたあと復帰した瞬間の連投を防ぐ。
    x_max_post_delay_minutes: int = Field(default=60, ge=1)

    # 概算コストの上限（USD/月）と単価。
    # 単価を設定に出しているのは、X の料金改定に追随するため。
    #
    # **$30 は「全投稿がリンク付き」を前提にした値。** 記事の元リンクを
    # 付けるようにしたので `has_link` は常に True で、単価は安い方（$0.015）
    # ではなく13倍の方（$0.20）が全件に効く。4件/日 × 30日 = 120件で
    # 投稿 $24.00 + 読み取り $1.20 = **$25.20**。$20 のままだと月の
    # 4週目で `is_over_budget` が立ち、**月末の数日は下書きが1件も
    # 積まれない**（計画側で止めるので、静かに投稿が消える）。
    # 投稿頻度（`x_posts_per_day`）を上げるならここも一緒に見直す。
    x_monthly_budget_usd: float = Field(default=30.0, gt=0)
    x_cost_per_post_usd: float = Field(default=0.015, ge=0)
    x_cost_per_post_with_link_usd: float = Field(default=0.20, ge=0)
    # 投稿1件の読み取り単価。計測が投稿ごとに2回読むぶんを概算に入れるために要る。
    # 実請求と突き合わせて、読み取りが概算から丸ごと落ちていることに気付いた。
    x_cost_per_read_usd: float = Field(default=0.005, ge=0)

    # 自動投稿スイッチの実体。ジョブ表（SQLite）と違い Azure Files を想定する
    # （リビジョン更新で消えると、画面で有効にした翌日に黙って投稿が止まる）。
    # 記事の選択状態（news_data_dir）と同じボリュームに置く。
    x_posting_switch_path: Path = Field(default=Path("./data/x_posting.json"))

    # 指標計測の実行時刻（SCHEDULE_TIMEZONE のローカル時刻、HH:MM）。
    # SCHEDULE_TIME（動画計画・X投稿計画）とは別の時刻にする。同時に回すと、
    # 記事選定（動画計画）・投稿計画・指標の読み取り課金が同じ枠で重なり、
    # どれが遅延の原因か切り分けにくくなる。
    #
    # 08:00〜20:00 の間で選ぶ。計測は「24時間前・7日前」を窓 ±12時間で
    # 探すため、投稿時刻（X_POST_TIMES、最も早くて08:00・最も遅くて21:30）
    # を1日分すべて窓の内側に収めるには、実行時刻がこの帯の中である必要がある
    # （境界に近いと、その日の最初か最後の投稿が窓から外れる）。
    x_metrics_time: str = Field(default="11:00")

    @field_validator("schedule_formats", mode="before")
    @classmethod
    def _parse_schedule_formats(cls, value: object) -> object:
        """カンマ区切りの文字列をリストに変換する。

        `SCHEDULE_FORMATS=short,long` と書けるようにする
        （pydantic は list を JSON として解釈しようとする）。
        """
        if isinstance(value, str):
            formats = [f.strip() for f in value.split(",") if f.strip()]
            return formats or ["short"]
        return value

    @field_validator("schedule_formats")
    @classmethod
    def _check_schedule_formats(cls, value: list[str]) -> list[str]:
        """未知の形式を起動時に弾く。

        定期実行の中で初めて弾かれると、気付くのが翌朝になる。
        """
        from src.models.formats import VideoFormat

        allowed = {f.value for f in VideoFormat}
        unknown = [f for f in value if f not in allowed]
        if unknown:
            raise ValueError(
                f"SCHEDULE_FORMATS に未知の形式があります: {unknown}（{sorted(allowed)}）"
            )
        return value

    @field_validator("schedule_time")
    @classmethod
    def _check_schedule_time(cls, value: str) -> str:
        """HH:MM として解釈できること。

        解釈できない値だと、スケジューラの起動時に落ちる。
        設定を読む時点で弾いた方が原因が分かりやすい。
        """
        from datetime import time as _time

        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            _time(hour=hour, minute=minute)
        except (ValueError, TypeError) as e:
            raise ValueError(f"SCHEDULE_TIME は HH:MM の形式で指定してください: {value!r}") from e
        return value

    @field_validator("schedule_timezone")
    @classmethod
    def _check_schedule_timezone(cls, value: str) -> str:
        """実在するタイムゾーン名であること。"""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"SCHEDULE_TIMEZONE が不正です: {value!r}") from e
        return value

    @field_validator("x_post_times", mode="before")
    @classmethod
    def _parse_x_comma_separated(cls, value: object) -> object:
        """カンマ区切りの文字列をリストに変換する。

        `schedule_formats` と同じ理由（pydantic は list を JSON として
        解釈しようとするため、素直な書き方を通すにはここで変換する）。
        """
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("x_metrics_time")
    @classmethod
    def _check_x_metrics_time(cls, value: str) -> str:
        """HH:MM として解釈できること。

        `schedule_time` と同じ理由: 解釈できない値だとスケジューラの
        起動時に落ちる。設定を読む時点で弾いた方が原因が分かりやすい。
        """
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            dt_time(hour=hour, minute=minute)
        except (ValueError, TypeError) as e:
            raise ValueError(f"X_METRICS_TIME は HH:MM の形式で指定してください: {value!r}") from e
        return value

    @field_validator("x_post_times")
    @classmethod
    def _check_x_post_times(cls, value: list[str]) -> list[str]:
        """各要素が HH:MM として解釈できること。

        `schedule_time` と同じ理由: 解釈できない値だとスケジューラの
        起動時に落ちる。設定を読む時点で弾いた方が原因が分かりやすい。
        """
        for item in value:
            try:
                hour, minute = (int(part) for part in item.split(":", 1))
                dt_time(hour=hour, minute=minute)
            except (ValueError, TypeError) as e:
                raise ValueError(f"X_POST_TIMES は HH:MM の形式で指定してください: {item!r}") from e
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

    @model_validator(mode="after")
    def _check_blob_store_is_configured(self) -> Self:
        """保存先が blob なのにアカウント URL が無い構成を弾く。

        起動時に落とす。生成が終わった段階で初めて失敗すると、
        画像6枚と音声・動画を作りきったあとに保存先が無いと分かることになり、
        時間とクォータを最も無駄にする。
        """
        needs_blob = [
            name
            for name, value in (
                ("ARTIFACT_STORE", self.artifact_store),
                ("TOKEN_STORE", self.token_store),
            )
            if value == "blob"
        ]
        if needs_blob and not self.azure_storage_account_url:
            raise ValueError(
                f"{' / '.join(needs_blob)}=blob には AZURE_STORAGE_ACCOUNT_URL が必要です"
                "（例: https://stnewsvideo.blob.core.windows.net）"
            )
        return self

    @field_validator(
        "azure_openai_endpoint", "azure_openai_image_endpoint", "azure_storage_account_url"
    )
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
    def schedule_run_at(self) -> "dt_time":
        """定期実行の時刻を `datetime.time` で返す。

        Returns:
            dt_time: 実行時刻（検証済みなので必ず解釈できる）
        """
        from datetime import time as _time

        hour, minute = (int(part) for part in self.schedule_time.split(":", 1))
        return _time(hour=hour, minute=minute)

    @property
    def metrics_run_at(self) -> "dt_time":
        """指標計測の実行時刻を `datetime.time` で返す。

        Returns:
            dt_time: 実行時刻（検証済みなので必ず解釈できる）
        """
        from datetime import time as _time

        hour, minute = (int(part) for part in self.x_metrics_time.split(":", 1))
        return _time(hour=hour, minute=minute)

    @property
    def token_paths(self) -> dict[str, Path]:
        """トークン保存先の 名前 -> ローカルパス。

        `TOKEN_STORE=local` のときに使う。名前は
        `src/storage/tokens.py` の定数と揃える必要がある
        （blob 保存でも同じ名前が Blob 名になるので、
        ローカルと blob を行き来しても同じ値を指す）。

        Returns:
            dict[str, Path]: 保存先の対応
        """
        return {
            "youtube_token": Path(self.youtube_token_file),
            "youtube_client_secrets": Path(self.youtube_client_secrets_file),
            "tiktok_token": Path(self.tiktok_token_file),
            "x_token": Path(self.x_token_file),
        }

    @property
    def voice_name_ja(self) -> str:
        """日本語ナレーションのボイス名。"""
        return self.azure_speech_voice_ja

    @property
    def voice_name_en(self) -> str:
        """英語ナレーションのボイス名。"""
        return self.azure_speech_voice_en

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
