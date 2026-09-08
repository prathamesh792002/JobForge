"""ATS score columns — already included in migration 001 (MySQL rewrite)

Revision ID: 002
Revises: 001
Create Date: 2026-06-18 00:00:00.000000

No-op: initial_ats_score and final_ats_score were added directly to
migration 001 when the migration was rewritten for MySQL compatibility.
This revision exists only to keep the Alembic chain unbroken.
"""

from typing import Sequence, Union

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
