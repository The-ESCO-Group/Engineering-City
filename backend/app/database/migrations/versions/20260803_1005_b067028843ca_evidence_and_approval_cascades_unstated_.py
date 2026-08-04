"""evidence and approval cascades, unstated engineering values

Two kinds of change, both of them about not stating things nobody stated.

*Cascades that destroy a record.* ``object_evidence_links.anchor_id``,
``finding_evidence.anchor_id`` and the three subject references on ``approvals``
were ``ON DELETE CASCADE``. Evidence anchors are hard-deleted on every re-parse
(anchor keys are positional, so a renamed routine or a parser-version bump
strands the old ones) and findings are hard-deleted by the agent executor when a
re-run cannot re-resolve their evidence. Under those perfectly ordinary events
the cascades removed the basis of an approved object, the evidence of a
delivered finding, and the record that a named engineer accepted a risk. They
become ``SET NULL``, which leaves a row that still reads.

*Booleans and an enum that asserted an engineering decision by default.* The
implementation-status flags held two values for a three-valued fact — "not
searched", "searched and absent", "present" — so a freshly ingested project
asserted that every documented protection was missing before anything had
looked. ``alarm_definitions.latched`` likewise asserted a self-clearing alarm,
and it is the sole gate on the reset-behaviour check. ``fault_definitions.
severity`` defaulted to ``medium``, which is a statement about how fast an
operator must respond. All become nullable, with NULL meaning the source did not
say.

Two further changes in the same commit are Python-side column defaults only
(``tags.external_access`` and ``hmi_bindings.access`` now default to the empty
string) and therefore carry no DDL.

Revision ID: b067028843ca
Revises: 792176727e30
Created: 2026-08-03 10:05:40.922482
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Portable column types used by the ORM (GUID, JSONDict, JSONList, UTCDateTime).
import app.database.types

revision: str = "b067028843ca"
down_revision: str | None = "792176727e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(table, column)``. Every one of these was ``NOT NULL`` with a Python-side
#: ``False`` default that this revision removes, so the downgrade has to put a
#: value back before it can re-impose the constraint — and the value it puts
#: back is exactly the fabricated one the upgrade exists to stop writing.
_BOOLEAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("interlocks", "is_implemented"),
    ("permissives", "is_implemented"),
    ("cause_effect_entries", "is_implemented"),
    ("sequence_steps", "is_implemented"),
    ("fault_definitions", "is_driven"),
    ("alarm_definitions", "latched"),
)

#: ``(table, column, referred table, constraint name)``. The names are spelled
#: out rather than reflected: they come from the metadata naming convention, and
#: a silent miss would leave the old cascade in place.
_CASCADE_TO_SET_NULL: tuple[tuple[str, str, str, str], ...] = (
    (
        "object_evidence_links",
        "anchor_id",
        "evidence_anchors",
        "fk_object_evidence_links_anchor_id_evidence_anchors",
    ),
    (
        "finding_evidence",
        "anchor_id",
        "evidence_anchors",
        "fk_finding_evidence_anchor_id_evidence_anchors",
    ),
    ("approvals", "finding_id", "findings", "fk_approvals_finding_id_findings"),
    (
        "approvals",
        "recommendation_id",
        "recommendations",
        "fk_approvals_recommendation_id_recommendations",
    ),
    ("approvals", "report_id", "reports", "fk_approvals_report_id_reports"),
)


def _rewrite_foreign_keys(ondelete: str) -> None:
    for table, column, referent, name in _CASCADE_TO_SET_NULL:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(name, type_="foreignkey")
            batch_op.create_foreign_key(name, referent, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    # ``finding_evidence.anchor_id`` has to become nullable before SET NULL can
    # mean anything; the other four were already nullable.
    with op.batch_alter_table("finding_evidence", schema=None) as batch_op:
        batch_op.alter_column("anchor_id", existing_type=app.database.types.GUID(), nullable=True)

    _rewrite_foreign_keys("SET NULL")

    for table, column in _BOOLEAN_COLUMNS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(column, existing_type=sa.Boolean(), nullable=True)

    with op.batch_alter_table("fault_definitions", schema=None) as batch_op:
        batch_op.alter_column("severity", existing_type=sa.String(length=16), nullable=True)


def downgrade() -> None:
    # Put back the values the upgrade stopped inventing, so the NOT NULL
    # constraints can be re-imposed. A row that legitimately said "nobody
    # stated this" cannot survive the downgrade — the old schema had nowhere to
    # put it, which is the defect this revision fixed.
    for table, column in _BOOLEAN_COLUMNS:
        op.execute(sa.text(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL"))  # noqa: S608
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(column, existing_type=sa.Boolean(), nullable=False)

    op.execute(sa.text("UPDATE fault_definitions SET severity = 'medium' WHERE severity IS NULL"))
    with op.batch_alter_table("fault_definitions", schema=None) as batch_op:
        batch_op.alter_column("severity", existing_type=sa.String(length=16), nullable=False)

    _rewrite_foreign_keys("CASCADE")

    op.execute(sa.text("DELETE FROM finding_evidence WHERE anchor_id IS NULL"))
    with op.batch_alter_table("finding_evidence", schema=None) as batch_op:
        batch_op.alter_column("anchor_id", existing_type=app.database.types.GUID(), nullable=False)
