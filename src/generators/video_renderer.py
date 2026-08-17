"""動画のレンダラを差し替え可能にする。

なぜ2つ持つか
-------------
Remotion（React で図解を描く）が本命だが、`ffmpeg`（静止画を並べる現行の
方式）は**今日動いているパイプライン**なので退路として残す。クラウドで
問題が出たら `VIDEO_RENDERER=ffmpeg` に戻すだけで復帰できる。

**自動フォールバックは作らない。** Remotion が失敗したら、そのままジョブを
失敗させて既存のリース・再試行（上限3回で FAILED）に任せる。黙って古い
レンダラに落ちると「毎朝の自動生成が古い見た目で回り続けて誰も気付かない」
状態になる（CD が無かった頃、マージしても反映されず旧コードで毎朝
走り続けていたのと同じ形の失敗）。切り替えは人が明示的に打つ。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.generators.remotion_renderer import RemotionRenderer
from src.generators.video_composer import VideoComposer
from src.models.scene import SceneVisual


class VideoRenderer(Protocol):
    """動画を作るものの契約。

    引数をすべてキーワード専用にしている。レンダラごとに使わない引数が
    あり（`FfmpegRenderer` は `scenes` を、`RemotionRenderer` は
    `image_paths` を使わない）、位置引数だと順序の取り違えが起きる。
    """

    def image_count(self, segment_count: int) -> int:
        """このレンダラが必要とする画像の枚数。

        以前は `needs_images: bool` だったが、これでは「ffmpeg は
        セグメントごとに1枚」と「Remotion は動画全体で共有する1枚だけ」の
        違いを表現できなかった（両方 True としか言えない）。0 を返せば
        `Pipeline` は `gpt-image-2` の呼び出しを丸ごと飛ばす
        （クォータはリージョン単位で上限4）。

        Args:
            segment_count: 台本のセグメント数

        Returns:
            int: 生成すべき画像の枚数
        """
        ...

    @property
    def draws_scene_text(self) -> bool:
        """シーンのラベル（`SceneVisual.items`）を画面に描くか。

        `needs_images` とは**別の問い**なので別のフラグにしている。混ぜると
        「画像は要らないがラベルは描かない」レンダラを表現できなくなり、
        この区別を落とした結果として起きたバグ（描かないレンダラでも
        記事に無い数値で台本生成が失敗した）がそのまま戻る。

        False なら `ScriptGenerator` は数値の根拠の検査を警告に留める。
        描かない文字のために再試行を使い切って FAILED にするのは、
        既定の経路に新しい失敗を持ち込むだけである。
        """
        ...

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
    ) -> Path: ...


class FfmpegRenderer:
    """現行の `VideoComposer` を `VideoRenderer` の形で包む。

    振る舞いは一切変えない。これが退路として機能するには、
    「今と同じものが出る」ことが担保されている必要がある。
    """

    # シーンのラベルは1文字も描かない（`scenes` を受け取っても捨てる）。
    draws_scene_text = False

    def image_count(self, segment_count: int) -> int:
        """静止画を並べる方式なので、セグメントごとに1枚必要。"""
        return segment_count

    def __init__(self) -> None:
        self._composer = VideoComposer()

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
        """静止画を並べて動画を合成する。

        `scenes` と `segment_narrations` と `illustration_path` は使わない
        （契約を揃えるために受ける。挿絵は Remotion レンダラだけが使う）。

        Args:
            audio_path: ナレーション音声
            output_path: 出力する動画のパス
            image_paths: 並べる画像
            scenes: 使わない
            text_overlays: 各画像に載せるテキスト
            segment_narrations: 使わない
            segment_timings: 各セグメントの開始秒
            language: フォント選択用の言語コード
            video_format: 形式名
            illustration_path: 使わない

        Returns:
            Path: 生成された動画のパス
        """
        return self._composer.compose(
            audio_path,
            image_paths,
            output_path,
            text_overlays=text_overlays,
            language=language,
            segment_timings=segment_timings,
            video_format=video_format,
        )


def build_video_renderer(name: str) -> VideoRenderer:
    """名前からレンダラを組み立てる。

    未知の名前は**黙って既定に落とさない**。定期実行の中で初めて分かると、
    気付くのが翌朝になる（`SCHEDULE_FORMATS` の検証と同じ判断）。

    Args:
        name: "ffmpeg" または "remotion"

    Returns:
        VideoRenderer: レンダラ

    Raises:
        ValueError: 未知の名前の場合
    """
    if name == "ffmpeg":
        return FfmpegRenderer()
    if name == "remotion":
        return RemotionRenderer()
    raise ValueError(f"未知のレンダラです: {name!r}（'ffmpeg' または 'remotion'）")
