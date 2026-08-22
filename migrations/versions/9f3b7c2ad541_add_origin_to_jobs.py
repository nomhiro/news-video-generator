"""add origin to jobs

ジョブを積んだ主体（定期実行か、画面からの手動か）を持つ列。

**代替の投入をこれで分ける。** 記事の題材が Azure OpenAI のコンテンツ
フィルタに拒否されたとき、定期実行なら別の記事で作り直してよいが、
手動なら人が選んだ記事を勝手に差し替えてはいけない。`enqueue_batch` は
定期実行（`plan_daily_batch`）と手動（`POST /generate`）の両方から
呼ばれており、両者を区別する列が無いとその判断ができなかった。

nullable にしてあるのは、既存の行（手動と定期の区別が付かない）を
手動と同じ扱い（＝差し替えない）に倒すため。安全側に落ちる。

Revision ID: 9f3b7c2ad541
Revises: 4443966ca043
Create Date: 2026-08-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f3b7c2ad541"
down_revision: str | Sequence[str] | None = "4443966ca043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("origin", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("origin")
