"""add_pwht_advice_to_defects

Revision ID: 152bb9705097
Revises: f7c25d51e36b
Create Date: 2026-08-25 16:01:43.110796

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '152bb9705097'
down_revision: Union[str, Sequence[str], None] = 'f7c25d51e36b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. 幂等添加 pwht_advice 列
    defects_columns = [col['name'] for col in inspector.get_columns('defects')]
    if 'pwht_advice' not in defects_columns:
        op.add_column(
            'defects',
            sa.Column(
                'pwht_advice',
                sa.JSON(),
                nullable=True,
                comment='PWHT焊后热处理修复工艺建议(结构化JSON)'
            )
        )

    # 2. 创建索引（保留原有的变更）
    with op.batch_alter_table('defects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_defects_defect_type'), ['defect_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_defects_risk_level'), ['risk_level'], unique=False)

    # 3. 修改 projects.name 为非空
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('name',
               existing_type=sa.VARCHAR(),
               nullable=False)

    # 4. 创建 tasks 索引
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_status'), ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. 删除 tasks 索引
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_tasks_project_id'))

    # 2. 恢复 projects.name 可为空
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.alter_column('name',
               existing_type=sa.VARCHAR(),
               nullable=True)

    # 3. 删除 defects 索引
    with op.batch_alter_table('defects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_defects_risk_level'))
        batch_op.drop_index(batch_op.f('ix_defects_defect_type'))

    # 4. 幂等删除 pwht_advice 列
    defects_columns = [col['name'] for col in inspector.get_columns('defects')]
    if 'pwht_advice' in defects_columns:
        op.drop_column('defects', 'pwht_advice')