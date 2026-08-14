"""News article data models."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class NewsCategory(StrEnum):
    """ニュースカテゴリの列挙型。

    Google News RSSのトピックに対応するカテゴリを定義。
    """

    AI = "ai"
    POLITICS = "politics"
    TECHNOLOGY = "technology"
    BUSINESS = "business"
    ENTERTAINMENT = "entertainment"
    SPORTS = "sports"
    SCIENCE = "science"
    HEALTH = "health"
    GENERAL = "general"

    @property
    def display_name(self) -> str:
        """日本語表示名を返す。"""
        names = {
            "ai": "AI・生成AI",
            "politics": "政治",
            "technology": "テクノロジー",
            "business": "ビジネス",
            "entertainment": "エンタメ",
            "sports": "スポーツ",
            "science": "科学",
            "health": "健康",
            "general": "総合",
        }
        return names.get(self.value, self.value)


@dataclass
class NewsArticle:
    """ニュース記事のデータモデル。

    Attributes:
        id: URLから生成される一意識別子
        title: 記事タイトル
        url: 元記事のURL
        source: ニュースソース名（例: NHK, 朝日新聞）
        category: ニュースカテゴリ
        summary: 記事の概要/説明
        content: 記事本文（スクレイピング後）
        thumbnail_url: サムネイル画像URL
        published_at: 公開日時
        fetched_at: 取得日時
        is_selected: ユーザーが動画生成用に選択したか
        video_generated: 動画が生成済みか
    """

    id: str
    title: str
    url: str
    source: str
    category: NewsCategory
    summary: str = ""
    content: str = ""
    thumbnail_url: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=datetime.now)
    is_selected: bool = False
    video_generated: bool = False

    @classmethod
    def generate_id(cls, url: str) -> str:
        """URLから一意のIDを生成する。

        Args:
            url: 記事のURL

        Returns:
            str: 16文字のハッシュID
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換する。

        Returns:
            Dict[str, Any]: 記事データの辞書
        """
        data = asdict(self)
        data["category"] = self.category.value
        if self.published_at:
            data["published_at"] = self.published_at.isoformat()
        data["fetched_at"] = self.fetched_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsArticle":
        """辞書から復元する。

        Args:
            data: 記事データの辞書

        Returns:
            NewsArticle: 復元された記事オブジェクト
        """
        data = data.copy()
        data["category"] = NewsCategory(data["category"])
        if data.get("published_at"):
            data["published_at"] = datetime.fromisoformat(data["published_at"])
        if data.get("fetched_at"):
            data["fetched_at"] = datetime.fromisoformat(data["fetched_at"])
        return cls(**data)

    def to_json_file(self, path: Path) -> None:
        """JSONファイルに保存する。

        Args:
            path: 保存先パス
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json_file(cls, path: Path) -> "NewsArticle":
        """JSONファイルから読み込む。

        Args:
            path: 読み込み元パス

        Returns:
            NewsArticle: 読み込まれた記事オブジェクト
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
