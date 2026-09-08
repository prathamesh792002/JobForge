"""Add enhancement columns for CRM, follow-up tracking, and analytics

Revision ID: 003
Revises: 002
Create Date: 2026-07-01 00:00:00.000000

Adds:
  - missing_skills  : JSON array (TEXT) of skills absent at tailoring time
  - salary_offered  : free-text salary string captured on offer status
  - notes           : free-text notes per application
  - followup_sent_at: timestamp when a follow-up email was dispatched
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("missing_skills", sa.Text, nullable=True))
    op.add_column("applications", sa.Column("salary_offered", sa.String(100), nullable=True))
    op.add_column("applications", sa.Column("notes", sa.Text, nullable=True))
    op.add_column(
        "applications",
        sa.Column("followup_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("applications", "followup_sent_at")
    op.drop_column("applications", "notes")
    op.drop_column("applications", "salary_offered")
    op.drop_column("applications", "missing_skills")
