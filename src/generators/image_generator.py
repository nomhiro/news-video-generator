"""Image generation using Azure AI Foundry gpt-image-2."""

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models.formats import get_spec
from src.utils.content_filter import is_content_filter_error
from src.utils.logger import log_error, log_step, log_success, log_warning


class ImageGenerationError(Exception):
    """Image generation failed."""

    pass


class ContentFilterError(ImageGenerationError):
    """Azure のコンテンツフィルタがプロンプトまたは生成画像を拒否した。

    リトライしても結果は変わらないため、即座に伝播させる。
    """

    pass


def validate_size(size: str) -> tuple[int, int]:
    """gpt-image-2 のサイズ制約を満たしているか検証する。

    gpt-image-2 の制約:
        - 両辺が16の倍数
        - 長辺が3840px以下（4K）
        - アスペクト比が3:1以下
        - 総ピクセル数が 655,360 〜 8,294,400

    Args:
        size: "<幅>x<高さ>" 形式の文字列（例: "1152x2048"）

    Returns:
        Tuple[int, int]: (幅, 高さ)

    Raises:
        ValueError: 制約を満たさない場合
    """
    try:
        width_str, height_str = size.lower().split("x")
        width, height = int(width_str), int(height_str)
    except ValueError:
        # int() や unpack が出す低レベルなメッセージは利用者に有用でないため、
        # 意図的にチェーンを切って呼び出し側に分かる文言だけを返す。
        raise ValueError(f"サイズの形式が不正です: {size!r} (期待: '<幅>x<高さ>')") from None

    if width <= 0 or height <= 0:
        raise ValueError(f"サイズが正の値ではありません: {size}")

    if width % 16 != 0 or height % 16 != 0:
        raise ValueError(
            f"両辺は16の倍数でなければなりません: {size} "
            f"(1080x1920 は 1080 が16の倍数でないため指定できません)"
        )

    long_edge = max(width, height)
    if long_edge > 3840:
        raise ValueError(f"長辺は3840px以下でなければなりません: {size}")

    ratio = long_edge / min(width, height)
    if ratio > 3.0:
        raise ValueError(f"アスペクト比は3:1以下でなければなりません: {size} (比={ratio:.2f})")

    pixels = width * height
    if not (655_360 <= pixels <= 8_294_400):
        raise ValueError(
            f"総ピクセル数は 655,360〜8,294,400 でなければなりません: {size} ({pixels:,}px)"
        )

    return width, height


class ImageGenerator:
    """Azure AI Foundry の gpt-image-2 を使用して画像を生成するクラス。

    台本生成（ScriptGenerator）と同じ Azure OpenAI の /openai/v1
    エンドポイントを使うため、資格情報とクライアントを共有できる。

    Attributes:
        client: OpenAI クライアント（Azure v1 エンドポイント向け）
        deployment: 画像生成モデルのデプロイ名
        max_concurrency: 同時に投げる画像生成リクエスト数
    """

    # 生成サイズは formats.py（FormatSpec.image_size）が持つ。
    # gpt-image-2 は両辺が16の倍数であることを要求するため、動画の出力解像度
    # 1080x1920 をそのまま指定できない。詳細は formats.py と validate_size()。

    # openai SDK は quality / output_format を Literal で型付けしているため、
    # 素の str ではなく Literal として宣言する（そうしないと overload に合致しない）
    QUALITY: Literal["low", "medium", "high"] = "high"
    OUTPUT_FORMAT: Literal["png", "jpeg"] = "png"
    MAX_RETRIES = 4

    # gpt-image-2 の既定クォータは 5 images/min per deployment。
    # 並行数を絞って 429 の頻発を避ける。クォータ引き上げ後は
    # コンストラクタ引数で上げられる。
    DEFAULT_MAX_CONCURRENCY = 3

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        max_concurrency: int | None = None,
    ):
        """ImageGeneratorを初期化する。

        Args:
            endpoint: Azure OpenAI endpoint URL
            api_key: Azure OpenAI API key
            deployment: 画像生成モデルのデプロイ名（例: "gpt-image-2"）
            max_concurrency: 同時リクエスト数（省略時は DEFAULT_MAX_CONCURRENCY）

        Raises:
            ValueError: endpoint / api_key / deployment が空の場合
        """
        if not endpoint:
            raise ValueError("Azure OpenAI endpoint が指定されていません")
        if not api_key:
            raise ValueError("Azure OpenAI API key が指定されていません")
        if not deployment:
            raise ValueError("画像生成モデルのデプロイ名が指定されていません")

        # Azure OpenAI v1 エンドポイント形式（ScriptGenerator と同じ組み立て）
        base_url = endpoint.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.deployment = deployment
        self.max_concurrency = max_concurrency or self.DEFAULT_MAX_CONCURRENCY

    def _size_for_format(self, video_format: str) -> str:
        """動画形式に対応する生成サイズを返す。

        サイズは formats.py が単一の情報源。

        Args:
            video_format: 動画形式 ("short", "tiktok", "long")

        Returns:
            str: "<幅>x<高さ>" 形式のサイズ
        """
        return get_spec(video_format).image_size

    def generate_batch(
        self,
        prompts: list[str],
        output_dir: Path,
        language: str = "ja",
        video_format: str = "short",
        *,
        size: str | None = None,
        enhance: bool = True,
    ) -> list[Path]:
        """複数のプロンプトから画像を生成する。

        プロンプトごとに1リクエストを投げる。gpt-image-2 の n パラメータは
        「同一プロンプトから複数枚」を得るもので、異なるプロンプトを
        1リクエストに畳むことはできないため、並行実行で短縮する。

        Args:
            prompts: 画像生成プロンプトのリスト
            output_dir: 出力ディレクトリ
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short", "tiktok", "long")
            size: 生成サイズを直接指定する。画像カードのような
                video_format という概念を持たない呼び出し元向け。
                省略時は video_format から導出する（既定動作は変えない）
            enhance: `_enhance_prompt` による装飾を適用するか。
                `_enhance_prompt` は**動画用の1行シーン記述**を飾るための
                ものであり、「縦長構図で」「テキストは描くな」を付け足す。
                画像カード（`build_card_prompt`）のように medium / palette /
                composition / constraints を自分で書き切った完結済みの
                プロンプトに重ねると、矛盾した指示が1つの文字列に混ざる
                （例: 1024x1024 を要求しているのに「縦長構図」を付け足す、
                「このラベルの文字を描け」の直後に「テキストは描くな」を
                付け足す）。完結済みのプロンプトを渡す呼び出し元は
                `enhance=False` にする。既定は True（既存動作を変えない）

        Returns:
            List[Path]: 生成された画像ファイルのパスリスト（prompts と同じ順序）

        Raises:
            ImageGenerationError: いずれかの画像生成に失敗した場合
        """
        if not prompts:
            raise ImageGenerationError("画像生成プロンプトが空です")

        size = size or self._size_for_format(video_format)
        validate_size(size)  # 定数の取り違えを起動時に検出する

        format_labels = {
            "long": f"ロング({size})",
            "tiktok": f"TikTok({size})",
            "short": f"ショート({size})",
        }
        format_label = format_labels.get(video_format, f"ショート({size})")
        log_step(
            f"{len(prompts)}枚の画像を生成中... ({format_label}, 並行数={self.max_concurrency})",
            "🎨",
        )

        output_dir.mkdir(parents=True, exist_ok=True)

        # 順序を保つため index を持ち回す
        def task(indexed: tuple[int, str]) -> Path:
            index, prompt = indexed
            final_prompt = (
                self._enhance_prompt(prompt, language, video_format) if enhance else prompt
            )
            output_path = output_dir / f"image_{index:03d}.png"
            return self._generate_single(final_prompt, output_path, size, index)

        workers = min(self.max_concurrency, len(prompts))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            try:
                # ContentFilterError も ImageGenerationError の一種なので
                # そのまま呼び出し元へ伝播する
                image_paths = list(executor.map(task, enumerate(prompts, 1)))
            except ImageGenerationError:
                raise
            except Exception as e:
                raise ImageGenerationError(f"予期しないエラー: {e}") from e

        log_success(f"{len(image_paths)}枚の画像を生成しました")
        return image_paths

    def _generate_single(self, prompt: str, output_path: Path, size: str, index: int) -> Path:
        """単一の画像を生成して保存する。

        Args:
            prompt: 画像生成プロンプト（強化済み）
            output_path: 出力ファイルパス
            size: "<幅>x<高さ>" 形式のサイズ
            index: 画像番号（ログ用、1始まり）

        Returns:
            Path: 生成された画像ファイルのパス

        Raises:
            ContentFilterError: コンテンツフィルタに拒否された場合
            ImageGenerationError: その他の理由で生成に失敗した場合
        """
        try:
            image_bytes = self._request_image(prompt, size, index)
        except ContentFilterError as e:
            log_error(f"画像{index}: コンテンツフィルタに拒否されました - {e}")
            log_error(f"画像{index}: プロンプト = {prompt}")
            raise
        except BadRequestError as e:
            log_error(f"画像{index}: リクエストが拒否されました - {e}")
            log_error(f"画像{index}: プロンプト = {prompt}")
            raise ImageGenerationError(f"画像{index}の生成に失敗しました: {e}") from e
        except Exception as e:
            log_error(f"画像{index}の生成に失敗: {e}")
            raise ImageGenerationError(f"画像{index}の生成に失敗しました: {e}") from e

        output_path.write_bytes(image_bytes)
        log_step(f"画像{index}: {output_path.name} ({len(image_bytes):,} bytes)", "🖼️")
        return output_path

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        ),
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _request_image(self, prompt: str, size: str, index: int) -> bytes:
        """画像生成APIを呼び出して画像バイト列を返す。

        レートリミット（429）・接続エラー・タイムアウト・5xx は
        指数バックオフで再試行する。コンテンツフィルタ拒否は
        再試行しても結果が変わらないため即座に伝播させる。

        Args:
            prompt: 画像生成プロンプト
            size: "<幅>x<高さ>" 形式のサイズ
            index: 画像番号（ログ用）

        Returns:
            bytes: PNG 画像のバイト列

        Raises:
            ContentFilterError: コンテンツフィルタに拒否された場合
            ImageGenerationError: レスポンスに画像が含まれない場合
        """
        try:
            response = self.client.images.generate(
                model=self.deployment,
                prompt=prompt,
                # openai 2.53.0 で size の型が Union[str, Literal[...]] に緩和され、
                # gpt-image-2 の任意解像度を型安全に渡せるようになった。
                # （それ以前は閉じた Literal で、extra_body 経由が必要だった）
                size=size,
                quality=self.QUALITY,
                n=1,
                output_format=self.OUTPUT_FORMAT,
            )
        except BadRequestError as e:
            if is_content_filter_error(e):
                raise ContentFilterError(str(e)) from e
            raise
        except RateLimitError:
            log_warning(
                f"画像{index}: レートリミット（gpt-image-2 の既定は 5 images/min）。"
                f"バックオフして再試行します"
            )
            raise

        if not response.data:
            raise ImageGenerationError(
                f"APIレスポンスに画像が含まれていません "
                f"(size={response.size}, quality={response.quality})"
            )

        b64_data = response.data[0].b64_json
        if not b64_data:
            # gpt-image-2 は常に base64 を返す。url が返ることはない。
            raise ImageGenerationError(
                f"APIレスポンスに b64_json が含まれていません (url={response.data[0].url!r})"
            )

        return base64.b64decode(b64_data)

    def _enhance_prompt(
        self, prompt: str, language: str = "ja", video_format: str = "short"
    ) -> str:
        """プロンプトを強化する。

        gpt-image-2 は指示追従性が高いため、旧 Imagen 向けの
        冗長な否定表現の列挙はやめ、簡潔な指示にとどめる。

        Args:
            prompt: 元のプロンプト
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short", "tiktok", "long")

        Returns:
            str: 強化されたプロンプト
        """
        parts = [prompt.strip()]

        # 構図の指定（size でも指定するが、構図の意図も言葉で伝える）
        if video_format == "long":
            parts.append(
                "Horizontal landscape composition, wide framing suitable for a "
                "16:9 explainer video."
            )
        else:
            parts.append(
                "Vertical portrait composition, tall framing suitable for a "
                "9:16 mobile short video."
            )

        # 品質指定（既に指定されている場合は重複させない）
        quality_keywords = ("high quality", "cinematic", "detailed", "professional")
        if not any(kw in prompt.lower() for kw in quality_keywords):
            parts.append("High quality, detailed, professional, cinematic lighting.")

        # テキストはオーバーレイで載せるため画像内には入れない
        parts.append("Do not render any text, letters, captions, or watermarks.")

        # 実在人物の肖像を避ける
        person_keywords = (
            "person",
            "people",
            "man",
            "woman",
            "minister",
            "president",
            "leader",
            "politician",
            "ceo",
            "executive",
            "speaker",
            "figure",
            "human",
            "face",
            "portrait",
            "headshot",
        )
        if any(kw in prompt.lower() for kw in person_keywords):
            parts.append(
                "Depict only generic, anonymous figures in a stylized illustration "
                "style. Do not depict any real, identifiable person."
            )
        else:
            parts.append("Avoid depicting human faces; use silhouettes if needed.")

        return " ".join(parts)
