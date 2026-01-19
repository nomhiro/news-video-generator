"""Pipeline orchestration for News Video Generator."""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.news import NewsArticle

from config import Config
from src.models.script import Script
from src.generators.script_generator import ScriptGenerator
from src.generators.voice_generator import VoiceGenerator
from src.generators.image_generator import ImageGenerator
from src.generators.video_composer import VideoComposer
from src.utils.logger import log_step, log_success, log_error


class PipelineError(Exception):
    """Pipeline execution failed."""

    pass


class Pipeline:
    """動画生成パイプラインを制御するクラス。

    Attributes:
        config: アプリケーション設定
        script_generator: 台本生成器
        voice_generator: 音声生成器
        image_generator: 画像生成器
        video_composer: 動画合成器
    """

    def __init__(self, config: Config):
        """Pipelineを初期化する。

        Args:
            config: アプリケーション設定
        """
        self.config = config
        self.script_generator = ScriptGenerator(
            config.azure_openai_endpoint,
            config.azure_openai_api_key,
            config.azure_openai_deployment,
        )
        self.voice_generator = VoiceGenerator(
            voice_name_ja=config.voice_name_ja,
            voice_name_en=config.voice_name_en,
            credentials_path=config.google_credentials_path,
        )
        self.image_generator = ImageGenerator(
            api_key=config.gemini_api_key,
        )
        self.video_composer = VideoComposer()

    def _sanitize_filename(self, name: str, max_length: int = 50) -> str:
        """ファイル名として安全な文字列に変換する。

        Args:
            name: 元の文字列
            max_length: 最大文字数

        Returns:
            str: サニタイズされたファイル名
        """
        import re
        # ファイル名に使えない文字を置換
        sanitized = re.sub(r'[\\/*?:"<>|]', '', name)
        # 連続する空白を1つに
        sanitized = re.sub(r'\s+', ' ', sanitized)
        # 前後の空白を削除
        sanitized = sanitized.strip()
        # 長さを制限
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length].strip()
        # 空になった場合はデフォルト名
        if not sanitized:
            sanitized = "video"
        return sanitized

    def run(
        self, news_topic: str, languages: List[str] = None, output_name: str = None,
        video_format: str = "short"
    ) -> Dict[str, Any]:
        """パイプライン全体を実行する。

        Args:
            news_topic: ニューストピック
            languages: 生成する言語のリスト（デフォルト: ["ja", "en"]）
            output_name: 出力ファイル名（指定しない場合はタイムスタンプ）
            video_format: 動画形式 ("short" or "long")

        Returns:
            Dict[str, Any]: 実行結果のサマリー

        Raises:
            PipelineError: パイプライン実行に失敗した場合
        """
        if languages is None:
            languages = ["ja", "en"]

        # 話速を動画形式に応じて設定
        speaking_rates = {"long": 1.1, "tiktok": 1.15, "short": 1.25}
        speaking_rate = speaking_rates.get(video_format, 1.25)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Determine base name for output files
        if output_name:
            base_name = f"{timestamp}_{self._sanitize_filename(output_name)}"
        else:
            base_name = timestamp

        # Ensure output directories exist
        self.config.ensure_output_dirs()

        try:
            # 1. Generate scripts for each language
            log_step("台本を生成中...", "📝")
            scripts: Dict[str, Script] = {}
            script_paths: Dict[str, Path] = {}

            for lang in languages:
                script = self.script_generator.generate(news_topic, lang, video_format)
                scripts[lang] = script

                # Save script to file
                script_path = (
                    self.config.output_dir / "scripts" / f"{base_name}_{lang}.json"
                )
                script.to_json_file(script_path)
                script_paths[lang] = script_path

            # 2. Generate images (use first language's prompts)
            log_step("画像を生成中...", "🎨")
            first_lang = languages[0]
            image_dir = self.config.output_dir / "images" / base_name
            image_paths = self.image_generator.generate_batch(
                scripts[first_lang].image_prompts, image_dir, language=first_lang,
                video_format=video_format
            )

            # 3. Generate voices for each language (with timing if available)
            log_step("音声を生成中...", "🎙️")
            audio_paths: Dict[str, Path] = {}
            segment_timings: Dict[str, List[float]] = {}

            for lang in languages:
                audio_path = (
                    self.config.output_dir / "audio" / f"{base_name}_{lang}.mp3"
                )

                # segment_narrationsがある場合はタイミング付き生成
                if scripts[lang].segment_narrations:
                    _, timings = self.voice_generator.generate_with_timings(
                        scripts[lang].segment_narrations, lang, audio_path,
                        speaking_rate=speaking_rate
                    )
                    segment_timings[lang] = timings
                else:
                    # フォールバック: 従来の生成方式
                    self.voice_generator.generate(
                        scripts[lang].full_narration, lang, audio_path,
                        speaking_rate=speaking_rate
                    )
                    segment_timings[lang] = []

                audio_paths[lang] = audio_path

            # 4. Compose videos for each language (with timing if available)
            log_step("動画を合成中...", "🎬")
            video_paths: Dict[str, Path] = {}

            for lang in languages:
                video_path = (
                    self.config.output_dir / "videos" / f"{base_name}_{lang}.mp4"
                )
                self.video_composer.compose(
                    audio_paths[lang],
                    image_paths,
                    video_path,
                    text_overlays=scripts[lang].text_overlays,
                    language=lang,
                    segment_timings=segment_timings.get(lang),
                    video_format=video_format,
                )
                video_paths[lang] = video_path

            log_success("パイプライン完了!")

            return {
                "status": "success",
                "timestamp": timestamp,
                "scripts": {lang: str(path) for lang, path in script_paths.items()},
                "images": [str(p) for p in image_paths],
                "audio": {lang: str(path) for lang, path in audio_paths.items()},
                "videos": {lang: str(path) for lang, path in video_paths.items()},
            }

        except Exception as e:
            log_error(f"パイプラインエラー: {e}")
            raise PipelineError(f"パイプライン実行に失敗しました: {e}")

    def run_from_article(
        self, article: Any, languages: List[str] = None, video_format: str = "short"
    ) -> Dict[str, Any]:
        """ニュース記事から動画を生成する。

        Args:
            article: NewsArticleオブジェクト（titleとcontentを持つ）
            languages: 生成する言語のリスト（デフォルト: ["ja"]）
            video_format: 動画形式 ("short" or "long")

        Returns:
            Dict[str, Any]: 実行結果のサマリー

        Raises:
            PipelineError: パイプライン実行に失敗した場合
        """
        if languages is None:
            languages = ["ja"]

        # Create topic from article title and content
        content = getattr(article, "content", "") or ""
        title = getattr(article, "title", "")

        # Limit content length to avoid API limits
        topic = f"{title}\n\n{content[:2000]}"

        return self.run(topic, languages, video_format=video_format)
