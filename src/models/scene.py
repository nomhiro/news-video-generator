"""動画に描くものの視覚指示。1シーン単位（`SceneVisual`）と動画1本単位

（`IllustrationConcept`）の2つを持つ。

なぜ画像生成モデルに描かせないか（`SceneVisual`）
--------------------------------------------------
図解主体に振ったので、描く対象は「絵」ではなく「構造」である。LLM に構造を
出させて Remotion（React）が描けば、文字は常に正確で、回ごとのブレも無く、
`gpt-image-2` のクォータ（サブスクリプション・リージョン単位で上限4）も
消費しない。動画がクォータを使わなくなると、X の画像カードとの共食いも消える。

`src/social/card_visual.py` の `CardVisual` と役割は似ているが、意図的に
別モデルにしてある。あちらは**画像生成モデルへの英語の指示**で、こちらは
**レンダラが読む構造**。共有すると、片方の都合でもう片方が壊れる。

なぜ挿絵（`IllustrationConcept`）だけは画像生成モデルに描かせるか
--------------------------------------------------------------------
挿絵は動画1本につき1枚だけで、`gpt-image-2` のクォータを消費してもX の
画像カードとの共食いは小さい。ただし主題を自由文で出させると場面を作って
しまうため、`unit` / `field` / `emphasis` の3語に構造を固定してある
（旧 `left` / `right` / `relation` を置き換えた経緯は `IllustrationConcept`
のdocstringを参照）。
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


# `IllustrationConcept` の各語の最大文字数。
#
# `SceneVisual.items` の名札（`MAX_LABEL_CHARS` = 8字）は日本語の名札用で、
# こちらは英語の画像生成プロンプト用の語なので別の定数にする。3つの
# フィールドは役割が違う（繰り返す1形状の名前 / 全体の記述 / その一部）ので、
# 長さの上限も1つに揃えない。
#
# unit は "square" のような形状1つの名前なので、複合語でも短い
# （"rounded square" 程度）。field/emphasis は個数や範囲の言い回しを含む
# 短い句（"a 10x10 grid", "the bottom tenth"）なので、unit より長い句が
# 通る上限にする。いずれも実物（実際に生成した挿絵）を見て決め直す前提の
# 暫定値であり、`MAX_LABEL_CHARS` と同じ経緯を辿る。
MAX_UNIT_CHARS = 20
MAX_FIELD_CHARS = 60
MAX_EMPHASIS_CHARS = 40


class IllustrationConcept(BaseModel):
    """動画全体で共有する挿絵1枚の主題を「反復する形の中の強調」で表す。

    `SceneVisual` が**1シーン**の図の構造であるのに対し、これは**動画1本**に
    つき1枚だけ生成する挿絵（`remotion/src/Illustration.tsx`）の主題である。
    どちらも「LLM に構造を出させ、コード側がスタイルを前置する」という
    二段構えは同じだが、対象（シーン単位 / 動画単位）が違うので同じ型に
    しない。

    なぜ自由文の1文（旧 `illustration_subject`）をやめたか
    --------------------------------------------------------
    自由文はモデルに「場面」を作らせてしまう。ルーティングでコストを
    1/10にする記事に対して実際に生成した挿絵は、オフィスでコーヒーを
    片手に働く人々、観葉植物、丸いアイコン4つを描いていた——文章としては
    主題に触れているが、絵として伝わるのは「AIっぽい何か」でしかない。
    `CardVisual.key_details` をちょうど2個に固定した判断と同じで、範囲では
    なく固定値の構造を強制すれば、モデルは場面を描く余地を持たない。

    なぜ `left` / `right` / `relation` の3語構造もやめたか（2026-08-17）
    --------------------------------------------------------------------
    自由文問題は解決したが、3語構造には別の欠陥があった。「2つの要素を
    矢印で繋ぐ」という**構図**そのものを強制してしまい、内容が何であれ
    同じ形の絵（クリップアート的な図解）しか作れない。実際に踏んだ壊れ方は
    3つとも構造ではなく**語の選び方**に起因していた——
    `left="expert models"` を画像モデルは「人間の専門家」と読んで人物を描き、
    `right="reduced compute"` は描けない抽象量なので CPU チップになり、
    `relation="selected for"` は素の矢印に潰れて「導く」の意味しか残らな
    かった。だが4つ目の問題は語の選び方では直らない——**2つの異なる物を
    矢印で繋ぐこと自体が構図として凡庸**で、内容を反映しない。

    代わりに「反復する同じ形の中で、一部だけが強調されている」という構図に
    固定する。多数のマスから数個だけが際立つ図は、一目で「全体のうち一部
    だけが特別」という関係を伝え、構図自体が作品として成立する
    （矢印2つ＋アイコン2つの図解より意匠性が高い）。

    Attributes:
        unit: 反復する描ける形の名前（例: "square", "bar", "node", "block"）。
            人物・抽象量は禁止で、実際に描ける単純な図形1つに限る
        field: その形が並ぶ全体（例: "a 10x10 grid", "a tall stack of 20 bars"）
        emphasis: 全体のうち際立たせる一部（例: "four cells", "the bottom tenth"）。
            **個数は近似でよい。** 画像生成モデルは正確な個数を守らない
            （「100個中4個」の指示が「90個中5個」で返ることがある）。
            図には数字を描かせないので読み手が見るのは「比率」の印象だけであり、
            厳密さは不要。厳密さが必要になったら、それはコードで図形を描く
            判断に切り替える場面であり、この3語では解決しない
    """

    unit: str
    field: str
    emphasis: str

    @model_validator(mode="after")
    def _check_words(self) -> IllustrationConcept:
        """各語が非空・長さ上限以内であることを検証する。

        `_validate_insights` と同じ理由で strip 後の長さを見る
        （空白だけの文字列を `Field(min_length=...)` では弾けない）。

        Returns:
            IllustrationConcept: 検証済みの自身

        Raises:
            ValueError: いずれかの語が空、空白のみ、または長すぎる場合
        """
        limits = {
            "unit": MAX_UNIT_CHARS,
            "field": MAX_FIELD_CHARS,
            "emphasis": MAX_EMPHASIS_CHARS,
        }
        for field_name, limit in limits.items():
            value: str = getattr(self, field_name)
            stripped = value.strip()
            if not stripped:
                raise ValueError(f"{field_name} が空です")
            if len(stripped) > limit:
                raise ValueError(
                    f"{field_name} が長すぎます（{len(stripped)}字、最大{limit}字）: {stripped!r}"
                )
        return self
