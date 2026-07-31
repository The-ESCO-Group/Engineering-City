"""Assembling a release package: files, import order, manifest, checksums.

The promise this module has to keep is short and load-bearing: **the same
approved revision always produces byte-identical artifacts.** Everything else
here follows from it.

* No wall-clock reading enters any file. Not in the L5X ``ExportDate``, not in
  the CSV ``remark`` preamble Studio writes, not in the manifest. A caller that
  needs a date supplies one that is itself part of the approved input.
* No identifier is invented during emission. Object ids come out of the model;
  none is generated here.
* Nothing iterates a mapping whose order came from somewhere else. Every
  collection is sorted explicitly before it is written.

Without those three, the ``sha256`` in the manifest records nothing more than
"somebody hashed something once", and a regenerated package cannot be diffed
against an approved one — which is the entire reason a release manifest exists.

Levels, not verdicts
--------------------
Every file carries the level it actually reached.
:attr:`~app.core.enums.ValidationLevel.ROUND_TRIP_PARSED` is set on an L5X only
when that file has been parsed back by :mod:`app.parsers.l5x_parser` and the
rungs it contains came back as the rungs we wrote. Everything else stays at
:attr:`~app.core.enums.ValidationLevel.GENERATED`, because a CSV nobody
validated is a CSV nobody validated. Nothing here can reach
``ROCKWELL_IMPORT_VERIFIED``; that needs the licensed-workstation adapter.

Two files are deliberately outside the manifest's own artifact list:
``manifest.json``, which cannot contain its own hash, and ``checksums.sha256``,
which hashes the manifest. ``checksums.sha256`` covers every file in the package
except itself, so one value verifies the whole package.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import Settings
from app.core.enums import (
    VALIDATION_LEVEL_ORDER,
    ControllerFamily,
    DocumentRole,
    ExportArtifactKind,
    FileKind,
    ImportDependencyKind,
    ImportVerificationStatus,
    ObjectType,
    ValidationLevel,
)
from app.generators.rockwell.csv_exports import (
    emit_comment_csv,
    emit_cross_reference_csv,
    emit_tag_csv,
)
from app.generators.rockwell.import_order import (
    ImportConstraint,
    ImportItem,
    render_import_order_markdown,
    resolve_import_order,
)
from app.generators.rockwell.l5x import (
    ControllerContext,
    LogicScope,
    RockwellGenerationError,
    emit_aoi_reference,
    emit_controller_project,
    emit_program,
    emit_routine,
    emit_udt,
    render_rung_text,
)
from app.models.plant import HardwareModule
from app.models.plc import (
    ControllerModel,
    ControllerTask,
    LogicComment,
    LogicReference,
    LogicRung,
    Program,
    Routine,
)
from app.models.release import (
    ExportArtifact,
    ImportDependency,
    ReleaseCandidate,
    ReleaseManifest,
    ValidationRun,
)
from app.models.tags import AOIDefinitionReference, Tag, UDTDefinition, UDTMember
from app.parsers.base import ParseContext
from app.parsers.l5x_parser import L5XParser
from app.security.paths import resolve_within

__all__ = [
    "AUDIT_DIR",
    "CONTROLLER_DIR",
    "DEFAULT_DELIVERY_SLOTS",
    "GENERATOR_NAME",
    "GENERATOR_VERSION",
    "IMPORT_READY_CANDIDATE",
    "MANIFEST_DIR",
    "MANIFEST_VERSION",
    "ApprovalRecord",
    "ChangeLogEntry",
    "DeliverySlot",
    "EvidenceIndexEntry",
    "GeneratedFile",
    "Limitation",
    "PackageOptions",
    "ProgramContent",
    "ProjectModel",
    "ReleasePackage",
    "RoutineContent",
    "TargetProfile",
    "TaskContent",
    "UdtContent",
    "UnsupportedItem",
    "ValidationSummary",
    "build_release_package",
]

GENERATOR_NAME = "app.generators.rockwell"
#: Bumped whenever the emitted bytes change for unchanged input. The first
#: question asked when a package behaves differently from one generated last
#: month is which build wrote it.
GENERATOR_VERSION = "1.0.0"
#: Schema version of ``manifest.json`` itself, so a reader written for an older
#: package knows whether it can parse this one.
MANIFEST_VERSION = "1.0"

#: The label a package carries until an actual Studio 5000 import has passed.
IMPORT_READY_CANDIDATE = "import-ready candidate"

MANIFEST_DIR = "00_Manifest"
CONTROLLER_DIR = "01_Controller"
AUDIT_DIR = "06_Audit"

_RELEASE_PREFIX = "ESCO_Project_Release_"

_MEDIA_TYPES: Mapping[str, str] = {
    ".L5X": "application/xml",
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".sha256": "text/plain",
}

_VERIFICATION_STATEMENT = (
    "Everything in this package was produced offline. The highest level anything "
    "here can reach is round_trip_parsed: this platform's own parser read the "
    "generated L5X back and found the rungs it wrote. That is not the same as "
    "proving Studio 5000 will import the file, and no Studio 5000 import has been "
    "attempted."
)

_DETERMINISM_STATEMENT = (
    "No wall-clock time and no generated identifier appears in any file in this "
    "package. Generating the same project revision again produces byte-identical "
    "files, so these checksums can be compared against a previously approved "
    "package rather than merely recorded."
)


# ---------------------------------------------------------------------------
# Input: the slice of the canonical model a package is generated from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetProfile:
    """The target platform as it stood at generation time.

    Snapshotted into the package rather than referenced, which is what stops a
    package being reinterpreted later against a profile somebody edited — the
    failure mode that makes a checksum meaningless.
    """

    controller_family: ControllerFamily
    target_studio_version: str = ""
    l5x_schema_revision: str = "1.0"
    software_revision: str = ""
    #: Written into the L5X ``ExportDate`` when supplied, omitted when not. See
    #: the module docstring: a clock reading would break byte-identity.
    export_date: str = ""
    export_profile_key: str = ""
    restricted_domain_modes: tuple[tuple[str, str], ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "controller_family": str(self.controller_family),
            "target_studio_version": self.target_studio_version,
            "l5x_schema_revision": self.l5x_schema_revision,
            "software_revision": self.software_revision,
            "export_date": self.export_date,
            "export_profile_key": self.export_profile_key,
            "restricted_domain_modes": dict(sorted(self.restricted_domain_modes)),
        }


@dataclass(frozen=True, slots=True)
class RoutineContent:
    """One routine and the rungs that belong to it.

    Rungs are passed rather than read off ``Routine.rungs`` so nothing in this
    package depends on a lazy load, and so a caller can generate from a slice of
    the model it has already fetched.
    """

    routine: Routine
    rungs: tuple[LogicRung, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgramContent:
    program: Program
    tags: tuple[Tag, ...] = ()
    routines: tuple[RoutineContent, ...] = ()


@dataclass(frozen=True, slots=True)
class UdtContent:
    udt: UDTDefinition
    members: tuple[UDTMember, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskContent:
    task: ControllerTask
    scheduled_program_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectModel:
    """Everything the generators read. Nothing here is fetched from a session."""

    project_name: str
    revision_number: int
    controller: ControllerModel
    tasks: tuple[TaskContent, ...] = ()
    programs: tuple[ProgramContent, ...] = ()
    controller_tags: tuple[Tag, ...] = ()
    udts: tuple[UdtContent, ...] = ()
    aoi_references: tuple[AOIDefinitionReference, ...] = ()
    modules: tuple[HardwareModule, ...] = ()
    logic_references: tuple[LogicReference, ...] = ()
    operand_comments: tuple[tuple[str, str, int, LogicComment], ...] = ()


# ---------------------------------------------------------------------------
# Input: everything the package says about itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limitation:
    """What this package does not do, in words an engineer reads before importing."""

    code: str
    summary: str
    detail: str

    def as_document(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class UnsupportedItem:
    """Something in the model this package could not represent."""

    kind: str
    subject: str
    detail: str
    location: str = ""

    def as_document(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One human decision, denormalised so the package carries its own provenance.

    ``approved_at`` is a string the caller supplies from the stored approval. It
    is never read from the clock here — an approval timestamp is a fact about
    something that already happened.
    """

    object_type: str
    object_label: str
    decision: str
    approver: str
    approved_at: str = ""
    note: str = ""

    def as_document(self) -> dict[str, str]:
        return {
            "object_type": self.object_type,
            "object_label": self.object_label,
            "decision": self.decision,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ChangeLogEntry:
    revision_number: int
    operation: str
    object_type: str
    object_label: str
    field_name: str = ""
    before: str = ""
    after: str = ""
    author: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "revision_number": self.revision_number,
            "operation": self.operation,
            "object_type": self.object_type,
            "object_label": self.object_label,
            "field": self.field_name,
            "before": self.before,
            "after": self.after,
            "author": self.author,
        }


@dataclass(frozen=True, slots=True)
class EvidenceIndexEntry:
    """One generated object and the basis it was generated from."""

    object_type: str
    object_label: str
    basis_kind: str
    basis_label: str
    source_label: str = ""
    anchor_key: str = ""

    def as_document(self) -> dict[str, str]:
        return {
            "object_type": self.object_type,
            "object_label": self.object_label,
            "basis_kind": self.basis_kind,
            "basis_label": self.basis_label,
            "source_label": self.source_label,
            "anchor_key": self.anchor_key,
        }


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """What the validators found, as of the revision this package was built from."""

    revision_number: int
    status: str = ""
    checks_run: int = 0
    error_count: int = 0
    warning_count: int = 0
    information_count: int = 0
    unsupported_count: int = 0
    human_review_count: int = 0
    disciplines: tuple[str, ...] = ()
    summary: str = ""

    @classmethod
    def from_run(cls, run: ValidationRun) -> ValidationSummary:
        return cls(
            revision_number=run.revision_number or 0,
            status=str(run.status),
            checks_run=run.checks_run or 0,
            error_count=run.error_count or 0,
            warning_count=run.warning_count or 0,
            information_count=run.information_count or 0,
            unsupported_count=run.unsupported_count or 0,
            human_review_count=run.human_review_count or 0,
            disciplines=tuple(sorted(str(item) for item in (run.disciplines or []))),
            summary=run.summary or "",
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "revision_number": self.revision_number,
            "status": self.status,
            "checks_run": self.checks_run,
            "counts": {
                "error": self.error_count,
                "warning": self.warning_count,
                "information": self.information_count,
                "unsupported": self.unsupported_count,
                "human_review_required": self.human_review_count,
            },
            "disciplines": list(self.disciplines),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class DeliverySlot:
    """A directory a partner-owned delivery plugin fills, present or not.

    An empty folder with no explanation reads as "nothing was needed", which is
    a claim nobody made. When ``is_present`` is false the absence becomes a
    limitation in ``LIMITATIONS.md`` naming the discipline and the reason.
    """

    directory: str
    discipline: str
    plugin_name: str = ""
    is_present: bool = False
    absence_reason: str = ""
    #: ``(relative path within the directory, bytes)``.
    files: tuple[tuple[str, bytes], ...] = ()


DEFAULT_DELIVERY_SLOTS: tuple[DeliverySlot, ...] = (
    DeliverySlot(directory="02_HMI", discipline="HMI"),
    DeliverySlot(directory="03_Alarms", discipline="Alarms"),
    DeliverySlot(directory="04_Testing", discipline="Testing"),
    DeliverySlot(directory="05_Reports", discipline="Reports"),
)


@dataclass(frozen=True, slots=True)
class PackageOptions:
    """Everything about the package that is not the engineering model itself."""

    target: TargetProfile
    validation: ValidationSummary | None = None
    approvals: tuple[ApprovalRecord, ...] = ()
    change_log: tuple[ChangeLogEntry, ...] = ()
    evidence_index: tuple[EvidenceIndexEntry, ...] = ()
    extra_limitations: tuple[Limitation, ...] = ()
    unsupported_items: tuple[UnsupportedItem, ...] = ()
    delivery_slots: tuple[DeliverySlot, ...] = DEFAULT_DELIVERY_SLOTS
    template_versions: tuple[tuple[str, str], ...] = ()
    #: When supplied, every generated L5X is parsed back and, if the rungs come
    #: back as written, its level is raised to ``ROUND_TRIP_PARSED``. Without it
    #: nothing is round-tripped and every file honestly stays at ``GENERATED``.
    settings: Settings | None = None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    """One file, its bytes, and the level it actually reached."""

    relative_path: str
    data: bytes
    kind: ExportArtifactKind
    validation_level: ValidationLevel = ValidationLevel.GENERATED
    source_object_type: ObjectType | None = None
    source_object_id: uuid.UUID | None = None
    template_id: str = ""
    template_version: str = ""
    import_order: int = 0
    is_produced_by_plugin: bool = False
    plugin_name: str = ""
    notes: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return PurePosixPath(self.relative_path).name

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def media_type(self) -> str:
        return _MEDIA_TYPES.get(
            PurePosixPath(self.relative_path).suffix, "application/octet-stream"
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "filename": self.filename,
            "kind": str(self.kind),
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "validation_level": str(self.validation_level),
            "import_order": self.import_order,
            "generator_name": GENERATOR_NAME,
            "generator_version": GENERATOR_VERSION,
            "source_object_type": (
                None if self.source_object_type is None else str(self.source_object_type)
            ),
            "source_object_id": (
                None if self.source_object_id is None else str(self.source_object_id)
            ),
            "template_id": self.template_id,
            "template_version": self.template_version,
            "produced_by_plugin": self.is_produced_by_plugin,
            "plugin_name": self.plugin_name,
            "notes": self.notes,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ReleasePackage:
    """An assembled package, held in memory until somebody asks for it on disk."""

    root_name: str
    files: tuple[GeneratedFile, ...]
    manifest: dict[str, Any]
    order: tuple[ImportItem, ...]
    constraints: tuple[ImportConstraint, ...]
    limitations: tuple[Limitation, ...]
    unsupported_items: tuple[UnsupportedItem, ...]
    #: Directories that exist in the package with nothing in them, so that
    #: :meth:`write` creates them rather than leaving a gap in the layout.
    empty_directories: tuple[str, ...] = ()

    @property
    def validation_level(self) -> ValidationLevel:
        """The weakest level any file reached.

        Rolling up to the *strongest* would quietly promote a file nobody
        checked to the standing of one that was, which is the overclaim the
        level scale exists to prevent.
        """
        if not self.files:
            return ValidationLevel.GENERATED
        return min(
            (file.validation_level for file in self.files),
            key=lambda level: VALIDATION_LEVEL_ORDER[level],
        )

    def file_at(self, relative_path: str) -> GeneratedFile:
        for file in self.files:
            if file.relative_path == relative_path:
                return file
        raise KeyError(relative_path)

    def write(self, destination: Path) -> Path:
        """Write the package under ``destination`` and return its root directory."""
        root = resolve_within(destination, self.root_name)
        root.mkdir(parents=True, exist_ok=True)
        for directory in self.empty_directories:
            resolve_within(root, directory).mkdir(parents=True, exist_ok=True)
        for file in self.files:
            target = resolve_within(root, file.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.data)
        return root

    # -- the rows a caller persists ---------------------------------------

    def artifact_rows(
        self, *, project_id: uuid.UUID, release_candidate_id: uuid.UUID
    ) -> list[ExportArtifact]:
        """One :class:`~app.models.release.ExportArtifact` per file, unpersisted."""
        return [
            ExportArtifact(
                project_id=project_id,
                release_candidate_id=release_candidate_id,
                kind=file.kind,
                relative_path=file.relative_path,
                filename=file.filename,
                media_type=file.media_type,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                validation_level=file.validation_level,
                generator_name=GENERATOR_NAME,
                generator_version=GENERATOR_VERSION,
                source_object_type=file.source_object_type,
                source_object_id=file.source_object_id,
                template_id=file.template_id,
                template_version=file.template_version,
                import_order=file.import_order,
                is_produced_by_plugin=file.is_produced_by_plugin,
                plugin_name=file.plugin_name,
                notes=file.notes,
                warnings=list(file.warnings),
            )
            for file in self.files
        ]

    def dependency_rows(
        self,
        artifact_ids: Mapping[str, uuid.UUID],
        *,
        project_id: uuid.UUID,
        release_candidate_id: uuid.UUID,
    ) -> list[ImportDependency]:
        """The edges, given a map from relative path to the stored artifact id.

        The ids are supplied rather than taken off freshly built objects because
        ``artifact_id`` is NOT NULL: a caller flushes the artifacts, then builds
        these, which is one ordering rule instead of a class of silent failures.
        """
        rows: list[ImportDependency] = []
        for constraint in self.constraints:
            artifact_id = artifact_ids.get(constraint.item_key)
            if artifact_id is None:
                raise RockwellGenerationError(
                    f"No stored artifact id for {constraint.item_key}, so its import "
                    "dependencies cannot be recorded.",
                    details={"relative_path": constraint.item_key},
                )
            depends_on = (
                artifact_ids.get(constraint.depends_on_key)
                if constraint.depends_on_key is not None
                else None
            )
            rows.append(
                ImportDependency(
                    project_id=project_id,
                    release_candidate_id=release_candidate_id,
                    artifact_id=artifact_id,
                    depends_on_artifact_id=depends_on,
                    depends_on_label=constraint.depends_on_label,
                    kind=constraint.kind,
                    reason=constraint.reason,
                )
            )
        return rows

    def manifest_row(
        self, *, project_id: uuid.UUID, release_candidate_id: uuid.UUID
    ) -> ReleaseManifest:
        manifest_bytes = self.file_at(f"{MANIFEST_DIR}/manifest.json").data
        checksums = self.file_at(f"{MANIFEST_DIR}/checksums.sha256")
        evidence = self.file_at(f"{AUDIT_DIR}/evidence_index.json")
        return ReleaseManifest(
            project_id=project_id,
            release_candidate_id=release_candidate_id,
            manifest_version=MANIFEST_VERSION,
            content=self.manifest,
            content_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            artifact_count=len(self.files),
            checksum_file_sha256=checksums.sha256,
            approval_summary=dict(self.manifest["approvals_summary"]),
            evidence_index_sha256=evidence.sha256,
        )

    def apply_to_candidate(self, candidate: ReleaseCandidate) -> None:
        """Write what this package proved onto the candidate that owns it.

        ``import_verification_status`` is set to ``NOT_ATTEMPTED`` and left
        there. Nothing offline can attempt it, and a blank would read as a pass.
        """
        candidate.validation_level = self.validation_level
        candidate.import_verification_status = ImportVerificationStatus.NOT_ATTEMPTED
        candidate.import_verification_detail = _VERIFICATION_STATEMENT
        candidate.generator_version = GENERATOR_VERSION
        candidate.target_profile_snapshot = dict(self.manifest["target"])
        candidate.template_versions = {
            entry["id"]: entry["version"] for entry in self.manifest["templates"]
        }
        candidate.package_path = self.root_name
        candidate.limitations = [item.as_document() for item in self.limitations]
        candidate.unsupported_items = [item.as_document() for item in self.unsupported_items]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _json_bytes(document: Any) -> bytes:
    """JSON with a fixed shape: two-space indent, key order as built, one newline."""
    text = json.dumps(
        document, indent=2, ensure_ascii=False, sort_keys=False, separators=(",", ": ")
    )
    return (text + "\n").encode("utf-8")


def _markdown_bytes(lines: Sequence[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _checksum_file(files: Sequence[GeneratedFile]) -> bytes:
    """``sha256sum`` format, sorted by path, LF line endings.

    Covers every file in the package except itself; a checksum file cannot
    contain its own hash, and ``ReleaseManifest.checksum_file_sha256`` is where
    that value lives instead.
    """
    lines = [
        f"{file.sha256}  {file.relative_path}"
        for file in sorted(files, key=lambda item: item.relative_path)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _safe_component(name: str) -> str:
    """A Logix identifier as a filename, refusing anything that is not one.

    Logix names are letters, digits and underscores, so a name that needs
    sanitising is a name this generator has no business writing to a file — and
    sanitising two different names into one would silently drop a file.
    """
    candidate = name.strip()
    if not candidate or not all(char.isalnum() or char == "_" for char in candidate):
        raise RockwellGenerationError(
            f"{name!r} is not a Logix identifier, so it cannot be used as a filename "
            "in the release package.",
            details={"name": name},
        )
    return candidate


# ---------------------------------------------------------------------------
# Round-trip verification
# ---------------------------------------------------------------------------


def _round_trip(
    data: bytes, *, filename: str, expected_rung_texts: Sequence[str] | None, settings: Settings
) -> tuple[bool, str]:
    """Parse the emitted bytes back and check the rungs came back as written.

    This is the only honest basis for claiming ``ROUND_TRIP_PARSED``: it proves
    the file is well-formed, that the parser finds the component where the
    generator put it, and that no rung was truncated or silently reordered on
    the way through.
    """
    with tempfile.TemporaryDirectory(prefix="esco-rockwell-roundtrip-") as directory:
        path = Path(directory) / filename
        path.write_bytes(data)
        context = ParseContext(
            file_id=uuid.UUID(int=0),
            project_id=uuid.UUID(int=0),
            path=path,
            filename=filename,
            file_kind=FileKind.L5X,
            document_role=DocumentRole.CONTROL_LOGIC,
            settings=settings,
        )
        try:
            result = L5XParser().parse(context)
        except Exception as exc:
            # Deliberately broad. Whatever went wrong, the honest outcome is the
            # same: this file did not come back, so its level does not rise and
            # the reason travels with it into the manifest.
            return False, f"The generated file did not parse back: {exc}"
    if expected_rung_texts is None:
        return True, ""
    parsed = [str(rung["text"]) for rung in result.content["rungs"]]
    if parsed != list(expected_rung_texts):
        return False, (
            "The generated file parsed, but the rungs did not come back as written "
            f"({len(parsed)} rung(s) read, {len(expected_rung_texts)} written)."
        )
    return True, ""


def _level_for_l5x(
    data: bytes,
    *,
    filename: str,
    expected_rung_texts: Sequence[str] | None,
    settings: Settings | None,
) -> tuple[ValidationLevel, tuple[str, ...]]:
    if settings is None:
        return ValidationLevel.GENERATED, ()
    ok, note = _round_trip(
        data, filename=filename, expected_rung_texts=expected_rung_texts, settings=settings
    )
    if ok:
        return ValidationLevel.ROUND_TRIP_PARSED, ()
    return ValidationLevel.GENERATED, (note,)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Builder:
    model: ProjectModel
    options: PackageOptions
    files: list[GeneratedFile] = field(default_factory=list)
    #: Paths whose round trip did not come back as written. Tracked separately
    #: from a file's warnings, because an artifact can carry a warning for a
    #: reason that has nothing to do with parsing — an Add-On Instruction whose
    #: definition was never supplied, say — and reporting that as a failed
    #: round trip would be a limitation that is not true.
    round_trip_failures: list[str] = field(default_factory=list)
    items: list[ImportItem] = field(default_factory=list)
    constraints: list[ImportConstraint] = field(default_factory=list)

    # -- names -------------------------------------------------------------

    def controller_context(self) -> ControllerContext:
        return ControllerContext.from_model(
            self.model.controller,
            schema_revision=self.options.target.l5x_schema_revision,
            software_revision=self.options.target.software_revision,
            export_date=self.options.target.export_date,
        )

    def scope_for(self, program: ProgramContent | None) -> LogicScope:
        tags = [tag.name for tag in self.model.controller_tags]
        routines: list[str] = []
        if program is not None:
            tags.extend(tag.name for tag in program.tags)
            routines.extend(content.routine.name for content in program.routines)
        return LogicScope.build(
            family=self.options.target.controller_family,
            tags=tags,
            routines=routines,
            aois=[aoi.name for aoi in self.model.aoi_references],
        )

    @staticmethod
    def udt_path(name: str) -> str:
        return f"{CONTROLLER_DIR}/UDTs/{_safe_component(name)}.L5X"

    @staticmethod
    def aoi_path(name: str) -> str:
        return f"{CONTROLLER_DIR}/AOIs/{_safe_component(name)}.L5X"

    @staticmethod
    def program_path(name: str) -> str:
        return f"{CONTROLLER_DIR}/Programs/{_safe_component(name)}.L5X"

    @staticmethod
    def routine_path(program_name: str, routine_name: str) -> str:
        # A hyphen cannot appear in a Logix identifier, so the join is reversible
        # and two programs may hold routines of the same name without collision.
        return (
            f"{CONTROLLER_DIR}/Routines/"
            f"{_safe_component(program_name)}-{_safe_component(routine_name)}.L5X"
        )

    @property
    def controller_path(self) -> str:
        return f"{CONTROLLER_DIR}/Controller_Project.L5X"

    @property
    def tags_path(self) -> str:
        return f"{CONTROLLER_DIR}/Controller_Tags.csv"

    @property
    def comments_path(self) -> str:
        return f"{CONTROLLER_DIR}/Controller_Comments.csv"

    @property
    def cross_reference_path(self) -> str:
        return f"{CONTROLLER_DIR}/Cross_Reference.csv"

    # -- emission ----------------------------------------------------------

    def note_round_trip(self, path: str, warnings: tuple[str, ...]) -> None:
        if warnings:
            self.round_trip_failures.append(path)

    def add(self, file: GeneratedFile) -> None:
        if any(existing.relative_path == file.relative_path for existing in self.files):
            raise RockwellGenerationError(
                f"Two files in this package claim the path {file.relative_path}.",
                details={"relative_path": file.relative_path},
            )
        self.files.append(file)

    def emit_controller(self) -> None:
        data = emit_controller_project(
            controller=self.controller_context(),
            modules=self.model.modules,
            tasks=[(entry.task, entry.scheduled_program_names) for entry in self.model.tasks],
            programs=[entry.program for entry in self.model.programs],
        )
        level, warnings = _level_for_l5x(
            data,
            filename="Controller_Project.L5X",
            expected_rung_texts=[],
            settings=self.options.settings,
        )
        self.note_round_trip(self.controller_path, warnings)
        self.add(
            GeneratedFile(
                relative_path=self.controller_path,
                data=data,
                kind=ExportArtifactKind.CONTROLLER_PROJECT_L5X,
                validation_level=level,
                # ``ObjectType`` has no controller member; the published
                # vocabulary is not extended from inside a generator.
                source_object_type=None,
                source_object_id=None,
                notes=(
                    "Controller skeleton: attributes, modules, tasks and a shell for "
                    "each program. Program tags and routine logic come from the "
                    "component files, so no object is declared twice with different "
                    "content."
                ),
                warnings=warnings,
            )
        )
        self.items.append(
            ImportItem(
                key=self.controller_path,
                label="Controller skeleton (modules, tasks, program shells)",
                kind=ExportArtifactKind.CONTROLLER_PROJECT_L5X,
                rank=10,
            )
        )

    def emit_udts(self) -> None:
        for content in sorted(self.model.udts, key=lambda item: item.udt.name):
            udt = content.udt
            path = self.udt_path(udt.name)
            data = emit_udt(controller=self.controller_context(), udt=udt, members=content.members)
            level, warnings = _level_for_l5x(
                data,
                filename=f"{udt.name}.L5X",
                expected_rung_texts=[],
                settings=self.options.settings,
            )
            self.note_round_trip(path, warnings)
            self.add(
                GeneratedFile(
                    relative_path=path,
                    data=data,
                    kind=ExportArtifactKind.UDT_L5X,
                    validation_level=level,
                    source_object_type=ObjectType.UDT_DEFINITION,
                    source_object_id=udt.id,
                    template_id=udt.template_id or "",
                    warnings=warnings,
                )
            )
            self.items.append(
                ImportItem(
                    key=path,
                    label=f"User-defined type {udt.name}",
                    kind=ExportArtifactKind.UDT_L5X,
                    rank=20,
                )
            )
            self.after_controller(path, f"User-defined type {udt.name}")

    def emit_aois(self) -> None:
        for aoi in sorted(self.model.aoi_references, key=lambda item: item.name):
            path = self.aoi_path(aoi.name)
            data = emit_aoi_reference(controller=self.controller_context(), aoi=aoi)
            level, warnings = _level_for_l5x(
                data,
                filename=f"{aoi.name}.L5X",
                expected_rung_texts=[],
                settings=self.options.settings,
            )
            self.note_round_trip(path, warnings)
            note = (
                "Interface reference only. This platform references Add-On "
                "Instructions and never fabricates them, so this file declares the "
                "parameters and contains no logic."
            )
            extra: tuple[str, ...] = ()
            if not aoi.is_available:
                extra = (
                    "The definition of this Add-On Instruction was not supplied, so "
                    "its interface could not be checked against anything: "
                    f"{aoi.availability_note or 'no reason recorded'}",
                )
            self.add(
                GeneratedFile(
                    relative_path=path,
                    data=data,
                    kind=ExportArtifactKind.AOI_L5X,
                    validation_level=level,
                    source_object_type=ObjectType.AOI_REFERENCE,
                    source_object_id=aoi.id,
                    notes=note,
                    warnings=warnings + extra,
                )
            )
            self.items.append(
                ImportItem(
                    key=path,
                    label=f"Add-On Instruction interface {aoi.name}",
                    kind=ExportArtifactKind.AOI_L5X,
                    rank=30,
                )
            )
            self.after_controller(path, f"Add-On Instruction {aoi.name}")
            self.constraints.append(
                ImportConstraint(
                    item_key=path,
                    depends_on_key=None,
                    depends_on_label=f"The compiled definition of Add-On Instruction {aoi.name}",
                    kind=ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION,
                    reason=(
                        "This file carries the interface only. Import the customer's own "
                        "definition of this Add-On Instruction before importing any "
                        "routine that calls it."
                    ),
                )
            )

    def emit_tags(self) -> None:
        program_names = {
            entry.program.id: entry.program.name
            for entry in self.model.programs
            if entry.program.id is not None
        }
        data = emit_tag_csv(
            sorted(self.model.controller_tags, key=lambda tag: tag.name), program_names
        )
        self.add(
            GeneratedFile(
                relative_path=self.tags_path,
                data=data,
                kind=ExportArtifactKind.TAG_CSV,
                notes=(
                    "Controller-scope tags only. Program-scope tags are declared in "
                    "their own program's L5X, so no tag is declared twice."
                ),
            )
        )
        self.items.append(
            ImportItem(
                key=self.tags_path,
                label="Controller-scope tags",
                kind=ExportArtifactKind.TAG_CSV,
                rank=40,
            )
        )
        self.after_controller(self.tags_path, "Controller-scope tags")

    def emit_programs_and_routines(self) -> None:
        for content in sorted(
            self.model.programs, key=lambda item: (item.program.ordinal, item.program.name)
        ):
            program = content.program
            scope = self.scope_for(content)
            program_path = self.program_path(program.name)
            data = emit_program(
                controller=self.controller_context(),
                program=program,
                tags=content.tags,
                routines=[(entry.routine, entry.rungs) for entry in content.routines],
                scope=scope,
            )
            expected = [
                render_rung_text(
                    rung,
                    scope,
                    location=f"{program.name} / {entry.routine.name} / Rung {rung.ordinal}",
                )
                for entry in sorted(
                    content.routines, key=lambda item: (item.routine.ordinal, item.routine.name)
                )
                for rung in sorted(entry.rungs, key=lambda item: item.ordinal)
            ]
            level, warnings = _level_for_l5x(
                data,
                filename=f"{program.name}.L5X",
                expected_rung_texts=expected,
                settings=self.options.settings,
            )
            self.note_round_trip(program_path, warnings)
            self.add(
                GeneratedFile(
                    relative_path=program_path,
                    data=data,
                    kind=ExportArtifactKind.PROGRAM_L5X,
                    validation_level=level,
                    source_object_type=ObjectType.PROGRAM,
                    source_object_id=program.id,
                    notes=(
                        "Program-scope tags and the full text of every routine, which is "
                        "the shape Studio 5000 exports. The individual routine files "
                        "hold the same logic and exist for review and for replacing one "
                        "routine on its own."
                    ),
                    warnings=warnings,
                )
            )
            self.items.append(
                ImportItem(
                    key=program_path,
                    label=f"Program {program.name} (tags and routines)",
                    kind=ExportArtifactKind.PROGRAM_L5X,
                    rank=50,
                )
            )
            self.after_controller(program_path, f"Program {program.name}")
            self.emit_routines(content, scope, program_path)

    def emit_routines(self, content: ProgramContent, scope: LogicScope, program_path: str) -> None:
        program = content.program
        for entry in sorted(
            content.routines, key=lambda item: (item.routine.ordinal, item.routine.name)
        ):
            routine = entry.routine
            path = self.routine_path(program.name, routine.name)
            data = emit_routine(
                controller=self.controller_context(),
                program_name=program.name,
                routine=routine,
                rungs=entry.rungs,
                scope=scope,
            )
            expected = [
                render_rung_text(
                    rung,
                    scope,
                    location=f"{program.name} / {routine.name} / Rung {rung.ordinal}",
                )
                for rung in sorted(entry.rungs, key=lambda item: item.ordinal)
            ]
            level, warnings = _level_for_l5x(
                data,
                filename=f"{routine.name}.L5X",
                expected_rung_texts=expected,
                settings=self.options.settings,
            )
            self.note_round_trip(path, warnings)
            self.add(
                GeneratedFile(
                    relative_path=path,
                    data=data,
                    kind=ExportArtifactKind.ROUTINE_L5X,
                    validation_level=level,
                    source_object_type=ObjectType.ROUTINE,
                    source_object_id=routine.id,
                    template_id=routine.template_id or "",
                    template_version=routine.template_version or "",
                    notes=(
                        "The same logic as the program export above. Import this one "
                        "only when replacing this routine on its own."
                    ),
                    warnings=warnings,
                )
            )
            self.items.append(
                ImportItem(
                    key=path,
                    label=f"Routine {program.name} / {routine.name}",
                    kind=ExportArtifactKind.ROUTINE_L5X,
                    rank=60,
                )
            )
            self.constraints.append(
                ImportConstraint(
                    item_key=path,
                    depends_on_key=program_path,
                    depends_on_label=f"Program {program.name}",
                    kind=ImportDependencyKind.REQUIRES_PROGRAM,
                    reason=(
                        f"{routine.name} is a routine inside {program.name}, which has to "
                        "exist and hold its program-scope tags before the routine can be "
                        "imported into it."
                    ),
                )
            )
            self.constrain_routine(entry, path, program)

    def constrain_routine(self, entry: RoutineContent, path: str, program: Program) -> None:
        """Data types, Add-On Instructions and tags this routine's rungs need."""
        controller_tag_names = {tag.name.upper(): tag for tag in self.model.controller_tags}
        aoi_names = {aoi.name.upper(): aoi for aoi in self.model.aoi_references}
        used_controller_tags: set[str] = set()
        used_aois: set[str] = set()
        for rung in entry.rungs:
            for instruction in rung.instructions or []:
                mnemonic = instruction.mnemonic.strip().upper()
                if mnemonic in aoi_names:
                    used_aois.add(aoi_names[mnemonic].name)
                for operand in instruction.operands or []:
                    symbol = (operand.resolved_name or operand.text or "").strip()
                    base = symbol.split(".", 1)[0].split("[", 1)[0].strip().upper()
                    if base in controller_tag_names:
                        used_controller_tags.add(controller_tag_names[base].name)
        if used_controller_tags:
            named = ", ".join(sorted(used_controller_tags)[:5])
            self.constraints.append(
                ImportConstraint(
                    item_key=path,
                    depends_on_key=self.tags_path,
                    depends_on_label="Controller-scope tags",
                    kind=ImportDependencyKind.REQUIRES_TAG,
                    reason=(
                        f"{program.name} / {entry.routine.name} references controller-scope "
                        f"tags ({named}). A rung importing before its tags fails with an "
                        "error that does not name the tag."
                    ),
                )
            )
        for name in sorted(used_aois):
            self.constraints.append(
                ImportConstraint(
                    item_key=path,
                    depends_on_key=self.aoi_path(name),
                    depends_on_label=f"Add-On Instruction {name}",
                    kind=ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION,
                    reason=(
                        f"{program.name} / {entry.routine.name} calls {name}, so the "
                        "instruction has to be defined before the rung that calls it."
                    ),
                )
            )

    def constrain_data_types(self) -> None:
        """Data types before the tags that use them; modules before I/O aliases."""
        udt_names = {content.udt.name.upper(): content.udt.name for content in self.model.udts}
        aoi_names = {aoi.name.upper(): aoi.name for aoi in self.model.aoi_references}

        def link(target_key: str, target_label: str, tags: Sequence[Tag]) -> None:
            for type_name in sorted(
                {
                    (tag.data_type_name or "").strip().upper()
                    for tag in tags
                    if (tag.data_type_name or "").strip()
                }
            ):
                if type_name in udt_names:
                    name = udt_names[type_name]
                    self.constraints.append(
                        ImportConstraint(
                            item_key=target_key,
                            depends_on_key=self.udt_path(name),
                            depends_on_label=f"User-defined type {name}",
                            kind=ImportDependencyKind.REQUIRES_DATA_TYPE,
                            reason=(
                                f"{target_label} declares at least one tag of type {name}. "
                                "A tag whose data type does not yet exist cannot be created."
                            ),
                        )
                    )
                elif type_name in aoi_names:
                    name = aoi_names[type_name]
                    self.constraints.append(
                        ImportConstraint(
                            item_key=target_key,
                            depends_on_key=self.aoi_path(name),
                            depends_on_label=f"Add-On Instruction {name}",
                            kind=ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION,
                            reason=(
                                f"{target_label} declares at least one tag of type {name}, "
                                "which is an Add-On Instruction type."
                            ),
                        )
                    )

        link(self.tags_path, "Controller_Tags.csv", self.model.controller_tags)
        for content in self.model.programs:
            link(
                self.program_path(content.program.name),
                f"Program {content.program.name}",
                content.tags,
            )

        aliases = [tag for tag in self.model.controller_tags if tag.is_alias or tag.io_point_id]
        if aliases and self.model.modules:
            named = ", ".join(sorted(tag.name for tag in aliases)[:5])
            self.constraints.append(
                ImportConstraint(
                    item_key=self.tags_path,
                    depends_on_key=self.controller_path,
                    depends_on_label="Controller skeleton (modules)",
                    kind=ImportDependencyKind.REQUIRES_MODULE,
                    reason=(
                        f"Controller_Tags.csv declares I/O aliases ({named}). An alias to a "
                        "module address cannot resolve until the module exists."
                    ),
                )
            )

    def after_controller(self, key: str, label: str) -> None:
        self.constraints.append(
            ImportConstraint(
                item_key=key,
                depends_on_key=self.controller_path,
                depends_on_label="Controller skeleton",
                kind=ImportDependencyKind.ORDERED_AFTER,
                reason=(f"{label} is imported into a controller, which has to exist first."),
            )
        )

    def emit_comment_and_cross_reference(self) -> None:
        rung_comments: list[tuple[str, str, LogicRung]] = []
        rung_numbers: dict[uuid.UUID, int] = {}
        mnemonics: dict[uuid.UUID, str] = {}
        for content in self.model.programs:
            for entry in content.routines:
                for rung in entry.rungs:
                    rung_comments.append((content.program.name, entry.routine.name, rung))
                    if rung.id is not None:
                        rung_numbers[rung.id] = rung.ordinal
                    for instruction in rung.instructions or []:
                        if instruction.id is not None:
                            mnemonics[instruction.id] = instruction.mnemonic.strip().upper()
        comments = emit_comment_csv(rung_comments, list(self.model.operand_comments))
        self.add(
            GeneratedFile(
                relative_path=self.comments_path,
                data=comments,
                kind=ExportArtifactKind.COMMENT_CSV,
                notes="Rung and operand comments, located by program, routine and rung.",
            )
        )
        self.items.append(
            ImportItem(
                key=self.comments_path,
                label="Rung and operand comments",
                kind=ExportArtifactKind.COMMENT_CSV,
                rank=70,
            )
        )
        for content in self.model.programs:
            self.constraints.append(
                ImportConstraint(
                    item_key=self.comments_path,
                    depends_on_key=self.program_path(content.program.name),
                    depends_on_label=f"Program {content.program.name}",
                    kind=ImportDependencyKind.ORDERED_AFTER,
                    reason=(
                        "A comment attaches to a rung, so the rungs it describes have to "
                        "be in the controller before the comments are applied."
                    ),
                )
            )

        cross_reference = emit_cross_reference_csv(
            sorted(
                self.model.logic_references,
                key=lambda reference: (
                    reference.tag_name,
                    reference.program_name or "",
                    reference.routine_name or "",
                ),
            ),
            rung_numbers=rung_numbers,
            instruction_mnemonics=mnemonics,
        )
        self.add(
            GeneratedFile(
                relative_path=self.cross_reference_path,
                data=cross_reference,
                kind=ExportArtifactKind.CROSS_REFERENCE_CSV,
                notes=(
                    "A report, not an import. Sorted so that every writer of a tag sits "
                    "next to every other writer of it."
                ),
            )
        )


def _templates(model: ProjectModel, options: PackageOptions) -> list[dict[str, str]]:
    """Every template that expanded into this package, id and version."""
    pairs: set[tuple[str, str]] = {
        (template_id, version) for template_id, version in options.template_versions
    }
    for content in model.programs:
        for entry in content.routines:
            if entry.routine.template_id:
                pairs.add((entry.routine.template_id, entry.routine.template_version or ""))
            for rung in entry.rungs:
                if rung.template_id:
                    pairs.add((rung.template_id, rung.template_version or ""))
    for udt in model.udts:
        if udt.udt.template_id:
            pairs.add((udt.udt.template_id, ""))
    return [{"id": template_id, "version": version} for template_id, version in sorted(pairs)]


def _standing_limitations(
    model: ProjectModel, options: PackageOptions, builder: _Builder
) -> list[Limitation]:
    """The limitations every package carries, plus the ones this one earned."""
    limitations: list[Limitation] = [
        Limitation(
            code="import_ready_candidate",
            summary=f"Everything in this package is an {IMPORT_READY_CANDIDATE}.",
            detail=_VERIFICATION_STATEMENT,
        ),
        Limitation(
            code="no_timestamps",
            summary="No file carries a generation timestamp.",
            detail=(
                "Studio 5000 writes an ExportDate attribute and a dated remark block at "
                "the top of a tag CSV. Both are omitted here: a clock reading would make "
                "two generations of the same approved revision differ, and the checksums "
                "in this manifest would then prove nothing. " + _DETERMINISM_STATEMENT
            ),
        ),
        Limitation(
            code="package_split_unverified",
            summary="The split between the controller skeleton and the component files "
            "has not been checked against a Studio 5000 import.",
            detail=(
                "Controller_Project.L5X carries the controller, its modules, its tasks "
                "and a shell for each program; program tags and routine logic come from "
                "the component files. That avoids declaring any object twice with "
                "different content. It has not been proved against a real import. If "
                "your Studio 5000 version refuses a program shell that holds no "
                "routines, import the program files first and then the controller file."
            ),
        ),
        Limitation(
            code="export_envelope_from_a_sample",
            summary="The L5X export envelope is modelled on a sample export, not on a schema.",
            detail=(
                "The RSLogix5000Content attributes (SchemaRevision, TargetType, TargetName, "
                'ContainsContext, ExportOptions) and the <Controller Use="Context"> wrapper '
                "are taken from a Studio 5000 v32 export and from what this platform's own "
                "parser reads. Two details are held with less confidence than the rest: the "
                "ExportOptions string is copied verbatim rather than derived, and the "
                'Use="Context" markers Studio writes on container elements such as '
                "<DataTypes> and <Programs> are not written here. Neither is checked against "
                "a published schema, because Rockwell does not publish one."
            ),
        ),
        Limitation(
            code="tag_csv_attributes",
            summary="The tag CSV carries only the attributes the model holds.",
            detail=(
                "Radix is not in the canonical model, so it is not written. Studio 5000 "
                "applies its default for the data type. A description containing a "
                "newline is written as correctly quoted CSV; some versions of the Logix "
                "importer do not accept an embedded newline, and such a description is "
                "worth shortening before import."
            ),
        ),
        Limitation(
            code="aoi_interface_only",
            summary="Add-On Instruction files declare an interface and contain no logic.",
            detail=(
                "This platform references Add-On Instructions and never fabricates them: "
                "an AOI is compiled logic with its own revision history and its own "
                "approval. Each file in 01_Controller/AOIs declares the parameters so a "
                "tag of that type resolves. Import the customer's own definition; do not "
                "import these over an existing Add-On Instruction."
            ),
        ),
        Limitation(
            code="cross_reference_is_a_report",
            summary="Cross_Reference.csv is a report and is not in the import order.",
            detail=(
                "It is generated from the cross-reference rows so that duplicate "
                "destructive writes to one tag can be seen at a glance. Nothing imports it."
            ),
        ),
    ]
    unavailable = sorted(aoi.name for aoi in model.aoi_references if not aoi.is_available)
    if unavailable:
        limitations.append(
            Limitation(
                code="aoi_definition_not_supplied",
                summary=(
                    "The definition of "
                    f"{', '.join(unavailable)} was not supplied, so the interface could "
                    "not be checked against anything."
                ),
                detail=(
                    "Tags of these types are modelled and referenced, and the parameter "
                    "list written into 01_Controller/AOIs is the one recorded when the "
                    "instruction was first seen used. Whether it matches the definition "
                    "the customer holds is not established."
                ),
            )
        )
    for slot in options.delivery_slots:
        if slot.is_present:
            continue
        limitations.append(
            Limitation(
                code=f"delivery_plugin_absent.{slot.directory}",
                summary=f"{slot.directory} is empty: no {slot.discipline} plugin ran.",
                detail=(
                    slot.absence_reason
                    or (
                        f"No delivery plugin for {slot.discipline} was available when this "
                        "package was generated. The directory exists so the layout is the "
                        "same in every package; it is empty because nothing produced "
                        "anything for it, not because nothing was needed."
                    )
                ),
            )
        )
    degraded = sorted(set(builder.round_trip_failures))
    if degraded:
        limitations.append(
            Limitation(
                code="round_trip_not_attained",
                summary="Some generated L5X did not come back from our own parser as written.",
                detail=(
                    "These files stayed at the 'generated' level rather than reaching "
                    "'round_trip_parsed': " + ", ".join(degraded) + ". Their per-file "
                    "warnings in manifest.json say what did not match."
                ),
            )
        )
    limitations.extend(options.extra_limitations)
    return limitations


def _limitations_markdown(
    limitations: Sequence[Limitation], release_name: str, level: ValidationLevel
) -> bytes:
    lines = [
        f"# Limitations - {release_name}",
        "",
        "## Read this before you import anything",
        "",
        f"The files in this package are **{IMPORT_READY_CANDIDATE}s**. No Studio 5000 import",
        f"has been verified. The highest level anything here reached is `{level}`,",
        "which means this platform's own parser read the generated files back and found",
        "what it wrote. That catches malformed XML, dropped operands, lost descriptions and",
        "silently truncated rungs. It does **not** establish that Studio 5000 will accept",
        "these files, and nothing in this package should be described as verified.",
        "",
        "Import into a copy of the project, not the live one, and review what the import",
        "dialog reports before you accept it.",
        "",
        "## Known limitations",
        "",
    ]
    for limitation in limitations:
        lines.append(f"### {limitation.summary}")
        lines.append("")
        lines.append(f"`{limitation.code}`")
        lines.append("")
        lines.append(limitation.detail)
        lines.append("")
    return _markdown_bytes(lines)


def build_release_package(model: ProjectModel, options: PackageOptions) -> ReleasePackage:
    """Generate every file in the package, in memory, deterministically."""
    builder = _Builder(model=model, options=options)
    builder.emit_controller()
    builder.emit_udts()
    builder.emit_aois()
    builder.emit_tags()
    builder.emit_programs_and_routines()
    builder.constrain_data_types()
    builder.emit_comment_and_cross_reference()

    ordered = resolve_import_order(builder.items, builder.constraints)
    positions = {item.key: position for position, item in enumerate(ordered, start=1)}

    release_name = f"{_RELEASE_PREFIX}{model.revision_number}"
    limitations = _standing_limitations(model, options, builder)
    unsupported = tuple(
        sorted(
            options.unsupported_items,
            key=lambda item: (item.kind, item.subject, item.location),
        )
    )

    # Re-stamp each file with its position in the import sequence now that the
    # sequence is known. Files that nothing imports keep 0.
    files = [
        replace(file, import_order=positions.get(file.relative_path, 0)) for file in builder.files
    ]

    validation = options.validation or ValidationSummary(revision_number=model.revision_number)
    order_markdown = render_import_order_markdown(
        ordered,
        builder.constraints,
        release_name=release_name,
        preamble=(
            "`Cross_Reference.csv` is a report and is not imported; it is not listed below.",
        ),
    )
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/IMPORT_ORDER.md",
            data=order_markdown,
            kind=ExportArtifactKind.IMPORT_ORDER,
        )
    )
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/target_profile.json",
            data=_json_bytes(options.target.as_document()),
            kind=ExportArtifactKind.TARGET_PROFILE,
        )
    )
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/validation_summary.json",
            data=_json_bytes(validation.as_document()),
            kind=ExportArtifactKind.VALIDATION_SUMMARY,
        )
    )
    files.append(
        GeneratedFile(
            relative_path=f"{AUDIT_DIR}/change_log.json",
            data=_json_bytes(
                {
                    "revision_number": model.revision_number,
                    "entries": [entry.as_document() for entry in options.change_log],
                }
            ),
            kind=ExportArtifactKind.CHANGE_LOG,
        )
    )
    files.append(
        GeneratedFile(
            relative_path=f"{AUDIT_DIR}/approval_log.json",
            data=_json_bytes(
                {
                    "revision_number": model.revision_number,
                    "approvals": [record.as_document() for record in options.approvals],
                }
            ),
            kind=ExportArtifactKind.APPROVAL_LOG,
        )
    )
    files.append(
        GeneratedFile(
            relative_path=f"{AUDIT_DIR}/evidence_index.json",
            data=_json_bytes(
                {
                    "revision_number": model.revision_number,
                    "entries": [entry.as_document() for entry in options.evidence_index],
                }
            ),
            kind=ExportArtifactKind.EVIDENCE_INDEX,
        )
    )
    for slot in options.delivery_slots:
        for relative_name, data in sorted(slot.files, key=lambda pair: pair[0]):
            files.append(
                GeneratedFile(
                    relative_path=f"{slot.directory}/{relative_name}",
                    data=data,
                    kind=ExportArtifactKind.REPORT,
                    is_produced_by_plugin=True,
                    plugin_name=slot.plugin_name,
                    notes=(
                        "Produced by a partner-owned delivery plugin. This platform does "
                        "not warrant code it did not generate."
                    ),
                )
            )

    levels = [file.validation_level for file in files] or [ValidationLevel.GENERATED]
    highest = max(levels, key=lambda value: VALIDATION_LEVEL_ORDER[value])
    lowest = min(levels, key=lambda value: VALIDATION_LEVEL_ORDER[value])
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/LIMITATIONS.md",
            data=_limitations_markdown(limitations, release_name, highest),
            kind=ExportArtifactKind.LIMITATIONS,
        )
    )

    files.sort(key=lambda file: file.relative_path)
    manifest = _manifest_document(
        model=model,
        options=options,
        release_name=release_name,
        files=files,
        ordered=ordered,
        constraints=builder.constraints,
        limitations=limitations,
        unsupported=unsupported,
        validation=validation,
        highest=highest,
        lowest=lowest,
    )
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/manifest.json",
            data=_json_bytes(manifest),
            kind=ExportArtifactKind.MANIFEST,
        )
    )
    files.sort(key=lambda file: file.relative_path)
    files.append(
        GeneratedFile(
            relative_path=f"{MANIFEST_DIR}/checksums.sha256",
            data=_checksum_file(files),
            kind=ExportArtifactKind.CHECKSUMS,
        )
    )
    files.sort(key=lambda file: file.relative_path)

    empty = tuple(slot.directory for slot in options.delivery_slots if not slot.files)
    return ReleasePackage(
        root_name=release_name,
        files=tuple(files),
        manifest=manifest,
        order=ordered,
        constraints=tuple(builder.constraints),
        limitations=tuple(limitations),
        unsupported_items=unsupported,
        empty_directories=empty,
    )


def _manifest_document(
    *,
    model: ProjectModel,
    options: PackageOptions,
    release_name: str,
    files: Sequence[GeneratedFile],
    ordered: Sequence[ImportItem],
    constraints: Sequence[ImportConstraint],
    limitations: Sequence[Limitation],
    unsupported: Sequence[UnsupportedItem],
    validation: ValidationSummary,
    highest: ValidationLevel,
    lowest: ValidationLevel,
) -> dict[str, Any]:
    by_item: dict[str, list[ImportConstraint]] = {}
    for constraint in constraints:
        by_item.setdefault(constraint.item_key, []).append(constraint)

    decisions: dict[str, int] = {}
    for record in options.approvals:
        decisions[record.decision] = decisions.get(record.decision, 0) + 1

    return {
        "manifest_version": MANIFEST_VERSION,
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "project": {
            "name": model.project_name,
            "revision_number": model.revision_number,
            "controller_name": model.controller.name or "",
            "release_directory": release_name,
        },
        "target": options.target.as_document(),
        "templates": _templates(model, options),
        "determinism": {
            "statement": _DETERMINISM_STATEMENT,
            "wall_clock_timestamps": "omitted",
        },
        "verification": {
            "label": IMPORT_READY_CANDIDATE,
            # Both ends are stated. The highest is what the strongest file
            # reached; the lowest is what the package as a whole can claim, and
            # rolling up to the highest would promote a file nobody checked.
            "highest_level_attained": str(highest),
            "package_level": str(lowest),
            "rockwell_import_verified": False,
            "statement": _VERIFICATION_STATEMENT,
        },
        "artifacts": [file.as_document() for file in files],
        "import_order": [
            {
                "position": position,
                "path": item.key,
                "label": item.label,
                "kind": str(item.kind),
                "depends_on": [
                    {
                        "path": constraint.depends_on_key,
                        "label": constraint.depends_on_label,
                        "kind": str(constraint.kind),
                        "reason": constraint.reason,
                    }
                    for constraint in sorted(
                        by_item.get(item.key, []),
                        key=lambda entry: (entry.depends_on_key or "", str(entry.kind)),
                    )
                ],
            }
            for position, item in enumerate(ordered, start=1)
        ],
        "limitations": [item.as_document() for item in limitations],
        "unsupported_items": [item.as_document() for item in unsupported],
        "approvals": [record.as_document() for record in options.approvals],
        "approvals_summary": {
            "count": len(options.approvals),
            "decisions": {key: decisions[key] for key in sorted(decisions)},
        },
        "validation": validation.as_document(),
    }
