"""Remotion のデザイン側の規約を検査する。

**これは既知の1つを名前で狙い撃つだけ**で、遅い描画一般を防ぐものではない
（box-shadow を10枚重ねれば同じことが起きる）。それでも置くのは、実測で
3倍の差が出ていて、tests/test_deploy_workflow.py や
tests/test_container_image.py と同じ「ファイルの中身を検査する」型に
収まるから。

コメントは除いてから検査する
--------------------------
`Background.tsx` と `theme.ts` は、禁止している構文そのもの（`blur(` /
`@font-face`）を**コメントの中で名指し**して、なぜ禁止かを説明している
（実測値つき）。そのコメントはファイル中で最も価値のある行なので、
「コメントを言い換えて検査を回避する」のではなく、検査側がコメントを
読み飛ばす。

除去は正規表現の近似（`//` 以降と `/* ... */` を削るだけ）で、
文字列リテラルの中に埋め込まれた違反はすり抜ける。厳密な構文解析はしていない。
"""

import re
from pathlib import Path

REMOTION_SRC = Path(__file__).resolve().parents[1] / "remotion" / "src"

_LINE_COMMENT = re.compile(r"//.*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str) -> str:
    """`//` 以降と `/* ... */` を取り除く。

    近似的な実装であることに注意（docstring 参照）。TypeScript の完全な
    トークナイザではないので、文字列リテラルの中に `//` や `/*` があると
    誤って削ってしまう可能性がある。それでもコメントで説明を書きたい
    このリポジトリの流儀とは相性が良い。
    """
    without_block = _BLOCK_COMMENT.sub("", text)
    return _LINE_COMMENT.sub("", without_block)


def test_no_blur_filter_anywhere() -> None:
    """全画面 blur は 199秒 → 598秒（3倍）にする。実測（2026-08-17）。

    グローを出したいときは blur ではなくグラデーションと不透明度で作る。
    """
    offenders = [
        path.relative_to(REMOTION_SRC).as_posix()
        # `.tsx` だけを見ると、`theme.ts` のような `.ts` に置いた共有ヘルパが
        # 検査をすり抜ける。Web フォントの検査と同じ範囲にそろえる。
        for path in REMOTION_SRC.rglob("*.ts*")
        if "blur(" in _strip_comments(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"filter: blur() を使っているファイル: {offenders}"


def test_no_web_fonts() -> None:
    """@font-face / @remotion/google-fonts を使わないこと。

    非同期に読ませると、delayRender / waitForFonts で待たない限り最初の
    数フレームだけフォールバックフォントで焼かれる。エラーにならないので
    気付きにくい。システムの fonts-noto-cjk を font-family で参照する。
    """
    for path in REMOTION_SRC.rglob("*.ts*"):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        assert "@font-face" not in text, f"{path.name} が @font-face を使っている"
        assert "google-fonts" not in text, f"{path.name} が google-fonts を使っている"


def _number_constant(path: Path, name: str) -> float:
    """`const NAME = 123;` の数値を取り出す。

    TS 側の定数を Python から読む。**TS のユニットテストランナーが無い**
    （`remotion/package.json` には typecheck しか無い）ため、算術の不変条件を
    検査する手段がこれしかない。`test_deploy_workflow.py` /
    `test_container_image.py` が「ファイルの中身を検査する」型で置かれているのと
    同じ考え方。
    """
    text = _strip_comments(path.read_text(encoding="utf-8"))
    match = re.search(rf"^const {name} = ([0-9.]+);", text, re.MULTILINE)
    assert match is not None, f"{path.name} に const {name} が見つからない"
    return float(match.group(1))


# `zones.ts` の比率はこの高さ（縦画面）を分母に書かれており、short / tiktok は
# 実際にこの解像度でレンダリングされる。ここでの検算はすべて px で行う。
FRAME_HEIGHT = 1920

_RATIO_EXPR = re.compile(r"^\s*([0-9.]+)\s*(?:/\s*([0-9.]+)\s*)?$")
_ZONE_ENTRY = re.compile(r"(\w+):\s*\{\s*top:\s*([^,]+),\s*height:\s*([^}]+)\}")
_GROUP_HEADER = re.compile(r"^\s*(shared|strip|statement):\s*\{", re.MULTILINE)


def _px(expr: str) -> float:
    """`800 / 1920` のような比率の式を、フレーム高さ基準の px に直す。"""
    match = _RATIO_EXPR.match(expr)
    assert match is not None, f"比率として読めない式: {expr!r}"
    numerator = float(match.group(1))
    denominator = float(match.group(2)) if match.group(2) else 1.0
    return numerator / denominator * FRAME_HEIGHT


def _block(text: str, opening: str) -> str:
    """`opening` で始まるオブジェクトリテラルの中身を返す（近似）。"""
    start = text.index(opening) + len(opening)
    end = text.index("};", start)
    return text[start:end]


def _zone_ratios() -> dict[str, dict[str, tuple[float, float]]]:
    """`zones.ts` の `RATIOS` を {グループ: {ゾーン名: (top, height)}} で返す。

    **ゾーンの高さを Python 側に写さない。** ここは以前
    `zone_height = 350` というハードコードで、コメントに「`zones.ts` を
    触ったらここも直す」と書いてあった。そのハードコードが指す値そのものを
    変える変更（Issue #44）で写し忘れれば、検査は**古い高さで計算して
    通ってしまう**。読めるものは読む。

    `ILLUSTRATION_SIZE`（`src/generators/remotion_renderer.py`）の方は
    帯のアスペクト比を人が判断して決める値なので、同じ手は使えない。
    """
    text = _strip_comments((REMOTION_SRC / "zones.ts").read_text(encoding="utf-8"))
    ratios_block = _block(text, "const RATIOS = {")

    groups: dict[str, dict[str, tuple[float, float]]] = {}
    headers = [(m.start(), m.group(1)) for m in _GROUP_HEADER.finditer(ratios_block)]
    assert {name for _, name in headers} == {"shared", "strip", "statement"}, (
        f"RATIOS のグループが変わっている: {headers}"
    )

    for entry in _ZONE_ENTRY.finditer(ratios_block):
        # 直前に現れたグループ見出しがこのゾーンの所属。
        group = [name for start, name in headers if start < entry.start()][-1]
        groups.setdefault(group, {})[entry.group(1)] = (
            _px(entry.group(2)),
            _px(entry.group(3)),
        )
    return groups


def _safe_bottom(video_format: str) -> float:
    """`zones.ts` の `SAFE_BOTTOM` から、その形式で空ける高さを px で返す。"""
    text = _strip_comments((REMOTION_SRC / "zones.ts").read_text(encoding="utf-8"))
    block = _block(text, "const SAFE_BOTTOM: Record<VideoFormat, number> = {")
    values = dict(re.findall(r"(\w+):\s*([^,]+),", block))
    assert video_format in values, f"SAFE_BOTTOM に {video_format} が無い: {values}"
    return _px(values[video_format])


def _subtitle_padding_bottom(video_format: str) -> float:
    """`Subtitle.tsx` の下パディングの導出を Python 側で再現する。

    実装は `Math.max(MIN_PADDING_BOTTOM, safeBottom + BOTTOM_CLEARANCE)`。
    式を写しているので、**TS 側の式を変えたらここも変わる**（定数だけを
    読んでいれば済んだ以前より結合が強い。それでも、下パディングが
    リテラルでなくなった以上、読み取りだけで済ませる方法は無い）。
    """
    subtitle = REMOTION_SRC / "Subtitle.tsx"
    floor = _number_constant(subtitle, "MIN_PADDING_BOTTOM")
    clearance = _number_constant(subtitle, "BOTTOM_CLEARANCE")
    return max(floor, _safe_bottom(video_format) + clearance)


def test_four_subtitle_lines_fit_in_the_zone_at_base_size() -> None:
    """字幕の4行が、縮小に頼らず基準サイズでゾーンに収まること。

    **`fitSubtitleSize` の存在は、この不変条件の代わりにはならない。**
    自動縮小があるので、パディングを食い潰しても「文字が切れる」症状は
    出ず、代わりに**文字が小さくなる**。実際に確かめた: 旧ジオメトリ
    （上160 / 下96 → テキストに使える高さ94px）に戻すと、切れの検査
    （`test_remotion_render_slow.py`）は**通ったまま**フォントが
    46px → 31px に落ちた。「切れていない」だけを見張ると、読めない
    大きさへ静かに退化する経路が残る。

    ここで4行を要求する根拠は、`FormatSpec.segment_char_cap`（short で
    48文字）が3〜4行に対応するため。3行しか収まらない設定に戻すと、
    上限どおりの台本で字幕が縮む。

    **Issue #44 の修正でこの余裕は意図的に変えていない。** UI を避けるために
    下パディングを 72→162 に増やしたぶん、ゾーンを 350→440 に伸ばして
    相殺した（使える高さは 266px のまま）。片方だけ動かすとここで落ちる。
    """
    subtitle = REMOTION_SRC / "Subtitle.tsx"
    base = _number_constant(subtitle, "BASE_SIZE")
    line_height = _number_constant(subtitle, "LINE_HEIGHT")
    padding_top = _number_constant(subtitle, "PADDING_TOP")
    padding_bottom = _subtitle_padding_bottom("short")

    zone_height = _zone_ratios()["strip"]["subtitle"][1]
    available = zone_height - padding_top - padding_bottom
    needed = 4 * base * line_height
    assert needed <= available, (
        f"基準サイズ{base}px・行送り{line_height}で4行（{needed}px）が"
        f"字幕ゾーンの使える高さ（{available}px）に収まらない。"
        "自動縮小が働くので切れはしないが、字幕が小さくなる"
    )


def test_the_subtitle_text_clears_the_platform_ui() -> None:
    """字幕の文字が、プラットフォームの UI が覆う帯に入らないこと。

    **これは画素の検査（`test_remotion_render_slow.py`）の代わりではなく、
    その手前の算術**。あちらは実際に焼いたフレームでインクを測るので確実だが、
    slow マーカーが付いていて node / ffmpeg が無い環境では静かに skip される。
    ここは定数だけで判定できるので、常に走る。

    Issue #44 の症状（Shorts の UI が字幕の最終行を丸ごと覆う）は、
    下パディング 72px に対して UI が実測 150px を覆っていたことによる。
    """
    for video_format in ("short", "tiktok"):
        safe_bottom = _safe_bottom(video_format)
        assert safe_bottom > 0, f"{video_format} は縦画面なので UI の帯を空ける"

        text_bottom = FRAME_HEIGHT - _subtitle_padding_bottom(video_format)
        safe_line = FRAME_HEIGHT - safe_bottom
        assert text_bottom <= safe_line, (
            f"{video_format}: 字幕の文字の下端（y={text_bottom}）が"
            f"セーフライン（y={safe_line}）より下にある。"
            "プラットフォームの UI に最終行が覆われる"
        )

    # **`long` は下限（従来値）に落ちること。** 横画面では UI の帯を空けない
    # ので導出値は `BOTTOM_CLEARANCE` だけになり、下限が無いと文字が
    # フレーム下端 12px まで寄る（Issue #44 の修正で持ち込みかねない退行）。
    floor = _number_constant(REMOTION_SRC / "Subtitle.tsx", "MIN_PADDING_BOTTOM")
    assert _subtitle_padding_bottom("long") == floor


def test_zones_stay_ordered_and_reach_the_frame_bottom() -> None:
    """ゾーンが上から順に並び、重ならず、字幕がフレーム下端まで届くこと。

    「各要素は自分のゾーンの内側にしか描かない」という設計は、ゾーン自体が
    重なっていないことを前提にしている。Issue #44 では字幕ゾーンを 90px
    上へ伸ばしたので、**関係ストリップと重なる経路が実際にあった**
    （伸ばした量ぶん、上の帯を詰める必要があった）。

    字幕の下端がフレーム下端であることも一緒に見る。下端のスクリムは
    ゾーンの高さと無関係に必ず下端まで伸びるので、ゾーンを実際より狭く
    書くと値がレイアウトの実態を表さなくなる（`zones.ts` のコメント）。
    """
    ratios = _zone_ratios()
    for layout, order in (
        ("strip", ("illustration", "chapter", "headline", "relation", "subtitle")),
        ("statement", ("illustration", "chapter", "headline", "subtitle")),
    ):
        zones = {**ratios["shared"], **ratios[layout]}
        assert set(zones) == set(order), f"{layout} のゾーンの構成が変わっている: {zones}"

        bottom = 0.0
        for name in order:
            top, height = zones[name]
            assert top >= bottom, (
                f"{layout} の {name}（top={top}）が直前のゾーンの下端（{bottom}）に"
                "食い込んでいる。要素が重なって描かれる"
            )
            bottom = top + height

        assert bottom == FRAME_HEIGHT, (
            f"{layout} の字幕ゾーンの下端が {bottom} で、フレーム下端（{FRAME_HEIGHT}）と一致しない"
        )
