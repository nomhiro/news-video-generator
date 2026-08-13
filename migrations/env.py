"""Alembic の実行環境。

接続先は `alembic.ini` ではなく**アプリの設定**（`DATABASE_URL`）から取る。
ini に URL を書くと2箇所に真実ができ、片方だけ変えたときに
「マイグレーションは通ったのにアプリは別の DB を見ている」という
分かりにくい状態になる。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# migrations/ はリポジトリルートの外から実行されることがあるため、
# ルートを import パスに入れる（`from src...` を解決するのに必要）。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.db import create_db_engine  # noqa: E402
from src.storage.tables import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate が差分を出すための対象。
target_metadata = Base.metadata


def _database_url() -> str:
    """接続先を決める。

    優先順位:
      1. `alembic -x url=...` で明示された値（一時DBを指すのに使う）
      2. プログラムから注入された値（`src/storage/schema.py` が入れる）
      3. アプリの設定（`DATABASE_URL`）
    """
    from_cli = context.get_x_argument(as_dictionary=True).get("url")
    if from_cli:
        return str(from_cli)

    injected = config.attributes.get("database_url")
    if injected:
        return str(injected)

    from config import Config

    return Config.from_env().database_url


def run_migrations_offline() -> None:
    """SQL を出力するだけのモード。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """実際に接続して適用するモード。"""
    # アプリと同じ create_db_engine を使う。SQLite の PRAGMA（WAL 等）を
    # ここでも効かせたいため、素の engine_from_config は使わない。
    engine = create_db_engine(_database_url())

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite は ALTER TABLE が貧弱で、列の変更に
            # 「新テーブルを作って入れ替える」手順が必要になる。
            # これを Alembic に任せる。
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
