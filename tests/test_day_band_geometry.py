"""「今日の時間軸」の縦のジオメトリの検証。

なぜ要るか
----------
帯の軌道は `overflow-hidden` の固定高で、中に「点 / 時刻 / 状態の語」を縦に積む。
**箱と中身の高さがどちらも別々に動かせるので、片方だけ変えると静かに壊れる。**

実際に壊れた形（`a239dc3`）: 記事プールを1画面に収めるために軌道を
`h-20`（80px）→ `h-16`（64px）に縮めたが、中身の 68px を数え直していなかった。
`text-[10px]` は font-size だけを出力し**行送りを指定しない**ため、Tailwind
preflight の `html{line-height:1.5}` が効いて1行 15px になる
（24 + 10 + 2 + 15 + 2 + 15 = 68px）。結果、**状態の語の行が下端で断ち切られた
まま動いていた**——尺や色ではなく「読めるかどうか」だけが壊れるので、画面を
見るまで気付けない。

対称の失敗は「行を1本足して軌道を直さない」なので、行数も数える。

テンプレートのピクセルを測るランナーはこのリポジトリに無い（ブラウザを起動する
検査は `-m slow` にも置いていない）。そこで `tests/test_remotion_design_rules.py`
と同じ型——**定数をファイルから読んで算術の不変条件を検査する**——を使う。
実物のピクセルは、ジオメトリを触ったときに人が測る（CLAUDE.md「文字が収まって
いるかは画素で検査する」と同じ分担）。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "partials" / "day_band.html"
APP_CSS = REPO_ROOT / "static" / "css" / "app.css"

# 枠の縦積み（点と2行のラベルを持つ div）の class を取る正規表現。
SLOT_STACK = r'<div class="(absolute top-[^"]*)"'


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _spacing_px() -> float:
    """Tailwind の間隔スケール1段のピクセル数。

    生成物から読む（`--spacing:.25rem`）。ここを直に 4 と書くと、テーマで
    スケールを変えたときにテストだけが古い前提のまま通る。rem → px は
    ルートの font-size が既定の 16px であることに乗っている（`input.css` は
    `html` の font-size を上書きしていない）。
    """
    match = re.search(r"--spacing:([0-9.]+)rem", APP_CSS.read_text(encoding="utf-8"))
    assert match is not None, "app.css に --spacing が見つからない（build:css を実行した？）"
    return float(match.group(1)) * 16


def _class_attr(pattern: str, text: str, what: str) -> str:
    """`class="..."` の中身を1つ取り出す。

    見つからなければ落とす。**「見つからなければ検査をやめる」形にしないこと**
    ——クラスの並び替えやタグの書き換えで、検査が静かに通るようになる。
    """
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"{TEMPLATE.name} から{what}を読めない（構造が変わった？）"
    return match.group(1)


def _token_px(class_attr: str, prefix: str, what: str) -> float:
    """`top-5` / `h-2.5` / `gap-0.5` のような間隔トークンを px にする。"""
    match = re.search(rf"(?:^|\s){re.escape(prefix)}-(\d+(?:\.\d+)?)(?:\s|$)", class_attr)
    assert match is not None, f"{what}に {prefix}-<数値> が無い: {class_attr!r}"
    return float(match.group(1)) * _spacing_px()


def _track_class() -> str:
    """軌道（目盛りと点を載せる灰色の箱）の class。"""
    return _class_attr(r'<div class="([^"]*overflow-hidden[^"]*)">', _template(), "軌道")


def _slot_block() -> str:
    """枠（点と2行のラベル）を描く部分。"""
    return _class_attr(r"\{% for slot in slots %\}(.*?)\{% endfor %\}", _template(), "枠のループ")


def _label_classes() -> list[str]:
    """枠の中の、文字を描く span の class を並び順で返す。"""
    return re.findall(r'<span class="([^"]*text-\[10px\][^"]*)"', _slot_block())


def test_状態の語が軌道の中に収まる() -> None:
    """点と2行のラベルの合計が、軌道の高さを超えないこと。

    超えると `overflow-hidden` が下の行を断ち切る。**軌道を伸ばして直すのは
    最後の手段**——記事プールの高さは `index.html` の
    `lg:h-[calc(100vh-17rem)]` で、`17rem` はこの帯の高さから決めた実測値
    （カード下端 890px / ビューポート 900px）。帯を伸ばすと、そちらを測り
    直すことになる。

    目盛りの時刻（`top-1` の span）はこの不変条件に入れない。点より上にあり、
    切れる側ではないため（間隔の調整であって、収まるかどうかの話ではない）。
    """
    track = _token_px(_track_class(), "h", "軌道")
    slot = _slot_block()
    stack = _class_attr(SLOT_STACK, slot, "枠の縦積み")

    top = _token_px(stack, "top", "枠の縦積み")
    gap = _token_px(stack, "gap", "枠の縦積み")
    dot = _token_px(_class_attr(r'<span class="(w-2\.5[^"]*)"', slot, "点"), "h", "点")

    labels = _label_classes()
    leadings = [_token_px(cls, "leading", "ラベル") for cls in labels]

    needed = top + dot + len(labels) * gap + sum(leadings)
    assert needed <= track, (
        f"点とラベルの合計 {needed}px が軌道の {track}px に収まらない"
        f"（上端 {top} + 点 {dot} + 隙間 {gap}×{len(labels)} + 行送り {leadings}）。"
        "overflow-hidden なので、はみ出した行は画面で断ち切られる"
    )


def test_枠のラベルはちょうど2行で行送りを明示している() -> None:
    """行数と行送りを固定する。

    行数: 上の算術は行数から計算しているので、3行にすれば自動的に落ちる。
    それでも別に数えるのは、**行を足すこと自体が壊れる操作である**と読める
    場所を残すため（状態の語を消して1行に戻す方向も同じく落ちる。成否を色
    だけで示してはいけない——CLAUDE.md「状態は語で示す」）。

    行送り: `text-[10px]` は行送りを出力しないので、`leading-*` を書かないと
    継承の 1.5（15px）が効く。これが `a239dc3` の退行の原因そのもの。
    """
    labels = _label_classes()
    assert len(labels) == 2, f"枠の文字行が2行ではない（{len(labels)}行）"
    for cls in labels:
        assert re.search(r"(?:^|\s)leading-\d", cls), (
            f"行送りが明示されていない: {cls!r}。"
            "text-[10px] は line-height を出力しないので、継承の 1.5 が効いて"
            "1行 15px になる"
        )


def test_軌道はoverflow_hiddenで閉じている() -> None:
    """`overflow-x-auto` に戻さないこと。

    以前この箱は `overflow-x-auto` で、`left: 100%` の `24:00` が中央合わせの
    translate で右へ半分はみ出していた（実測 scrollWidth 1227 > clientWidth
    1214）。**予定が0件でも常に 15px の横スクロールバーが出て**、帯の下に
    意味のないグレーの棒が見えていた。

    いま目盛りは左右に余白（`mx-7`）を持つ内側の箱に対して置いてあるので、
    はみ出す経路が無い。`auto` に戻すと、上の縦の不変条件も「切れる」から
    「スクロールできる」に意味が変わってしまう。
    """
    track = _track_class()
    assert "overflow-hidden" in track, f"軌道が overflow-hidden ではない: {track!r}"
    assert "overflow-x-auto" not in track, "軌道に overflow-x-auto が戻っている"
