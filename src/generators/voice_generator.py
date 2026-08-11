"""Voice generation using Google Cloud Text-to-Speech API (Chirp 3 HD)."""

import time
from pathlib import Path
from typing import ClassVar

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import texttospeech_v1beta1 as texttospeech

from src.utils.logger import log_error, log_step, log_success


class VoiceGenerationError(Exception):
    """Voice generation failed."""

    pass


class VoiceGenerator:
    """Google Cloud Text-to-Speech APIを使用して音声を生成するクラス。

    Chirp 3 HD Voicesモデルを使用。

    Attributes:
        client: Google Cloud TTS client
        voice_names: 言語別のボイス名辞書
    """

    MAX_RETRIES = 3
    BASE_DELAY = 1.0
    API_ENDPOINT = "texttospeech.googleapis.com"

    # Default Chirp 3 HD voice mappings
    DEFAULT_VOICES: ClassVar[dict[str, str]] = {
        "ja": "ja-JP-Chirp3-HD-Zephyr",  # Japanese - Zephyr (Female)
        "en": "en-US-Chirp3-HD-Zephyr",  # English - Zephyr (Female)
    }

    # Language code mappings (short code -> BCP-47)
    LANGUAGE_CODES: ClassVar[dict[str, str]] = {
        "ja": "ja-JP",
        "en": "en-US",
    }

    def __init__(
        self,
        voice_name_ja: str | None = None,
        voice_name_en: str | None = None,
        credentials_path: str | None = None,
    ):
        """VoiceGeneratorを初期化する。

        Args:
            voice_name_ja: 日本語用ボイス名 (e.g., "ja-JP-Chirp3-HD-Zephyr")
            voice_name_en: 英語用ボイス名 (e.g., "en-US-Chirp3-HD-Zephyr")
            credentials_path: Google Cloud認証情報JSONファイルのパス
                             (省略時はGOOGLE_APPLICATION_CREDENTIALS環境変数を使用)
        """
        # Set up client options
        client_options = ClientOptions(api_endpoint=self.API_ENDPOINT)

        # Create client (uses ADC or GOOGLE_APPLICATION_CREDENTIALS automatically)
        if credentials_path:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(credentials_path)
            self.client = texttospeech.TextToSpeechClient(
                credentials=credentials,
                client_options=client_options,
            )
        else:
            self.client = texttospeech.TextToSpeechClient(
                client_options=client_options,
            )

        # Set up voice mappings
        self.voice_names: dict[str, str] = {
            "ja": voice_name_ja or self.DEFAULT_VOICES["ja"],
            "en": voice_name_en or self.DEFAULT_VOICES["en"],
        }

    def generate(
        self, text: str, language: str, output_path: Path, speaking_rate: float = 1.25
    ) -> Path:
        """テキストから音声を生成する。

        Args:
            text: 読み上げるテキスト
            language: 言語コード ("ja" or "en")
            output_path: 出力ファイルパス
            speaking_rate: 話速 (範囲: 0.25-2.0、デフォルト: 1.25)

        Returns:
            Path: 生成された音声ファイルのパス

        Raises:
            VoiceGenerationError: 音声生成に失敗した場合
        """
        log_step(f"音声を生成中... ({language}, 話速{speaking_rate}x)", "🎙️")

        voice_name = self.voice_names.get(language, self.voice_names["en"])
        language_code = self.LANGUAGE_CODES.get(language, "en-US")

        # Prepare request parameters
        synthesis_input = texttospeech.SynthesisInput(text=text)

        voice_params = texttospeech.VoiceSelectionParams(
            name=voice_name,
            language_code=language_code,
        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speaking_rate,
        )

        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.client.synthesize_speech(
                    input=synthesis_input,
                    voice=voice_params,
                    audio_config=audio_config,
                )

                # Save audio content to file
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(response.audio_content)

                log_success(f"{language}音声を生成しました")
                return output_path

            except google_exceptions.ResourceExhausted as e:
                # Rate limited / quota exceeded
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error("Google Cloud TTS API呼び出しに失敗しました (Quota exceeded)")
                raise VoiceGenerationError(
                    "Google Cloud TTS API呼び出しに失敗しました (Quota exceeded)\n"
                    "→ 3回リトライしましたが失敗しました\n"
                    "→ クォータを確認してください"
                ) from e

            except google_exceptions.InvalidArgument as e:
                log_error(f"無効な引数: {e}")
                raise VoiceGenerationError(f"無効なパラメータです: {e}") from e

            except google_exceptions.GoogleAPICallError as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"Google Cloud TTS APIエラー: {e}")
                raise VoiceGenerationError(f"音声生成に失敗しました: {e}") from e

            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"予期しないエラー: {e}")
                raise VoiceGenerationError(f"音声生成に失敗しました: {e}") from e

        raise VoiceGenerationError("最大リトライ回数を超えました")

    def generate_segments_individually(
        self,
        segment_narrations: list[str],
        language: str,
        output_path: Path,
        speaking_rate: float = 1.25,
    ) -> tuple[Path, list[float]]:
        """各セグメントを個別に音声生成し、結合して正確なタイミングを取得する。

        Chirp 3 HDはSSML Markタイムポイントをサポートしていないため、
        各セグメントを個別に生成して実際の音声長さから正確なタイミングを取得する。

        Args:
            segment_narrations: 各セグメントのナレーションテキスト
            language: 言語コード ("ja" or "en")
            output_path: 最終的な結合音声ファイルのパス
            speaking_rate: 話速 (範囲: 0.25-2.0、デフォルト: 1.25)

        Returns:
            Tuple[Path, List[float]]: (音声ファイルパス, 各セグメントの開始時刻リスト)
                タイミングリストはセグメント数+1の長さ（最後は終了時刻）

        Raises:
            VoiceGenerationError: 音声生成に失敗した場合
        """
        import tempfile

        from mutagen.mp3 import MP3
        from pydub import AudioSegment

        log_step(
            f"セグメント別に音声を生成中... ({language}, 話速{speaking_rate}x, {len(segment_narrations)}セグメント)",
            "🎙️",
        )

        voice_name = self.voice_names.get(language, self.voice_names["en"])
        language_code = self.LANGUAGE_CODES.get(language, "en-US")

        voice_params = texttospeech.VoiceSelectionParams(
            name=voice_name,
            language_code=language_code,
        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speaking_rate,
        )

        segment_audio_files: list[Path] = []
        segment_durations: list[float] = []

        # 一時ディレクトリを作成
        temp_dir = Path(tempfile.mkdtemp())

        try:
            # 各セグメントを個別に生成
            for i, segment_text in enumerate(segment_narrations):
                # 空のセグメントをチェック
                if not segment_text or not segment_text.strip():
                    raise VoiceGenerationError(
                        f"セグメント{i + 1}が空です。台本生成で問題が発生した可能性があります。"
                    )

                segment_path = temp_dir / f"segment_{i}.mp3"

                synthesis_input = texttospeech.SynthesisInput(text=segment_text)

                for attempt in range(self.MAX_RETRIES):
                    try:
                        response = self.client.synthesize_speech(
                            input=synthesis_input,
                            voice=voice_params,
                            audio_config=audio_config,
                        )

                        # セグメント音声を保存
                        with open(segment_path, "wb") as f:
                            f.write(response.audio_content)

                        # 音声の長さを取得
                        audio = MP3(str(segment_path))
                        if audio.info is None:
                            raise VoiceGenerationError(
                                f"生成した音声を解析できませんでした: {segment_path}"
                            )
                        duration = audio.info.length
                        segment_durations.append(duration)
                        segment_audio_files.append(segment_path)

                        log_step(f"  セグメント{i + 1}: {duration:.2f}秒", "")
                        break

                    except google_exceptions.ResourceExhausted as e:
                        if attempt < self.MAX_RETRIES - 1:
                            delay = self.BASE_DELAY * (2**attempt)
                            time.sleep(delay)
                            continue
                        raise VoiceGenerationError("Google Cloud TTS API クォータ超過") from e

                    except Exception as e:
                        if attempt < self.MAX_RETRIES - 1:
                            delay = self.BASE_DELAY * (2**attempt)
                            time.sleep(delay)
                            continue
                        raise VoiceGenerationError(f"セグメント{i + 1}の音声生成に失敗: {e}") from e

            # 全セグメントを結合
            combined = AudioSegment.empty()
            for segment_path in segment_audio_files:
                segment_audio = AudioSegment.from_mp3(str(segment_path))
                combined += segment_audio

            # 結合音声を保存
            output_path.parent.mkdir(parents=True, exist_ok=True)
            combined.export(str(output_path), format="mp3")

            # タイミングリストを作成（各セグメントの開始時刻）
            timings: list[float] = [0.0]
            cumulative = 0.0
            for duration in segment_durations:
                cumulative += duration
                timings.append(cumulative)

            log_success(
                f"{language}音声を生成しました（{len(segment_narrations)}セグメント, 合計{cumulative:.2f}秒）"
            )
            return output_path, timings

        finally:
            # 一時ファイルを削除
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)

    def _estimate_timings_from_text(
        self,
        segment_narrations: list[str],
        audio_path: Path,
        language: str,
    ) -> list[float]:
        """音声ファイルの長さと文字数から各セグメントの開始時刻を推定する。

        Chirp 3 HDがSSML Markタイムポイントをサポートしていないため、
        文字数比率に基づいて各セグメントの開始時刻を推定する。

        Args:
            segment_narrations: 各セグメントのナレーションテキスト
            audio_path: 生成された音声ファイルのパス
            language: 言語コード ("ja" or "en")

        Returns:
            List[float]: 各セグメントの推定開始時刻リスト（セグメント数+1の長さ）
        """
        from mutagen.mp3 import MP3

        # 音声ファイルの長さを取得
        try:
            audio = MP3(str(audio_path))
            if audio.info is None:
                raise VoiceGenerationError(f"音声を解析できませんでした: {audio_path}")
            total_duration = audio.info.length
        except Exception as e:
            log_error(f"音声ファイルの長さを取得できませんでした: {e}")
            # フォールバック: 空リストを返して均等分割を使わせる
            return []

        # 各セグメントの文字数を計算
        char_counts = [len(segment) for segment in segment_narrations]
        total_chars = sum(char_counts)

        if total_chars == 0:
            return []

        # 文字数比率に基づいて各セグメントの開始時刻を計算
        timings: list[float] = [0.0]  # 最初のセグメントは0秒から開始
        cumulative_time = 0.0

        for char_count in char_counts:
            # このセグメントの推定時間（文字数比率 × 全体時間）
            segment_duration = (char_count / total_chars) * total_duration
            cumulative_time += segment_duration
            timings.append(cumulative_time)

        log_step(f"文字数ベースでタイミングを推定: {len(timings)}ポイント", "📊")
        for i, t in enumerate(timings[:-1]):
            duration = timings[i + 1] - t
            log_step(f"  セグメント{i + 1}: {t:.2f}s〜{timings[i + 1]:.2f}s ({duration:.2f}s)", "")

        return timings

    def _build_ssml_with_marks(self, segment_narrations: list[str]) -> str:
        """セグメントごとにマーカーを挿入したSSMLを構築する。

        Args:
            segment_narrations: 各セグメントのナレーションテキスト

        Returns:
            str: マーカー付きSSML文字列
        """
        ssml_parts = ["<speak>"]
        for i, segment in enumerate(segment_narrations):
            # セグメントの前にマーカーを挿入
            ssml_parts.append(f'<mark name="seg{i}"/>')
            # SSML特殊文字をエスケープ
            escaped_segment = (
                segment.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
            )
            ssml_parts.append(escaped_segment)
        # 終了マーカーを追加（最後のセグメントの終了時刻取得用）
        ssml_parts.append(f'<mark name="seg{len(segment_narrations)}"/>')
        ssml_parts.append("</speak>")
        return "".join(ssml_parts)

    def generate_with_timings(
        self,
        segment_narrations: list[str],
        language: str,
        output_path: Path,
        speaking_rate: float = 1.25,
    ) -> tuple[Path, list[float]]:
        """セグメント付きナレーションから音声を生成し、タイミング情報を返す。

        Chirp 3 HDはSSML Markタイムポイントをサポートしていないため、
        各セグメントを個別に生成して正確なタイミングを取得する方式を使用する。

        Args:
            segment_narrations: 各セグメントのナレーションテキスト
            language: 言語コード ("ja" or "en")
            output_path: 出力ファイルパス
            speaking_rate: 話速 (範囲: 0.25-2.0、デフォルト: 1.25)

        Returns:
            Tuple[Path, List[float]]: (音声ファイルパス, 各セグメントの開始時刻リスト)
                タイミングリストはセグメント数+1の長さ（最後は終了時刻）

        Raises:
            VoiceGenerationError: 音声生成に失敗した場合
        """
        # Chirp 3 HDはSSML <mark>タグをサポートしていないため、
        # セグメント別生成方式を使用して正確なタイミングを取得
        return self.generate_segments_individually(
            segment_narrations=segment_narrations,
            language=language,
            output_path=output_path,
            speaking_rate=speaking_rate,
        )

    def _generate_with_timings_ssml(
        self,
        segment_narrations: list[str],
        language: str,
        output_path: Path,
        speaking_rate: float = 1.25,
    ) -> tuple[Path, list[float]]:
        """（非推奨）SSML Markを使用してタイミングを取得する。

        注意: Chirp 3 HDはSSML Markをサポートしていないため、この方法では
        タイムポイントが返されません。generate_segments_individuallyを使用してください。

        Args:
            segment_narrations: 各セグメントのナレーションテキスト
            language: 言語コード ("ja" or "en")
            output_path: 出力ファイルパス
            speaking_rate: 話速 (範囲: 0.25-2.0、デフォルト: 1.25)

        Returns:
            Tuple[Path, List[float]]: (音声ファイルパス, 各セグメントの開始時刻リスト)

        Raises:
            VoiceGenerationError: 音声生成に失敗した場合
        """
        log_step(f"音声を生成中（SSML Mark方式）... ({language}, 話速{speaking_rate}x)", "🎙️")

        voice_name = self.voice_names.get(language, self.voice_names["en"])
        language_code = self.LANGUAGE_CODES.get(language, "en-US")

        # SSMLを構築
        ssml = self._build_ssml_with_marks(segment_narrations)
        synthesis_input = texttospeech.SynthesisInput(ssml=ssml)

        voice_params = texttospeech.VoiceSelectionParams(
            name=voice_name,
            language_code=language_code,
        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speaking_rate,
        )

        for attempt in range(self.MAX_RETRIES):
            try:
                # enable_time_pointingでSSML_MARKを指定
                response = self.client.synthesize_speech(
                    request=texttospeech.SynthesizeSpeechRequest(
                        input=synthesis_input,
                        voice=voice_params,
                        audio_config=audio_config,
                        enable_time_pointing=[
                            texttospeech.SynthesizeSpeechRequest.TimepointType.SSML_MARK
                        ],
                    )
                )

                # タイムポイントを抽出
                timings: list[float] = []
                for timepoint in response.timepoints:
                    timings.append(timepoint.time_seconds)

                # 音声ファイル保存（タイミング推定前に必要）
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(response.audio_content)

                # タイミングが取得できなかった場合のフォールバック（文字数ベース推定）
                # Chirp 3 HDはSSML <mark>タグをサポートしていないため、タイムポイントが返されない
                if not timings:
                    log_step("Chirp 3 HDはタイムポイント未対応。文字数ベースで推定します。", "⏱️")
                    timings = self._estimate_timings_from_text(
                        segment_narrations, output_path, language
                    )

                log_success(f"{language}音声を生成しました（{len(timings)}個のタイムポイント）")
                return output_path, timings

            except google_exceptions.ResourceExhausted as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error("Google Cloud TTS API呼び出しに失敗しました (Quota exceeded)")
                raise VoiceGenerationError(
                    "Google Cloud TTS API呼び出しに失敗しました (Quota exceeded)\n"
                    "→ 3回リトライしましたが失敗しました\n"
                    "→ クォータを確認してください"
                ) from e

            except google_exceptions.InvalidArgument as e:
                log_error(f"無効な引数: {e}")
                raise VoiceGenerationError(f"無効なパラメータです: {e}") from e

            except google_exceptions.GoogleAPICallError as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"Google Cloud TTS APIエラー: {e}")
                raise VoiceGenerationError(f"音声生成に失敗しました: {e}") from e

            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"予期しないエラー: {e}")
                raise VoiceGenerationError(f"音声生成に失敗しました: {e}") from e

        raise VoiceGenerationError("最大リトライ回数を超えました")
