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

from src.generators.video_composer import _available_cpus, _tail, mux_audio
from src.models.formats import get_spec
from src.models.scene import SceneVisual
from src.utils.line_break import insert_break_opportunities
from src.utils.logger import log_error, log_step, log_success


class RemotionRenderError(Exception):
    """Remotion のレンダリングに失敗した。"""


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

    # 画像生成を必要としない。これが `Pipeline` が gpt-image-2 の呼び出しを
    # 丸ごと飛ばせる根拠で、クォータの律速が消える理由。
    needs_images = False

    # シーンのラベルを実際に画面へ描く。これが `ScriptGenerator` に数値の根拠を
    # **強制させる**根拠（記事に無い数値が画面に出るのは、ニュースを扱う以上
    # 最も害が大きい種類の誤り）。`needs_images` とは別の問いなので別に持つ。
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

        props = {
            "width": spec.output_width,
            "height": spec.output_height,
            "fps": self.FRAME_RATE,
            "durationInFrames": total_frames,
            "scenes": [
                {
                    "layout": scene.layout.value,
                    "items": scene.items,
                    # ZWSP の挿入はここ（レンダリング直前）で行う。
                    # `MAX_HEADLINE_CHARS` は script.py が生成時点の文字数を
                    # 検証しており、この挿入より前に済んでいるので文字数には影響しない。
                    "headline": insert_break_opportunities(headline, language),
                    "subtitle": insert_break_opportunities(subtitle, language),
                    "fromFrame": start,
                    "durationInFrames": duration,
                }
                # 長さは上で一致を確認済みなので strict=True で守る
                for scene, headline, subtitle, (start, duration) in zip(
                    scenes, text_overlays, segment_narrations, spans, strict=True
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

        log_success(f"動画を描画しました ({audio_duration:.1f}秒)")
        return output_path

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
