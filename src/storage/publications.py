"""どの動画をどのチャネルに公開したかの記録。

**なぜ DB ではなくファイルなのか。** これは二重公開を止める権威である。
当時のジョブ表・投稿表の SQLite はコンテナのローカルディスクにあり、`main` への
マージが即デプロイなのでリビジョン更新で消えた。消えた直後に同じ動画を
もう一度 YouTube に上げれば、**取り消せない外向きの操作が2回起きる**。
（DB は 2026-08-23 に共有の PostgreSQL へ移して消えなくなったが、権威を
動かす作業は移行手順とセットでしかやらないので、ここはファイルのまま。）
X 側は「二度出さない」ことを最優先に組んである（`src/models/social.py` の
遷移表に `POSTING → SCHEDULED` が無い）のに、YouTube 側にはガードが1つも
無かった。置き場所は記事の選択状態（`data/news/*.json`）や投稿スイッチ
（`data/x_posting.json`）と同じ Azure Files。

**キーは生成物のキー（`videos/....mp4`）で、記事ではない。** 1つの記事から
複数の動画ができる（形式 short/tiktok/long × 言語 ja/en）。記事に持たせると
「3本作って1本だけ公開した」を表せず、ガードが必要とする粒度に届かない。
`article_id` は記事カードにバッジを出すための逆引きとして**添える**だけで、
無くてもガードは働く——CLI の自由テキストから作った動画は台本の
`source_url` が空で記事に辿れないし、手で置いた動画には台本 JSON が無い。

**時刻は UTC で持つ。** 表示は画面側が `SCHEDULE_TIMEZONE` に直す。
`NewsArticle.consumed` は naive な現地時刻を書いているが、あちらは
コンテナ（UTC）とローカル（JST）で意味が変わる既知の弱点なので倣わない。
"""

import json
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.storage.artifacts import normalize_key
from src.utils.logger import log_error

CHANNEL_YOUTUBE = "youtube"
CHANNEL_TIKTOK = "tiktok"

# 画面に出す名前。チャネル名そのものを画面に出すと小文字の英語になる。
CHANNEL_LABELS = {CHANNEL_YOUTUBE: "YouTube", CHANNEL_TIKTOK: "TikTok"}


@dataclass(frozen=True)
class Publication:
    """1つの動画を1つのチャネルに出した記録。"""

    channel: str
    # 公開先が返した ID。YouTube は動画 ID、TikTok は publish_id
    # （あちらの API は動画 ID を返さない）。
    external_id: str
    # 公開先の URL。**TikTok では実際の動画 URL ではない**
    # （`TikTokUploader` が固定のプロフィール誘導を返す）。
    url: str
    # 公開した時刻（UTC、読めなければ None）。
    at: datetime | None

    @property
    def label(self) -> str:
        """画面に出すチャネル名。"""
        return CHANNEL_LABELS.get(self.channel, self.channel)


class PublicationStore:
    """`data/publications.json` の読み書き。

    ロックの作りは `NewsAggregator` に倣う（`threading.RLock` + 一時ファイル
    への書き出し + `Path.replace`）。**読み取りもロックで守る**——Windows では
    置換の瞬間に読み手が `PermissionError` を受ける（あちらで実測した）。
    `PostingSwitch` がロックを持たないのは真偽値1つで read-modify-write が
    無いためで、こちらは「既存の記録に1件足す」なので同じではいけない。
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def record(
        self,
        video_key: str,
        channel: str,
        *,
        external_id: str,
        url: str,
        article_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """公開を記録する。

        同じ動画・同じチャネルの記録は**上書きする**。限定公開から公開へ
        やり直す経路が実在するので、2回目の公開を拒否はしない——記録の役割は
        「1回目があったことを画面に出す」ことで、押させないことではない。

        Args:
            video_key: 生成物のキー（`videos/....mp4`）
            channel: CHANNEL_YOUTUBE / CHANNEL_TIKTOK
            external_id: 公開先が返した ID
            url: 公開先の URL
            article_id: 元記事の ID（辿れなければ None）
            at: 公開時刻（既定は現在時刻）
        """
        key = normalize_key(video_key)
        stamp = (at or datetime.now(UTC)).isoformat()

        with self._locked():
            data = self._load()
            entry = data.setdefault(key, {})
            # `article_id` は後から辿れなくなることがある（台本を消した動画）。
            # 既にあるものを None で消さない。
            if article_id:
                entry["article_id"] = article_id
            entry[channel] = {"id": external_id, "url": url, "at": stamp}
            self._save(data)

    def for_video(self, video_key: str) -> list[Publication]:
        """その動画の公開記録を、チャネル名の順で返す。"""
        try:
            key = normalize_key(video_key)
        except ValueError:
            return []
        with self._locked():
            entry = self._load().get(key, {})
        return _publications(entry)

    def by_article(self) -> dict[str, list[Publication]]:
        """記事 ID ごとの公開記録。

        記事カードに「公開まで届いたか」を出すための逆引き。1つの記事から
        複数の動画ができるので、値はリストになる（`article_id` を持たない
        記録は入らない）。
        """
        with self._locked():
            data = self._load()

        found: dict[str, list[Publication]] = {}
        for entry in data.values():
            article_id = entry.get("article_id")
            if not isinstance(article_id, str) or not article_id:
                continue
            found.setdefault(article_id, []).extend(_publications(entry))
        return found

    def _load(self) -> dict[str, dict[str, Any]]:
        """ファイルを読む。**壊れていたら「無い」として扱う。**

        例外にすると、書き込みが中断された壊れた JSON が残っただけで
        画面が 500 になり、公開の記録も画面も同時に失う。記録が読めない
        場合に失う安全は「1回目の公開が画面に出ない」ことだけで、
        画面全体が落ちるよりは軽い（`TokenStore.read_json` と同じ判断）。
        """
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log_error(f"公開の記録を読めませんでした（無いものとして扱います）: {e}")
            return {}
        if not isinstance(data, dict):
            log_error("公開の記録の形式が想定と違います（無いものとして扱います）")
            return {}
        return {key: value for key, value in data.items() if isinstance(value, dict)}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_path.replace(self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise


def _publications(entry: dict[str, Any]) -> list[Publication]:
    """1件の記録（`article_id` とチャネルの混在）を `Publication` に直す。"""
    found = []
    for channel in (CHANNEL_YOUTUBE, CHANNEL_TIKTOK):
        raw = entry.get(channel)
        if not isinstance(raw, dict):
            continue
        found.append(
            Publication(
                channel=channel,
                external_id=str(raw.get("id", "")),
                url=str(raw.get("url", "")),
                at=_parse(raw.get("at")),
            )
        )
    return found


def _parse(value: Any) -> datetime | None:
    """ISO 文字列を datetime に直す。読めなければ None。

    時刻が読めないことで記録全体を捨てない。**公開したという事実の方が
    重要**で、時刻は画面の補足にすぎない。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
