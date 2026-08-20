"""Script data model for video narration.

Azure OpenAI の Structured Outputs (responses.parse) のスキーマを兼ねる。
このモデルが LLM 出力の契約そのものになるため、フィールドの追加・変更は
生成される JSON スキーマに直接反映される。
"""

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from src.models.scene import IllustrationConcept, SceneLayout, SceneVisual


def _join_narration(segments: list[str], language: str) -> str:
    """セグメントを連結して完全なナレーションを作る。

    日本語は語間に空白を入れない。英語などは単語境界が必要なので
    半角空白で連結する。

    Args:
        segments: ナレーションセグメント
        language: 言語コード ("ja" or "en")

    Returns:
        str: 連結されたナレーション
    """
    separator = "" if language == "ja" else " "
    return separator.join(s.strip() for s in segments)


class _HasAlignedSegments(Protocol):
    """整合性検証が必要とする4フィールドだけを表す構造的な型。

    ScriptDraft と Script の両方がこれを満たす。
    """

    segment_narrations: list[str]
    image_prompts: list[str]
    text_overlays: list[str]
    scenes: list[SceneVisual]


class _HasInsights(Protocol):
    """独自解説の2フィールドだけを表す構造的な型。

    ScriptDraft と Script の両方がこれを満たす。
    """

    technical_insight: str
    practical_impact: str


# 独自解説フィールドの最低文字数。
#
# 言語非依存の値にしている。ScriptDraft は意図的に `language` を持たない
# （呼び出し元が権威を持つ）ため、バリデータの中で言語別の閾値を選べない。
# 40文字は「一言で流していない」ことの担保であって、質の保証ではない
# （質はスキーマでは担保できないので、生成物を読む工程が必要）。
MIN_INSIGHT_CHARS = 40


# 見出し（`text_overlays` の1要素）の最大文字数。
#
# なぜ必要か: Remotion では `text_overlays[i]` が画面中央の見出しとして
# 92px（`statement` は112px）で描かれる。`AbsoluteFill` はスクロールしないので、
# 伸びた見出しは下の字幕スクリムに重なるか画面外に切れる。**ffmpeg レンダラでは
# 症状が出なかった**（`video_composer._wrap_text` が14文字で機械的に折り返して
# 吸収していた）ため、この制約が無いことに気付きにくい。
#
# 45 の根拠（**実際にレンダリングしたフレームから逆算した値**。以前の 60 は
# 描画幅からの算術的な見積りで、レンダリングして確かめておらず、
# 実際には描画不可能な値だった）:
#   - BudouX のフレーズ境界改行（ZWSP 挿入、`Headline.tsx` の
#     `CHARS_PER_LINE_AT_BASE` 参照）は行を理論上の文字数まで詰められない。
#     実測では92pxのとき1行あたり約8字（10字ではない）。
#   - 上限まで縮めたとき（`MIN_SCALE = 0.5`）でも1行約16字 × 3行 = 48字が
#     描画できる限度。60字はこの限度を超えており、**3行のどの組み合わせでも
#     収まらず末尾の文字が無音で消える**ことを実測で確認した
#     （43字の見出しで末尾「根本から変わることになった」が切れて描かれなかった）。
#   - 行の折れ端に余裕を残すため 45 を上限にする。
#   - `ScriptDraft` は意図的に `language` を持たないため、閾値は言語非依存で
#     なければならない（`MIN_INSIGHT_CHARS` と同じ制約）。英語の妥当な見出し
#     （例: "Inference costs drop by an order of magnitude" = 45字）がちょうど
#     境界に来る値になっている。文字数ではなく語数で切ると日本語側が
#     表現できない。
#
# `MAX_LABEL_CHARS` と同じく**実物を見て決め直す前提の暫定値**。カードでは
# 上限90字が正常な出力を3回連続で弾いた前例があるので、渋りすぎも害になる。
MAX_HEADLINE_CHARS = 45


# 話速 1.1〜1.25 での実測に基づく読み上げ速度。
# 42.82秒 / 255文字 ≒ 6.0 文字/秒（日本語, 話速1.25）
# 英語は 1語 ≒ 2.6 語/秒 相当。
_CHARS_PER_SECOND_JA = 6.0
_WORDS_PER_SECOND_EN = 2.6


def estimate_duration_sec(narration: str, language: str) -> int:
    """ナレーションの分量から尺を推定する。

    モデルが自己申告する ``estimated_duration`` は実尺と一致しない
    （35と申告して実測59.6秒だった）。文字数からの推定の方が当たる。
    最終的な検証は合成後の ffprobe で行う。

    Args:
        narration: ナレーション全文
        language: 言語コード ("ja" or "en")

    Returns:
        int: 推定秒数（最低1秒）
    """
    if language == "ja":
        seconds = len(narration) / _CHARS_PER_SECOND_JA
    else:
        seconds = len(narration.split()) / _WORDS_PER_SECOND_EN
    return max(1, round(seconds))


def _validate_aligned_segments(model: _HasAlignedSegments) -> None:
    """segment_narrations / image_prompts / text_overlays の整合性を検証する。

    Args:
        model: 上記3フィールドを持つモデル

    Raises:
        ValueError: 要素数が一致しない、または空要素がある場合
    """
    segments = model.segment_narrations
    if not segments:
        raise ValueError("segment_narrations が空です")

    counts = {
        "segment_narrations": len(segments),
        "image_prompts": len(model.image_prompts),
        "text_overlays": len(model.text_overlays),
        "scenes": len(model.scenes),
    }
    if len(set(counts.values())) != 1:
        detail = ", ".join(f"{k}={v}" for k, v in counts.items())
        raise ValueError(f"配列長の不一致: {detail}")

    for field_name in ("segment_narrations", "image_prompts", "text_overlays"):
        for i, value in enumerate(getattr(model, field_name), 1):
            if not value or not value.strip():
                raise ValueError(f"{field_name} の{i}番目が空です")


def _validate_insights(model: _HasInsights) -> None:
    """独自解説フィールドが実質的に埋まっているか検証する。

    `Field(min_length=...)` は空白だけの文字列を通してしまう
    （全角空白を40個並べれば通る）。strip 後の長さで見る。

    Args:
        model: 独自解説の2フィールドを持つモデル

    Raises:
        ValueError: 空、空白のみ、または短すぎる場合
    """
    for field_name in ("technical_insight", "practical_impact"):
        value: str = getattr(model, field_name)
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field_name} が空です")
        if len(stripped) < MIN_INSIGHT_CHARS:
            raise ValueError(
                f"{field_name} が短すぎます: {len(stripped)}文字 (最低{MIN_INSIGHT_CHARS}文字)"
            )


def _validate_headlines(overlays: list[str]) -> None:
    """見出しが画面に収まる長さか検証する。

    空の検査は `_validate_aligned_segments` が持っているので、ここは長さだけ見る。
    上限の根拠は `MAX_HEADLINE_CHARS` のコメントを参照。

    Args:
        overlays: 見出しの配列（`text_overlays`）

    Raises:
        ValueError: 上限を超える要素がある場合
    """
    for i, text in enumerate(overlays, 1):
        if len(text.strip()) > MAX_HEADLINE_CHARS:
            raise ValueError(
                f"text_overlays の{i}番目が長すぎます"
                f"（{len(text.strip())}字、最大{MAX_HEADLINE_CHARS}字）: {text!r}"
            )


def _validate_scenes(scenes: list[SceneVisual]) -> None:
    """図を持たないシーンが多すぎないか検証する。

    `statement` は図を持たない。モデルが全部これを選べば図が1枚も出ず、
    **静止画スライドショーだった頃と同じ紙芝居に戻る**。これは実在する
    劣化経路で、モデルは常に楽な選択肢に寄る。`check_length_budget` と
    同じ判断で、指示ではなく検査で抑える。

    Args:
        scenes: 検証するシーン

    Raises:
        ValueError: statement が半数を超える場合
    """
    limit = len(scenes) // 2
    statements = sum(1 for scene in scenes if scene.layout is SceneLayout.STATEMENT)
    if statements > limit:
        raise ValueError(
            f"図を持たない statement が多すぎます: {statements}個"
            f"（{len(scenes)}シーン中 最大{limit}個）"
        )


def _with_source(description: str, source_url: str, language: str) -> str:
    """説明文に出典 URL を追記する。

    YouTube の「再利用されたコンテンツ」ポリシー対策として、出典は必ず
    説明文に載せる。モデルに書かせず**コード側で追記する**理由は、
    モデルが URL を知らないため（プロンプト入力は記事のタイトルと本文だけ）。
    出させれば確実に捏造する。

    Args:
        description: モデルが書いた説明文
        source_url: 出典 URL（空なら何もしない）
        language: 言語コード ("ja" or "en")

    Returns:
        str: 出典を含む説明文
    """
    # 空のときは追記しない。CLI は自由テキストのトピックを取るので
    # URL を持たない呼び出しがある。
    if not source_url:
        return description
    # モデルが本文中に URL を書いていた場合の二重追記を避ける
    if source_url in description:
        return description
    label = "出典" if language == "ja" else "Source"
    return f"{description}\n\n{label}: {source_url}"


class ScriptDraft(BaseModel):
    """LLM が出力する台本（Structured Outputs のスキーマ）。

    `Script` との違いは意図的なもの:

    - `language` を持たない — 呼び出し元が権威を持つ値であり、
      モデルに出させて上書きするのは無駄で紛らわしい。
    - `full_narration` を持たない — `segment_narrations` の連結で導出できる。
      両方を出力させると「連結が full_narration と一致すること」という
      冗長な制約が生まれ、モデルは一致を優先して空セグメントで
      パディングする挙動を示した。導出にすれば矛盾は構造的に起こらない。
    - `source_url` を持たない — モデルは URL を知らない（プロンプト入力は
      記事のタイトルと本文だけ）。出させれば捏造する。`language` と同じく
      呼び出し元が権威を持つ値として `to_script` で受ける。

    `image_prompts` は Remotion レンダラでは使わないが**残してある**。
    `VIDEO_RENDERER=ffmpeg` への退路を生かすため、両レンダラが同じ台本から
    動く状態を保つ。

    `technical_insight` / `practical_impact` を必須にしている理由:
    ニュースをなぞるだけの出力は埋もれるうえ、YouTube の
    「再利用されたコンテンツ」ポリシーに抵触するリスクがある。
    Structured Outputs では**必須フィールドをモデルは省略できない**ので、
    プロンプトでお願いするより保証が強い。

    Attributes:
        title: 動画タイトル（YouTube/TikTokアルゴリズム最適化）
        description: 動画説明文（CTA・ハッシュタグ含む）
        hashtags: ハッシュタグリスト（5〜8個）
        hook: フック（冒頭で視聴者を引き付ける）
        main_points: メインポイントのリスト
        conclusion: 結論（CTA含む）
        technical_insight: 技術的にどういう仕組みなのかの独自解説
        practical_impact: 実務・現場で何がインパクトなのかの独自考察
        image_prompts: 画像生成プロンプト（英語）
        text_overlays: 各画像に表示するテキスト
        estimated_duration: 推定秒数
        segment_narrations: 各画像に対応するナレーションセグメント
        scenes: 各セグメントの図解の構造（レンダラが読む）
        illustration_concept: 動画全体で共有する挿絵1枚の主題を
            「2つの要素とその関係」で表したもの。Remotion レンダラが
            1本につき1枚だけ生成する挿絵の「何を描くか」。
            `CardVisual.subject` と同じ二段構え（LLM が主題、コード側が
            スタイルを前置）の *what* 側で、スタイルの語（medium / palette /
            rendering technique）は含めない。検証は `IllustrationConcept`
            自身が持つ（ネストした pydantic モデルなので自動で走る）
    """

    title: str
    description: str
    hashtags: list[str]
    hook: str
    main_points: list[str]
    conclusion: str
    technical_insight: str = Field(min_length=MIN_INSIGHT_CHARS)
    practical_impact: str = Field(min_length=MIN_INSIGHT_CHARS)
    image_prompts: list[str]
    text_overlays: list[str]
    estimated_duration: int
    segment_narrations: list[str]
    scenes: list[SceneVisual]
    illustration_concept: IllustrationConcept

    @model_validator(mode="after")
    def _check_content(self) -> "ScriptDraft":
        """セグメントの整合性と独自解説の実質を検証する。

        音声のタイミング同期と動画合成が
        「segment_narrations / image_prompts / text_overlays / scenes の
        要素数が一致していること」に依存しているため、ここで担保する。

        Returns:
            ScriptDraft: 検証済みの自身

        Raises:
            ValueError: 要素数が一致しない、空要素がある、見出しが長すぎる、
                独自解説が空・短すぎる、または statement が多すぎる場合
        """
        _validate_aligned_segments(self)
        _validate_headlines(self.text_overlays)
        _validate_insights(self)
        _validate_scenes(self.scenes)
        return self

    def narration_length(self, language: str) -> int:
        """導出されるナレーション全体の文字数。

        Args:
            language: 言語コード ("ja" or "en")

        Returns:
            int: 文字数
        """
        return len(_join_narration(self.segment_narrations, language))

    def check_length_budget(self, language: str, budget: tuple[int, int]) -> str | None:
        """ナレーションの分量が許容範囲に収まっているか調べる。

        モデルはプロンプトの文字数指示を守らない。実測では
        「合計250〜330文字」の指示に対して 484文字（47%超過）を返し、
        35秒目標の動画が59.6秒になった。指示ではなく検査で抑える。

        下限は緩く見る（短すぎる方が実害が小さく、内容を薄く伸ばす
        誘導を避けたい）。上限は超えたら再生成させる。

        Args:
            language: 言語コード
            budget: (下限, 上限) の文字数

        Returns:
            範囲外なら理由の文字列。収まっていれば None
        """
        low, high = budget
        actual = self.narration_length(language)
        if actual > high:
            over = round((actual / high - 1) * 100)
            return f"ナレーションが長すぎます: {actual}文字 (上限{high}文字を{over}%超過)"
        # 下限の半分を切るのは、明らかに内容が足りていない
        if actual < low // 2:
            return f"ナレーションが短すぎます: {actual}文字 (目標下限{low}文字)"
        return None

    def to_script(
        self,
        language: str,
        actual_duration_sec: float | None = None,
        source_url: str = "",
    ) -> "Script":
        """言語と出典を与えて `Script` に変換する。

        `full_narration` はセグメントの連結で導出する。
        `estimated_duration` はモデルの自己申告を使わず、
        文字数からの推定、または実測値で置き換える。
        出典 URL は `description` に追記する（モデルには書かせない）。

        Args:
            language: 言語コード ("ja" or "en")
            actual_duration_sec: 音声生成後に実測した秒数。
                与えられればこれを採用する
            source_url: 元記事の URL。空なら説明文に追記しない

        Returns:
            Script: 完成した台本
        """
        narration = _join_narration(self.segment_narrations, language)
        if actual_duration_sec is not None:
            duration = round(actual_duration_sec)
        else:
            duration = estimate_duration_sec(narration, language)

        payload = self.model_dump()
        payload["estimated_duration"] = duration
        payload["description"] = _with_source(self.description, source_url, language)
        return Script(
            language=language,
            full_narration=narration,
            source_url=source_url,
            **payload,
        )


class Script(BaseModel):
    """動画用台本データモデル。

    `source_url` は必須フィールドだが**空文字列を許す**。CLI は自由テキストの
    トピックを受け取るので、URL を持たない呼び出しが実在する
    （ニュース経由の生成では常に `NewsArticle.url` が入る）。

    Attributes:
        language: 言語コード ("ja" or "en")
        title: 動画タイトル（YouTube/TikTokアルゴリズム最適化）
        description: 動画説明文（CTA・ハッシュタグ・出典含む）
        hashtags: ハッシュタグリスト（5〜8個）
        hook: フック（冒頭で視聴者を引き付ける）
        main_points: メインポイントのリスト
        conclusion: 結論（CTA含む）
        technical_insight: 技術的にどういう仕組みなのかの独自解説
        practical_impact: 実務・現場で何がインパクトなのかの独自考察
        source_url: 元記事の URL（呼び出し元が与える。無ければ空文字列）
        full_narration: 完全なナレーション台本
        image_prompts: 画像生成プロンプト（英語）
        text_overlays: 各画像に表示するテキスト
        estimated_duration: 推定秒数
        segment_narrations: 各画像に対応するナレーションセグメント（音声タイミング同期用）
        scenes: 各セグメントの図解の構造（レンダラが読む）
        illustration_concept: 動画全体で共有する挿絵1枚の主題を
            「2つの要素とその関係」で表したもの
    """

    language: str
    title: str
    description: str
    hashtags: list[str]
    hook: str
    main_points: list[str]
    conclusion: str
    technical_insight: str = Field(min_length=MIN_INSIGHT_CHARS)
    practical_impact: str = Field(min_length=MIN_INSIGHT_CHARS)
    source_url: str
    full_narration: str
    image_prompts: list[str]
    text_overlays: list[str]
    estimated_duration: int
    segment_narrations: list[str]
    scenes: list[SceneVisual]
    illustration_concept: IllustrationConcept

    @model_validator(mode="after")
    def _check_content(self) -> "Script":
        """セグメントの整合性と独自解説の実質を検証する。

        Returns:
            Script: 検証済みの自身

        Raises:
            ValueError: 要素数が一致しない、空要素がある、見出しが長すぎる、
                独自解説が空・短すぎる、または statement が多すぎる場合
        """
        _validate_aligned_segments(self)
        _validate_headlines(self.text_overlays)
        _validate_insights(self)
        _validate_scenes(self.scenes)
        return self

    def to_dict(self) -> dict:
        """辞書形式に変換する。

        Returns:
            dict: データを辞書形式で返す
        """
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "Script":
        """辞書からScriptオブジェクトを生成する。

        Args:
            data: Script属性を含む辞書

        Returns:
            Script: 生成されたScriptオブジェクト
        """
        return cls.model_validate(data)

    def to_json_file(self, path: Path) -> None:
        """JSONファイルに保存する。

        Args:
            path: 保存先のファイルパス
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json_file(cls, path: Path) -> "Script":
        """JSONファイルから読み込む。

        Args:
            path: 読み込むファイルパス

        Returns:
            Script: 読み込んだScriptオブジェクト
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
