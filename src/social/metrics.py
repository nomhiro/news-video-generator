"""投稿の指標を取り、保存先に日次ファイルとして記録する。

なぜ2回だけ測るか
-----------------
読み取りも従量課金（$0.005/投稿）。月240投稿を毎日追うと月 $8 を超えるが、
1投稿につき2回なら約 $2 で済む。24時間で初速、7日で最終的な伸びが分かる。

なぜ Blob に置くか
------------------
ジョブ表の SQLite はコンテナのローカルディスクにあってリビジョン更新で
消える。指標は蓄積してこそ意味を持つデータなので、そこには置けない。

**自動最適化はしない。** 数十件のデータで型や時間帯を自動調整すると、
ノイズに追従して安定しない。伸びたテーマは人が見て判断する。
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from src.models.social import SocialPost
from src.storage.social import SocialPostRepository
from src.utils.logger import log_step, log_success

# 投稿からどれだけ経ったものを測るか。
MEASUREMENT_OFFSETS: tuple[timedelta, timedelta] = (timedelta(hours=24), timedelta(days=7))

# 1回の問い合わせで測る件数の上限（GET /2/tweets?ids= の制約）。
BATCH_SIZE = 100

# 対象を選ぶときの時刻の許容幅。
# 計測は1日1回なので、offset ぴったりの投稿は存在しない。
WINDOW = timedelta(hours=12)


class SupportsMetrics(Protocol):
    """指標の取得だけ。"""

    def fetch_metrics(self, tweet_ids: list[str]) -> dict[str, dict[str, int]]: ...


class SupportsPublish(Protocol):
    """保存先への publish だけ。

    実プロトコル（`src/storage/artifacts.py` の `ArtifactStore`）は
    `local_path: Path` を受け、参照用 URI（`str`）を返す。ここでは
    戻り値を使わないが、実装（`LocalArtifactStore` / `BlobArtifactStore`）
    をそのまま渡せるように型を一致させる。
    """

    def publish(self, local_path: Path, key: str) -> str: ...


def collect_metrics(
    repository: SocialPostRepository,
    client: SupportsMetrics,
    store: SupportsPublish,
    work_dir: Path,
    now: datetime | None = None,
) -> int:
    """対象の投稿の指標を取り、日次ファイルとして保存する。

    ファイルは `{"measured_at": ..., "records": [...]}` の形。1レコードが
    1測定で、`tweet_id` / `offset_hours`（24 か 168）/ `posted_at` /
    `kind` / `article_title` / `metrics` を持つ。**平坦な
    `tweet_id -> metrics` にしないこと**（理由は下のコメント）。

    Args:
        repository: 投稿表
        client: 指標を取れるクライアント
        store: 保存先
        work_dir: 一時ファイルを置く場所（ローカル）
        now: 現在時刻（UTC aware）

    Returns:
        int: 実際に指標が取れたレコードの件数（応答に無かったものは
            レコードとして残すが、この数には入れない）
    """
    moment = now or datetime.now(UTC)

    # (何時間後の計測か, 対象の投稿)。**どの offset の測定値かを行に持たせる。**
    #
    # 以前は日次ファイルに `tweet_id -> metrics` の平坦な辞書を書いていた。
    # 24時間後の値と7日後の値が同じ形で同じ辞書に入るため、ある行が
    # どちらの測定なのかは `posted_at` と突き合わせないと分からない。
    # その `posted_at` は SQLite にしか無く、**デプロイごとに消える**。
    # Blob に書いていた理由（蓄積して人が読む）が1回のマージで無くなる。
    targets: list[tuple[int, SocialPost]] = []
    seen: set[tuple[str, int]] = set()
    for offset in MEASUREMENT_OFFSETS:
        offset_hours = round(offset.total_seconds() / 3600)
        target = moment - offset
        for post in repository.list_posted_between(target - WINDOW, target + WINDOW):
            if post.tweet_id is None:
                continue
            marker = (post.tweet_id, offset_hours)
            if marker in seen:
                continue
            seen.add(marker)
            targets.append((offset_hours, post))

    if not targets:
        # 空のファイルを毎日置くと Blob にごみが積もる
        return 0

    # 問い合わせは tweet_id 単位で重複を排す（同じ投稿を2つの offset で
    # 測る状況は今の窓幅では起きないが、起きたときに課金を二重に払わない）。
    tweet_ids = list(dict.fromkeys(tid for tid, _ in seen))
    log_step(f"{len(tweet_ids)}件の投稿の指標を取得します", "📈")
    metrics: dict[str, dict[str, int]] = {}
    for start in range(0, len(tweet_ids), BATCH_SIZE):
        metrics.update(client.fetch_metrics(tweet_ids[start : start + BATCH_SIZE]))

    # 1測定 = 1レコード。あとから読む人が SQLite に頼らずに解釈できるよう、
    # 投稿の識別（tweet_id / article_title / kind）と時刻（posted_at）を
    # レコード自身に持たせる。すべて既に読み込んでいる行から取れる。
    records: list[dict[str, object]] = [
        {
            "tweet_id": post.tweet_id,
            "offset_hours": offset_hours,
            "posted_at": post.posted_at.isoformat() if post.posted_at is not None else None,
            "kind": str(post.kind),
            "article_title": post.article_title,
            # 応答に無い（削除された等）投稿も空で残す。落とすと
            # 「測ろうとして取れなかった」ことが記録から消える。
            "metrics": metrics.get(post.tweet_id or "", {}),
        }
        for offset_hours, post in targets
    ]

    key = f"metrics/x/{moment:%Y-%m-%d}.json"
    payload = {"measured_at": moment.isoformat(), "records": records}

    work_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=work_dir, suffix=".json")
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        store.publish(temp_path, key)
    finally:
        temp_path.unlink(missing_ok=True)

    measured = sum(1 for record in records if record["metrics"])
    log_success(f"指標を保存しました（{key}、{len(records)}レコード / 取得 {measured}件）")
    return measured
