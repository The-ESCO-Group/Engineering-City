"""preserve change proposals and branch paths

Revision ID: 4f7a9c2d1e6b
Revises: 38af6b21dc04
Created: 2026-08-05 09:00:00

``change_operations.after`` used to serve two incompatible purposes. Before a
merge it held the proposed values plus a reserved ``$basis`` entry; after a
successful merge (or dry run) it held the observed outcome plus that same basis.
The new schema separates the immutable proposal into ``values`` / ``basis`` and
leaves ``before`` / ``after`` as the merge record.

That history cannot always be reconstructed exactly. Once a legacy operation
was applied, or successfully rehearsed, its proposal had already been
overwritten by the outcome. For those rows this migration preserves the known
outcome and basis but leaves ``values`` empty rather than falsely claiming the
outcome was exactly what the author proposed. For an operation that was never
applied or successfully rehearsed, the legacy ``after`` value is still the
proposal and can be moved losslessly.

Legacy merge results likewise contain only a count and prose explanation for
validation errors. ``validation_errors`` is therefore backfilled as an empty
list; inventing structured records from prose would make the audit trail look
more precise than the information that was actually retained.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.selectable import TableClause

import app.database.types

revision: str = "4f7a9c2d1e6b"
down_revision: str | None = "38af6b21dc04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASIS_KEY = "$basis"
_DRY_RUN_OK = "dry_run_ok"


def _change_operations() -> TableClause:
    return sa.table(
        "change_operations",
        sa.column("id", app.database.types.GUID()),
        sa.column("change_set_id", app.database.types.GUID()),
        sa.column("applied", sa.Boolean()),
        sa.column("after", app.database.types.JSONDict()),
        sa.column("values", app.database.types.JSONDict()),
        sa.column("basis", app.database.types.JSONList()),
    )


def _merge_results() -> TableClause:
    return sa.table(
        "merge_results",
        sa.column("change_set_id", app.database.types.GUID()),
        sa.column("is_dry_run", sa.Boolean()),
        sa.column("outcome", sa.String(length=24)),
        sa.column("validation_errors", app.database.types.JSONList()),
    )


def _plain_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _proposal_values(legacy_after: Mapping[str, Any]) -> dict[str, Any]:
    """Return exactly what the legacy proposal accessor exposed."""
    return {key: value for key, value in legacy_after.items() if not key.startswith("$")}


def _basis_entries(legacy_after: Mapping[str, Any]) -> list[Any]:
    raw = legacy_after.get(_BASIS_KEY)
    return list(raw) if isinstance(raw, list) else []


def _successful_dry_run_change_sets(connection: sa.Connection) -> set[Any]:
    results = _merge_results()
    return set(
        connection.execute(
            sa.select(results.c.change_set_id).where(
                results.c.is_dry_run.is_(True),
                results.c.outcome == _DRY_RUN_OK,
            )
        ).scalars()
    )


def _backfill_change_operations() -> None:
    connection = op.get_bind()
    operations = _change_operations()
    rehearsed = _successful_dry_run_change_sets(connection)

    rows = connection.execute(
        sa.select(
            operations.c.id,
            operations.c.change_set_id,
            operations.c.applied,
            operations.c.after,
        )
    ).mappings()
    for row in rows:
        legacy_after = _plain_mapping(row["after"])
        proposal_or_outcome = _proposal_values(legacy_after)
        has_recorded_outcome = bool(row["applied"]) or row["change_set_id"] in rehearsed
        connection.execute(
            sa.update(operations)
            .where(operations.c.id == row["id"])
            .values(
                # Applied/rehearsed legacy rows no longer retain enough evidence
                # to assert their original proposal. An empty object states that
                # limitation honestly; it does not manufacture history.
                values={} if has_recorded_outcome else proposal_or_outcome,
                basis=_basis_entries(legacy_after),
                # The outcome remains the outcome. An untouched proposal moves
                # out of ``after`` so release change logs never present a plan as
                # work that actually landed.
                after=proposal_or_outcome if has_recorded_outcome else {},
            )
        )


def _restore_legacy_change_operations() -> None:
    """Fold the split representation back together before dropping its columns."""
    connection = op.get_bind()
    operations = _change_operations()
    rehearsed = _successful_dry_run_change_sets(connection)
    rows = connection.execute(
        sa.select(
            operations.c.id,
            operations.c.change_set_id,
            operations.c.applied,
            operations.c.after,
            operations.c["values"],
            operations.c.basis,
        )
    ).mappings()
    for row in rows:
        has_recorded_outcome = bool(row["applied"]) or row["change_set_id"] in rehearsed
        legacy_after = _plain_mapping(row["after"] if has_recorded_outcome else row["values"])
        basis = row["basis"]
        if isinstance(basis, list) and basis:
            legacy_after[_BASIS_KEY] = list(basis)
        connection.execute(
            sa.update(operations).where(operations.c.id == row["id"]).values(after=legacy_after)
        )


def upgrade() -> None:
    # Add nullable columns first. Python-side ORM defaults do not populate rows
    # that already exist, and PostgreSQL correctly refuses an immediate NOT NULL
    # addition to a populated table.
    with op.batch_alter_table("change_operations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("values", app.database.types.JSONDict(), nullable=True))
        batch_op.add_column(sa.Column("basis", app.database.types.JSONList(), nullable=True))

    with op.batch_alter_table("merge_results", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("validation_errors", app.database.types.JSONList(), nullable=True)
        )

    with op.batch_alter_table("logic_instructions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch_id", app.database.types.GUID(), nullable=True))
        batch_op.create_foreign_key(
            "fk_logic_instructions_branch_id_logic_branches",
            "logic_branches",
            ["branch_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_logic_instructions_branch_id", ["branch_id"], unique=False)

    _backfill_change_operations()

    results = _merge_results()
    op.get_bind().execute(
        sa.update(results).where(results.c.validation_errors.is_(None)).values(validation_errors=[])
    )

    # No server defaults remain in the final schema: new writes must pass
    # through the ORM, while the backfill above makes every legacy row valid.
    with op.batch_alter_table("change_operations", schema=None) as batch_op:
        batch_op.alter_column(
            "values",
            existing_type=app.database.types.JSONDict(),
            nullable=False,
        )
        batch_op.alter_column(
            "basis",
            existing_type=app.database.types.JSONList(),
            nullable=False,
        )

    with op.batch_alter_table("merge_results", schema=None) as batch_op:
        batch_op.alter_column(
            "validation_errors",
            existing_type=app.database.types.JSONList(),
            nullable=False,
        )


def downgrade() -> None:
    _restore_legacy_change_operations()

    with op.batch_alter_table("logic_instructions", schema=None) as batch_op:
        batch_op.drop_index("ix_logic_instructions_branch_id")
        batch_op.drop_constraint(
            "fk_logic_instructions_branch_id_logic_branches", type_="foreignkey"
        )
        batch_op.drop_column("branch_id")

    with op.batch_alter_table("merge_results", schema=None) as batch_op:
        batch_op.drop_column("validation_errors")

    with op.batch_alter_table("change_operations", schema=None) as batch_op:
        batch_op.drop_column("basis")
        batch_op.drop_column("values")
