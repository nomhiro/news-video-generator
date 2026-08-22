"""Video composition using FFmpeg."""

import json
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import ClassVar
from unicodedata import east_asian_width

from PIL import ImageFont
from PIL.ImageFont import FreeTypeFont

from src.models.formats import FormatSpec, get_spec
from src.utils.logger import log_error, log_step, log_success, log_warning


@lru_cache(maxsize=8)
def _load_font(font_path: str, size: int) -> FreeTypeFont:
    """フォントを読む（同じフォントを何度も開かないようにキャッシュする）。

    字幕は1本の動画で6〜10個あり、そのそれぞれで折り返し幅を測るため、
    毎回開くとフォントファイルの読み込みがその回数だけ走る。
    """
    return ImageFont.truetype(font_path, size)


class VideoCompositionError(Exception):
    """Video composition failed."""

    pass


# cgroup のパス。テストから差し替えられるように定数にしておく。
CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def _available_cpus(
    cpu_max: Path = CGROUP_V2_CPU_MAX,
    quota_path: Path = CGROUP_V1_QUOTA,
    period_path: Path = CGROUP_V1_PERIOD,
) -> int:
    """このプロセスが実際に使える CPU 数を返す。

    なぜ `os.cpu_count()` を使わないか
    ---------------------------------
    コンテナの中でも `os.cpu_count()` は**ホストのコア数**を返す
    （実測: Container Apps の 2 vCPU 割り当てに対して 20 を返した）。
    ffmpeg / x264 は既定でこの数だけスレッドを立て、スレッドごとに
    フレームバッファを持つため、割り当てメモリを超えて OOM killer に
    殺される。実際に長尺（1920x1080 / 307秒）が
    `終了コード -9` で落ちた。34秒のショートは通っていたので、
    長い動画でだけ露見する。

    cgroup v2 の `cpu.max`（"quota period" または "max period"）と
    v1 の `cpu.cfs_quota_us` / `cpu.cfs_period_us` を見て、
    割り当てられた CPU 数を求める。

    Args:
        cpu_max: cgroup v2 の cpu.max
        quota_path: cgroup v1 の cpu.cfs_quota_us
        period_path: cgroup v1 の cpu.cfs_period_us

    Returns:
        int: 使える CPU 数（最低1）
    """
    # cgroup v2
    if cpu_max.is_file():
        try:
            quota_text, period_text = cpu_max.read_text().split()
            if quota_text != "max":
                quota, period = int(quota_text), int(period_text)
                if quota > 0 and period > 0:
                    return max(1, quota // period)
        except (OSError, ValueError):
            pass

    # cgroup v1
    if quota_path.is_file() and period_path.is_file():
        try:
            quota = int(quota_path.read_text().strip())
            period = int(period_path.read_text().strip())
            if quota > 0 and period > 0:
                return max(1, quota // period)
        except (OSError, ValueError):
            pass

    # 制限が無い（ローカル実行など）
    return max(1, os.cpu_count() or 1)


def _tail(text: str | None, limit: int = 2000) -> str:
    """標準エラーの末尾だけを返す。

    ffmpeg は進捗を1行ずつ stderr に出すため、全部残すと2万文字を超え、
    UI に出したときに肝心のエラー行が埋もれる（実際に埋もれた）。

    Args:
        text: 標準エラー全体
        limit: 残す文字数

    Returns:
        str: 末尾（切り詰めたことが分かる印を付ける）
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"...(前略 {len(text) - limit}文字)\n{text[-limit:]}"


def mux_audio(
    silent_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    timeout_sec: int,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> None:
    """無音の映像に音声を多重化する。**映像は再エンコードしない。**

    なぜ独立した関数か
    ------------------
    レンダラが2つ（ffmpeg / Remotion）あり、どちらも「無音の映像を作ってから
    音声を混ぜる」という同じ2段構えを取る。コピーすると片方だけ直される日が
    来るので共有する。

    なぜ2段に分けるか
    ------------------
    1回で音声ごと合成していた頃、長尺（1920x1080 / 341秒）が OOM killer に
    殺されていた（終了コード -9）。エンコード速度は 1.04x 出ているのに
    出力サイズが数百フレームぶん変化せず、子プロセスのピーク RSS が
    4,077MB に達していた。マクサーが映像パケットを溜め込んでいる。
    この段は `-c:v copy` なので溜め込む対象が無い（2段構えでピーク617MB）。

    なぜストリームを明示的に選ぶか
    ------------------------------
    **`-map` を省くとナレーションが黙って捨てられる。** ffmpeg の既定の
    ストリーム選択は「入力全体からチャンネル数が最も多い音声を1本」選ぶ。
    Remotion は既定（`enforceAudioTrack`）で**無音のステレオ**トラックを
    焼き込むため、モノラルのナレーション（Azure Speech は 24kHz / 1ch）
    より優先され、第1入力の無音が採用される。

    実害はローカルの生成物で実測した（2026-08-22）。remotion を既定にした
    後の動画5本すべてが mean_volume **-91.0 dB**（= 実質デジタル無音）で、
    音声トラックも尺も解像度もすべて正しいまま**音だけが無い**という
    壊れ方をしていた。`-map 0:v:0 -map 1:a:0` は「映像は第1入力、音声は
    第2入力」を宣言するので、中間ファイルが音声トラックを持つか否かに
    依存しなくなる（ffmpeg 側は `-an`、Remotion 側は持つ、という非対称を
    呼び出し側に意識させない）。

    中間ファイルの削除はここでは**行わない**。失敗時に何を残すかは
    呼び出し側の判断（テキストオーバーレイの一時ファイルなど、
    ここが知らないものも一緒に片付ける必要がある）。

    Args:
        silent_path: 音声を持たない映像
        audio_path: 混ぜる音声
        output_path: 出力先
        timeout_sec: ffmpeg を諦めるまでの秒数
        audio_codec: 音声コーデック
        audio_bitrate: 音声ビットレート

    Raises:
        VideoCompositionError: ffmpeg が失敗、またはタイムアウトした場合
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(silent_path),
        "-i",
        str(audio_path),
        # 既定のストリーム選択に任せない（上の「なぜストリームを明示的に選ぶか」）。
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        audio_bitrate,
        "-shortest",
        str(output_path),
    ]
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_sec,
        )
    except subprocess.CalledProcessError as e:
        # 終了コードを必ず残す。負の値はシグナルで殺されたことを意味し
        # （-9 なら OOM killer の可能性が高い）、stderr の内容だけでは
        # 「エンコードの失敗」と区別できない。
        raise VideoCompositionError(
            f"音声の多重化に失敗しました (終了コード {e.returncode}):\n"
            f"stdout: {_tail(e.stdout)}\nstderr: {_tail(e.stderr)}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise VideoCompositionError(f"音声の多重化が {timeout_sec}秒でタイムアウトしました") from e


class VideoComposer:
    """FFmpegを使用して動画を合成するクラス。

    出力解像度と形式ごとのパラメータは `src/models/formats.py` が持つ。
    """

    FRAME_RATE = 30
    VIDEO_CODEC = "libx264"
    AUDIO_CODEC = "aac"
    AUDIO_BITRATE = "192k"
    CRF = 23
    PRESET = "medium"

    # ffmpeg を諦めるまでの時間。
    #
    # エンコードは実時間より遅い。Container Apps の 1 vCPU では
    # 1080x1920 / preset=medium が実測 0.4x speed だった。
    # 以前の 300秒では、長尺（5分）を作ろうとすると必ずタイムアウトする。
    # 長く取ると本当にハングしたときの発覚が遅れるが、ジョブのリース
    # （15分・heartbeat で延長）が別に見張っている。
    FFMPEG_TIMEOUT_SEC = 1800

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
    # 行間（ピクセル）。drawtext の `line_spacing` は**行送りに加算される**値で、
    # 行の高さそのものではない。1080x1920 / fontsize=64 のとき行送りは実測 88px
    # （字面の高さは 68px）なので、詰めるつもりで -70 を入れると行送りが 18px に
    # なり、2行が重なってまったく読めない動画が出来ていた（実際にそうなっていた）。
    # 合成は成功し尺も解像度も音声も正しいので、ffprobe では気付けない。
    # -12 で行送り 76px。詰まって見えるが字面は重ならない。
    # **フォントサイズを変えたらこの値も測り直す**（絶対値なので追従しない）。
    TEXT_LINE_SPACING = -12
    # 1行の最大文字数（**全角換算**）。上限の幅は
    # `TEXT_MAX_CHARS_PER_LINE * TEXT_FONT_SIZE` ピクセルとして扱う。
    # 14 × 64 = 896px。box の余白（15×2）を足しても 1080px に収まる。
    TEXT_MAX_CHARS_PER_LINE = 14

    # テキストオーバーレイに使う日本語フォントの候補。
    #
    # Windows と Linux の両方を並べる理由: 開発は Windows、コンテナは Linux。
    # 以前は Windows のパスしか持っておらず、Linux コンテナでは
    # 必ず「日本語フォントが見つかりません」で動画合成が失敗していた。
    #
    # 環境変数 VIDEO_FONT_PATH を設定すれば、この一覧より優先される。
    JAPANESE_FONT_CANDIDATES: ClassVar[list[str]] = [
        # Windows
        "C:/Windows/Fonts/YuGothB.ttc",  # Yu Gothic Bold（太めで見やすい）
        "C:/Windows/Fonts/meiryob.ttc",  # Meiryo Bold
        "C:/Windows/Fonts/meiryo.ttc",  # Meiryo
        "C:/Windows/Fonts/YuGothM.ttc",  # Yu Gothic Medium
        "C:/Windows/Fonts/msgothic.ttc",  # MS Gothic
        # Linux（Dockerfile が fonts-noto-cjk を入れる）
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        # macOS
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]

    # フォントを明示指定する環境変数。
    # 候補に無いフォントを使いたい場合や、コンテナイメージの
    # フォント配置が変わった場合の逃げ道。
    FONT_PATH_ENV_VAR = "VIDEO_FONT_PATH"

    def _get_japanese_font_path(self) -> str:
        """使用可能な日本語フォントのパスを取得する。

        Returns:
            str: FFmpeg用にエスケープされたフォントパス

        Raises:
            VideoCompositionError: 使用可能な日本語フォントが見つからない場合
        """
        return self._escape_for_ffmpeg(self._resolve_japanese_font_path())

    def _resolve_japanese_font_path(self) -> str:
        """使用可能な日本語フォントの実パスを返す。

        ffmpeg 用のエスケープと分けている理由: 折り返し幅の実測
        （`_line_width`）で Pillow に同じフォントを読ませるため、
        エスケープしていない生のパスが必要になる。

        Returns:
            str: フォントファイルのパス

        Raises:
            VideoCompositionError: 使用可能な日本語フォントが見つからない場合
        """
        override = os.environ.get(self.FONT_PATH_ENV_VAR)
        candidates = (
            [override, *self.JAPANESE_FONT_CANDIDATES]
            if override
            else list(self.JAPANESE_FONT_CANDIDATES)
        )

        for font_path in candidates:
            if font_path and Path(font_path).exists():
                return font_path

        raise VideoCompositionError(
            "日本語フォントが見つかりません。次のいずれかで解決してください:\n"
            f"  - {self.FONT_PATH_ENV_VAR} にフォントファイルのパスを設定する\n"
            "  - Windows: Meiryo / Yu Gothic / MS Gothic を入れる\n"
            "  - Linux: fonts-noto-cjk を入れる（apt install fonts-noto-cjk）\n"
            f"探索したパス:\n" + "\n".join(f"    {c}" for c in candidates if c)
        )

    @staticmethod
    def _escape_for_ffmpeg(font_path: str) -> str:
        """fontfile の値として ffmpeg のフィルタ式に埋め込める形にする。

        ffmpeg のフィルタ記法ではコロンが引数の区切りなので、
        Windows のドライブレター（C:/...）をそのまま渡すと解釈が壊れる。

        Args:
            font_path: フォントファイルのパス

        Returns:
            str: エスケープ済みのパス
        """
        return font_path.replace("\\", "/").replace(":", "\\:")

    def _line_width(self, text: str) -> float:
        """1行を描いたときの幅をピクセルで見積もる。

        なぜ実フォントを測るか
        ----------------------
        以前は文字数だけで折り返していたため、半角英数が混じった行が
        極端に短くなった（実測 fontsize=64: 全角14文字は 881px だが
        "Anthropicが最強AI" の14文字は 551px）。Yu Gothic や Noto Sans CJK は
        ラテン文字がプロポーショナルなので、幅は文字種だけでも決まらない
        （'A' は 43.7px、'a' 相当は 33px 前後）。

        ffmpeg には「この文字列の幅」を問い合わせる口が無いため、
        drawtext に渡すのと同じ TTF を Pillow に読ませて送り幅を得る。
        実測では ffmpeg の描画幅と1〜2%以内で一致し、常に Pillow 側が
        わずかに大きい（送り幅にサイドベアリングを含むため）。
        はみ出す方向に誤らないので都合がよい。

        Args:
            text: 幅を測る文字列（改行を含まない）

        Returns:
            float: 幅（ピクセル）
        """
        font = self._overlay_font()
        if font is not None:
            return float(font.getlength(text))

        # フォントを読めないときの近似。全角を1文字ぶん、半角を半文字ぶんとして
        # 数える。ラテン大文字では実際より狭く見積もるが、折り返しが
        # 例外で止まるより粗く折り返す方がまだよい。
        return sum(
            self.TEXT_FONT_SIZE * (1.0 if east_asian_width(ch) in "WFA" else 0.5) for ch in text
        )

    def _overlay_font(self) -> FreeTypeFont | None:
        """幅の実測に使うフォントを返す。読めなければ None。

        フォントが見つからない場合、動画合成そのものは字幕を諦めて続行する
        （`compose` が `VideoCompositionError` を捕まえている）。
        折り返しだけが例外で落ちると、その経路まで壊してしまう。
        """
        try:
            return _load_font(self._resolve_japanese_font_path(), self.TEXT_FONT_SIZE)
        except (VideoCompositionError, OSError) as e:
            log_warning(f"フォントを読めないため折り返し幅を近似します: {e}")
            return None

    def _wrap_text(self, text: str, max_chars: int | None = None) -> str:
        """テキストを描画幅で自動改行する。

        `max_chars` は**全角換算**の文字数で、上限の幅は
        `max_chars * TEXT_FONT_SIZE` ピクセルとして扱う。
        全角だけの行では従来の文字数指定と同じ結果になる。

        語の途中で割らない
        ------------------
        英数の連なり（"Anthropic" / "gpt-image-2"）は1つのまとまりとして扱う。
        文字数で折り返していた頃は "Anthro / pic" のように語の途中で
        改行されていた。英語（`-l en`）でも動画を作るのでここは効く。
        1語で1行に収まらない場合だけ文字単位で割る。

        禁則処理
        --------
        `？` や `。` を行頭に置かない（`_break_line`）。実際に生成した動画で
        "Claude vs GPT画像 何が違う" までが1行に入り、`？` だけが
        2行目に落ちた。

        2行のときは幅を均す
        -------------------
        貪欲に詰めると最後の行が極端に短くなる（実際に生成した動画で
        "Claude Opus 5 最新モデル登" / "場" になった）。字幕はほとんどが
        2行なので、2行に収まるときだけ分割位置を選び直す（`_balance_two_lines`）。

        Args:
            text: 元のテキスト
            max_chars: 1行の最大文字数（全角換算。既定は TEXT_MAX_CHARS_PER_LINE）

        Returns:
            改行を含むテキスト
        """
        if max_chars is None:
            max_chars = self.TEXT_MAX_CHARS_PER_LINE

        max_width = max_chars * self.TEXT_FONT_SIZE
        if self._line_width(text) <= max_width:
            return text

        tokens = self._tokenize_for_wrap(text)
        lines = self._greedy_wrap(tokens, max_width)
        if len(lines) == 2:
            lines = self._balance_two_lines(tokens, max_width) or lines

        return "\n".join(lines)

    def _greedy_wrap(self, tokens: list[str], max_width: float) -> list[str]:
        """上限の幅まで詰めて折り返す。"""
        lines: list[str] = []
        current = ""
        for token in tokens:
            # 1つで1行に収まらない語は文字単位に崩す（崩さないとはみ出す）
            pieces = [token] if self._line_width(token) <= max_width else list(token)
            for piece in pieces:
                if not current and piece.isspace():
                    continue  # 行頭の空白は捨てる
                if current and self._line_width(current + piece) > max_width:
                    settled, carry = self._break_line(current, piece, max_width)
                    lines.append(settled)
                    current = carry + ("" if piece.isspace() else piece)
                else:
                    current += piece
        if current.strip():
            lines.append(current.rstrip())
        return lines

    def _balance_two_lines(self, tokens: list[str], max_width: float) -> list[str] | None:
        """2行に割るとき、左右の幅が最も揃う位置で割る。

        貪欲に詰めると1行目を上限まで使うので、2行目が1文字だけになることがある
        （実測: "Claude Opus 5 最新モデル登" / "場"）。分割位置の候補を全部試して
        幅の差が最小のものを選ぶと "Claude Opus 5" / "最新モデル登場" になる。

        候補が無ければ None を返す（貪欲の結果を使う）。1語で1行に収まらない
        テキストは `tokens` を崩さないここでは割れないため、その経路になる。

        Args:
            tokens: `_tokenize_for_wrap` の結果
            max_width: 1行の上限幅

        Returns:
            list[str] | None: 2行、または候補が無ければ None
        """
        best_key: tuple[float, int, int] | None = None
        best_lines: list[str] | None = None

        for i in range(1, len(tokens)):
            first = "".join(tokens[:i]).rstrip()
            second = "".join(tokens[i:]).lstrip()
            if not first or not second:
                continue
            # 禁則は貪欲の場合と同じ基準で見る
            if second[0] in self.LINE_START_FORBIDDEN or first[-1] in self.LINE_END_FORBIDDEN:
                continue
            first_width = self._line_width(first)
            second_width = self._line_width(second)
            if first_width > max_width or second_width > max_width:
                continue

            # 幅の差が同じなら、空白で割れる位置（語の境界）を優先する
            at_space = 0 if tokens[i - 1].isspace() or tokens[i].isspace() else 1
            key = (abs(first_width - second_width), at_space, i)
            if best_key is None or key < best_key:
                best_key, best_lines = key, [first, second]

        return best_lines

    # 行頭に置かない文字（句読点・閉じ括弧・繰り返し記号・長音など）。
    # 直前の1文字を一緒に次の行へ送って避ける（ぶら下げにしない。
    # ぶら下げると上限の幅を超え、フレームからはみ出しうる）。
    LINE_START_FORBIDDEN: ClassVar[frozenset[str]] = frozenset(
        "、。，．,.・:;?!？！）］｝」』】〕〉》”’ー〜%‰℃々ゝゞ"
    )

    # 行末に置かない文字（開き括弧）。次の行へ送る。
    LINE_END_FORBIDDEN: ClassVar[frozenset[str]] = frozenset("（［｛「『【〔〈《“‘")

    def _break_line(self, current: str, piece: str, max_width: float) -> tuple[str, str]:
        """折り返し位置を禁則に合わせてずらす。

        Args:
            current: ここまで積んだ行
            piece: 次の行の先頭に来る予定のもの
            max_width: 1行の上限幅（ずらした結果がはみ出さないことの確認に使う）

        Returns:
            tuple[str, str]: (確定する行, 次の行の頭に送る文字列)
        """
        settled = current.rstrip()
        carry = ""

        # 行頭禁則: 次に来る文字が行頭に置けないなら、直前の1文字を一緒に送る。
        # 1文字しか残らない行を作らないため、2文字以上あるときだけ動かす。
        if piece[:1] in self.LINE_START_FORBIDDEN and len(settled) > 1:
            carry, settled = settled[-1], settled[:-1].rstrip()

        # 行末禁則: 開き括弧が行末に残るなら次の行へ送る。連続することがある。
        while len(settled) > 1 and settled[-1] in self.LINE_END_FORBIDDEN:
            carry, settled = settled[-1] + carry, settled[:-1].rstrip()

        # ずらした結果が次の行に収まらないなら、ずらさない。
        # はみ出す方が読めなくなるので、禁則より幅を優先する。
        if carry and self._line_width(carry + piece) > max_width:
            return current.rstrip(), ""
        return settled, carry

    # 折り返しで途中で割らない「語」。英数の連なりと、その間を繋ぐ記号
    # （"gpt-image-2" / "MAI-Image-2.6" / "don't" を1つとして扱う）。
    WORD_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[0-9A-Za-z]+(?:[-'.’][0-9A-Za-z]+)*")

    def _tokenize_for_wrap(self, text: str) -> list[str]:
        """折り返しの単位に分ける。英数の語はまとめ、それ以外は1文字ずつ。"""
        tokens: list[str] = []
        pos = 0
        for match in self.WORD_PATTERN.finditer(text):
            tokens.extend(text[pos : match.start()])
            tokens.append(match.group())
            pos = match.end()
        tokens.extend(text[pos:])
        return tokens

    def _create_text_file(self, text: str, output_dir: Path | None = None, index: int = 0) -> Path:
        """テキストオーバーレイ用のファイルを作成する。

        Args:
            text: 表示するテキスト
            output_dir: 出力ディレクトリ（指定時は永続ファイル、未指定時は一時ファイル）
            index: テキストのインデックス（ファイル名用）

        Returns:
            Path: テキストファイルのパス
        """
        wrapped_text = self._wrap_text(text)  # 自動改行を適用

        # 改行は必ず LF で書く（`newline=""`）。
        #
        # テキストモードの既定は環境の改行に変換するため、Windows では
        # CRLF になる。drawtext は textfile の CR を行の一部として扱わず、
        # **改行が2つあるものとして空行を1行挟む**（実測: 2行の字幕の
        # 縦幅が 156px → 260px）。開発は Windows でコンテナは Linux なので、
        # 直さないと手元とクラウドで字幕の見た目が変わる。
        if output_dir:
            # 出力ディレクトリに永続ファイルとして作成
            text_path = output_dir / f"_overlay_text_{index:02d}.txt"
            with open(text_path, "w", encoding="utf-8", newline="") as f:
                f.write(wrapped_text)
        else:
            # 一時ファイルに作成
            fd, temp_path = tempfile.mkstemp(suffix=".txt")
            with open(fd, "w", encoding="utf-8", newline="") as f:
                f.write(wrapped_text)
            text_path = Path(temp_path)

        return text_path

    def compose(
        self,
        audio_path: Path,
        image_paths: list[Path],
        output_path: Path,
        text_overlays: list[str] | None = None,
        language: str = "ja",
        segment_timings: list[float] | None = None,
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
        # 解像度は formats.py が単一の情報源
        spec = get_spec(video_format)
        output_width = spec.output_width
        output_height = spec.output_height

        log_step(f"動画を合成中... ({spec.label} {output_width}x{output_height})", "🎬")

        if not audio_path.exists():
            raise VideoCompositionError(f"音声ファイルが見つかりません: {audio_path}")

        for img_path in image_paths:
            if not img_path.exists():
                raise VideoCompositionError(f"画像ファイルが見つかりません: {img_path}")

        try:
            # Get audio duration
            audio_duration = self._get_media_duration(audio_path)
            num_images = len(image_paths)

            # Calculate durations for each image
            durations = self._calculate_durations(num_images, audio_duration, segment_timings)

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

            # 実尺を測って許容範囲に収まっているか確認する。
            # モデルが自己申告する estimated_duration は実尺と一致しない
            # （35と申告して実測59.6秒だった）ため、ここで実測で見る。
            actual = self._get_media_duration(output_path)
            self._warn_if_duration_out_of_range(actual, spec)

            log_success(f"動画を合成しました ({actual:.1f}秒)")
            return output_path

        except VideoCompositionError:
            raise
        except Exception as e:
            log_error(f"動画合成エラー: {e}")
            raise VideoCompositionError(f"動画合成に失敗しました: {e}") from e

    @staticmethod
    def _warn_if_duration_out_of_range(actual_sec: float, spec: FormatSpec) -> None:
        """完成した動画の尺が形式の想定範囲から外れていたら警告する。

        失敗させずに警告に留める理由: 動画自体は再生でき、投稿もできる。
        止めてしまうと生成コスト（画像6枚＋音声）が無駄になる。
        分量の抑制は台本生成側の再生成で行い、ここは最後の観測点にする。

        Args:
            actual_sec: ffprobe で測った実際の秒数
            spec: 形式の仕様
        """
        if spec.max_duration_sec and actual_sec > spec.max_duration_sec:
            log_warning(
                f"{spec.label}の想定尺を超えています: "
                f"{actual_sec:.1f}秒 (上限{spec.max_duration_sec:.0f}秒)"
            )
        elif spec.min_duration_sec and actual_sec < spec.min_duration_sec:
            log_warning(
                f"{spec.label}の想定尺に届いていません: "
                f"{actual_sec:.1f}秒 (下限{spec.min_duration_sec:.0f}秒)"
            )

    def _calculate_durations(
        self,
        num_images: int,
        audio_duration: float,
        segment_timings: list[float] | None = None,
    ) -> list[float]:
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

    def _get_media_duration(self, media_path: Path) -> float:
        """メディアファイルの長さを ffprobe で取得する。

        音声にも動画にも使う。合成後の動画の実尺を測るのは、
        モデルが自己申告する estimated_duration が実尺と一致しないため。

        Args:
            media_path: 音声または動画のファイルパス

        Returns:
            float: 長さ（秒）

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
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )

            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            return duration

        except subprocess.CalledProcessError as e:
            raise VideoCompositionError(f"ffprobeの実行に失敗しました: {e.stderr}") from e
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            raise VideoCompositionError(f"音声の長さを解析できませんでした: {e}") from e

    def _create_filelist(self, image_paths: list[Path], durations: list[float]) -> Path:
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
            # strict=True: 画像と表示時間の数は _calculate_durations で
            # 必ず一致するはずなので、ずれたら黙って画像を落とすのではなく失敗させる
            for img_path, duration in zip(image_paths, durations, strict=True):
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
        text_overlays: list[str] | None = None,
        durations: list[float] | None = None,
        num_images: int = 0,
        segment_timings: list[float] | None = None,
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

                # segment_timingsがある場合は音声タイミングを直接使用。
                # フラグではなく絞り込んだローカル変数に入れることで、
                # 以降のブロックで None ではないことが型として保証される。
                timings: list[float] | None = None
                if segment_timings and len(segment_timings) >= len(text_overlays):
                    timings = segment_timings
                    log_step(
                        f"音声タイミングを使用: {[f'{t:.2f}s' for t in timings[: len(text_overlays) + 1]]}",
                        "🎯",
                    )

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
                    if timings is not None and i < len(timings):
                        start_time = timings[i]
                        if i + 1 < len(timings):
                            end_time = timings[i + 1]
                        else:
                            end_time = (
                                audio_duration if audio_duration > 0 else start_time + durations[i]
                            )
                        log_step(
                            f"テキスト{i + 1}: '{text[:15]}...' → {start_time:.2f}s - {end_time:.2f}s",
                            "📝",
                        )
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

        # --- 2段構えにする理由 ---
        #
        # 以前は「画像 + 音声 → 出力」を1回の ffmpeg で行っていた。これが
        # 長尺（1920x1080 / 341秒）でメモリを食い潰し、OOM killer に
        # 殺されていた（終了コード -9）。
        #
        # ローカルで 2 vCPU / 4Gi の制限を与えて再現したところ、
        # エンコード速度は 1.04x 出ているのに **出力サイズが
        # 数百フレームぶん変化しない**まま、子プロセスのピーク RSS が
        # 4,077MB に達して落ちた。マクサーが書き出さずに映像パケットを
        # 溜め込んでいる（stderr の "buffers queued in out_#0:0" と一致）。
        #
        # そこで、映像だけを作る第1段と、音声を混ぜる第2段に分ける。
        # 第2段は `-c copy` で再エンコードしないため、溜め込む対象が無い。
        silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")
        video_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(filelist_path),
            # 音声はこの段では扱わない
            "-an",
            "-vf",
            video_filter,
            "-c:v",
            self.VIDEO_CODEC,
            "-preset",
            self.PRESET,
            "-crf",
            str(self.CRF),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.FRAME_RATE),
            # 映像の長さは音声に合わせる。concat の最後の画像は
            # 尺を持たないため、指定しないと1フレームで終わる。
            "-t",
            f"{audio_duration:.3f}",
            # スレッド数を割り当て CPU 数に合わせる。
            #
            # 既定（0 = 自動）だと ffmpeg はホストのコア数だけスレッドを
            # 立てる。コンテナの割り当てを見ないので、2 vCPU の環境で
            # 20 スレッドが動き、スレッドごとのフレームバッファで
            # メモリを食い潰して OOM killer に殺される
            # （実測: 長尺 1920x1080 / 307秒 が 終了コード -9 で落ちた）。
            "-threads",
            str(_available_cpus()),
            str(silent_path),
        ]

        try:
            log_step(f"FFmpegフィルター: {video_filter[:200]}...", "🔧")
            log_step(f"FFmpegスレッド数: {_available_cpus()}", "🧵")
            log_step("映像を作成中（音声なし）...", "🎞️")
            # check=True なので失敗時は例外になる。戻り値は使わない。
            subprocess.run(
                video_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=self.FFMPEG_TIMEOUT_SEC,
            )
            log_step("音声を多重化中（映像は再エンコードしない）...", "🔉")
            mux_audio(
                silent_path,
                audio_path,
                output_path,
                timeout_sec=self.FFMPEG_TIMEOUT_SEC,
                audio_codec=self.AUDIO_CODEC,
                audio_bitrate=self.AUDIO_BITRATE,
            )
            silent_path.unlink(missing_ok=True)
        except VideoCompositionError:
            # mux_audio が投げた場合。メッセージは既に整形されているので
            # 包み直さず、中間ファイルだけ片付けて上へ流す。
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
            raise
        except subprocess.CalledProcessError as e:
            # Cleanup temp files before raising
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
            # 終了コードを必ず残す。負の値はシグナルで殺されたことを意味し
            # （-9 なら OOM killer の可能性が高い）、stderr の内容だけでは
            # 「エンコードの失敗」と区別できない。実際にコンテナ上で困った。
            raise VideoCompositionError(
                f"FFmpegの実行に失敗しました (終了コード {e.returncode}):\n"
                f"stdout: {_tail(e.stdout)}\nstderr: {_tail(e.stderr)}"
            ) from e
        except subprocess.TimeoutExpired as e:
            # Cleanup temp files before raising
            for text_file in text_files:
                text_file.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
            # TimeoutExpired はタイムアウト値と部分出力を持つので連結して残す
            raise VideoCompositionError(
                f"FFmpegの実行が {self.FFMPEG_TIMEOUT_SEC}秒でタイムアウトしました"
            ) from e

        # Cleanup text overlay temp files on success
        for text_file in text_files:
            text_file.unlink(missing_ok=True)
