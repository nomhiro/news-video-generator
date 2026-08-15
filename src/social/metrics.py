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

    Args:
        repository: 投稿表
        client: 指標を取れるクライアント
        store: 保存先
        work_dir: 一時ファイルを置く場所（ローカル）
        now: 現在時刻（UTC aware）

    Returns:
        int: 測った件数
    """
    moment = now or datetime.now(UTC)

    tweet_ids: list[str] = []
    for offset in MEASUREMENT_OFFSETS:
        target = moment - offset
        for post in repository.list_posted_between(target - WINDOW, target + WINDOW):
            if post.tweet_id and post.tweet_id not in tweet_ids:
                tweet_ids.append(post.tweet_id)

    if not tweet_ids:
        # 空のファイルを毎日置くと Blob にごみが積もる
        return 0

    log_step(f"{len(tweet_ids)}件の投稿の指標を取得します", "📈")
    metrics: dict[str, dict[str, int]] = {}
    for start in range(0, len(tweet_ids), BATCH_SIZE):
        metrics.update(client.fetch_metrics(tweet_ids[start : start + BATCH_SIZE]))

    key = f"metrics/x/{moment:%Y-%m-%d}.json"
    payload = {"measured_at": moment.isoformat(), "metrics": metrics}

    work_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=work_dir, suffix=".json")
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        store.publish(temp_path, key)
    finally:
        temp_path.unlink(missing_ok=True)

    log_success(f"指標を保存しました（{key}、{len(metrics)}件）")
    return len(metrics)
