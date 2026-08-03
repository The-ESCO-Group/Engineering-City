"""Alembic environment.

The URL comes from application settings rather than ``alembic.ini``, so
migrations cannot be run against a different database than the app uses and no
connection string is ever committed.

``render_as_batch`` is enabled because SQLite cannot ``ALTER TABLE DROP COLUMN``
or alter a constraint; batch mode rewrites the table instead, which is what
makes demonstration-mode upgrades work at all.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.database.types import StrEnumType

# Importing the package registers every table on Base.metadata, which is what
# autogenerate diffs against.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

settings = get_settings()

# NOT via ``config.set_main_option``. Alembic stores that value in a
# ``configparser``, which applies pyformat interpolation to it — so a PostgreSQL
# password containing any character RFC 3986 requires to be percent-encoded
# (``@ / : # ? %`` or a space) arrives as ``%40``, ``%2F``, ``%21`` and
# configparser rejects the whole string before a single migration runs.
#
# The failure is nastier than it sounds because it is *asymmetric*: SQLAlchemy
# parses the same URL correctly, so the API and the worker start and connect
# fine. Only the migration runner dies — the one tool needed to create the
# schema in the first place. And SQLite URLs never contain a ``%``, so demo mode
# never sees it.
_ESCAPED_DATABASE_URL = settings.database_url.replace("%", "%%")
config.set_main_option("sqlalchemy.url", _ESCAPED_DATABASE_URL)


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep autogenerate focused on the application's own tables."""
    if type_ == "table" and name is not None and name.startswith("sqlite_"):
        return False
    return True


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render ``StrEnumType`` columns as the plain ``VARCHAR`` they actually are.

    Two reasons, and the first is that the alternative does not work. Alembic
    renders a custom type by inspecting its ``impl``, so a ``StrEnumType`` comes
    out as ``StrEnumType(length=32)`` — missing the required ``enum_cls``
    argument, and the generated migration raises on import.

    The second reason is the one that would matter even if it did work. The DDL
    for an enum column *is* ``VARCHAR(n)``; the enum only governs how Python
    reads the value back. Writing the enum class into a migration would pin a
    historical migration to a Python symbol that a later refactor could rename,
    and a migration that stops running because an enum moved is a migration
    that has captured the wrong thing.
    """
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.length})"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        user_module_prefix="app.database.types.",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    The engine is built straight from the settings rather than through
    ``engine_from_config``, which would route the URL back through configparser
    interpolation. Escaping it on the way in is enough to stop the crash; not
    round-tripping it at all is enough to stop the class of bug.
    """
    connectable = create_engine(settings.database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        if connection.dialect.name == "sqlite":
            # SQLite disables foreign keys per connection; without this a
            # migration could leave orphaned rows behind.
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            render_item=_render_item,
            user_module_prefix="app.database.types.",
        )
        with context.begin_transaction():
            context.run_migrations()

        # SQLite reports non-transactional DDL, so Alembic's begin_transaction()
        # is a no-op there and the INSERT into alembic_version is left
        # uncommitted -- SQLAlchemy 2.0 connections do not autocommit. Without
        # this the schema is created but the revision is never stamped, and the
        # next `alembic upgrade` believes it has nothing to do.
        connection.commit()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
