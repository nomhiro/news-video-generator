"""Image generation using Google Imagen 4 Fast."""

import time
from pathlib import Path
from typing import List, Optional

from google import genai
from google.genai import types

from src.utils.logger import log_step, log_success, log_error


class ImageGenerationError(Exception):
    """Image generation failed."""

    pass


class ImageGenerator:
    """Google Imagen 4 Fast を使用して画像を生成するクラス。

    高速・低コストな画像生成（$0.02/枚）。

    Attributes:
        client: Google GenAI client
    """

    MODEL = "imagen-3.0-generate-002"
    MAX_RETRIES = 3
    BASE_DELAY = 1.0

    def __init__(self, api_key: Optional[str] = None):
        """ImageGeneratorを初期化する。

        Args:
            api_key: Gemini API key (省略時は環境変数 GEMINI_API_KEY を使用)
        """
        # API キーが明示的に渡された場合はそれを使用
        # そうでない場合は環境変数から自動取得
        if api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            # 環境変数 GEMINI_API_KEY または GOOGLE_API_KEY から自動取得
            self.client = genai.Client()

    def generate_batch(
        self, prompts: List[str], output_dir: Path, language: str = "ja",
        video_format: str = "short"
    ) -> List[Path]:
        """複数のプロンプトから画像を生成する。

        Args:
            prompts: 画像生成プロンプトのリスト
            output_dir: 出力ディレクトリ
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short" or "long")

        Returns:
            List[Path]: 生成された画像ファイルのパスリスト

        Raises:
            ImageGenerationError: 画像生成に失敗した場合
        """
        # アスペクト比を動画形式に応じて設定
        # TikTok format uses vertical 9:16 like short format
        aspect_ratio = "16:9" if video_format == "long" else "9:16"
        format_labels = {
            "long": "ロング(16:9)",
            "tiktok": "TikTok(9:16)",
            "short": "ショート(9:16)"
        }
        format_label = format_labels.get(video_format, "ショート(9:16)")
        log_step(f"{len(prompts)}枚の画像を生成中... ({format_label})", "🎨")

        output_dir.mkdir(parents=True, exist_ok=True)
        image_paths: List[Path] = []

        for i, prompt in enumerate(prompts, 1):
            enhanced_prompt = self._enhance_prompt(prompt, language, video_format)
            output_path = output_dir / f"image_{i:03d}.png"
            
            # デバッグ: プロンプトの長さを確認
            log_step(f"画像{i}: プロンプト長={len(enhanced_prompt)}文字", "🔍")

            try:
                path = self._generate_single(enhanced_prompt, output_path, aspect_ratio)
                image_paths.append(path)
            except ImageGenerationError:
                log_error(f"画像{i}の生成に失敗")
                log_error(f"完全なプロンプト: {enhanced_prompt}")
                raise  # 元のエラーをそのまま伝播
            except Exception as e:
                log_error(f"画像{i}の生成に失敗: {e}")
                raise ImageGenerationError(f"予期しないエラー (画像{i}): {e}")

        log_success(f"{len(image_paths)}枚の画像を生成しました")
        return image_paths

    def _generate_single(self, prompt: str, output_path: Path, aspect_ratio: str = "9:16") -> Path:
        """単一の画像を生成する。

        Args:
            prompt: 画像生成プロンプト
            output_path: 出力ファイルパス
            aspect_ratio: アスペクト比 ("9:16" or "16:9")

        Returns:
            Path: 生成された画像ファイルのパス

        Raises:
            ImageGenerationError: 画像生成に失敗した場合
        """
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.client.models.generate_images(
                    model=self.MODEL,
                    prompt=prompt,
                    config=types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio=aspect_ratio,
                    ),
                )

                # レスポンスから画像を抽出して保存
                if response.generated_images:
                    response.generated_images[0].image.save(str(output_path))
                    return output_path

                # 画像が生成されなかった場合 - 詳細なレスポンス情報をログに出力
                error_details = ""
                if hasattr(response, 'prompt_feedback'):
                    error_details += f"\nPrompt feedback: {response.prompt_feedback}"
                if hasattr(response, 'candidates') and response.candidates:
                    for i, candidate in enumerate(response.candidates):
                        if hasattr(candidate, 'finish_reason'):
                            error_details += f"\nCandidate {i} finish_reason: {candidate.finish_reason}"
                        if hasattr(candidate, 'safety_ratings'):
                            error_details += f"\nCandidate {i} safety_ratings: {candidate.safety_ratings}"
                log_error(f"APIレスポンス詳細: {response}{error_details}")
                raise ImageGenerationError(
                    f"画像が生成されませんでした\n"
                    f"プロンプト: {prompt[:100]}..."
                )

            except ImageGenerationError:
                raise
            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    log_step(f"リトライ中 ({attempt + 1}/{self.MAX_RETRIES})...", "⏳")
                    time.sleep(delay)
                    continue
                raise ImageGenerationError(f"画像生成に失敗しました: {e}")

        raise ImageGenerationError("最大リトライ回数を超えました")

    def _enhance_prompt(self, prompt: str, language: str = "ja", video_format: str = "short") -> str:
        """プロンプトを強化する。

        Args:
            prompt: 元のプロンプト
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short" or "long")

        Returns:
            str: 強化されたプロンプト
        """
        enhanced = prompt

        # 縦向き/横向きを明示的に指定（API側のaspect_ratioだけでは不十分な場合がある）
        if video_format in ("short", "tiktok"):
            # 縦長 9:16 の構図を明示的に指示 (short と tiktok は同じ)
            enhanced = f"IMPORTANT: Vertical portrait orientation (9:16 aspect ratio), tall composition optimized for mobile viewing. {enhanced}"
        else:
            # 横長 16:9 の構図を明示的に指示
            enhanced = f"IMPORTANT: Horizontal landscape orientation (16:9 aspect ratio), wide composition optimized for desktop viewing. {enhanced}"

        # Check if quality keywords already present
        quality_keywords = ["high quality", "cinematic", "detailed", "professional"]
        if not any(kw in prompt.lower() for kw in quality_keywords):
            enhanced = f"{enhanced}, high quality, detailed, professional, cinematic lighting"

        # 画像内にテキストを含めない（テキストはオーバーレイで追加するため）
        enhanced = f"{enhanced}, no text, no words, no letters, no writing, no captions, no labels, no watermarks"
        
        # 人物に関するキーワードが含まれているかチェック
        person_keywords = ["person", "people", "man", "woman", "minister", "president", 
                          "leader", "politician", "ceo", "executive", "speaker", "figure",
                          "human", "face", "portrait", "headshot"]
        has_person = any(kw in prompt.lower() for kw in person_keywords)
        
        if has_person:
            # 人物が含まれる場合: 実在の人物の肖像権を避けるため、
            # 特定の人物ではなく一般的な人物として描写するよう指示
            enhanced = f"{enhanced}, generic anonymous person, not a real celebrity or politician, stylized illustration style"
        else:
            # 人物が含まれない場合: 顔なしの制約を追加
            enhanced = f"{enhanced}, no human faces, no portraits, no facial features, show people from behind or silhouettes only if needed"

        return enhanced
