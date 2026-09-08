"""Add demanded_skills column (full JD skill-demand signal)

Revision ID: 004
Revises: 003
Create Date: 2026-07-05 00:00:00.000000

Adds:
  - demanded_skills : JSON (TEXT) of the complete tiered skill set each JD
                      demanded (not just what the candidate lacked). Powers the
                      deterministic weekly market-demand report + /coach.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("demanded_skills", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "demanded_skills")
