"""Script data model for video narration.

Azure OpenAI の Structured Outputs (responses.parse) のスキーマを兼ねる。
このモデルが LLM 出力の契約そのものになるため、フィールドの追加・変更は
生成される JSON スキーマに直接反映される。
"""

import json
from pathlib import Path
from typing import List

from pydantic import BaseModel, model_validator


def _join_narration(segments: List[str], language: str) -> str:
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


def _validate_aligned_segments(model: BaseModel) -> None:
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
    hashtags: List[str]
    hook: str
    main_points: List[str]
    conclusion: str
    image_prompts: List[str]
    text_overlays: List[str]
    estimated_duration: int
    segment_narrations: List[str]

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

    def to_script(self, language: str) -> "Script":
        """言語を与えて `Script` に変換する。

        `full_narration` はセグメントの連結で導出する。

        Args:
            language: 言語コード ("ja" or "en")

        Returns:
            Script: 完成した台本
        """
        return Script(
            language=language,
            full_narration=_join_narration(self.segment_narrations, language),
            **self.model_dump(),
        )


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
    hashtags: List[str]
    hook: str
    main_points: List[str]
    conclusion: str
    full_narration: str
    image_prompts: List[str]
    text_overlays: List[str]
    estimated_duration: int
    segment_narrations: List[str]

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
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
