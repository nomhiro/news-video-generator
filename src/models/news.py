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


# 記事を消費したチャネルの名前。
#
# 動画1本ぶんの `video_generated: bool` から一般化した。X が動画への
# 導線ではなく独立した発信の柱になったため、フラグ1本では
# 「動画にはしたが X には出していない」を表せない。
CHANNEL_VIDEO = "video"
CHANNEL_X = "x"


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
        consumed: チャネル名 -> 消費した時刻（ISO 文字列）
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
    # チャネル名 -> 消費した時刻（ISO 文字列）。
    #
    # **これが「もう投稿した」の権威。** ジョブ表の SQLite はコンテナの
    # ローカルディスクにあってリビジョン更新で消えるため、そこに置くと
    # デプロイ直後に同じ記事が再投稿される。記事データは Azure Files に
    # あるので残る。
    consumed: dict[str, str] = field(default_factory=dict)

    @property
    def video_generated(self) -> bool:
        """動画を作り終えているか。

        `consumed` に一般化する前のフィールド名。テンプレートと
        `planner._pick_candidates` が参照しているため property で受ける。
        書き込みは `mark_consumed` を使う（権威を1箇所に保つ）。
        """
        return self.is_consumed_by(CHANNEL_VIDEO)

    def is_consumed_by(self, channel: str) -> bool:
        """そのチャネルで既に使ったか。"""
        return channel in self.consumed

    def mark_consumed(self, channel: str, at: datetime | None = None) -> None:
        """そのチャネルで使ったと記録する。

        他のチャネルの記録は消さない（動画と X の両方で使う運用なので、
        上書きすると片方の記録を失う）。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            at: 消費時刻。省略時は現在時刻
        """
        self.consumed[channel] = (at or datetime.now()).isoformat()

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

        # 旧形式（video_generated: bool）を consumed に読み替える。
        #
        # 移行スクリプトを書かない理由: クラウドの Azure Files 上の JSON を
        # 書き換える手順が必要になり、その手順を実行し忘れた状態で
        # デプロイすると記事を全部読めなくなる。読み込み時に変換すれば、
        # 次回の保存で自然に新形式になる。
        legacy = data.pop("video_generated", None)
        if legacy and not data.get("consumed"):
            fetched = data.get("fetched_at")
            stamp = fetched.isoformat() if isinstance(fetched, datetime) else ""
            data["consumed"] = {CHANNEL_VIDEO: stamp or datetime.now().isoformat()}

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
