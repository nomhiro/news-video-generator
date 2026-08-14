"""Pipeline orchestration for News Video Generator."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from config import Config
from src.generators.image_generator import ImageGenerator
from src.generators.script_generator import ScriptGenerator
from src.generators.video_composer import VideoComposer
from src.generators.voice_generator import VoiceGenerator
from src.models.formats import get_spec
from src.models.script import Script
from src.storage.artifacts import ArtifactStore, build_artifact_store
from src.utils.logger import log_error, log_step, log_success


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

    def __init__(self, config: Config, artifact_store: ArtifactStore | None = None):
        """Pipelineを初期化する。

        Args:
            config: アプリケーション設定
            artifact_store: 生成物の保存先。省略時は設定から組み立てる
                （テストではフェイクを渡す）
        """
        self.config = config
        # 生成は必ず output_dir（ローカル）で行う。ffmpeg は外部プロセスで
        # パスしか受け取れないため。保存先だけが差し替え可能。
        self.artifact_store = artifact_store or build_artifact_store(
            config.artifact_store,
            local_root=config.output_dir,
            account_url=config.azure_storage_account_url,
            container_name=config.azure_storage_container,
        )
        api_key = config.azure_openai_api_key.get_secret_value()
        self.script_generator = ScriptGenerator(
            config.azure_openai_endpoint,
            api_key,
            config.azure_openai_deployment,
        )
        self.voice_generator = VoiceGenerator(
            api_key=config.azure_speech_api_key.get_secret_value(),
            region=config.azure_speech_region,
            voice_name_ja=config.voice_name_ja,
            voice_name_en=config.voice_name_en,
        )
        # 画像生成は台本生成と別リソースのことがある（別リージョンの
        # 専用 Foundry プロジェクト）。config が使い分けを解決する。
        self.image_generator = ImageGenerator(
            endpoint=config.image_endpoint,
            api_key=config.image_api_key.get_secret_value(),
            deployment=config.azure_openai_image_deployment,
            max_concurrency=config.image_max_concurrency,
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
        sanitized = re.sub(r'[\\/*?:"<>|]', "", name)
        # 連続する空白を1つに
        sanitized = re.sub(r"\s+", " ", sanitized)
        # 前後の空白を削除
        sanitized = sanitized.strip()
        # 長さを制限
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length].strip()
        # 空になった場合はデフォルト名
        if not sanitized:
            sanitized = "video"
        return sanitized

    def _artifact_key(self, path: Path) -> str:
        """ローカルパスを保存先のキーに変換する。

        キーは `output_dir` からの相対パスを posix 形式にしたもの
        （`videos/20260814_005245_ja.mp4`）。Windows の `\\` をそのまま
        使うと Blob 名として別物になるため posix に寄せる。

        Args:
            path: 生成したファイルのパス

        Returns:
            str: 保存先のキー
        """
        return path.resolve().relative_to(self.config.output_dir.resolve()).as_posix()

    def _publish_artifacts(self, paths: list[Path]) -> list[str]:
        """生成物を保存先へ送る。

        1件の失敗で生成全体を失敗させない。動画は既にローカルに存在して
        おり、アップロードだけが失敗した状態は「あとで再送すればよい」。
        ここで例外を投げると、成功した動画も含めて生成が失敗扱いになる。

        Args:
            paths: 生成したファイル

        Returns:
            list[str]: 保存に成功したキー
        """
        published: list[str] = []
        failed: list[str] = []
        for path in paths:
            key = self._artifact_key(path)
            try:
                self.artifact_store.publish(path, key)
                published.append(key)
            # 保存の失敗で生成物を失わせない（ローカルには残る）
            except Exception as e:
                log_error(f"生成物の保存に失敗しました（{key}）: {e}")
                failed.append(key)

        if failed:
            log_error(f"{len(failed)}件の生成物を保存できませんでした（ローカルには残っています）")
        return published

    def run(
        self,
        news_topic: str,
        languages: list[str] | None = None,
        output_name: str | None = None,
        video_format: str = "short",
        source_url: str = "",
    ) -> dict[str, Any]:
        """パイプライン全体を実行する。

        Args:
            news_topic: ニューストピック
            languages: 生成する言語のリスト（デフォルト: ["ja", "en"]）
            output_name: 出力ファイル名（指定しない場合はタイムスタンプ）
            video_format: 動画形式 ("short" or "long")
            source_url: 元記事の URL。台本の説明文に出典として載せる
                （再利用コンテンツ対策）。CLI の自由テキスト実行では空

        Returns:
            Dict[str, Any]: 実行結果のサマリー

        Raises:
            PipelineError: パイプライン実行に失敗した場合
        """
        if languages is None:
            languages = ["ja", "en"]

        # 形式ごとのパラメータは formats.py が単一の情報源
        spec = get_spec(video_format)
        speaking_rate = spec.speaking_rate

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
            scripts: dict[str, Script] = {}
            script_paths: dict[str, Path] = {}

            for lang in languages:
                script = self.script_generator.generate(
                    news_topic, lang, video_format, source_url=source_url
                )
                scripts[lang] = script

                # Save script to file
                script_path = self.config.output_dir / "scripts" / f"{base_name}_{lang}.json"
                script.to_json_file(script_path)
                script_paths[lang] = script_path

            # 2. Generate images (use first language's prompts)
            log_step("画像を生成中...", "🎨")
            first_lang = languages[0]
            image_dir = self.config.output_dir / "images" / base_name
            image_paths = self.image_generator.generate_batch(
                scripts[first_lang].image_prompts,
                image_dir,
                language=first_lang,
                video_format=video_format,
            )

            # 3. Generate voices for each language (with timing if available)
            log_step("音声を生成中...", "🎙️")
            audio_paths: dict[str, Path] = {}
            segment_timings: dict[str, list[float]] = {}

            for lang in languages:
                audio_path = self.config.output_dir / "audio" / f"{base_name}_{lang}.mp3"

                # segment_narrationsがある場合はタイミング付き生成
                if scripts[lang].segment_narrations:
                    _, timings = self.voice_generator.generate_with_timings(
                        scripts[lang].segment_narrations,
                        lang,
                        audio_path,
                        speaking_rate=speaking_rate,
                    )
                    segment_timings[lang] = timings
                else:
                    # フォールバック: 従来の生成方式
                    self.voice_generator.generate(
                        scripts[lang].full_narration, lang, audio_path, speaking_rate=speaking_rate
                    )
                    segment_timings[lang] = []

                audio_paths[lang] = audio_path

            # 4. Compose videos for each language (with timing if available)
            log_step("動画を合成中...", "🎬")
            video_paths: dict[str, Path] = {}

            for lang in languages:
                video_path = self.config.output_dir / "videos" / f"{base_name}_{lang}.mp4"
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

            # 5. 生成物を保存先へ送る
            #
            # ローカル保存なら生成した場所がそのまま保存先なので何も起きない。
            # Blob 保存なら実際のアップロードが走る。
            artifact_keys = self._publish_artifacts(
                [
                    *script_paths.values(),
                    *image_paths,
                    *audio_paths.values(),
                    *video_paths.values(),
                ]
            )

            log_success("パイプライン完了!")

            return {
                "status": "success",
                "timestamp": timestamp,
                "scripts": {lang: str(path) for lang, path in script_paths.items()},
                "images": [str(p) for p in image_paths],
                "audio": {lang: str(path) for lang, path in audio_paths.items()},
                "videos": {lang: str(path) for lang, path in video_paths.items()},
                # 保存先の中でのキー。ローカルパスと違い、Blob でもそのまま通じる
                "artifact_keys": {
                    "scripts": {
                        lang: self._artifact_key(path) for lang, path in script_paths.items()
                    },
                    "images": [self._artifact_key(p) for p in image_paths],
                    "audio": {lang: self._artifact_key(path) for lang, path in audio_paths.items()},
                    "videos": {
                        lang: self._artifact_key(path) for lang, path in video_paths.items()
                    },
                },
                "published": artifact_keys,
            }

        except Exception as e:
            log_error(f"パイプラインエラー: {e}")
            raise PipelineError(f"パイプライン実行に失敗しました: {e}") from e

    def run_from_article(
        self, article: Any, languages: list[str] | None = None, video_format: str = "short"
    ) -> dict[str, Any]:
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
