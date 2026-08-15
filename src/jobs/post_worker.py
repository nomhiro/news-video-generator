"""投稿を実行するワーカー。

ジョブワーカー（src/jobs/worker.py）との違い
-------------------------------------------
リースと heartbeat を持たない。投稿は数秒で終わるので、15分のリースを
延ばし続ける仕組みは意味を持たない。

代わりに、送信結果が不明なときの扱いが厳しい。X API に冪等キーが無いため、
「届いたか分からない」行は再送せず NEEDS_REVIEW にする。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from src.models.social import PostKind
from src.social.x_auth import XTokenExpiredError
from src.social.x_client import XClient, XSendUncertainError
from src.storage.social import SocialPostRepository
from src.utils.logger import log_error, log_step, log_success

# キューが空のときに次を見に行くまでの間隔。
# 投稿時刻の精度は分単位で十分なので、30秒で足りる。
POLL_INTERVAL_SEC = 30.0


class SupportsSwitch(Protocol):
    """自動投稿の有効/無効の判定だけ。"""

    def is_enabled(self) -> bool: ...


def post_due_once(
    repository: SocialPostRepository,
    client: XClient,
    switch: SupportsSwitch,
    now: datetime | None = None,
    fetch_image: Callable[[str], Path] | None = None,
    on_posted: Callable[[str], None] | None = None,
) -> bool:
    """予定時刻を過ぎた投稿を1件だけ出す。

    Args:
        repository: 投稿表
        client: X クライアント
        switch: 自動投稿の有効/無効
        now: 現在時刻（UTC aware）
        fetch_image: 画像キー -> ローカルパス（画像カードのとき必要）
        on_posted: 投稿できたときに article_id を渡す（消費記録の更新用）

    Returns:
        bool: 投稿を試みたら True、何もしなかったら False
    """
    moment = now or datetime.now(UTC)

    # 無効なら掴まない。掴んでから止めると POSTING の行が残り、
    # 次の起動で NEEDS_REVIEW に落ちてしまう。
    if not switch.is_enabled():
        return False

    post = repository.claim_due(moment)
    if post is None:
        return False

    # スレッドの2件目以降は、直前の投稿への返信にする。
    reply_to: str | None = None
    if post.position > 0:
        reply_to = repository.group_posted_tweet_id(post.group_id, post.position - 1)
        if reply_to is None:
            # 直前が出ていない。時間をおいて続けると文脈が切れるので、
            # このまとまりは人が見る。
            repository.mark_needs_review(
                post.id, "スレッドの直前の投稿が出ていないため中断しました"
            )
            return True

    try:
        media_ids: list[str] | None = None
        if post.kind is PostKind.CARD and post.image_key and fetch_image is not None:
            media_ids = [client.upload_media(fetch_image(post.image_key))]

        tweet_id = client.create_post(post.body, reply_to=reply_to, media_ids=media_ids)
    except XSendUncertainError as e:
        # 届いたか分からない。**再送しない。**
        repository.mark_needs_review(post.id, f"送信結果が不明です: {e}")
        log_error(f"投稿 {post.id}: 送信結果が不明のため要確認にしました - {e}")
        return True
    except XTokenExpiredError as e:
        repository.mark_needs_review(post.id, f"再認証が必要です: {e}")
        log_error(f"投稿 {post.id}: トークンが失効しています - {e}")
        return True
    except Exception as e:
        repository.mark_failed(post.id, str(e))
        log_error(f"投稿 {post.id} 失敗: {e}")
        return True

    repository.mark_posted(post.id, tweet_id=tweet_id, posted_at=moment)
    if on_posted is not None:
        on_posted(post.article_id)
    log_success(f"投稿 {post.id} 完了: {post.article_title[:30]}（{post.kind}）")
    return True


class PostWorker:
    """投稿表をポーリングして実行するワーカー。

    `JobWorker` と同じ形（`threading.Thread`、`daemon=False`、`stop()` で
    join、ループは絶対に落とさない）。**リースと heartbeat は持たない**
    （このモジュールの docstring を参照）。
    """

    def __init__(
        self,
        repository: SocialPostRepository,
        client_factory: Callable[[], XClient],
        switch: SupportsSwitch,
        poll_interval: float = POLL_INTERVAL_SEC,
        fetch_image: Callable[[str], Path] | None = None,
        on_posted: Callable[[str], None] | None = None,
    ):
        """初期化する。

        Args:
            repository: 投稿表
            client_factory: XClient を作る処理。呼び出しごとに作る
                （アクセストークンが `ensure_fresh` で更新されうるため、
                起動時に1つ作って使い回すと古いトークンを使い続ける）
            switch: 自動投稿の有効/無効
            poll_interval: キューが空のときの待ち時間（秒）
            fetch_image: 画像キー -> ローカルパス（画像カードのとき必要）
            on_posted: 投稿できたときに article_id を渡す（消費記録の更新用）
        """
        self._repository = repository
        self._client_factory = client_factory
        self._switch = switch
        self._poll_interval = poll_interval
        self._fetch_image = fetch_image
        self._on_posted = on_posted

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """ワーカースレッドを起動する。

        起動時に必ず `recover_stuck_posting()` を1回呼ぶ。前回の
        プロセスが送信中に落ちた行は「届いたか分からない」ので、
        SCHEDULED に戻さず NEEDS_REVIEW にして人が見る対象にする。
        """
        if self._thread is not None:
            raise RuntimeError("ワーカーは既に起動しています")

        recovered = self._repository.recover_stuck_posting(
            "前回の実行中に送信結果が確認できなくなったため要確認にしました"
        )
        if recovered:
            log_error(f"送信中に中断された投稿 {recovered}件を要確認にしました")

        self._thread = threading.Thread(target=self._loop, name="post-worker", daemon=False)
        self._thread.start()
        log_step("投稿ワーカーを起動しました", "🐦")

    def stop(self, timeout: float = 30.0) -> None:
        """停止を要求し、スレッドの終了を待つ。

        Args:
            timeout: 待つ秒数
        """
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log_error(f"投稿ワーカーが {timeout}秒で停止しませんでした")
        self._thread = None

    @property
    def is_running(self) -> bool:
        """スレッドが生きているか。"""
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        """停止要求が来るまで投稿を出し続ける。"""
        while not self._stop.is_set():
            try:
                client = self._client_factory()
                posted = post_due_once(
                    self._repository,
                    client,
                    self._switch,
                    fetch_image=self._fetch_image,
                    on_posted=self._on_posted,
                )
                if not posted:
                    self._stop.wait(self._poll_interval)
            except Exception as e:
                # ループ自体は絶対に落とさない。落とすとキューに残った
                # 投稿が二度と出なくなる。
                log_error(f"投稿ワーカーのループでエラー: {e}")
                self._stop.wait(self._poll_interval)

        log_step("投稿ワーカーを停止しました", "🐦")
