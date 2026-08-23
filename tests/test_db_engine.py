"""エンジンの組み立ての検査。

なぜ要るか
----------
クラウドの DB は Azure Database for PostgreSQL で、**パスワードを持たない**
（マネージド ID で取った Entra のアクセストークンをパスワード欄に渡す）。
この配線が外れると起動時の `alembic upgrade head` が認証で落ちる。落ちること
自体は目立つが、**原因がトークンなのかロール名なのかファイアウォールなのか
区別できない**ので、少なくとも「トークンを要求していること」はここで固定する。

実接続は1本だけにしてある
-------------------------
`do_connect` は DBAPI を呼ぶ直前に発火するので、接続が失敗しても「トークンを
取りに行ったか」は観測できる。ただし**実接続は遅い**——psycopg は既定で
`localhost:1` に対して260秒、`127.0.0.1:1` に対して130秒待ってから諦める
（実測）。`connect_timeout=2` を付けて2秒に抑えたうえで、実接続を使うのは
「本当に psycopg の接続経路で発火する」ことを見る1本だけにしている。
残りは登録されたリスナーを直接呼ぶ。

`.githooks/pre-push` の実行時間は約90秒で、30秒→60秒→90秒と伸びてきている。
1本あたり2分かかる検査を足すと `--no-verify` への圧力になる。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from src.storage import db

# 実接続の宛先。使われないポートを指し、`connect_timeout` で待ちを2秒に抑える
# （libpq は2未満を2として扱うので、これが下限）。
REFUSED_URL = "postgresql+psycopg://app-identity@127.0.0.1:1/newsvideo?connect_timeout=2"

# 「URL にパスワードがある」ことだけを表す1文字。値は使われない
# （`create_db_engine` は `make_url(url).password is None` しか見ない）。
PASSWORD_PRESENT = "x"


@dataclass
class FakeToken:
    token: str
    expires_on: float


@dataclass
class FakeCredential:
    """`DefaultAzureCredential` の代わり。取得したスコープを記録する。"""

    token: str = "fake-access-token"
    lifetime_sec: float = 3600.0
    calls: list[str] = field(default_factory=list)

    def get_token(self, scope: str) -> FakeToken:
        self.calls.append(scope)
        return FakeToken(token=self.token, expires_on=time.time() + self.lifetime_sec)


@pytest.fixture
def credential(monkeypatch: pytest.MonkeyPatch) -> FakeCredential:
    fake = FakeCredential()
    monkeypatch.setattr(db, "_credential", lambda: fake)
    return fake


def _connect_listeners(engine: Engine) -> list[Any]:
    """`do_connect` に登録されたリスナー。

    `do_connect` は方言のイベントなので、エンジンではなく
    `engine.dialect.dispatch` に載る（`engine.dispatch.do_connect` は
    `AttributeError`）。イベント名を打ち間違えると `event.listens_for` が
    その場で落ちるので、「登録されているのに呼ばれない」形の欠陥は起きない。
    """
    return list(engine.dialect.dispatch.do_connect)


def _fire_connect(engine: Engine) -> dict[str, Any]:
    """接続直前の処理を走らせ、DBAPI に渡る引数を返す。"""
    cparams: dict[str, Any] = {}
    listeners = _connect_listeners(engine)
    assert listeners, "do_connect のリスナーが登録されていない"
    for listener in listeners:
        listener(engine.dialect, None, [], cparams)
    return cparams


def test_実際に接続するとトークンを取りに行く(credential: FakeCredential) -> None:
    """psycopg の接続経路で本当に発火すること（実接続を使う唯一の検査）。

    宛先が居ないので接続は必ず失敗するが、`do_connect` は DBAPI を呼ぶ
    **前**に走るので、取りに行ったかどうかは観測できる。
    """
    engine = db.create_db_engine(REFUSED_URL)

    with pytest.raises(Exception):  # noqa: B017 - 例外の型は駆動する DBAPI 次第
        with engine.connect():
            pass

    assert credential.calls == [db.ENTRA_DB_SCOPE], (
        "トークンを取りに行っていない（パスワード無しでは接続できない）"
    )


def test_トークンをパスワードとして渡す(credential: FakeCredential) -> None:
    """`cparams` の password に入れること。

    Azure Database for PostgreSQL は Entra のアクセストークンを
    パスワード欄で受け取る。別のキーに入れても認証されない。
    """
    engine = db.create_db_engine("postgresql+psycopg://app-identity@db.example:5432/newsvideo")

    assert _fire_connect(engine)["password"] == "fake-access-token"


def test_パスワード付きのURLではトークンを注入しない(credential: FakeCredential) -> None:
    """ローカルの Docker で立てた PostgreSQL に対する検証経路。

    ここで注入するとパスワードが上書きされ、
    `tests/test_db_postgres_slow.py` が認証できなくなる。

    URL に**パスワードらしい文字列を書かない**。分岐が見ているのは値ではなく
    有無だけで、literal を置くと秘密検出が鳴る（実際に GitGuardian が鳴った）。
    """
    engine = db.create_db_engine(
        f"postgresql+psycopg://postgres:{PASSWORD_PRESENT}@db.example:5432/postgres"
    )

    assert _connect_listeners(engine) == []
    assert credential.calls == []


def test_トークンは期限まで使い回す(credential: FakeCredential) -> None:
    """接続のたびに取り直さないこと。

    `PostWorker` は30秒ごとに、`/status` はもっと短い間隔で接続しうる。
    毎回 Entra を叩くのは無駄な往復になる。
    """
    engine = db.create_db_engine("postgresql+psycopg://app-identity@db.example:5432/newsvideo")

    _fire_connect(engine)
    _fire_connect(engine)

    assert len(credential.calls) == 1


def test_期限が切れたトークンは取り直す(monkeypatch: pytest.MonkeyPatch) -> None:
    """期限の余裕（5分）を過ぎたら捨てること。

    キャッシュを直接呼ぶのは、時刻を進めずに期限切れを作るため。
    """
    fake = FakeCredential(lifetime_sec=0.0)
    monkeypatch.setattr(db, "_credential", lambda: fake)
    cache = db._TokenCache()

    assert cache.token() == fake.token
    assert cache.token() == fake.token

    assert len(fake.calls) == 2, "期限切れのトークンを使い回している"


def test_SQLiteでは従来どおりPRAGMAが当たる(tmp_path: Path) -> None:
    """PostgreSQL 対応を入れても SQLite の経路を壊していないこと。

    ローカル実行と pytest はこちらを通る。
    """
    url = f"sqlite:///{(tmp_path / 'sub' / 'engine.db').as_posix()}"

    engine = db.create_db_engine(url, journal_mode="DELETE")

    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "delete"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    # 親ディレクトリを作らないと `unable to open database file` になる。
    assert (tmp_path / "sub" / "engine.db").exists()


def test_SQLiteではトークンを取りに行かない(credential: FakeCredential, tmp_path: Path) -> None:
    """SQLite の経路で Entra を叩かないこと（azure-identity は遅延 import）。"""
    engine = db.create_db_engine(f"sqlite:///{(tmp_path / 'engine.db').as_posix()}")

    with engine.connect():
        pass

    assert credential.calls == []
