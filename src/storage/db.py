"""データベース接続。

なぜ SQLAlchemy か
------------------
永続化したいのはジョブ表と投稿表の2つで、SQLite に直接 `sqlite3` で書いても
動く。それでも SQLAlchemy を挟むのは、**接続先を差し替えられること**に
価値があるため。この差し替えは 2026-08-23 に実際に起きた（下記）。

なぜクラウドは PostgreSQL か（Issue #56 / #3）
---------------------------------------------
`jobs` と `social_posts` は**コンテナのローカルディスク上の SQLite**に
置いていた。Azure Files のマウントは `/app/data` で `/app/state` はマウント外
なので、**リビジョンごとに新しい空のディスク**になり、起動時の
`alembic upgrade head` が空のテーブルを作り直していた。エラーも警告も出ない
まま、次の3つが起きていた。

1. **X 投稿キューがデプロイで消え、その日の残りの投稿が出ないまま終わる。**
   下書きを積むのは 06:30 の日次タスク1回だけで catch-up は作っていない。
   直近24時間で CD は8回走っていたので、マージする日はほぼ毎回起きていた
2. 実行待ちの動画ジョブと履歴が消える
3. **予算ガードが実質効いていなかった。** `monthly_post_counts` は POSTED 行を
   数えるが、その行がデプロイごとに消える。月の実支出がいくらでも
   `is_over_budget`（$30）がほぼ発火しない。`collect_metrics` の
   24時間 / 7日の指標も同じ理由で履歴を失っていた

**下書きを Azure Files に写して復元する案は却下した。** ACA は
`activeRevisionsMode = Single` で、新リビジョンが ready になるまで旧リビジョンを
落とさない。つまりデプロイのたびに**2つのレプリカが1〜2分同時に走る**。
per-replica のコピーを持たせると両方が同じ行を持ち、**各自が別のファイルを見る
ので claim の排他が効かず、その間に予定時刻が来た投稿は二度出る**。共有 DB なら
claim が1箇所の条件付き UPDATE になるので、この窓は構造的に消える。

**Azure Files（SMB）の上の SQLite に再挑戦しないこと。** journal_mode を DELETE に
しても CREATE TABLE で固まり、リビジョンが Activating のまま起動しない（同じ
イメージでローカルディスクに向けると25秒で起動する、という切り分けまで実測した）。

PostgreSQL の認証はパスワードを持たない
---------------------------------------
マネージド ID で取った Entra のアクセストークンを**パスワード欄に渡す**のが
Azure Database for PostgreSQL の公式の方式で、サーバー側は
`passwordAuth: 'Disabled'` にしてある。トークンの有効期間は5〜60分なので
**新しい接続ごとに**渡し、期限の5分前までキャッシュする。ワーカースレッドと
イベントループが同時に接続するのでロックで囲む。

**URL にパスワードが書かれているときは注入しない。** ローカルの Docker で立てた
PostgreSQL に対して（パスワード認証で）マイグレーションとリポジトリを検証できる
ようにするため（`tests/test_db_postgres_slow.py`）。実機の URL にパスワードは
無いので、本番では必ずトークン経路を通る。

SQLite の設定
-------------
`journal_mode=WAL` にしないと、書き込み中の読み取りが
`database is locked` で失敗しやすい。ワーカーが数分かかるジョブを実行しながら
`/status` が読む構成なので、これは避けられない衝突になる。

ただし **Azure Files（SMB）の上では WAL が使えない**。WAL は共有メモリ
（`-shm` の mmap）を要求し、SMB はそれを提供しないため
`disk I/O error` になる。クラウドでファイル共有にマウントするときは
`SQLITE_JOURNAL_MODE=DELETE` にする。

`busy_timeout` は「ロックが取れなくても即座に諦めない」ための待ち時間。
既定は 0 で、競合したら例外になる。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# SQLite でロック待ちを諦めるまでの時間。
# ワーカーの書き込みと /status の読み取りが競合するため、
# 0（既定）だと即座に "database is locked" になる。
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Azure Database for PostgreSQL のトークンのスコープ。
# 他のリソース向けのトークンを渡しても認証は通らない。
ENTRA_DB_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# 期限のどれだけ前に取り直すか。トークンは5〜60分で切れるので、
# 接続の途中で切れないよう余裕を持って捨てる。
_TOKEN_EXPIRY_MARGIN_SEC = 300

# 待機の長いアプリなので、PostgreSQL 側に切られた接続を掴まないようにする。
# pool_recycle は「この秒数より古い接続は捨てる」。
_PG_POOL_RECYCLE_SEC = 1800


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _credential() -> Any:
    """Entra の資格情報を作る。

    遅延 import にしてあるのは、SQLite だけで動かす経路（ローカル実行と
    pytest）で azure-identity を読み込まないため。テストはこの関数を
    差し替えてフェイクを返す。

    ユーザー割り当て ID では `AZURE_CLIENT_ID` の指定が必須で、省略すると
    システム割り当てを探して認証に失敗する（Blob と同じ制約。Container App の
    env に入れてある）。
    """
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


class _TokenCache:
    """アクセストークンを期限まで持ち回す。

    エンジンごとに1つ持つ（モジュール変数にしない）。テストが状態を
    引きずらないため。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._credential: Any = None
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        """有効なトークンを返す。期限が近ければ取り直す。"""
        with self._lock:
            if self._token is not None and time.time() < self._expires_at:
                return self._token
            if self._credential is None:
                self._credential = _credential()
            access = self._credential.get_token(ENTRA_DB_SCOPE)
            self._token = str(access.token)
            self._expires_at = float(access.expires_on) - _TOKEN_EXPIRY_MARGIN_SEC
            return self._token


def create_db_engine(url: str, journal_mode: str = "WAL") -> Engine:
    """エンジンを作る。

    Args:
        url: SQLAlchemy の接続 URL
            （例: `sqlite:///./data/newsvideo.db`、
            `postgresql+psycopg://<identity>@<host>:5432/newsvideo?sslmode=require`）
        journal_mode: SQLite の journal_mode。
            Azure Files（SMB）の上では WAL が使えないため DELETE を渡す
            （WAL は共有メモリの mmap を要求し、SMB は提供しない）。
            PostgreSQL では無視される。

    Returns:
        Engine: 接続プール込みのエンジン
    """
    if _is_sqlite(url):
        # ワーカーは別スレッドで動く。SQLite の接続はスレッドを跨げないのが
        # 既定なので、チェックを外してプールに任せる。
        _ensure_parent_dir(url)
        engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
        _apply_sqlite_pragmas(engine, journal_mode)
        return engine

    engine = create_engine(
        url,
        future=True,
        # 待機中に切られた接続を掴むと、最初のクエリが落ちる。
        pool_pre_ping=True,
        pool_recycle=_PG_POOL_RECYCLE_SEC,
    )
    if make_url(url).password is None:
        _apply_entra_token(engine)
    return engine


def _ensure_parent_dir(url: str) -> None:
    """SQLite のファイルを置くディレクトリを作る。

    無いと `unable to open database file` になる。メッセージから
    「ディレクトリが無い」ことは読み取れない。
    """
    # sqlite:///./data/newsvideo.db -> ./data/newsvideo.db
    path_part = url.split("///", 1)[-1]
    if not path_part or path_part == ":memory:":
        return
    Path(path_part).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _apply_entra_token(engine: Engine) -> None:
    """接続のたびにアクセストークンをパスワードとして渡す。

    `connect` ではなく `do_connect` を使う理由: `connect` は接続が**できた後**に
    発火するので、パスワードを差し込む余地が無い。`do_connect` は DBAPI を呼ぶ
    直前で、`cparams` を書き換えられる。

    プールに残っている接続は、トークンが切れた後もそのまま使える
    （認証は接続時に一度だけ行われる）。
    """
    cache = _TokenCache()

    @event.listens_for(engine, "do_connect")
    def _inject_token(_dialect: Any, _conn_rec: Any, _cargs: Any, cparams: dict[str, Any]) -> None:
        cparams["password"] = cache.token()
        # None を返すと既定の接続処理がそのまま続く。
        return None


def _apply_sqlite_pragmas(engine: Engine, journal_mode: str) -> None:
    """接続ごとに SQLite の設定を入れる。

    PRAGMA は接続単位の設定なので、プールが新しい接続を作るたびに
    適用する必要がある（起動時に1回では効かない）。
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # WAL なら書き込み中でも読み取れる（ワーカーの書き込み中に
        # /status が失敗しない）。SMB 上では使えないので設定で切り替える。
        cursor.execute(f"PRAGMA journal_mode={journal_mode}")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        # 外部キーは既定で無効。有効にしないと制約が単なる飾りになる。
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """セッションファクトリを作る。

    `expire_on_commit=False` にする理由: commit の後にオブジェクトの属性を
    読むと、既定では再読み込みのクエリが走る。ワーカーは commit 直後に
    ログ出力で属性を読むため、無駄なクエリと（セッションを閉じていれば）
    `DetachedInstanceError` の原因になる。

    Args:
        engine: エンジン

    Returns:
        sessionmaker: セッションファクトリ
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """トランザクション境界。

    例外時に rollback する。これが無いと、失敗したトランザクションを
    抱えた接続がプールに戻り、次の利用者が
    `PendingRollbackError` を踏む。

    Args:
        factory: セッションファクトリ

    Yields:
        Session: セッション
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
