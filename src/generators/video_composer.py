"""Video composition using FFmpeg."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from src.utils.logger import log_step, log_success, log_error


class VideoCompositionError(Exception):
    """Video composition failed."""

    pass


class VideoComposer:
    """FFmpegを使用して動画を合成するクラス。"""

    OUTPUT_WIDTH = 1080
    OUTPUT_HEIGHT = 1920
    FRAME_RATE = 30
    VIDEO_CODEC = "libx264"
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "192k"
    CRF = 23
    PRESET = "medium"

    # テキストオーバーレイ設定
    TEXT_FONT_SIZE = 64  # 幅1080pxに収まるサイズ
    TEXT_COLOR = "yellow"  # 目立つ黄色
    TEXT_BORDER_COLOR = "black"
    TEXT_BORDER_WIDTH = 4
    TEXT_SHADOW_COLOR = "black@0.8"
    TEXT_SHADOW_X = 3
    TEXT_SHADOW_Y = 3
    TEXT_BOX = 1  # 背景ボックス有効
    TEXT_BOX_COLOR = "black@0.7"  # 半透明黒背景
    TEXT_BOX_BORDER = 15  # ボックスの余白
    TEXT_Y_POSITION = "(h-text_h)/3"  # 上から1/3の位置（中央寄り）
    TEXT_LINE_SPACING = -70  # 行間（ピクセル）- 負の値で行を詰める
    TEXT_MAX_CHARS_PER_LINE = 14  # 1行の最大文字数

    # Windows用日本語フォントパス（親しみのある丸ゴシック系を優先）
    JAPANESE_FONTS_WINDOWS = [
        "C:/Windows/Fonts/YuGothB.ttc",    # Yu Gothic Bold（太めで見やすい）
        "C:/Windows/Fonts/meiryob.ttc",    # Meiryo Bold
        "C:/Windows/Fonts/meiryo.ttc",     # Meiryo
        "C:/Windows/Fonts/YuGothM.ttc",    # Yu Gothic Medium
        "C:/Windows/Fonts/msgothic.ttc",   # MS Gothic
    ]

    def _get_japanese_font_path(self) -> str:
        """使用可能な日本語フォントのパスを取得する。

        Returns:
            str: FFmpeg用にエスケープされたフォントパス

        Raises:
            VideoCompositionError: 使用可能な日本語フォントが見つからない場合
        """
        for font_path in self.JAPANESE_FONTS_WINDOWS:
            if os.path.exists(font_path):
                # Escape for FFmpeg on Windows: C:/path -> C\:/path
                escaped_path = font_path.replace(":", "\\:")
                return escaped_path

        raise VideoCompositionError(
            "日本語フォントが見つかりません。以下のいずれかをインストールしてください:\n"
            "- Meiryo\n- Yu Gothic\n- MS Gothic"
        )

    def _wrap_text(self, text: str, max_chars: int = None) -> str:
        """テキストを指定文字数で自動改行する。

        Args:
            text: 元のテキスト
            max_chars: 1行の最大文字数（デフォルトはTEXT_MAX_CHARS_PER_LINE）

        Returns:
            改行を含むテキスト
        """
        if max_chars is None:
            max_chars = self.TEXT_MAX_CHARS_PER_LINE

        if len(text) <= max_chars:
            return text

        lines = []
        current_line = ""
        for char in text:
            current_line += char
            if len(current_line) >= max_chars:
                lines.append(current_line)
                current_line = ""
        if current_line:
            lines.append(current_line)

        return "\n".join(lines)

    def _create_text_file(self, text: str, output_dir: Optional[Path] = None, index: int = 0) -> Path:
        """テキストオーバーレイ用のファイルを作成する。

        Args:
            text: 表示するテキスト
            output_dir: 出力ディレクトリ（指定時は永続ファイル、未指定時は一時ファイル）
            index: テキストのインデックス（ファイル名用）

        Returns:
            Path: テキストファイルのパス
        """
        wrapped_text = self._wrap_text(text)  # 自動改行を適用

        if output_dir:
            # 出力ディレクトリに永続ファイルとして作成
            text_path = output_dir / f"_overlay_text_{index:02d}.txt"
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(wrapped_text)
        else:
            # 一時ファイルに作成
            fd, temp_path = tempfile.mkstemp(suffix=".txt")
            with open(fd, "w", encoding="utf-8") as f:
                f.write(wrapped_text)
            text_path = Path(temp_path)

        return text_path

    def compose(
        self,
        audio_path: Path,
        image_paths: List[Path],
        output_path: Path,
        text_overlays: Optional[List[str]] = None,
        language: str = "ja",
        segment_timings: Optional[List[float]] = None,
        video_format: str = "short",
    ) -> Path:
        """音声と画像から動画を合成する。

        Args:
            audio_path: 音声ファイルパス
            image_paths: 画像ファイルパスのリスト
            output_path: 出力動画ファイルパス
            text_overlays: 各画像に表示するテキストのリスト
            language: 言語コード（フォント選択用）
            segment_timings: 各セグメントの開始時刻リスト（SSML Markから取得）
            video_format: 動画形式 ("short" or "long")

        Returns:
            Path: 生成された動画ファイルのパス

        Raises:
            VideoCompositionError: 動画合成に失敗した場合
        """
        # 動画形式に応じて解像度を設定
        # TikTok format uses vertical 1080x1920 like short format
        if video_format == "long":
            output_width = 1920
            output_height = 1080
            format_label = "ロング(1920x1080)"
        else:  # "short" or "tiktok"
            output_width = self.OUTPUT_WIDTH  # 1080
            output_height = self.OUTPUT_HEIGHT  # 1920
            format_label = "TikTok(1080x1920)" if video_format == "tiktok" else "ショート(1080x1920)"

        log_step(f"動画を合成中... ({format_label})", "🎬")

        if not audio_path.exists():
            raise VideoCompositionError(f"音声ファイルが見つかりません: {audio_path}")

        for img_path in image_paths:
            if not img_path.exists():
                raise VideoCompositionError(f"画像ファイルが見つかりません: {img_path}")

        try:
            # Get audio duration
            audio_duration = self._get_audio_duration(audio_path)
            num_images = len(image_paths)

            # Calculate durations for each image
            durations = self._calculate_durations(
                num_images, audio_duration, segment_timings
            )

            # Create concat file with variable durations
            filelist_path = self._create_filelist(image_paths, durations)

            # Run FFmpeg with text overlays
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._run_ffmpeg(
                filelist_path,
                audio_path,
                output_path,
                text_overlays=text_overlays,
                durations=durations,
                num_images=num_images,
                segment_timings=segment_timings,
                audio_duration=audio_duration,
                output_width=output_width,
                output_height=output_height,
            )

            # Cleanup temp file
            filelist_path.unlink(missing_ok=True)

            log_success("動画を合成しました")
            return output_path

        except VideoCompositionError:
            raise
        except Exception as e:
            log_error(f"動画合成エラー: {e}")
            raise VideoCompositionError(f"動画合成に失敗しました: {e}")

    def _calculate_durations(
        self,
        num_images: int,
        audio_duration: float,
        segment_timings: Optional[List[float]] = None,
    ) -> List[float]:
        """各画像の表示時間を計算する。

        Args:
            num_images: 画像の数
            audio_duration: 音声の総時間（秒）
            segment_timings: 各セグメントの開始時刻リスト

        Returns:
            List[float]: 各画像の表示時間リスト
        """
        # タイミング情報がある場合は可変duration
        if segment_timings and len(segment_timings) >= num_images:
            durations = []
            for i in range(num_images):
                start = segment_timings[i]
                # 次のセグメントの開始時刻、または音声の終了時刻を使用
                if i + 1 < len(segment_timings):
                    end = segment_timings[i + 1]
                else:
                    end = audio_duration
                duration = max(end - start, 0.1)  # 最低0.1秒
                durations.append(duration)

            log_step(f"可変タイミングを使用: {[f'{d:.2f}s' for d in durations]}", "⏱️")
            return durations

        # フォールバック: 均等分割
        duration_per_image = audio_duration / num_images
        log_step(f"均等分割を使用: {duration_per_image:.2f}秒/画像", "⏱️")
        return [duration_per_image] * num_images

    def _get_audio_duration(self, audio_path: Path) -> float:
        """音声ファイルの長さを取得する。

        Args:
            audio_path: 音声ファイルパス

        Returns:
            float: 音声の長さ（秒）

        Raises:
            VideoCompositionError: 長さの取得に失敗した場合
        """
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=True,
            )

            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            return duration

        except subprocess.CalledProcessError as e:
            raise VideoCompositionError(f"ffprobeの実行に失敗しました: {e.stderr}")
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise VideoCompositionError(f"音声の長さを解析できませんでした: {e}")

    def _create_filelist(
        self, image_paths: List[Path], durations: List[float]
    ) -> Path:
        """FFmpeg concatデマクサー用のファイルリストを作成する。

        Args:
            image_paths: 画像ファイルパスのリスト
            durations: 各画像の表示時間リスト（秒）

        Returns:
            Path: ファイルリストのパス
        """
        # Create temp file for filelist
        fd, filelist_path = tempfile.mkstemp(suffix=".txt")

        with open(fd, "w", encoding="utf-8") as f:
            for img_path, duration in zip(image_paths, durations):
                # Use forward slashes and escape single quotes
                safe_path = str(img_path.resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
                f.write(f"duration {duration:.3f}\n")

            # Add last image again for smooth ending
            if image_paths:
                safe_path = str(image_paths[-1].resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        return Path(filelist_path)

    def _run_ffmpeg(
        self,
        filelist_path: Path,
        audio_path: Path,
        output_path: Path,
        text_overlays: Optional[List[str]] = None,
        durations: Optional[List[float]] = None,
        num_images: int = 0,
        segment_timings: Optional[List[float]] = None,
        audio_duration: float = 0.0,
        output_width: int = 1080,
        output_height: int = 1920,
    ) -> None:
        """FFmpegコマンドを実行する。

        Args:
            filelist_path: ファイルリストのパス
            audio_path: 音声ファイルパス
            output_path: 出力動画ファイルパス
            text_overlays: 各画像に表示するテキストのリスト
            durations: 各画像の表示時間リスト（秒）
            num_images: 画像の総数
            segment_timings: 各セグメントの開始時刻リスト（SSML Markから取得）
            audio_duration: 音声の総時間（秒）
            output_width: 出力動画の幅（ピクセル）
            output_height: 出力動画の高さ（ピクセル）

        Raises:
            VideoCompositionError: FFmpegの実行に失敗した場合
        """
        # Build video filter
        # fps filter is required to normalize PTS from concat demuxer, ensuring drawtext enable expressions work correctly
        video_filter = (
            f"fps={self.FRAME_RATE},"
            f"scale={output_width}:{output_height}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:black"
        )

        # Add text overlays if provided
        text_files = []  # Track temp files for cleanup
        if text_overlays and len(text_overlays) > 0 and durations:
            try:
                font_path = self._get_japanese_font_path()

                # segment_timingsがある場合は音声タイミングを直接使用
                use_segment_timings = segment_timings and len(segment_timings) >= len(text_overlays)
                if use_segment_timings:
                    log_step(f"音声タイミングを使用: {[f'{t:.2f}s' for t in segment_timings[:len(text_overlays)+1]]}", "🎯")

                # フォールバック用の累積時間
                cumulative_time = 0.0
                for i, text in enumerate(text_overlays):
                    if i >= num_images or i >= len(durations) or not text:
                        if i < len(durations):
                            cumulative_time += durations[i]
                        continue

                    # Create text file in output directory (safer for non-ASCII and ensures persistence during FFmpeg processing)
                    text_file = self._create_text_file(text, output_dir=output_path.parent, index=i)
                    text_files.append(text_file)
                    # Use absolute path and escape for FFmpeg
                    text_file_path = str(text_file.resolve()).replace("\\", "/").replace(":", "\\:")

                    # Calculate timing: 音声タイミングを優先、なければdurationsベース
                    if use_segment_timings and i < len(segment_timings):
                        start_time = segment_timings[i]
                        if i + 1 < len(segment_timings):
                            end_time = segment_timings[i + 1]
                        else:
                            end_time = audio_duration if audio_duration > 0 else start_time + durations[i]
                        log_step(f"テキスト{i+1}: '{text[:15]}...' → {start_time:.2f}s - {end_time:.2f}s", "📝")
                    else:
                        # フォールバック: durationsベースの累積時間
                        start_time = cumulative_time
                        end_time = cumulative_time + durations[i]

                    cumulative_time += durations[i]

                    # Build drawtext filter using textfile for Japanese text
                    # Use between for timing (single comma, properly escaped)
                    enable_expr = f"between(t\\,{start_time:.3f}\\,{end_time:.3f})"
                    log_step(f"  enable式: {enable_expr}", "🔧")
                    video_filter += (
                        f",drawtext="
                        f"fontfile='{font_path}':"
                        f"textfile='{text_file_path}':"
                        f"fontsize={self.TEXT_FONT_SIZE}:"
                        f"fontcolor={self.TEXT_COLOR}:"
                        f"borderw={self.TEXT_BORDER_WIDTH}:"
                        f"bordercolor={self.TEXT_BORDER_COLOR}:"
                        f"shadowcolor={self.TEXT_SHADOW_COLOR}:"
                        f"shadowx={self.TEXT_SHADOW_X}:"
                        f"shadowy={self.TEXT_SHADOW_Y}:"
                        f"box={self.TEXT_BOX}:"
                        f"boxcolor={self.TEXT_BOX_COLOR}:"
                        f"boxborderw={self.TEXT_BOX_BORDER}:"
                        f"line_spacing={self.TEXT_LINE_SPACING}:"
                        f"x=(w-text_w)/2:"
                        f"y={self.TEXT_Y_POSITION}:"
                        f"enable='{enable_expr}'"
                    )
            except VideoCompositionError as e:
                log_error(f"テキストオーバーレイをスキップ: {e}")

        # Build FFmpeg command
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(filelist_path),
            "-i",
            str(audio_path),
            "-vf",
            video_filter,
            "-c:v",
            self.VIDEO_CODEC,
            "-preset",
            self.PRESET,
            "-crf",
            str(self.CRF),
            "-c:a",
            self.AUDIO_CODEC,
            "-b:a",
            self.AUDIO_BITRATE,
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.FRAME_RATE),
            str(output_path),
        ]

        try:
            log_step(f"FFmpegフィルター: {video_filter[:200]}...", "🔧")
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=True, timeout=300
            )
        except subprocess.CalledProcessError as e:
            # Cleanup temp files before raising
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            raise VideoCompositionError(
                f"FFmpegの実行に失敗しました:\nstdout: {e.stdout}\nstderr: {e.stderr}"
            )
        except subprocess.TimeoutExpired:
            # Cleanup temp files before raising
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            raise VideoCompositionError("FFmpegの実行がタイムアウトしました")

        # Cleanup text overlay temp files on success
        for text_file in text_files:
            text_file.unlink(missing_ok=True)
