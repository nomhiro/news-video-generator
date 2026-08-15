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
    #
    # **この判定は `PostWorker._run_one` の判定と意図的に重複している。**
    # `post_due_once` はテストから直接呼ばれる関数なので、単体で
    # 安全でなければならない。`_run_one` 側の判定（クライアントを
    # 作る前に無効なら帰る）を「重複しているから」と削らないこと。
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
        max_post_delay_minutes: int | None = None,
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
            max_post_delay_minutes: これ以上遅れた予定は出さずに捨てる。
                **遅れた投稿を後から出し直さない**ための値。省略時は
                掃かない（テストで discard_stale の影響を受けたくない
                呼び出しのため）。デプロイやプロセス停止で数時間止まった後、
                復帰した瞬間に古い投稿を連投すると、閲覧者にはスパムに見える
                （4件が一斉に出るのはニュースとしての新鮮さも失っている）
        """
        self._repository = repository
        self._client_factory = client_factory
        self._switch = switch
        self._poll_interval = poll_interval
        self._fetch_image = fetch_image
        self._on_posted = on_posted
        self._max_post_delay_minutes = max_post_delay_minutes

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 未認証/失効の状態が変わったときだけログを出すためのフラグ。
        # `_run_one` の docstring を参照。
        self._needs_reauth = False

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
                if not self._run_one():
                    self._stop.wait(self._poll_interval)
            except Exception as e:
                # ループ自体は絶対に落とさない。落とすとキューに残った
                # 投稿が二度と出なくなる。
                log_error(f"投稿ワーカーのループでエラー: {e}")
                self._stop.wait(self._poll_interval)

        log_step("投稿ワーカーを停止しました", "🐦")

    def _run_one(self) -> bool:
        """1回分のループ本体（掃く→判定→作る→出す→閉じる）。

        テストがスレッドを起動せずに1周ぶんだけ検証できるように、
        `_loop` から切り出している。

        Returns:
            bool: 投稿を試みたら True
        """
        # claim_due は予定時刻順に最古を取るだけで、遅れの大きさを
        # 見ない。出す前に掃かないと、数時間止まっていた後の復帰時に
        # 一番古い（＝一番遅れた）投稿から連投してしまう。
        if self._max_post_delay_minutes is not None:
            discarded = self._repository.discard_stale(
                datetime.now(UTC), self._max_post_delay_minutes
            )
            if discarded:
                log_error(f"予定時刻から遅れすぎた投稿 {discarded}件を見送りました")

        # スイッチが無効ならここで終える。`post_due_once` にも同じ判定が
        # あるが（意図的な重複。あちらの docstring 参照）、ここで先に
        # 判定する目的は別にある: クライアントを作らない・接続を開かない。
        # ここで判定せずに毎ポーリング（既定30秒ごと）クライアントを
        # 作って渡すと、無効な間（開発中や、Task 7 の認証画面ができるまで
        # の全デプロイの既定状態）ずっと `httpx.Client` を開いて捨てる
        # ことになり、接続プールが漏れ続ける。
        if not self._switch.is_enabled():
            return False

        try:
            client = self._client_factory()
        except XTokenExpiredError as e:
            # 未認証・失効は珍しい異常ではない（再認証されるまで続く）。
            # 毎ポーリング同じ行をログに出し続けると、本当に見るべき
            # エラーがログに埋もれる。**止めるのは繰り返しだけで、
            # 最初の1回は必ずエラーとして記録する。**
            if not self._needs_reauth:
                log_error(f"X の認証が必要です。再認証されるまで投稿を保留します: {e}")
                self._needs_reauth = True
            return False

        if self._needs_reauth:
            log_step("X の認証が回復しました。投稿を再開します", "🐦")
            self._needs_reauth = False

        try:
            return post_due_once(
                self._repository,
                client,
                self._switch,
                fetch_image=self._fetch_image,
                on_posted=self._on_posted,
            )
        finally:
            # 掴めなかった場合・送信が例外で終わった場合を含め、
            # 作ったクライアントは必ず閉じる。閉じないと接続プールが
            # ポーリングごとに漏れる。
            client.close()
