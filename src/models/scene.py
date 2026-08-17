"""動画の1シーンに描くものの視覚指示。

なぜ画像生成モデルに描かせないか
--------------------------------
図解主体に振ったので、描く対象は「絵」ではなく「構造」である。LLM に構造を
出させて Remotion（React）が描けば、文字は常に正確で、回ごとのブレも無く、
`gpt-image-2` のクォータ（サブスクリプション・リージョン単位で上限4）も
消費しない。動画がクォータを使わなくなると、X の画像カードとの共食いも消える。

`src/social/card_visual.py` の `CardVisual` と役割は似ているが、意図的に
別モデルにしてある。あちらは**画像生成モデルへの英語の指示**で、こちらは
**レンダラが読む構造**。共有すると、片方の都合でもう片方が壊れる。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, model_validator

# ラベル1つの最大文字数。
#
# 名札の役割に留める。長い文を入れると縦1920pxの中で図が文字に埋まる。
# 値は `card_visual.MAX_LABEL_CHARS` と同じ8だが**借り物**である。カードは
# 1024x1024、動画は 1080x1920 で面積が違うので、実物を見て決め直す前提の
# 暫定値（設計書の「未確定」に挙げてある）。カードでは上限90字が正常な出力を
# 3回連続で弾いた前例があるので、動画でも実測で決める。
MAX_LABEL_CHARS = 8

# 関係性ラベル（relation）1個の最大文字数。
#
# items と同じ名札の役割（矢印や対比の脇に置く短い語）であって、文ではない。
# `切替` `1/10` `並列化` のような単語1つを想定している。MAX_LABEL_CHARS と
# 値は同じ8だが、こちらも**暫定値**である。カードの MAX_LABEL_CHARS が
# 実測で決まった経緯（90字が正常な出力を弾いた前例）と同じく、動画の実フレームを
# 見てから再測定する前提を残す。
MAX_RELATION_CHARS = 8


class SceneLayout(StrEnum):
    """シーンの型。1つにつき React コンポーネントが1つ対応する。

    閉じた集合にしている理由: 自由記述を許すと、モデルは毎回違う構図を要求し、
    レンダラ側に描けないものが混ざる。`CardVisual.key_details` を
    ちょうど2個に固定したのと同じ判断。
    """

    STATEMENT = "statement"  # 図なし。見出しだけを大きく（フック・結論向け）
    COMPARE = "compare"  # 対比する2つを左右に置く
    FLOW = "flow"  # 原因 → 結果。矢印で繋ぐ


# 各レイアウトが要求する要素数。
#
# 範囲ではなく固定値にする。カードでの実測では、範囲を与えるとモデルは
# 上限まで使い、図が複数のグループに割れてスマホで読めなくなった。
ITEMS_PER_LAYOUT: dict[SceneLayout, int] = {
    SceneLayout.STATEMENT: 0,
    SceneLayout.COMPARE: 2,
    SceneLayout.FLOW: 2,
}


class SceneVisual(BaseModel):
    """1シーンに描くもの。LLM への出力契約そのもの。

    **見出しと字幕はここに持たない。** 見出しは `Script.text_overlays[i]`、
    字幕は `Script.segment_narrations[i]` から取る。同じ文字列を2箇所に
    持たせない理由は2つある。

    1. 880c95f の教訓 — キャプションが画像に描かれるなら本文で繰り返さない。
       同じ主張が2回出ても読み手の情報は増えない
    2. 検証フレームの実物 — 見出し・キャプション・字幕を3つ乗せたところ、
       キャプションと字幕が同じことを言っていた。縦画面に文字ブロック3つは多い

    Attributes:
        layout: シーンの型
        items: 図に入れる短いラベル。個数は `ITEMS_PER_LAYOUT` が決める
        relation: 2つの要素の関係性を表す短い語（`切替` `1/10` `並列化` など）。
            `compare` / `flow` では必須、`statement` では空文字列を要求する
            （対比・因果の図が無いシーンに関係性は存在しない）
    """

    layout: SceneLayout
    items: list[str]
    relation: str

    @model_validator(mode="after")
    def _check_items(self) -> SceneVisual:
        """レイアウトが要求する要素数と、各要素の長さを検証する。

        Returns:
            SceneVisual: 検証済みの自身

        Raises:
            ValueError: 要素数が合わない、空、または長すぎる場合
        """
        expected = ITEMS_PER_LAYOUT[self.layout]
        if len(self.items) != expected:
            raise ValueError(
                f"layout={self.layout.value} は items をちょうど{expected}個要求します"
                f"（{len(self.items)}個でした）"
            )
        for i, item in enumerate(self.items, 1):
            if not item.strip():
                raise ValueError(f"items の{i}番目が空です")
            if len(item) > MAX_LABEL_CHARS:
                raise ValueError(
                    f"items の{i}番目が長すぎます"
                    f"（{len(item)}字、最大{MAX_LABEL_CHARS}字）: {item!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_relation(self) -> SceneVisual:
        """relation の要否と長さを検証する。

        `statement` は2つの要素を持たないので、関係性ラベルは描く場所が無い
        （どこにも表示されない値をモデルに出させると、次に見た人が「なぜ
        使われていないのか」を調べる無駄が生まれる）。

        Returns:
            SceneVisual: 検証済みの自身

        Raises:
            ValueError: statement で relation が非空、または compare/flow で
                relation が空・長すぎる場合
        """
        if self.layout is SceneLayout.STATEMENT:
            if self.relation.strip():
                raise ValueError(
                    f"layout=statement は relation を空文字列にする必要があります"
                    f"（図が無く、関係性を描く場所が無いため）: {self.relation!r}"
                )
            return self

        if not self.relation.strip():
            raise ValueError(f"layout={self.layout.value} は relation が空です")
        if len(self.relation) > MAX_RELATION_CHARS:
            raise ValueError(
                f"relation が長すぎます"
                f"（{len(self.relation)}字、最大{MAX_RELATION_CHARS}字）: {self.relation!r}"
            )
        return self
