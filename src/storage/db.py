"""データベース接続。

なぜ SQLAlchemy か
------------------
永続化したいのは当面ジョブ表だけで、SQLite に直接 `sqlite3` で書いても
動く。それでも SQLAlchemy を挟むのは、**接続先を差し替えられること**に
価値があるため。

ジョブ表の目的は「進捗をプロセスから切り離す」ことだが、SQLite の
ファイルは1台のファイルシステム上にしかない。Container Apps で
レプリカを2つにするなら、共有できる DB（Azure Database for PostgreSQL）が
必要になる。そのとき差分が `DATABASE_URL` の1行で済むようにしておく。

SQLite の設定
-------------
`journal_mode=WAL` にしないと、書き込み中の読み取りが
`database is locked` で失敗する。ワーカーが数分かかるジョブを実行しながら
`/status` が読む構成なので、これは避けられない衝突になる。

`busy_timeout` は「ロックが取れなくても即座に諦めない」ための待ち時間。
既定は 0 で、競合したら例外になる。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# SQLite でロック待ちを諦めるまでの時間。
# ワーカーの書き込みと /status の読み取りが競合するため、
# 0（既定）だと即座に "database is locked" になる。
_SQLITE_BUSY_TIMEOUT_MS = 5000


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_db_engine(url: str) -> Engine:
    """エンジンを作る。

    Args:
        url: SQLAlchemy の接続 URL（例: `sqlite:///./data/newsvideo.db`）

    Returns:
        Engine: 接続プール込みのエンジン
    """
    connect_args: dict[str, Any] = {}
    if _is_sqlite(url):
        # ワーカーは別スレッドで動く。SQLite の接続はスレッドを跨げないのが
        # 既定なので、チェックを外してプールに任せる。
        connect_args["check_same_thread"] = False
        _ensure_parent_dir(url)

    engine = create_engine(url, connect_args=connect_args, future=True)

    if _is_sqlite(url):
        _apply_sqlite_pragmas(engine)
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


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """接続ごとに SQLite の設定を入れる。

    PRAGMA は接続単位の設定なので、プールが新しい接続を作るたびに
    適用する必要がある（起動時に1回では効かない）。
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # 書き込み中でも読み取れるようにする。
        # これが無いとワーカーの書き込み中に /status が失敗する。
        cursor.execute("PRAGMA journal_mode=WAL")
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
