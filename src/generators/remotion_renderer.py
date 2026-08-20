"""Remotion（React）で動画を描くレンダラ。

なぜ ffmpeg の concat ではなくブラウザで描くか
--------------------------------------------
図解主体に振ったので、描く対象は「画像」ではなく「構造」になった。
React で描けば文字は常に正確で、回ごとのブレも無く、`gpt-image-2` の
クォータも消費しない。モーション（要素が順に現れる、次の図へ移る）も
ffmpeg のフィルタグラフより桁違いに書きやすく、**何よりデザインを詰める
反復が速い**（ブラウザで見ながら直せる）。

実測（2026-08-17、2 vCPU / 4Gi / concurrency 2）
------------------------------------------------
1080x1920 / 30fps / 35秒（1050フレーム）で **199秒・ピーク1,915MB**。
OOM は起きない。ただし全画面 `filter: blur()` を入れた版は 598秒（3倍）に
なった。**デザイン側の制約**として `remotion/src/` に書いてある。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from src.generators.script_generator import chapter_labels
from src.generators.video_composer import _available_cpus, _tail, mux_audio
from src.models.formats import get_spec
from src.models.scene import IllustrationConcept, SceneVisual
from src.utils.line_break import insert_break_opportunities
from src.utils.logger import log_error, log_step, log_success, log_warning


class RemotionRenderError(Exception):
    """Remotion のレンダリングに失敗した。"""


# 挿絵1枚の固定スタイル文。**ここが単一の情報源。**
#
# `src/social/card_visual.py` の `CARD_STYLE_PROMPT` を再利用しない理由:
# あちらの地は off-white の紙で、`remotion/src/theme.ts` の暗い地
# （`COLORS.bg = "#1b1a1d"`）の上に紙色の挿絵を貼ると「貼り付けた画像」に
# 見える。この動画の地の暗さはオーナーが選んだ既定であり、挿絵側がそれに
# 合わせる。配色はテーマの実際の HEX 値（`COLORS.accent` / `COLORS.accent2`）
# から取っており、コードとデザインを別々に触ると必ずずれるので、値を変えたら
# ここも直す。
#
# **手描きチョーク調から、フラットな概念図に変えた。** 実際にレンダリングした
# 動画をオーナーが見て「スケッチっぽすぎて、一目で何を言っているか分からない」
# という判断を受けた。原因は質感だけでなく**内容**にもあった——「エキスパートを
# 選んでルーティングし、コストを1/10にする」という記事に対して、当時の自由文
# プロンプト（旧 `illustration_subject`）はオフィスで働く人々・コーヒー・
# 観葉植物・丸いアイコン4つを描いた。「AIっぽい何か」にしかならない。
# だから2つを同時に変える: スタイルをフラットにし、主題を
# `IllustrationConcept` の構造で強制する。
#
# 制約に「文字を一切描かない」に加え、**付随物（コーヒー・観葉植物・部屋など）
# を明示的に禁じる**。以前は禁じていなかったため、モデルは主題に触れつつ
# 「場面」を描いた。「これは場面ではない」と言い切ることで、2要素以外を
# 描く余地を塞ぐ。
#
# **2026-08-17: `left`/`right`/`relation` を捨てて構図まで固定した経緯。**
# 3語構造にしても、実際に生成した挿絵は「3人の人物ピクトグラム＋オレンジの
# 矢印＋CPUチップ」だった（`IllustrationConcept` のdocstring参照）。語の
# 選び方の問題（人物・抽象量・矢印への収束）は語を差し替えても直らない——
# **「2つの異なる物を矢印で繋ぐ」という構図自体が凡庸**で、内容に関わらず
# 同じ形の絵しか作れないため。だから構図の指示（Composition mandate）を
# ここに明文化し、「人物を一切描かない」「アクセントカラーは強調部分だけ」
# 「矢印は最後の手段」を固定する側の権威にする。
#
# **2026-08-20: 文字の全面禁止を解いて「名札付きの説明図」にした経緯。**
# 3語構造（unit/field/emphasis）で生成した実物は「10本の棒のうち1本だけが
# ティール」で、構図としては成立していたが**何の話か分からなかった**。
# オーナーの判断は「概念図すぎる。全体解説図にすべき」。
#
# 抽象に振れた原因はここにあった——スタイル文が
# `no text, letters, or numerals anywhere` で文字を全面禁止していたため、
# 「これが何か」を示す手段が構図しか残っておらず、モデルは意味を運べる
# 最小の形（比率のモチーフ）に収束するしかなかった。**説明図は本質的に
# 名札を必要とする。**
#
# そこで禁止を「短い日本語の名札は可、ただし数字と文は禁止」に緩める。
# 日本語で描かせる根拠は `CardVisual._labels_must_be_short`——2026-08-16 に
# 実画像で字形の正確さとスマホでの可読性を確認しており、「英大文字のみ」
# という以前の前提は誤りだと分かっている。
#
# **数字だけは禁じ続ける。** カードでは記事に無い「¥980」が絵に描かれた
# （880c95f）。いまの接地検査はシーンのラベルにしか効いておらず、挿絵は
# 検査の対象外なので、描かせない方が安全である。
#
# 構図の禁止も併せて緩める。「2つの異なる物を矢印で繋ぐな」は抽象モチーフを
# 守るための制約だったが、仕組みを説明する図では接続そのものが内容を運ぶ。
# 代わりに「1つの仕組みを1枚で」「必要な部分だけ」で歯止めをかける。
ILLUSTRATION_STYLE_PROMPT = """Medium: flat conceptual diagram — a clean explanatory figure of the kind a
  technical whitepaper would print. Solid fills, crisp edges, uniform line
  weight. NO texture, NO grain, NO visible brush or chalk strokes, NO
  sketchiness, NO hand-drawn wobble.
Ground: dark charcoal (#1b1a1d). Palette strictly limited to off-white
  (#f5f2ea), one teal (#2dd4bf), and one amber (#f2a93c) on that ground.
  Flat fills only — no gradients, no shadows, no 3D, no perspective.
Composition: ONE mechanism explained in ONE diagram, centred, front-on flat
  view, generous margins. Show its named parts and how they relate, so a
  reader grasps how the thing works from the figure alone. Connections
  (a line, an arrow, a nesting, a branch) are allowed when they carry the
  mechanism — but draw only the parts the explanation needs. One idea only —
  no comic panels, no multi-step timeline, no repeated variants of the same
  figure.
Accent discipline: the accent colour (teal or amber) marks ONLY the part the
  explanation turns on. Every other shape stays dim or off-white. Spreading
  the accent decoratively across unrelated shapes makes the colour carry no
  meaning and reads as clip art.
Typography: labels in this image MUST be Japanese, rendered accurately and
  large enough to read on a phone. Correct Japanese glyphs matter more than
  decoration — do not invent, distort, or romanise characters. Use a clean
  geometric Japanese sans-serif. Place each label beside the part it names.
  Keep the total amount of text small: only the short labels supplied below,
  and no sentence, caption, title, or paragraph anywhere.
Requirement: a reader must see what the mechanism is within one second at
  phone size.
Constraints: NO numerals or digits of any kind, and no invented figure — no
  prices, percentages, dates, version numbers, counts, or statistics. If a
  quantity matters, express it by the shapes themselves, never by writing a
  number. No watermarks, no logos, no UI chrome, no photorealism.
  NO human figures of any kind — no real people, no human pictograms,
  no silhouettes, no avatars; a pictogram of a person is still a person
  for this rule.
  NO incidental props — no cups, plants, desks, chairs, rooms, or
  environments. It is not a scene. Do NOT depict an abstract quantity
  (efficiency, cost, performance, "reduced compute") as an object — draw
  only shapes that literally exist."""

# 挿絵の生成サイズ。**動画の出力解像度（`FormatSpec.image_size`）とは無関係に
# 固定する。** 挿絵は縦画面（short/tiktok）でも横画面（long）でも同じ帯
# （`remotion/src/zones.ts` の `shared.illustration`、1080x920 ≒ 1.174:1）に
# 表示するので、生成サイズもその帯のアスペクト比に合わせるべきで、動画の
# アスペクト比（9:16 や 16:9）に合わせるべきではない。
#
# 以前は `FormatSpec.image_size`（9:16 の 1152x2048）で生成していた。
# 帯（1080x920）に収めるには縦方向の55%しか使わず、残り45%は捨てていた。
# たまたま被写体が画像の中央55%に収まっていたので破綻していなかったが、
# それは運であって保証ではない（実物で確認して気付いた）。
#
# `gpt-image-2` の制約（`validate_size()` 参照: 両辺16の倍数、長辺3840以下、
# アスペクト比3:1以下、総ピクセル数655,360〜8,294,400）を満たしつつ、
# 帯のアスペクト比（1080/920 ≈ 1.1739）に最も近い値を選んでいる
# （1216/1040 ≈ 1.1692）。**帯の寸法を変えたら、この値も一緒に見直す**
# （ここが単一の情報源では無いので、`zones.ts` の値とは別々にメンテする
# 必要がある——`illustration_concept` の主題自体は言語モデルに出させているため、
# ビルド時に TS 側の値をここへ自動で伝える手段が無い）。
ILLUSTRATION_SIZE = "1216x1040"


def build_illustration_prompt(concept: IllustrationConcept) -> str:
    """gpt-image-2 に渡す挿絵プロンプトを組む。

    `src/social/card_visual.py` の `build_card_prompt` と同じ二段構え
    （LLM が *what*、コード側が *how* を前置する）。構造も同じ形に
    寄せてあるので、組み立て方も揃える——揃えておけば、片方で見つかった
    壊れ方をもう片方に移しやすい。

    カードとの唯一の違いは `caption_ja` が無いこと。動画では見出しと字幕を
    Remotion が絵の下に描くので、絵にも1行を描かせると同じ主張が2回出る
    （`IllustrationConcept` のdocstring参照）。

    Args:
        concept: `Script.illustration_concept`（主題・視覚要素・日本語の名札）

    Returns:
        str: 固定のスタイル文を前置したプロンプト
    """
    parts = [ILLUSTRATION_STYLE_PROMPT, f"Subject: {concept.subject}"]
    parts.append("Key details: " + "; ".join(concept.key_details))
    if concept.labels:
        quoted = ", ".join(f'"{label}"' for label in concept.labels)
        parts.append(
            f"Labels: render exactly these Japanese words, {quoted}, in a clean "
            "geometric Japanese sans-serif placed beside the element each one "
            "names. Render no other text of any kind."
        )
    else:
        # 名札なしを明示する。書かないと、モデルは「説明図」という指示から
        # 勝手に見出しや注釈を書き足す（カードでも同じ理由で none を明記して
        # いる）。
        parts.append("Labels: none. Render no text of any kind.")
    return "\n".join(parts)


def resolve_frame_spans(
    segment_timings: list[float],
    audio_duration_sec: float,
    fps: int,
    count: int,
) -> list[tuple[int, int]]:
    """セグメントの開始秒を、シーンごとのフレーム範囲に解く。

    なぜ Python 側で解くか
    ----------------------
    単調増加の強制と「タイミングの要素数はセグメント数+1（末尾は音声全体の
    終了時刻）」という契約は、既に `voice_generator` / `video_composer` 側に
    ある。同じ計算を TypeScript に持たせると、必ず片方だけ直される日が来る。
    React には**解決済みのフレーム範囲だけ**を渡す。

    丸めの罠
    --------
    各開始秒を独立に丸めると、近接したタイミングで**長さ0や負のシーン**が
    できる。現行の `_calculate_durations` が `max(end - start, 0.1)` で
    守っているのと同じ場所で、ここでは境界を厳密に増加させることで
    最低1フレームを保証する。崩れると Remotion は例外を出さず、
    シーンが飛んだ動画を黙って作る。

    Args:
        segment_timings: 各セグメントの開始秒（末尾は音声全体の終了秒）。
            要素数が `count + 1` に足りなければ均等割りにフォールバックする
        audio_duration_sec: ffprobe で測った音声の長さ
        fps: フレームレート
        count: シーン数（1以上）

    Returns:
        list[tuple[int, int]]: (開始フレーム, 表示フレーム数)。要素数は count。
        隙間も重なりも無く、合計は全体のフレーム数に一致する

    Raises:
        ValueError: count が1未満の場合
    """
    if count < 1:
        raise ValueError(f"シーン数は1以上でなければなりません: {count}")

    # 全体のフレーム数。シーン数を下回らせない（各シーンに最低1フレーム要る）。
    total = max(count, round(audio_duration_sec * fps))

    if len(segment_timings) >= count + 1:
        raw = [t * fps for t in segment_timings[: count + 1]]
    else:
        # bookmark が取れなかった場合のフォールバック。均等割り。
        step = total / count
        raw = [i * step for i in range(count)] + [float(total)]

    # 境界を整数にし、**厳密に増加**させる。i 番目の境界は「後続の
    # シーンに最低1フレームずつ残せる位置」より後ろには置かない。
    bounds = [0] * (count + 1)
    bounds[count] = total
    for i in range(count):
        lower = bounds[i - 1] + 1 if i > 0 else 0
        upper = total - (count - i)
        bounds[i] = min(max(round(raw[i]), lower), upper)

    return [(bounds[i], bounds[i + 1] - bounds[i]) for i in range(count)]


# Remotion プロジェクトの場所。リポジトリ直下の remotion/。
# CSS 用の ../package.json とは別パッケージ（あちらは devDependencies だけで
# 「実行時に Node は不要」が前提。混ぜるとその前提が壊れる）。
REMOTION_DIR = Path(__file__).resolve().parents[2] / "remotion"

# Remotion のエントリとコンポジション ID。remotion/src/ 側と一致させる。
ENTRY_POINT = "src/index.ts"
COMPOSITION_ID = "NewsVideo"


class RemotionRenderer:
    """Remotion で動画を描くレンダラ。

    **無音の映像までしか作らない。** 音声の多重化は `mux_audio` に任せる。
    Remotion 内で `<Audio>` を使って1発で作る形は選べない — 1回で音声ごと
    合成していた頃、マクサーが映像パケットを溜め込んでピーク RSS 4,077MB で
    OOM killer に殺された実測がある。検証済みの2段構えを崩さない。
    """

    FRAME_RATE = 30

    # Remotion を諦めるまでの秒数。
    #
    # 実測199秒（2 vCPU / blur なし / 35秒の動画）の4.5倍。
    # `VideoComposer.FFMPEG_TIMEOUT_SEC`（1800秒）を流用しない — Remotion の
    # 実測に対して緩すぎ、本当にハングしたときの発覚が遅れる。
    #
    # ジョブのリース（既定15分 = 900秒）とほぼ同じ長さになるが問題にならない。
    # `JobWorker._start_heartbeat` が独立した daemon スレッドでリースを
    # 延ばし続けるため、ここでブロックしていてもリースは切れない
    # （src/jobs/worker.py:211）。**そこを同期処理に変えると前提が崩れる。**
    TIMEOUT_SEC = 900

    # 動画全体で共有する挿絵1枚だけで足りる。これが `Pipeline` が
    # `gpt-image-2` の呼び出しを1本あたり6回から1回に減らせる根拠。
    def image_count(self, segment_count: int) -> int:
        """セグメント数に関わらず、共有する挿絵は常に1枚。"""
        return 1

    # シーンのラベルを実際に画面へ描く。これが `ScriptGenerator` に数値の根拠を
    # **強制させる**根拠（記事に無い数値が画面に出るのは、ニュースを扱う以上
    # 最も害が大きい種類の誤り）。`image_count` とは別の問いなので別に持つ。
    draws_scene_text = True

    def render(
        self,
        *,
        audio_path: Path,
        output_path: Path,
        image_paths: list[Path],
        scenes: list[SceneVisual],
        text_overlays: list[str],
        segment_narrations: list[str],
        segment_timings: list[float],
        language: str,
        video_format: str,
        illustration_path: Path | None = None,
    ) -> Path:
        """図解の構造から動画を作る。

        Args:
            audio_path: ナレーション音声
            output_path: 出力する動画のパス
            image_paths: 使わない（`FfmpegRenderer` と契約を揃えるため受ける）
            scenes: 各セグメントの図解の構造
            text_overlays: 各シーンの見出し
            segment_narrations: 各シーンの字幕
            segment_timings: 各セグメントの開始秒（末尾は音声の終了秒）
            language: 日本語の折り返し位置（ZWSP）を挿入するかの判定に使う
                （フォントはシステムのものを font-family で選ぶため未使用）
            video_format: 形式名。解像度は formats.py が持つ
            illustration_path: 動画全体で共有する挿絵。`None` なら
                地のみで描く（生成に失敗した場合もここに `None` が渡る。
                装飾的な要素の欠落でレンダリング本体を落とさない）

        Returns:
            Path: 生成された動画のパス

        Raises:
            RemotionRenderError: 配列長の不一致、または Remotion の失敗
        """
        counts = {
            "scenes": len(scenes),
            "text_overlays": len(text_overlays),
            "segment_narrations": len(segment_narrations),
        }
        if len(set(counts.values())) != 1:
            detail = ", ".join(f"{k}={v}" for k, v in counts.items())
            raise RemotionRenderError(f"配列長の不一致: {detail}")
        if not scenes:
            raise RemotionRenderError("シーンが空です")
        if not audio_path.exists():
            raise RemotionRenderError(f"音声ファイルが見つかりません: {audio_path}")

        spec = get_spec(video_format)
        audio_duration = self._audio_duration(audio_path)
        spans = resolve_frame_spans(segment_timings, audio_duration, self.FRAME_RATE, len(scenes))
        total_frames = spans[-1][0] + spans[-1][1]

        log_step(
            f"動画を描画中... ({spec.label} {spec.output_width}x{spec.output_height}, "
            f"{len(scenes)}シーン, {total_frames}フレーム)",
            "🎬",
        )

        # 章ラベル（フック/事実/仕組み/インパクト/結論）は segment_index から
        # 一意に決まる構造的な事実なので、LLM には出させず1回だけ計算する。
        chapters = chapter_labels(len(scenes), language)

        # 挿絵は全シーンで共有する1枚なので、シーンごとの辞書ではなく
        # トップレベルの props に持たせる（`width` / `height` と同じ階層）。
        illustration_filename = self._place_illustration(illustration_path, output_path)

        props = {
            "width": spec.output_width,
            "height": spec.output_height,
            "fps": self.FRAME_RATE,
            "durationInFrames": total_frames,
            "illustration": illustration_filename,
            "scenes": [
                {
                    "layout": scene.layout.value,
                    "items": scene.items,
                    # relation と chapter は8文字以内の単一ラベルで折り返さないので、
                    # headline/subtitle と違い ZWSP を入れない。
                    "relation": scene.relation,
                    "chapter": chapter,
                    # ZWSP の挿入はここ（レンダリング直前）で行う。
                    # `MAX_HEADLINE_CHARS` は script.py が生成時点の文字数を
                    # 検証しており、この挿入より前に済んでいるので文字数には影響しない。
                    "headline": insert_break_opportunities(headline, language),
                    "subtitle": insert_break_opportunities(subtitle, language),
                    "fromFrame": start,
                    "durationInFrames": duration,
                }
                # 長さは上で一致を確認済みなので strict=True で守る
                for scene, chapter, headline, subtitle, (start, duration) in zip(
                    scenes, chapters, text_overlays, segment_narrations, spans, strict=True
                )
            ],
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        props_path = output_path.with_name(f"{output_path.stem}_props.json")
        silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")

        try:
            props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
            self._run_remotion(props_path, silent_path)
            log_step("音声を多重化中（映像は再エンコードしない）...", "🔉")
            mux_audio(
                silent_path,
                audio_path,
                output_path,
                timeout_sec=self.TIMEOUT_SEC,
            )
        finally:
            # 中間ファイルは成功時・失敗時ともに消す。残すと生成物が2倍になり、
            # Blob にも余計なものが上がる（*_silent.mp4 と同じ扱い）。
            props_path.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
            # 挿絵も同じ扱い。remotion/public/ はコミット対象のディレクトリ
            # なので、レンダリングごとに増えたままだと蓄積する。
            if illustration_filename:
                (self._public_dir() / illustration_filename).unlink(missing_ok=True)

        log_success(f"動画を描画しました ({audio_duration:.1f}秒)")
        return output_path

    @staticmethod
    def _public_dir() -> Path:
        """Remotion が `staticFile()` で読む `public/` ディレクトリ。"""
        return REMOTION_DIR / "public"

    def _place_illustration(self, illustration_path: Path | None, output_path: Path) -> str:
        """挿絵を `remotion/public/` へ置き、`staticFile()` 用のファイル名を返す。

        `staticFile()` は `public/` からの相対名しか受け取らないため、
        Pipeline が作業ディレクトリに生成したファイルをそのまま渡せない
        （絶対パスでは解決できない）。ファイル名は `output_path.stem`
        （呼び出し元がタイムスタンプ+言語で一意にしている）から作るので、
        並行するレンダリングとも衝突しない。

        **挿絵の欠落・生成失敗でレンダリングを落とさない**。章ラベルと
        同じ判断で、装飾的な要素のために本体を失敗させるのは本末転倒。

        Args:
            illustration_path: Pipeline が生成した挿絵。`None` なら未生成
            output_path: 出力する動画のパス（ファイル名の元にする）

        Returns:
            str: `remotion/public/` に置いたファイル名。置けなかった場合は
                空文字列（React 側はこれを「地のみで描く」と解釈する）
        """
        if illustration_path is None:
            return ""
        if not illustration_path.exists():
            log_warning(f"挿絵が見つかりません。地のみで続行します: {illustration_path}")
            return ""

        try:
            public_dir = self._public_dir()
            public_dir.mkdir(parents=True, exist_ok=True)
            filename = f"illustration-{output_path.stem}{illustration_path.suffix}"
            shutil.copyfile(illustration_path, public_dir / filename)
            return filename
        except OSError as e:
            log_warning(f"挿絵の配置に失敗しました。地のみで続行します: {e}")
            return ""

    def _run_remotion(self, props_path: Path, silent_path: Path) -> None:
        """Remotion の CLI を呼んで無音の映像を作る。

        props を**ファイル経由**で渡す理由: `--props` に JSON を直接書くと、
        6セグメントぶんの日本語データで Windows のコマンドライン長上限
        （8,191文字）に当たる。

        Args:
            props_path: props の JSON ファイル
            silent_path: 出力する無音の映像

        Raises:
            RemotionRenderError: Remotion が失敗、またはタイムアウトした場合
        """
        # Windows では npx は npx.cmd なので、shell=False では解決できない。
        npx = shutil.which("npx")
        if npx is None:
            raise RemotionRenderError("npx が PATH にありません（Node 22 以上が必要です）")

        concurrency = _available_cpus()
        cmd = [
            npx,
            "remotion",
            "render",
            ENTRY_POINT,
            COMPOSITION_ID,
            str(silent_path.resolve()),
            f"--props={props_path.resolve()}",
            # **必ず明示する。** 既定は「ホストの CPU スレッド数の半分」で
            # cgroup を見ないため、2 vCPU の割り当てに10スレッドが立つ
            # （os.cpu_count() で踏んだ罠と同じ構造）。
            f"--concurrency={concurrency}",
            "--log=info",
        ]

        log_step(f"Remotion の並行数: {concurrency}", "🧵")
        try:
            subprocess.run(
                cmd,
                cwd=REMOTION_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=self.TIMEOUT_SEC,
            )
        except FileNotFoundError as e:
            raise RemotionRenderError(
                f"Remotion のプロジェクトが見つかりません: {REMOTION_DIR}"
            ) from e
        except subprocess.CalledProcessError as e:
            log_error(f"Remotion のレンダリングに失敗しました（終了コード {e.returncode}）")
            # 終了コードを必ず残す。負の値はシグナルで殺されたことを意味する
            # （-9 なら OOM killer の可能性が高い）。
            raise RemotionRenderError(
                f"Remotion のレンダリングに失敗しました (終了コード {e.returncode}):\n"
                f"stdout: {_tail(e.stdout)}\nstderr: {_tail(e.stderr)}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RemotionRenderError(
                f"Remotion のレンダリングが {self.TIMEOUT_SEC}秒でタイムアウトしました"
            ) from e

    def _audio_duration(self, audio_path: Path) -> float:
        """音声の長さを ffprobe で測る。

        `VideoComposer._get_media_duration` と同じことをするが、単独関数では
        なく**インスタンスメソッド**にしている。テストがここを
        `lambda self, path: 3.0` で差し替えるため（`monkeypatch.setattr` は
        クラス属性を直接書き換えるので、`self` を取らない `staticmethod` には
        `self` 引数が渡らずシグネチャが合わない）。

        Args:
            audio_path: 音声ファイル

        Returns:
            float: 長さ（秒）

        Raises:
            RemotionRenderError: 測定に失敗した場合
        """
        from src.generators.video_composer import VideoComposer, VideoCompositionError

        try:
            return VideoComposer()._get_media_duration(audio_path)
        except VideoCompositionError as e:
            raise RemotionRenderError(f"音声の長さを測れませんでした: {e}") from e
