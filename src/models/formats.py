"""動画形式ごとのパラメータの単一の情報源。

なぜこのモジュールがあるか
--------------------------
形式ごとの値がコードベース中に散らばっていた。

- 出力解像度: video_composer.py の分岐
- 話速: pipeline.py の辞書リテラル
- 生成画像のアスペクト比: image_generator.py の三項演算子
- セグメント数と目標文字数: 6種類のプロンプト文字列に「6個」「250〜300文字」
  などと直接埋め込まれていた

散っていると整合が取れなくなる。実測では、プロンプトに「合計250〜330文字」と
書いてあるのに 484文字（47%超過）が返り、35秒目標の動画が59.6秒になった。
さらに ``estimated_duration`` はモデルの自己申告で、実尺と一致しない。

ここに集約し、プロンプトはこの定義から生成する。文字数の上限は
スキーマのバリデータで強制し、最終的な尺は ffprobe の実測で検証する。
「お願いする」のではなく「守らせる」構造にする。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VideoFormat(StrEnum):
    """生成する動画の形式。"""

    SHORT = "short"
    TIKTOK = "tiktok"
    LONG = "long"


@dataclass(frozen=True)
class FormatSpec:
    """1形式ぶんのパラメータ。

    Attributes:
        output_width: 完成動画の幅
        output_height: 完成動画の高さ
        image_size: 画像生成に要求するサイズ。``gpt-image-2`` は両辺が
            16の倍数であることを要求するため、出力解像度をそのまま
            使えない（1080 は16の倍数でない）。アスペクト比は出力と
            厳密に一致させ、ffmpeg 側は縮小のみで済むようにする
        segment_count: ナレーションのセグメント数。画像枚数と字幕数もこれに一致する
        chars_per_segment: 1セグメントあたりの目標文字数（日本語）
        words_per_segment: 1セグメントあたりの目標語数（英語）
        speaking_rate: TTS の話速
        min_duration_sec: 完成動画の最小の長さ。0 なら下限なし
        max_duration_sec: 完成動画の最大の長さ。0 なら上限なし
        label: ログ表示用の名前
    """

    output_width: int
    output_height: int
    image_size: str
    segment_count: int
    chars_per_segment: tuple[int, int]
    words_per_segment: tuple[int, int]
    speaking_rate: float
    min_duration_sec: float
    max_duration_sec: float
    label: str

    @property
    def total_chars(self) -> tuple[int, int]:
        """ナレーション全体の目標文字数（日本語）。"""
        low, high = self.chars_per_segment
        return low * self.segment_count, high * self.segment_count

    @property
    def total_words(self) -> tuple[int, int]:
        """ナレーション全体の目標語数（英語）。"""
        low, high = self.words_per_segment
        return low * self.segment_count, high * self.segment_count

    @property
    def aspect_label(self) -> str:
        """アスペクト比の表示（"9:16" など）。"""
        return "9:16" if self.output_height > self.output_width else "16:9"

    def char_budget(self, language: str) -> tuple[int, int]:
        """言語に応じた全体の分量の許容範囲を返す。

        英語は語数で数えるが、超過検出は文字数で行うため
        1語あたり約6文字（空白込み）として換算する。

        Args:
            language: 言語コード ("ja" or "en")

        Returns:
            (下限, 上限)
        """
        if language == "ja":
            return self.total_chars
        low, high = self.total_words
        return low * 6, high * 6


# 分量は目標尺から逆算する。読み上げ速度の実測値は
# src/models/script.py の estimate_duration_sec を参照（日本語 6.0 文字/秒、
# 英語 2.6 語/秒）。tests/test_formats.py が
# 「目標文字数から推定される尺が min/max の範囲に収まること」を検証するので、
# ここを変えるときは尺の範囲との整合も確認される。
SPECS: dict[VideoFormat, FormatSpec] = {
    # 約35秒が目標。6セグメント × 30〜40文字 = 180〜240文字 → 30〜40秒
    VideoFormat.SHORT: FormatSpec(
        output_width=1080,
        output_height=1920,
        image_size="1152x2048",  # 厳密な 9:16
        segment_count=6,
        chars_per_segment=(30, 40),
        words_per_segment=(13, 17),
        speaking_rate=1.25,
        min_duration_sec=20.0,
        max_duration_sec=60.0,
        label="ショート",
    ),
    # 60〜90秒が目標。6セグメント × 60〜88文字 = 360〜528文字 → 60〜88秒
    VideoFormat.TIKTOK: FormatSpec(
        output_width=1080,
        output_height=1920,
        image_size="1152x2048",  # 厳密な 9:16
        segment_count=6,
        chars_per_segment=(60, 88),
        words_per_segment=(26, 38),
        speaking_rate=1.15,
        min_duration_sec=55.0,
        max_duration_sec=100.0,
        label="TikTok",
    ),
    # 約5〜7分。10セグメント × 200〜250文字 = 2,000〜2,500文字 → 333〜417秒
    #
    # 収益化の観点では8分以上（ミッドロール広告の境界）が望ましく、
    # その場合は segment_count=16 / chars_per_segment=(200, 250) で
    # 3,200〜4,000文字にする。ただし画像も16枚必要になり、
    # gpt-image-2 の既定クォータ 5 images/min では約4分待つことになる。
    # クォータを引き上げてから切り替える。
    VideoFormat.LONG: FormatSpec(
        output_width=1920,
        output_height=1080,
        image_size="2048x1152",  # 厳密な 16:9
        segment_count=10,
        chars_per_segment=(200, 250),
        words_per_segment=(87, 108),
        speaking_rate=1.1,
        min_duration_sec=280.0,
        max_duration_sec=420.0,
        label="ロング",
    ),
}


def get_spec(video_format: str | VideoFormat) -> FormatSpec:
    """形式名から仕様を取得する。

    未知の形式は SHORT として扱う。CLI は choices で制限しているが、
    Web のフォームからは任意の文字列が来る可能性がある。

    Args:
        video_format: 形式名

    Returns:
        FormatSpec: 対応する仕様
    """
    try:
        key = VideoFormat(video_format)
    except ValueError:
        key = VideoFormat.SHORT
    return SPECS[key]
