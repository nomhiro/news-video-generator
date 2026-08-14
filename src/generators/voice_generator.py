"""音声合成（Azure AI Speech）。

なぜ Google Cloud TTS から移行したか
------------------------------------
以前は Chirp 3 HD を使っていたが、SSML の ``<mark>`` をサポートしないため
セグメント境界のタイミングを取得できなかった。その結果、実装が3系統に
分かれていた。

1. セグメントごとに個別に合成し、mutagen で実測して pydub で結合する
2. 全体を1回合成し、文字数比で按分してタイミングを推定する
3. SSML の ``<mark>`` でタイムポイントを取ろうとするが、Chirp 3 HD が
   非対応なので必ず 2 にフォールバックする（実質デッドコード）

実際に使われていたのは 2 の推定で、ナレーションと画像の切り替えが
ずれていた。

Azure AI Speech は SSML ``<bookmark>`` と ``bookmark_reached`` イベントで
**1回の合成で正確なオフセット**を返す。実測（3セグメント）:
0.000s / 3.780s / 9.680s、総尺 11.485s。
これで 1〜3 の全部が1系統に置き換わり、`pydub` / `mutagen` /
`audioop-lts` / `google-cloud-texttospeech` の4依存も不要になった。

ボイスの選定
------------
標準の Neural ボイスを使う。Dragon HD 系（``*:MAI-Voice-*``）は
``<prosody>`` をサポートせず、形式別の話速（1.1〜1.25）を指定できない。
音質は上がるが機能が退行するため採用しない。
"""

from pathlib import Path
from typing import ClassVar
from xml.sax.saxutils import escape as xml_escape

import azure.cognitiveservices.speech as speechsdk
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.utils.logger import log_error, log_step, log_success


class VoiceGenerationError(Exception):
    """Voice generation failed."""

    pass


class VoiceRetryableError(VoiceGenerationError):
    """再試行すれば成功する見込みのある失敗（スロットリング・接続エラー）。"""

    pass


# Speech SDK は 100ナノ秒刻み（tick）でオフセットを返す。
_TICKS_PER_SECOND = 10_000_000


class VoiceGenerator:
    """Azure AI Speech でナレーション音声を生成するクラス。

    Attributes:
        voice_name_ja: 日本語のボイス名
        voice_name_en: 英語のボイス名
        region: Speech リソースのリージョン
    """

    # 既定のボイス。いずれも標準 Neural（<prosody> 対応）。
    DEFAULT_VOICE_JA = "ja-JP-NanamiNeural"
    DEFAULT_VOICE_EN = "en-US-AvaNeural"

    LANGUAGE_CODES: ClassVar[dict[str, str]] = {
        "ja": "ja-JP",
        "en": "en-US",
    }

    # 出力形式。動画の音声トラックに使うので 24kHz あれば足りる。
    # ffmpeg 側で AAC に再エンコードされる。
    OUTPUT_FORMAT = speechsdk.SpeechSynthesisOutputFormat.Audio24Khz96KBitRateMonoMp3

    API_RETRIES = 4

    def __init__(
        self,
        api_key: str,
        region: str,
        voice_name_ja: str | None = None,
        voice_name_en: str | None = None,
    ):
        """VoiceGeneratorを初期化する。

        Args:
            api_key: Speech リソースの API キー
            region: Speech リソースのリージョン（例: japaneast）
            voice_name_ja: 日本語ボイス名（省略時は DEFAULT_VOICE_JA）
            voice_name_en: 英語ボイス名（省略時は DEFAULT_VOICE_EN）

        Raises:
            ValueError: api_key または region が空の場合
        """
        if not api_key:
            raise ValueError("Azure Speech の API キーが指定されていません")
        if not region:
            raise ValueError("Azure Speech のリージョンが指定されていません")

        self._api_key = api_key
        self.region = region
        self.voice_name_ja = voice_name_ja or self.DEFAULT_VOICE_JA
        self.voice_name_en = voice_name_en or self.DEFAULT_VOICE_EN

    def _voice_for(self, language: str) -> str:
        """言語に対応するボイス名を返す。"""
        return self.voice_name_ja if language == "ja" else self.voice_name_en

    def _locale_for(self, language: str) -> str:
        """言語コードを BCP-47 に変換する。"""
        return self.LANGUAGE_CODES.get(language, "ja-JP")

    def build_ssml(self, segments: list[str], language: str, speaking_rate: float) -> str:
        """セグメント境界に bookmark を置いた SSML を組み立てる。

        各セグメントの**先頭**に bookmark を置く。読み上げ中に
        `bookmark_reached` が発火し、その時点の音声オフセットが得られる。
        これがセグメントの開始時刻になり、動画側の画像切り替えに使う。

        テキストは XML エスケープする。記事タイトルに `&` や `<` が
        混じることが実際にあり、そのまま埋め込むと SSML が壊れる。

        Args:
            segments: ナレーションのセグメント
            language: 言語コード ("ja" or "en")
            speaking_rate: 話速（1.0 が標準）

        Returns:
            str: SSML 文字列
        """
        marked = "".join(
            f'<bookmark mark="seg_{i}"/>{xml_escape(segment.strip())}'
            for i, segment in enumerate(segments)
        )
        return (
            '<speak version="1.0" '
            'xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{self._locale_for(language)}">'
            f'<voice name="{self._voice_for(language)}">'
            # rate は倍率で指定する。Dragon HD 系は prosody 非対応なので
            # 標準 Neural ボイスを使う前提。
            f'<prosody rate="{speaking_rate:.2f}">{marked}</prosody>'
            "</voice></speak>"
        )

    def generate_with_timings(
        self,
        segments: list[str],
        language: str,
        output_path: Path,
        speaking_rate: float = 1.25,
    ) -> tuple[Path, list[float]]:
        """セグメントを1回で合成し、各セグメントの開始時刻を返す。

        Args:
            segments: ナレーションのセグメント（空要素は不可）
            language: 言語コード ("ja" or "en")
            output_path: 出力する MP3 のパス
            speaking_rate: 話速

        Returns:
            tuple[Path, list[float]]: (音声ファイル, 開始時刻のリスト)。
            開始時刻は各セグメントの先頭に加えて、末尾に音声全体の
            終了時刻が入る（要素数はセグメント数 + 1）。
            `video_composer._calculate_durations` がこの形を期待している。

        Raises:
            VoiceGenerationError: 合成に失敗した場合
        """
        if not segments:
            raise VoiceGenerationError("ナレーションのセグメントが空です")

        log_step(
            f"音声を合成中... ({language}, {self._voice_for(language)}, "
            f"話速{speaking_rate}x, {len(segments)}セグメント)",
            "🎙️",
        )

        ssml = self.build_ssml(segments, language, speaking_rate)
        offsets, total_duration = self._synthesize(ssml, output_path)

        timings = self._build_timings(offsets, total_duration, len(segments))

        log_success(
            f"{language}音声を合成しました（{len(segments)}セグメント, 合計{total_duration:.2f}秒）"
        )
        return output_path, timings

    def generate(
        self,
        text: str,
        language: str,
        output_path: Path,
        speaking_rate: float = 1.25,
    ) -> Path:
        """1つのテキストを合成する（タイミングは取らない）。

        Args:
            text: 読み上げるテキスト
            language: 言語コード
            output_path: 出力する MP3 のパス
            speaking_rate: 話速

        Returns:
            Path: 音声ファイルのパス

        Raises:
            VoiceGenerationError: 合成に失敗した場合
        """
        audio_path, _ = self.generate_with_timings([text], language, output_path, speaking_rate)
        return audio_path

    @staticmethod
    def _build_timings(
        offsets: dict[int, float], total_duration: float, segment_count: int
    ) -> list[float]:
        """bookmark のオフセットから開始時刻のリストを作る。

        bookmark が欠けた場合（読み上げがスキップされた等）は、
        直前の開始時刻を使って単調増加を保つ。ここが崩れると
        動画側の duration 計算が負の値になる。

        Args:
            offsets: セグメント番号 -> 開始時刻
            total_duration: 音声全体の長さ
            segment_count: セグメント数

        Returns:
            list[float]: 長さ segment_count + 1 の開始時刻リスト
        """
        timings: list[float] = []
        previous = 0.0
        for i in range(segment_count):
            current = offsets.get(i, previous)
            # 単調増加を保証する
            current = max(current, previous)
            timings.append(current)
            previous = current

        # 末尾に音声の終了時刻を足す。
        # 最後のセグメントの表示時間を決めるのに必要。
        timings.append(max(total_duration, previous))
        return timings

    @retry(
        retry=retry_if_exception_type(VoiceRetryableError),
        stop=stop_after_attempt(API_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _synthesize(self, ssml: str, output_path: Path) -> tuple[dict[int, float], float]:
        """SSML を合成してファイルに書き、bookmark のオフセットを集める。

        Args:
            ssml: 合成する SSML
            output_path: 出力する MP3 のパス

        Returns:
            tuple[dict[int, float], float]: (セグメント番号 -> 開始秒, 全体の秒数)

        Raises:
            VoiceRetryableError: スロットリングや接続の問題
            VoiceGenerationError: それ以外の失敗
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        speech_config = speechsdk.SpeechConfig(subscription=self._api_key, region=self.region)
        speech_config.set_speech_synthesis_output_format(self.OUTPUT_FORMAT)

        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
            audio_config=speechsdk.audio.AudioOutputConfig(filename=str(output_path)),
        )

        offsets: dict[int, float] = {}

        def on_bookmark(event: speechsdk.SpeechSynthesisBookmarkEventArgs) -> None:
            # mark は "seg_<番号>" の形で埋め込んでいる
            name = event.text
            if not name.startswith("seg_"):
                return
            try:
                index = int(name.removeprefix("seg_"))
            except ValueError:
                return
            offsets[index] = event.audio_offset / _TICKS_PER_SECOND

        synthesizer.bookmark_reached.connect(on_bookmark)

        result = synthesizer.speak_ssml_async(ssml).get()

        if result.reason == speechsdk.ResultReason.Canceled:
            details = result.cancellation_details
            message = f"{details.reason}: {details.error_details}"
            # スロットリングと接続の問題は再試行する価値がある。
            # 認証エラーや不正な SSML は何度やっても同じ。
            if self._is_retryable(details):
                log_error(f"音声合成が中断されました（再試行します）: {message}")
                raise VoiceRetryableError(message)
            raise VoiceGenerationError(f"音声合成に失敗しました: {message}")

        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            raise VoiceGenerationError(f"音声合成が完了しませんでした: {result.reason}")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise VoiceGenerationError(f"音声ファイルが作成されませんでした: {output_path}")

        return offsets, result.audio_duration.total_seconds()

    @staticmethod
    def _is_retryable(details: speechsdk.CancellationDetails) -> bool:
        """中断の理由が再試行に値するか判定する。

        Args:
            details: SDK が返した中断の詳細

        Returns:
            bool: 再試行すべきなら True
        """
        retryable_codes = {
            speechsdk.CancellationErrorCode.TooManyRequests,
            speechsdk.CancellationErrorCode.ServiceUnavailable,
            speechsdk.CancellationErrorCode.ServiceTimeout,
            speechsdk.CancellationErrorCode.ConnectionFailure,
            speechsdk.CancellationErrorCode.ServiceError,
        }
        return details.error_code in retryable_codes
