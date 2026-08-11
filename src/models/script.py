"""Script data model for video narration.

Azure OpenAI の Structured Outputs (responses.parse) のスキーマを兼ねる。
このモデルが LLM 出力の契約そのものになるため、フィールドの追加・変更は
生成される JSON スキーマに直接反映される。
"""

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, model_validator


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
    """整合性検証が必要とする3フィールドだけを表す構造的な型。

    ScriptDraft と Script の両方がこれを満たす。
    """

    segment_narrations: list[str]
    image_prompts: list[str]
    text_overlays: list[str]


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
    }
    if len(set(counts.values())) != 1:
        detail = ", ".join(f"{k}={v}" for k, v in counts.items())
        raise ValueError(f"配列長の不一致: {detail}")

    for field_name in ("segment_narrations", "image_prompts", "text_overlays"):
        for i, value in enumerate(getattr(model, field_name), 1):
            if not value or not value.strip():
                raise ValueError(f"{field_name} の{i}番目が空です")


class ScriptDraft(BaseModel):
    """LLM が出力する台本（Structured Outputs のスキーマ）。

    `Script` との違いは意図的なもの:

    - `language` を持たない — 呼び出し元が権威を持つ値であり、
      モデルに出させて上書きするのは無駄で紛らわしい。
    - `full_narration` を持たない — `segment_narrations` の連結で導出できる。
      両方を出力させると「連結が full_narration と一致すること」という
      冗長な制約が生まれ、モデルは一致を優先して空セグメントで
      パディングする挙動を示した。導出にすれば矛盾は構造的に起こらない。

    Attributes:
        title: 動画タイトル（YouTube/TikTokアルゴリズム最適化）
        description: 動画説明文（CTA・ハッシュタグ含む）
        hashtags: ハッシュタグリスト（5〜8個）
        hook: フック（冒頭で視聴者を引き付ける）
        main_points: メインポイントのリスト
        conclusion: 結論（CTA含む）
        image_prompts: 画像生成プロンプト（英語）
        text_overlays: 各画像に表示するテキスト
        estimated_duration: 推定秒数
        segment_narrations: 各画像に対応するナレーションセグメント
    """

    title: str
    description: str
    hashtags: list[str]
    hook: str
    main_points: list[str]
    conclusion: str
    image_prompts: list[str]
    text_overlays: list[str]
    estimated_duration: int
    segment_narrations: list[str]

    @model_validator(mode="after")
    def _check_segment_alignment(self) -> "ScriptDraft":
        """セグメント・画像・オーバーレイの整合性を検証する。

        音声のタイミング同期と動画合成が
        「segment_narrations / image_prompts / text_overlays の
        要素数が一致していること」に依存しているため、ここで担保する。

        Returns:
            ScriptDraft: 検証済みの自身

        Raises:
            ValueError: 要素数が一致しない、または空要素がある場合
        """
        _validate_aligned_segments(self)
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

    def to_script(self, language: str, actual_duration_sec: float | None = None) -> "Script":
        """言語を与えて `Script` に変換する。

        `full_narration` はセグメントの連結で導出する。
        `estimated_duration` はモデルの自己申告を使わず、
        文字数からの推定、または実測値で置き換える。

        Args:
            language: 言語コード ("ja" or "en")
            actual_duration_sec: 音声生成後に実測した秒数。
                与えられればこれを採用する

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
        return Script(language=language, full_narration=narration, **payload)


class Script(BaseModel):
    """動画用台本データモデル。

    Attributes:
        language: 言語コード ("ja" or "en")
        title: 動画タイトル（YouTube/TikTokアルゴリズム最適化）
        description: 動画説明文（CTA・ハッシュタグ含む）
        hashtags: ハッシュタグリスト（5〜8個）
        hook: フック（冒頭で視聴者を引き付ける）
        main_points: メインポイントのリスト
        conclusion: 結論（CTA含む）
        full_narration: 完全なナレーション台本
        image_prompts: 画像生成プロンプト（英語）
        text_overlays: 各画像に表示するテキスト
        estimated_duration: 推定秒数
        segment_narrations: 各画像に対応するナレーションセグメント（音声タイミング同期用）
    """

    language: str
    title: str
    description: str
    hashtags: list[str]
    hook: str
    main_points: list[str]
    conclusion: str
    full_narration: str
    image_prompts: list[str]
    text_overlays: list[str]
    estimated_duration: int
    segment_narrations: list[str]

    @model_validator(mode="after")
    def _check_segment_alignment(self) -> "Script":
        """セグメント・画像・オーバーレイの整合性を検証する。

        Returns:
            Script: 検証済みの自身

        Raises:
            ValueError: 要素数が一致しない、または空要素がある場合
        """
        _validate_aligned_segments(self)
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
