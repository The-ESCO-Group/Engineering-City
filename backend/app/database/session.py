"""Engine and session management.

Synchronous SQLAlchemy is used deliberately: the parsers (openpyxl, pypdf,
python-docx) and the worker loop are CPU-bound and blocking, so an async ORM
would buy nothing but complexity. FastAPI runs sync dependencies in a
threadpool, which is the right execution model here.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _sqlite_file_path(url: str) -> Path | None:
    """Extract the on-disk path from a SQLite URL, or ``None`` for in-memory."""
    _, _, tail = url.partition(":///")
    if not tail or tail == ":memory:":
        return None
    return Path(tail).expanduser()


def build_engine(settings: Settings) -> Engine:
    """Create an engine configured for the active dialect."""
    url = settings.database_url
    kwargs: dict[str, Any] = {"echo": settings.database_echo, "future": True}

    if url.startswith("sqlite"):
        # ``check_same_thread=False`` is required because FastAPI hands sync
        # dependencies to a threadpool and the worker runs on its own thread.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url or "mode=memory" in url:
            # One shared connection, otherwise each thread would get its own
            # empty database.
            kwargs["poolclass"] = StaticPool
        else:
            path = _sqlite_file_path(url)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs["pool_size"] = settings.database_pool_size
        kwargs["max_overflow"] = settings.database_max_overflow
        kwargs["pool_pre_ping"] = True

    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Enable the SQLite settings this application depends on.

    ``foreign_keys`` is off by default in SQLite, which would silently disable
    every FK constraint the schema declares. WAL plus a busy timeout lets the
    API and the worker thread write concurrently without ``database is locked``.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


def get_engine() -> Engine:
    """Process-wide engine singleton."""
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings())
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


def configure(engine: Engine) -> None:
    """Point the module at a specific engine (used by tests and by the CLI)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


def reset() -> None:
    """Drop the cached engine/session factory."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work (worker loop, CLI scripts)."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session.

    The session is *not* committed here. Route handlers commit explicitly so
    that a handler which raises after a partial write cannot accidentally
    persist half of an operation.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
