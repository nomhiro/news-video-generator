"""News article data models."""

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.utils.html_text import strip_html


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
    # **これが「もう投稿した」の権威。** 当時のジョブ表・投稿表の SQLite は
    # コンテナのローカルディスクにあってリビジョン更新で消えたため、そこに
    # 置くとデプロイ直後に同じ記事が再投稿された。記事データは Azure Files に
    # あるので残る（DB は 2026-08-23 に共有の PostgreSQL へ移したが、
    # 二重投稿を止める権威は移行手順とセットでしか動かさない）。
    consumed: dict[str, str] = field(default_factory=dict)
    # 人が「この記事は使わない」と決めた記録。
    #
    # **消費済み（`consumed`）とは別。** あちらは「もう出した」で、こちらは
    # 「出さない」。AI カテゴリは実測53件あり、題材の合わない記事（芸能・
    # PR 転載）を毎回読み飛ばすことになるので、畳む手段が要る。
    #
    # 権威は `consumed` と同じくこの記事データ（Azure Files 上の JSON）。
    # 当時の SQLite に置くとリビジョン更新で消え、外したはずの記事が翌日
    # 戻っていた。
    dismissed: bool = False
    # チャネル名 -> 拒否された時刻（ISO 文字列）。
    #
    # Azure OpenAI のコンテンツフィルタが記事の題材を拒否した記録。
    # **`consumed` とも `dismissed` とも別。** `consumed` は「もう出した」、
    # `dismissed` は「人が出さないと決めた」、これは「出そうとしたが
    # 恒久的に拒否された」。
    #
    # `consumed` を流用できない理由: `video_generated` が真になり、画面に
    # 「動画を作り終えた」と嘘が出る（動画は1本も出来ていない）。
    # `dismissed` を流用できない理由: 人の判断という意味論が壊れるうえ、
    # 記事が一覧から消えるので拒否されたことが画面から分からなくなる。
    #
    # 形を `consumed` に揃えてあるのは、`_must_survive_refetch` が時刻を
    # 読んで保持期間を判断する仕組みにそのまま乗せられるため。
    content_filtered: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """要約から HTML を落とす。

        **正規化はここ1箇所で行う。** RSS の `summary` は HTML を含んでよい
        仕様で、note.com のフィードは本文の先頭を要素ごと渡してくる。
        画面はテンプレートで正しくエスケープするため、素で持つと
        `<h2 id="dc8acd71-...">` がそのまま文字として見える（実測）。

        情報源ごとに落とすと片方だけが腐る。実際に `GoogleNewsSource` は
        インラインの正規表現で落としていた一方 `RssSource` は素で入れており、
        フィードに切り替えた時点で症状が出た。**構築の時点**で正規化すれば、
        `from_dict`（保存済み JSON の読み込み）も同じ経路を通るので、
        すでに汚れているデータも読んだ瞬間から読めるようになり、
        次の保存で自然に直る。

        タイトルと本文には掛けない。タイトルは HTML を含まず、本文は
        trafilatura が抽出した平文なので、どちらも対象ではない。
        """
        self.summary = strip_html(self.summary)

    @property
    def video_generated(self) -> bool:
        """動画を作り終えているか。

        `consumed` に一般化する前のフィールド名。いまの読み手は
        `templates/partials/news_list.html`（記事プールの「動画」の丸）と
        `to_dict`（切り戻し用に旧キーを書き出す）の2つ。
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

    def is_content_filtered_for(self, channel: str) -> bool:
        """そのチャネルでコンテンツフィルタに拒否されたか。"""
        return channel in self.content_filtered

    def mark_content_filtered(self, channel: str, at: datetime | None = None) -> None:
        """そのチャネルで恒久的に使えないと記録する。

        `mark_consumed` と同じく他のチャネルの記録は消さない。台本生成が
        拒否された記事は、同じ Azure OpenAI を使う X の下書き生成でも
        拒否されうるが、**判断はチャネルごとに分ける**（X は記事単位で次の
        候補へ進むので当日の投稿は落ちない。動画側だけが1件で0本になる）。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            at: 拒否された時刻。省略時は現在時刻
        """
        self.content_filtered[channel] = (at or datetime.now()).isoformat()

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

        # 旧フィールドも書く。**切り戻しのための1リリース限りの措置。**
        #
        # CLAUDE.md が定めた切り戻し手順は「前のイメージタグで
        # `az containerapp update --image`」。旧コードの `from_dict` は
        # `cls(**data)` なので、旧 `NewsArticle` に無いキーを渡されると
        # `TypeError` になる。`_load_category` は JSONDecodeError /
        # KeyError / OSError しか捕まえないため、記事一覧・動画の計画・
        # 投稿の計画がまとめて落ちる。
        #
        # 新しい `from_dict` は `video_generated` を pop するので、
        # 出しても往復は壊れない（`consumed` が権威で、こちらは派生値）。
        #
        # **ただしこれだけでは切り戻しは成立しない。** `consumed` 自体も
        # 旧 `__init__` にとって未知のキーなので、旧コードは
        # `unexpected keyword argument 'consumed'` で落ちる（実測済み）。
        # 完全に安全にするには、未知のキーを無視する `from_dict` を
        # **先に** main へ入れて1回デプロイし、そのあとで X 運用の変更を
        # 載せる、という2段のデプロイが必要。ここで出しているのは
        # 「動画の消費記録だけは失わせない」ぶんの担保。
        #
        # **消すのは意図的な判断として行う。** 「もう使っていないフィールド」
        # として掃除すると、その時点で切り戻し経路がさらに壊れる。
        # 旧イメージを使わないと決めた時点で消す。
        data["video_generated"] = self.video_generated
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

        # 知らないキーは捨てる。
        #
        # 以前は `cls(**data)` だったので、**新しいフィールドを足した版で
        # 保存した JSON を古い版が読めなかった**（`unexpected keyword
        # argument` で `_load_category` の捕まえない例外になり、記事一覧・
        # 動画の計画・投稿の計画がまとめて落ちる）。CLAUDE.md が
        # 「切り戻しを安全にしたいなら2段階で入れる」と書いているのはこの罠で、
        # その1段目がこれ。**ここから先に足すフィールドは切り戻しても壊れない**
        # （このコードを含むイメージまで戻せる限り）。
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

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
