"""毎日決まった時刻に計画を走らせるスケジューラ。

なぜアプリの中で動かすか
------------------------
Container Apps Jobs（cron）で別コンテナから起こす方法も考えたが、この構成では
成立しない。ジョブ表は SQLite で**そのコンテナのローカルディスク**にあり
（Azure Files 上では動かなかった）、別コンテナからは書けない。
HTTP で叩く手もあるが、公開エンドポイントは Entra ID 認証で閉じているので
呼び出し側の認証を足すことになる。

アプリ内のスレッドなら、ジョブ表に直接書けて認証も要らない。
レプリカは1固定なので、二重に走る心配もない。
**レプリカを増やすときは、この前提が崩れる**（ジョブ表を PostgreSQL に
移すのと同時に、リーダー選出か Container Apps Jobs へ移す必要がある）。

`schedule` などのライブラリは入れない。「1日1回、指定時刻」だけなら
次回時刻の計算は数行で足りる。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logger import log_error, log_step

# 起動直後に走らせない。デプロイのたびに生成が始まると、
# リビジョン更新を繰り返した日に何本も作ってしまう。
# 次の指定時刻まで待つ。


def next_run_at(now: datetime, run_at: time, timezone: ZoneInfo) -> datetime:
    """次に走る時刻を返す。

    Args:
        now: 現在時刻（tz aware）
        run_at: 走らせたい時刻（timezone のローカル時刻）
        timezone: 基準のタイムゾーン

    Returns:
        datetime: 次回の実行時刻（tz aware）

    Raises:
        ValueError: now が naive な場合
    """
    if now.tzinfo is None:
        raise ValueError("now はタイムゾーン付きで渡してください")

    local_now = now.astimezone(timezone)
    candidate = local_now.replace(hour=run_at.hour, minute=run_at.minute, second=0, microsecond=0)
    if candidate <= local_now:
        # 今日の分は過ぎている。翌日にする。
        # 「過ぎていたら即実行」にしない: 再起動のたびに走ってしまう。
        candidate += timedelta(days=1)
    return candidate


class DailyScheduler:
    """1日1回、指定時刻にコールバックを実行するスレッド。

    Attributes:
        run_at: 実行時刻（ローカル）
        timezone: 基準のタイムゾーン
    """

    def __init__(
        self,
        task: Callable[[], Coroutine[Any, Any, object]],
        run_at: time,
        timezone: str = "Asia/Tokyo",
    ):
        """初期化する。

        Args:
            task: 実行する非同期処理（ニュース取得を含むので async）
            run_at: 実行時刻
            timezone: タイムゾーン名（既定は日本時間）
        """
        self._task = task
        self.run_at = run_at
        self.timezone = ZoneInfo(timezone)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """スレッドを起動する。"""
        if self._thread is not None:
            raise RuntimeError("スケジューラは既に起動しています")
        self._thread = threading.Thread(target=self._loop, name="daily-scheduler", daemon=True)
        self._thread.start()
        upcoming = next_run_at(datetime.now(UTC), self.run_at, self.timezone)
        log_step(
            f"定期実行を有効にしました（次回 {upcoming:%Y-%m-%d %H:%M %Z}）",
            "🗓️",
        )

    def stop(self, timeout: float = 10.0) -> None:
        """停止を要求して待つ。

        待機中なら即座に抜ける。実行中の計画は完了を待つ
        （途中で止めると、投入したジョブと投入していないジョブが混ざる）。

        Args:
            timeout: 待つ秒数
        """
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def is_running(self) -> bool:
        """スレッドが生きているか。"""
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        """次回時刻まで待って実行する、を繰り返す。"""
        while not self._stop.is_set():
            now = datetime.now(UTC)
            target = next_run_at(now, self.run_at, self.timezone)
            wait_seconds = (target - now).total_seconds()

            # 待機は Event で行う。sleep だと停止要求に反応できず、
            # デプロイ時のシャットダウンが最大1日待ちになる。
            if self._stop.wait(wait_seconds):
                return

            try:
                # ニュース取得が async なので、このスレッド専用の
                # イベントループで回す（Web のループとは別）。
                asyncio.run(self._task())
            except Exception as e:
                # 1回の失敗でスケジューラを止めない。止めると翌日以降も
                # 走らなくなり、気付くのが遅れる。
                log_error(f"定期実行に失敗しました（次回は予定どおり走ります）: {e}")
