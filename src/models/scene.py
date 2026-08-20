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
画像カードとの共食いは小さい。構造は `src/social/card_visual.py` の
`CardVisual` と同じ形（subject / key_details / labels）にしてある。
自由文・3語構造（unit/field/emphasis）を試して実物で否決した経緯は
`IllustrationConcept` のdocstringを参照。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

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


# 挿絵の視覚要素1つの最大文字数。
#
# `src/social/card_visual.py` の `MAX_DETAIL_CHARS` と同じ 120 で、こちらは
# **実測で決まった値の借用**である（暫定値ではない）。カードでは上限を90に
# したところ99字の正常な句を3回連続で弾き、カードを1枚も作れなかった。
# まともな句は40〜100字に収まり、壊れた出力（パネル1枚ぶんの記述）は
# 250〜350字になるので、閾値はその間に置く。
#
# 上限が必要な理由もカードと同じ。上限を置かないとモデルは1項目に
# パネル1枚ぶんの記述を書き、図がコマ割りになってスマホで読めなくなる。
MAX_DETAIL_CHARS = 120


class IllustrationConcept(BaseModel):
    """動画全体で共有する挿絵1枚を「名札付きの説明図」として表す。

    `SceneVisual` が**1シーン**の図の構造であるのに対し、これは**動画1本**に
    つき1枚だけ生成する挿絵（`remotion/src/Illustration.tsx`）の主題である。
    どちらも「LLM に構造を出させ、コード側がスタイルを前置する」という
    二段構えは同じだが、対象（シーン単位 / 動画単位）が違うので同じ型に
    しない。

    なぜ `CardVisual` と同じ形にしたか（2026-08-20）
    ------------------------------------------------
    ここまでに2つの構造を試して、どちらも実物で否決された。

    1. 自由文の英語1文（`illustration_subject`）——モデルに「主題」ではなく
       「場面」を作らせた。ルーティングでコストを1/10にする記事に対し、
       オフィスでコーヒーを片手に働く人々と観葉植物を描いた。
    2. `unit` / `field` / `emphasis` の3語——場面は消えたが、
       **抽象化しすぎて何の話か分からない絵**になった。実際に生成したのは
       「10本の棒のうち1本だけがティール」で、比率は伝わるが
       *何の*比率かは絵から読めない。UI のローディング・スケルトンに
       見える、という指摘を受けた。

    3語構造が抽象に振れたのは偶然ではない。スタイル文が
    `no text, letters, or numerals anywhere` で文字を全面禁止していたため、
    「これが何か」を示す手段が構図しか残っていなかった。**説明図は本質的に
    名札を必要とする。**

    そこで `CardVisual`（`src/social/card_visual.py`）と同じ形に寄せる。
    あれは「説明図＋短い日本語ラベル」のために設計され、実測で
    「2要素＋名札」の構図が最も明快だと確定している。3つ目の独自スキームを
    作らず、実証済みの契約の形を流用する。

    **`caption_ja` は持たない。** カードには「画像の下に1行」があるが、
    動画では見出し（`text_overlays`）と字幕（`segment_narrations`）を
    Remotion が絵の下に描くので、同じ主張が2回出る。880c95f の教訓
    （画像に描くならテキストで繰り返さない）をそのまま適用する。

    Attributes:
        subject: 1枚で説明する仕組みを英語1文で。記事のトーンではなく
            「図として描ける具体物」に翻訳したもの
        key_details: 描く視覚要素とその関係を**ちょうど2個**、英語の短い句で。
            3個許すと図が3グループに割れる（カードでの実測）
        labels: 画像内に描く短い**日本語**の名札を0〜4個。各
            `MAX_LABEL_CHARS` 字以内。日本語で描かせる根拠は
            `CardVisual._labels_must_be_short` を参照（2026-08-16 に実画像で
            字形の正確さを確認済み）。読み手は日本語話者なので、
            英語ラベルは「読めるが分からない」状態を作るだけだった
    """

    subject: str
    key_details: list[str] = Field(min_length=2, max_length=2)
    labels: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _check_lengths(self) -> IllustrationConcept:
        """主題が非空であること、要素と名札が長さ上限以内であることを検証する。

        `_validate_insights` と同じ理由で strip 後の長さを見る
        （空白だけの文字列を `Field(min_length=...)` では弾けない）。

        Returns:
            IllustrationConcept: 検証済みの自身

        Raises:
            ValueError: subject が空、または要素・名札が長すぎる場合
        """
        if not self.subject.strip():
            raise ValueError("subject が空です")
        for detail in self.key_details:
            stripped = detail.strip()
            if not stripped:
                raise ValueError("key_details に空の要素があります")
            if len(stripped) > MAX_DETAIL_CHARS:
                raise ValueError(
                    f"視覚要素が長すぎます（{len(stripped)}字、最大{MAX_DETAIL_CHARS}字）。"
                    f"場面の説明ではなく短い句にしてください: {stripped[:40]!r}"
                )
        for label in self.labels:
            stripped = label.strip()
            if not stripped:
                raise ValueError("labels に空のラベルがあります")
            if len(stripped) > MAX_LABEL_CHARS:
                raise ValueError(
                    f"ラベルが長すぎます（{len(stripped)}字、最大{MAX_LABEL_CHARS}字）: {stripped!r}"
                )
        return self
