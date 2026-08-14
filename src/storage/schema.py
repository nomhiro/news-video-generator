"""スキーマの適用。

起動時に `alembic upgrade head` を実行する。

なぜ自動で当てるか
------------------
このアプリは開発者のローカルと単一コンテナで動く。手順を1つ増やすと
「起動したがテーブルが無い」で詰まる（エラーメッセージからは原因が
読み取りづらい）。マイグレーションは前方互換な追加が主なので、
起動時に当てても壊れにくい。

なぜ `Base.metadata.create_all` にしないか
------------------------------------------
create_all は既存テーブルの差分を当てられない。列を1つ足した時点で
「新規環境では動くが既存環境では動かない」状態になり、後から
Alembic を入れるのは（既存の DB に初期リビジョンを刻む作業が必要で）
苦痛になる。最初から Alembic に寄せる。

レプリカを2つ以上にするときは、ここを止めて起動前の1回に切り出す
（複数プロセスが同時に upgrade を走らせるのは安全ではない）。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from src.utils.logger import log_step

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def build_alembic_config(database_url: str, journal_mode: str = "WAL") -> AlembicConfig:
    """Alembic の設定を組み立てる。

    URL は ini ではなくここで注入する。ini に書くと真実が2箇所になる。

    Args:
        database_url: 接続 URL
        journal_mode: SQLite の journal_mode。
            **アプリ側と必ず揃える必要がある。** マイグレーションは
            アプリより先に走るため、ここが WAL のままだと Azure Files
            （SMB）上では WAL の設定でハングし、起動が終わらない
            （ログにも何も出ないので原因が分かりにくい。一度踏んだ）。

    Returns:
        AlembicConfig: 設定
    """
    config = AlembicConfig(str(ALEMBIC_INI))
    # script_location は ini の %(here)s に依存するため、
    # カレントディレクトリがどこであっても解決できるよう明示する。
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # env.py は attributes 経由でこの URL を読む
    # （-x url が指定されていればそちらが優先される）。
    config.attributes["database_url"] = database_url
    config.attributes["sqlite_journal_mode"] = journal_mode
    return config


def upgrade_to_head(database_url: str, journal_mode: str = "WAL") -> None:
    """最新のリビジョンまで適用する。

    Args:
        database_url: 接続 URL
        journal_mode: SQLite の journal_mode（アプリ側と揃える）
    """
    log_step("データベーススキーマを適用中...", "🗄️")
    command.upgrade(build_alembic_config(database_url, journal_mode), "head")
