"""widen worker_id

`jobs.worker_id` を VARCHAR(64) から VARCHAR(128) に広げる。

Container Apps のレプリカ名だけで実測58字
（`ca-newsvideo-img-mimujd6zyifm6--0000019-7fd4f56486-pwqdv`）あり、
`JobWorker.worker_id`（ホスト名 + PID + 乱数6字）を足すと66字になって
VARCHAR(64) を超える。SQLite は列長を強制しないので気付かず、
PostgreSQL に移した直後（2026-08-23）に `claim_next` の UPDATE が
`StringDataRightTruncation` で毎回失敗し、ジョブが QUEUED のまま
無限リトライになって動画が1本も進まなくなった（詳細は
`src/storage/tables.py` の `worker_id` 列のコメント）。

Revision ID: c49cbe0213cb
Revises: 9f3b7c2ad541
Create Date: 2026-08-23 20:11:51.135095

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c49cbe0213cb"
down_revision: str | Sequence[str] | None = "9f3b7c2ad541"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "worker_id",
            existing_type=sa.String(length=64),
            type_=sa.String(length=128),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "worker_id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
