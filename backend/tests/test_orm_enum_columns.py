"""Enum columns must survive a round-trip through the database.

Declaring ``Mapped[ProjectStatus] = mapped_column(String(32))`` looks right and
silently is not: SQLAlchemy writes the string but returns a plain ``str`` on
load. Identity comparisons (``status is ProjectStatus.ARCHIVED``) then evaluate
``False`` for every persisted row and ``.value`` raises ``AttributeError``.

The failure is invisible to any test that inspects an object it just created in
the same session, which is how it survived a full green suite once already. It
broke, among other things, the guard that stops an archived project accepting
uploads and the file-deletion endpoint.

The structural test below is the important one: it fails for *any* future enum
column that forgets the mapping, not just the ones that were wrong.
"""

from __future__ import annotations

import re
from enum import StrEnum

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core import enums as domain_enums
from app.core.enums import (
    DocumentRole,
    EventType,
    FileKind,
    FileStatus,
    ProjectRole,
    ProjectStatus,
    RoleName,
    TaskKind,
    TaskStatus,
)
from app.database import session as db_session
from app.database.types import StrEnumType
from app.models import Base
from app.models.event import Event
from app.models.file import UploadedFile
from app.models.project import Project, ProjectMember
from app.models.task import Task
from app.models.user import Role

#: ``Mapped[Foo]`` / ``Mapped[Foo | None]`` as it appears in a model's
#: ``__annotations__``. Because the models use ``from __future__ import
#: annotations``, these are strings, which is convenient: the *declared* type is
#: exactly what we need, and a column that mistakenly resolved to ``String``
#: still declares its enum here.
_MAPPED_ANNOTATION = re.compile(r"^Mapped\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*None\s*)?\]$")


def _declared_enum_columns() -> list[tuple[str, str, type[StrEnum], object]]:
    """Every column declared as ``Mapped[<StrEnum subclass>]``, with its column type.

    Reads the declared annotation rather than ``column.type.python_type``: a
    broken column reports ``str`` for its Python type, which is precisely the
    case this needs to catch.
    """
    found: list[tuple[str, str, type[StrEnum], object]] = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        annotations: dict[str, str] = {}
        for klass in reversed(model.__mro__):
            annotations.update(getattr(klass, "__annotations__", {}))

        for attribute, annotation in annotations.items():
            match = _MAPPED_ANNOTATION.match(str(annotation).strip())
            if match is None:
                continue
            enum_cls = getattr(domain_enums, match.group(1), None)
            if not (isinstance(enum_cls, type) and issubclass(enum_cls, StrEnum)):
                continue
            prop = mapper.column_attrs.get(attribute)
            if prop is None:
                continue
            found.append((model.__name__, attribute, enum_cls, prop.columns[0].type))
    return found


def test_every_enum_column_uses_the_enum_type_decorator() -> None:
    """A plain String on an enum column loads back as str; catch it structurally.

    This is the guard that stops the bug class returning. A new
    ``Mapped[SomeStrEnum]`` column that forgets its ``type_annotation_map`` entry
    fails here rather than at runtime on a row someone loaded a week later.
    """
    offenders = [
        f"{model}.{attribute} ({enum_cls.__name__} -> {type(column_type).__name__})"
        for model, attribute, enum_cls, column_type in _declared_enum_columns()
        if not isinstance(column_type, StrEnumType)
    ]
    assert offenders == [], "these enum columns would load back as plain str: " + ", ".join(
        offenders
    )


def test_the_scan_actually_finds_the_known_enum_columns() -> None:
    """Guard the guard: a scan that silently finds nothing would always pass."""
    columns = {(model, attribute) for model, attribute, _, _ in _declared_enum_columns()}
    for expected in (
        ("Project", "status"),
        ("ProjectMember", "role"),
        ("UploadedFile", "file_kind"),
        ("UploadedFile", "status"),
        ("Task", "kind"),
        ("Event", "event_type"),
        ("Role", "name"),
        ("Finding", "severity"),
        ("Approval", "kind"),
        ("AuditEntry", "action"),
    ):
        assert expected in columns, f"scan missed {expected}"
    assert len(columns) >= 20


@pytest.fixture
def reloaded(engine: Engine) -> Session:
    """A session that has never seen the objects under test.

    Reading through the identity map of the session that created a row would
    return the in-memory enum and hide the bug entirely.
    """
    return db_session.get_session_factory()()


def test_project_status_round_trips_as_an_enum(db: Session, reloaded: Session) -> None:
    project = Project(name="Synthetic Round Trip", status=ProjectStatus.ARCHIVED)
    db.add(project)
    db.commit()

    loaded = reloaded.get(Project, project.id)
    assert loaded is not None
    assert isinstance(loaded.status, ProjectStatus)
    assert loaded.status is ProjectStatus.ARCHIVED
    assert loaded.status.value == "archived"


def test_is_archived_is_true_for_a_reloaded_archived_project(
    db: Session, reloaded: Session
) -> None:
    """This property gates uploads; when it lied, archived projects stayed writable."""
    project = Project(name="Synthetic Archived", status=ProjectStatus.ARCHIVED)
    db.add(project)
    db.commit()

    loaded = reloaded.get(Project, project.id)
    assert loaded is not None
    assert loaded.is_archived is True
    assert loaded.accepts_work is False


def test_file_enums_round_trip(db: Session, reloaded: Session) -> None:
    project = Project(name="Synthetic Files")
    db.add(project)
    db.flush()
    record = UploadedFile(
        project_id=project.id,
        original_filename="IO List.csv",
        stored_filename="IO List.csv",
        relative_path="originals/IO List.csv",
        file_kind=FileKind.CSV,
        document_role=DocumentRole.IO_LIST,
        status=FileStatus.UPLOADED,
    )
    db.add(record)
    db.commit()

    loaded = reloaded.get(UploadedFile, record.id)
    assert loaded is not None
    assert loaded.file_kind is FileKind.CSV
    assert loaded.document_role is DocumentRole.IO_LIST
    assert loaded.status is FileStatus.UPLOADED
    # The deletion endpoint reads .value on all three; this is what 500'd.
    assert (loaded.file_kind.value, loaded.document_role.value, loaded.status.value) == (
        "csv",
        "io_list",
        "uploaded",
    )
    assert loaded.is_available is True


def test_task_and_event_and_role_enums_round_trip(db: Session, reloaded: Session) -> None:
    project = Project(name="Synthetic Task Enums")
    db.add(project)
    db.flush()
    task = Task(
        project_id=project.id,
        kind=TaskKind.PARSE_FILE,
        title="Parse one file",
        status=TaskStatus.QUEUED,
        idempotency_key="parse:one",
    )
    from app.core.clock import utcnow

    event = Event(
        seq=1,
        project_id=project.id,
        event_type=EventType.FILE_PARSED,
        occurred_at=utcnow(),
    )
    member_role = Role(name=RoleName.REVIEWER)
    db.add_all([task, event, member_role])
    db.commit()

    assert reloaded.get(Task, task.id).kind is TaskKind.PARSE_FILE  # type: ignore[union-attr]
    assert reloaded.get(Task, task.id).status is TaskStatus.QUEUED  # type: ignore[union-attr]
    assert reloaded.get(Event, event.id).event_type is EventType.FILE_PARSED  # type: ignore[union-attr]
    assert reloaded.get(Role, member_role.id).name is RoleName.REVIEWER  # type: ignore[union-attr]


def test_project_member_role_round_trips(
    db: Session, reloaded: Session, seeded: dict[str, object]
) -> None:
    """Permission checks compare this against a frozenset of enum members."""
    from app.models.user import User
    from app.security.permissions import WRITE_ROLES

    engineer = seeded["engineer"]
    assert isinstance(engineer, User)

    project = Project(name="Synthetic Membership")
    db.add(project)
    db.flush()
    member = ProjectMember(project_id=project.id, user_id=engineer.id, role=ProjectRole.ENGINEER)
    db.add(member)
    db.commit()

    loaded = reloaded.get(ProjectMember, member.id)
    assert loaded is not None
    assert loaded.role is ProjectRole.ENGINEER
    assert loaded.role in WRITE_ROLES


def test_an_unknown_stored_value_fails_loudly(db: Session, engine: Engine) -> None:
    """Corruption must be reported, not coerced into a plausible-looking enum.

    Enum members are only ever added, never renamed, so an unrecognised value
    means the row was written by something other than this application.
    """
    from sqlalchemy import text

    project = Project(name="Synthetic Corrupt")
    db.add(project)
    db.commit()

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE projects SET status = :bogus WHERE id = :pid"),
            {"bogus": "not_a_real_status", "pid": str(project.id)},
        )

    fresh = db_session.get_session_factory()()
    try:
        with pytest.raises(ValueError, match="not a valid member"):
            fresh.get(Project, project.id)
    finally:
        fresh.close()
