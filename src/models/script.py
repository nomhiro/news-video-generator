"""Script data model for video narration."""

from dataclasses import dataclass, asdict, field
from typing import List, Optional
from pathlib import Path
import json


@dataclass
class Script:
    """動画用台本データモデル。

    Attributes:
        language: 言語コード ("ja" or "en")
        title: 動画タイトル（40文字程度、YouTube/TikTokアルゴリズム最適化）
        description: 動画説明文（ショート動画アルゴリズム向け、CTA・ハッシュタグ含む）
        hashtags: ハッシュタグリスト（5〜8個）
        hook: フック（冒頭5秒で視聴者を引き付ける）
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
    segment_narrations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """辞書形式に変換する。

        Returns:
            dict: データを辞書形式で返す
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Script":
        """辞書からScriptオブジェクトを生成する。

        Args:
            data: Script属性を含む辞書

        Returns:
            Script: 生成されたScriptオブジェクト
        """
        # 後方互換性: segment_narrationsがない場合は空リストを使用
        if "segment_narrations" not in data:
            data = {**data, "segment_narrations": []}
        return cls(**data)

    def to_json_file(self, path: Path) -> None:
        """JSONファイルに保存する。

        Args:
            path: 保存先のファイルパス
        """
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
