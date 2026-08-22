"""Remotion を実際に動かして、経路全体が通ることを確認する。

**2秒（60フレーム）のコンポジションで測る。**
.githooks/pre-push は `-m "not live"` なので slow を含む。実運用と同じ
1050フレームを焼くと push が30秒から4分になり、--no-verify される道を
作ってしまう。2秒でも通る経路は同じ（Node が呼ばれる / Chrome が動く /
mp4 ができる / 音声が多重化される / 中間ファイルが消える）。
フル尺の実測は移行時の手動確認で行う。

**レンダリングは1回だけ行い、全テストで共有する**（`rendered` フィクスチャ）。
以前は検査ごとに焼いていた。pre-push の所要時間は 30秒 → 60秒 → 90秒と
推移しており（CLAUDE.md「チェックは pre-push に寄せている」）、その最大の
内訳がこのレンダリングである。**検査を足すたびに焼き直す形にすると、
検査を足すこと自体が --no-verify への圧力になる。** 焼くのは1回にして、
props を「最悪ケース」に寄せることで1回のレンダリングから取れる情報を
増やす方針にした。
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.generators.remotion_renderer import RemotionRenderer
from src.models.formats import get_spec
from src.models.scene import MAX_LABEL_CHARS, MAX_RELATION_CHARS, SceneLayout, SceneVisual
from src.models.script import MAX_HEADLINE_CHARS

pytestmark = pytest.mark.slow

REMOTION_DIR = Path(__file__).resolve().parents[1] / "remotion"

# --- 最悪ケースの props -------------------------------------------------------
#
# **サイズや行数を保証するテストが1本も無い**ことが CLAUDE.md で
# 「最も重要な記録」として残されており、その穴が同じ日に2件の不具合
# （statement での45字見出しの欠け、compare/flow での8字ラベルの語中改行）を
# 生んだ。ここでは各フィールドをスキーマ上の**上限**で埋める——上限で通れば
# 実際の出力でも通る。長さはスキーマの定数と突き合わせて、将来の編集で
# 静かに緩まないようにする。

# 45字（`MAX_HEADLINE_CHARS`）。長い複合語が続く言い回しを選ぶ。
# CLAUDE.md の実測では、同じ45字でも `推論コストが…解き明かします` は
# 4行に収まり、複合語が続く方は5行必要になった。厳しい側を使う。
WORST_HEADLINE = (
    "大規模言語モデルの推論基盤における計算資源配分の最適化技術が一般提供へ、詳細を解説します。"
)

# 48字（short の `segment_char_cap`）。2026-08-22 の生成物で実際に字幕の
# 1行目が切れたセグメントを、上限まで詰めた形。`AI` のような ASCII を
# 含めるのは、`estimateEmWidth` の全角/半角の見積りを実物で通すため。
WORST_SUBTITLE = (
    "職場でAIを使ったことを一文添えるだけで済む話が、サイレントAIユーザーに新たな開示を迫ります。"
)

# 8字（`MAX_LABEL_CHARS` / `MAX_RELATION_CHARS`）。
WORST_ITEMS = ["従来型の推論基盤", "混合専門家方式へ"]
WORST_RELATION = "資源配分の最適化"

# 字幕ゾーンの上端（`remotion/src/zones.ts` の `subtitle.top` = 1570/1920）。
# **TS 側と Python 側で同じ値を持つ単一の情報源が無い**——ゾーンの定義は
# レンダラ（TS）にあり、ビルド時に Python へ伝える手段がない
# （`ILLUSTRATION_SIZE` と同じ構造の重複）。ずれたらこのテストが
# 「切れていない」を誤って通すので、`zones.ts` を触ったらここも直す。
SUBTITLE_ZONE_TOP = 1570

# 「切れていたら必ずインクが出る」帯。字幕の文字は最速でも
# ゾーン上端 + `PADDING_TOP`(12px) から始まるので、ここは本来つねに
# 文字が無い。切れている場合は `overflow: hidden` が上端で断ち切るため、
# 上端の直下から文字が連続して現れる。
CLIP_PROBE_TOP = SUBTITLE_ZONE_TOP + 2
CLIP_PROBE_BOTTOM = SUBTITLE_ZONE_TOP + 10

# 白文字（`COLORS.text` = #eef1f5）の閾値。地は #14161a なので大きく離れている。
INK_THRESHOLD = 170


@pytest.fixture(scope="module")
def toolchain_available() -> None:
    """Node / ffmpeg / node_modules が揃っていること。

    揃っていなければ skip する。**.githooks/pre-push が node と ffmpeg の
    存在を先に検査している**ので、push 経路では skip されない。
    """
    if shutil.which("node") is None:
        pytest.skip("node が PATH にない")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg / ffprobe が PATH にない")
    if not (REMOTION_DIR / "node_modules").is_dir():
        pytest.skip("remotion/node_modules が無い（cd remotion && npm install）")


@pytest.fixture(scope="module")
def two_second_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """2秒の**音の出る** MP3 を作る。ffmpeg で生成するので外部素材が要らない。

    **無音（anullsrc）にしてはいけない。** 以前はそうしていて、そのために
    「音声トラックはあるが中身が無音」という壊れ方を検出できなかった
    （`mux_audio` が `-map` を持たず、Remotion の無音ステレオトラックが
    モノラルのナレーションより優先されていた。実測で生成物5本すべてが
    mean_volume -91.0 dB）。無音の入力では、正しい出力と壊れた出力が
    ビット単位で区別できない。

    ナレーションと同じ**モノラル 24kHz** で作る。ステレオにすると
    Remotion 側の無音トラックとチャンネル数で並ぶため、`-map` を消しても
    テストが通ってしまう（既定のストリーム選択はチャンネル数で決める）。
    """
    audio = tmp_path_factory.mktemp("audio") / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=24000:duration=2",
            "-ac",
            "1",
            str(audio),
        ],
        capture_output=True,
        check=True,
    )
    return audio


@pytest.fixture(scope="module")
def rendered(
    toolchain_available: None,
    two_second_audio: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    """最悪ケースの props で1回だけレンダリングし、(出力, 作業ディレクトリ) を返す。

    3レイアウト（statement / compare / flow）をすべて含める。ゾーンと文字サイズは
    レイアウトごとに違うので、1つだけ焼くと残り2つの回帰を見逃す。

    **テキストは素のまま渡す。** ZWSP（BudouX のフレーズ境界）は
    `RemotionRenderer.render()` が props を組む直前に挿入する。手で ZWSP を
    埋めた文字列を渡すと、本番と違う改行位置で折り返されて**検査が何も
    保証しなくなる**（`keep-all` は ZWSP の位置でしか折らないため、
    ZWSP の入り方が変わると行数が変わる）。
    """
    workdir = tmp_path_factory.mktemp("render")
    output = workdir / "out.mp4"
    RemotionRenderer().render(
        audio_path=two_second_audio,
        output_path=output,
        image_paths=[],
        scenes=[
            SceneVisual(layout=SceneLayout.STATEMENT, items=[], relation=""),
            SceneVisual(layout=SceneLayout.COMPARE, items=WORST_ITEMS, relation=WORST_RELATION),
            SceneVisual(layout=SceneLayout.FLOW, items=WORST_ITEMS, relation=WORST_RELATION),
        ],
        text_overlays=[WORST_HEADLINE] * 3,
        segment_narrations=[WORST_SUBTITLE] * 3,
        segment_timings=[0.0, 0.7, 1.4, 2.0],
        language="ja",
        video_format="short",
    )
    return output, workdir


def _probe(path: Path, stream: str) -> str:
    """ffprobe で指定した種類のストリームの codec_type を返す（無ければ空）。

    末尾のカンマを削る。Remotion が焼く h264 ストリームには side_data
    （実測: 空の side_data_list）が付き、`csv=p=0` がそれを空フィールドとして
    出力してしまう（`"video,"` のように）。ダミー画像から作った動画では
    出ない、実物でしか踏めない類の違い。codec_type 自体は正しく1つだけ
    入っているので、末尾のカンマは無視してよい。
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().rstrip(",")


def _mean_volume_db(path: Path) -> float:
    """音声の平均音量（dBFS）を返す。デジタル無音なら -91.0 が返る。

    ffprobe では測れない（ストリームの有無しか分からない）ため
    `volumedetect` フィルタを通す。実際に**音が入っているか**を見るには
    デコードするしかない。
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise AssertionError(f"volumedetect の出力に mean_volume が無い: {result.stderr}")


def _extract_frame(video: Path, at_sec: float, dest: Path) -> Path:
    """指定秒のフレームを PNG で取り出す。

    JPEG ではなく PNG にする。閾値で「文字があるか」を判定するので、
    JPEG のリンギング（暗地と白文字の境界に中間色が出る）を持ち込まない。
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{at_sec}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest


def _ink_in_band(frame: Path, top: int, bottom: int) -> int:
    """帯の中の「白い画素」の数を返す。文字があるかを機械的に測る。

    画素を1つずつ読むのではなく、帯を切り出してヒストグラムを取る。
    `Image.load()` の返り値は型が `PixelAccess | None` で mypy が通らないうえ、
    1080 × 数百回のループは遅い。ヒストグラムなら閾値より明るいバケットの
    合計がそのまま画素数になる。
    """
    image = Image.open(frame).convert("L")
    band = image.crop((0, top, image.width, bottom + 1))
    return sum(band.histogram()[INK_THRESHOLD + 1 :])


def test_worst_case_props_sit_at_the_schema_limits() -> None:
    """最悪ケースの props がスキーマの上限そのものであること。

    このテストが無いと、下の検査は「たまたま短い入力なら通る」状態に
    静かに退化しうる（文字列を短く直しても誰も気付かない）。
    """
    assert len(WORST_HEADLINE) == MAX_HEADLINE_CHARS
    assert len(WORST_SUBTITLE) == get_spec("short").segment_char_cap("ja")
    assert all(len(item) == MAX_LABEL_CHARS for item in WORST_ITEMS)
    assert len(WORST_RELATION) == MAX_RELATION_CHARS


def test_render_produces_a_playable_video(rendered: tuple[Path, Path]) -> None:
    """Node の起動から音声の多重化・中間ファイルの後始末まで、経路全体が通ること。

    ここが通らないと（Remotion のレンダリング失敗、多重化の抜け、
    後始末忘れ）本番の35秒レンダリングも同じ壊れ方をする。
    """
    output, workdir = rendered
    assert output.exists()
    # 音声トラックがあること。無ければ多重化が抜けている
    assert _probe(output, "a:0") == "audio"
    # **トラックの有無だけでは足りない。** Remotion が焼く無音ステレオトラックが
    # 採用されると、トラックも尺も解像度も正しいまま音だけが消える。
    # 440Hz のサイン波なら十分大きいので、無音（-91.0 dB）と明確に分かれる。
    assert _mean_volume_db(output) > -40.0
    assert _probe(output, "v:0") == "video"
    # 中間ファイルを残さないこと
    assert list(workdir.glob("*_silent.mp4")) == []
    assert list(workdir.glob("*_props.json")) == []


def test_render_uses_the_format_resolution(rendered: tuple[Path, Path]) -> None:
    """解像度は formats.py が決める。short は 1080x1920。

    Remotion の props（width/height）が spec からずれていないかを見る。
    ずれると画像生成レンダラと出力仕様が食い違う。
    """
    output, _ = rendered
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # 末尾のカンマは _probe と同じ理由（side_data）で削る。
    assert result.stdout.strip().rstrip(",") == "1080,1920"


@pytest.mark.parametrize(
    ("at_sec", "layout"),
    [(0.45, "statement"), (1.15, "compare"), (1.85, "flow")],
)
def test_subtitle_is_not_clipped_at_the_zone_boundary(
    rendered: tuple[Path, Path], tmp_path: Path, at_sec: float, layout: str
) -> None:
    """字幕がゾーン上端で断ち切られていないこと。**全レイアウトで**。

    これが今まで自動で検査されておらず、実運用の動画で**尺の約52%（46.2秒の
    うち24秒）にわたって字幕の1行目が横一直線に切れていた**
    （2026-08-22）。ffprobe では気付けない類の壊れ方で——尺・解像度・
    音声トラックはすべて正しく、**画素を見るしか判定手段が無い**。

    ゾーン上端の直下に白い画素があれば、`overflow: hidden` がそこで文字を
    断ち切っている。正常なら字幕の文字は上端 + パディング(12px) より下から
    始まるので、この帯は空でなければならない。

    各レイアウトの中央付近のフレームで測る。**シーンの先頭では測れない**
    ——字幕は6フレームでフェードインするので、明るさが閾値に届かない。
    """
    output, _ = rendered
    frame = _extract_frame(output, at_sec, tmp_path / f"{layout}.png")
    ink = _ink_in_band(frame, CLIP_PROBE_TOP, CLIP_PROBE_BOTTOM)
    assert ink == 0, (
        f"{layout} の字幕がゾーン上端（y={SUBTITLE_ZONE_TOP}）で切れている: "
        f"y={CLIP_PROBE_TOP}..{CLIP_PROBE_BOTTOM} に白い画素が{ink}個ある"
    )


@pytest.mark.parametrize(
    ("at_sec", "layout"),
    [(0.45, "statement"), (1.15, "compare"), (1.85, "flow")],
)
def test_subtitle_is_actually_drawn(
    rendered: tuple[Path, Path], tmp_path: Path, at_sec: float, layout: str
) -> None:
    """字幕が実際に描かれていること。

    **上の「切れていない」検査だけでは不十分。** 字幕が1文字も描かれなければ
    帯は空になり、切れの検査は通ってしまう。ゾーンの下半分に相当量の
    インクがあることを併せて見る（`fitSubtitleSize` が下限まで縮めても
    48字は十分な面積を占める）。
    """
    output, _ = rendered
    frame = _extract_frame(output, at_sec, tmp_path / f"{layout}-drawn.png")
    ink = _ink_in_band(frame, SUBTITLE_ZONE_TOP + 20, 1919)
    assert ink > 2000, f"{layout} の字幕が描かれていない（白い画素が{ink}個）"
