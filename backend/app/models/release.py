"""Validation runs and the release pipeline: profiles, artifacts, manifests.

An artifact is never "valid". It has reached a *level*. :class:`ValidationLevel`
is strictly ordered and every artifact and candidate carries the highest level it
has actually attained, because "valid" is a single word that hides the difference
between the checks we were able to run and the check the customer actually cares
about.

Everything this backend can do offline stops at ``ROUND_TRIP_PARSED``. Proving
that our own generator's output parses back into the same model is worth real
money — it catches malformed XML, dropped operands, lost descriptions and
silently truncated rungs — and it is *not* the same as proving Studio 5000 will
import the file. ``ROCKWELL_IMPORT_VERIFIED`` is unreachable without the optional
licensed-workstation adapter (:class:`WorkstationVerificationJob`), which most
deployments will never configure.

So nothing here is ever labelled verified because a lower level passed. Levels
are recorded as they are attained, never inferred from one another: a package
that was only round-trip parsed says exactly that on its face, and
:meth:`ReleaseCandidate.is_import_ready_candidate_only` exists so the UI, the
manifest and the package README all have one place to ask.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.core.enums import (
    VALIDATION_LEVEL_ORDER,
    ControllerFamily,
    ExportArtifactKind,
    ImportDependencyKind,
    ImportVerificationStatus,
    ObjectType,
    ReleaseCandidateStatus,
    ValidationDiscipline,
    ValidationLevel,
    ValidationRunStatus,
    ValidationSeverity,
)
from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import GUID, JSONDict, JSONList, UTCDateTime
from app.models.core import EngineeringObjectMixin


class ExportProfile(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """Named settings for one generation run.

    Kept separate from the project profile because *what the controller is* and
    *how we choose to export it* change independently. A project has one target
    platform, but an engineer may reasonably produce a tags-only package for a
    naming review in the morning and a full controller package for site in the
    afternoon, from the same model, at the same revision.
    """

    __tablename__ = "export_profiles"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_export_profiles_project_id_key"),
    )

    #: Stable machine name, e.g. ``full_controller`` or ``tags_only``. Unique per
    #: project rather than globally: two projects may both sensibly own a
    #: profile called "default", and automation refers to profiles by this key.
    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: The Studio 5000 version this package is written *for*, e.g. ``32.03``.
    #: Free text because it records the version the customer actually has
    #: installed, which is not a set this platform gets to enumerate.
    target_studio_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    controller_family: Mapped[ControllerFamily] = mapped_column(
        default=ControllerFamily.OTHER, nullable=False
    )

    #: :class:`ExportArtifactKind` values, as their stored strings — JSON has no
    #: enum, so a reader coerces. A list rather than a boolean column per kind
    #: because the set of kinds grows with every delivery plugin, and a column
    #: per kind would make adding a plugin a schema migration.
    artifact_kinds: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    #: Whether partner-plugin outputs (HMI, alarms, test plans) are included.
    #: Separable from ``artifact_kinds`` because a plugin can be unavailable at
    #: generation time, and "we chose not to" must stay distinguishable from
    #: "we could not".
    include_delivery_artifacts: Mapped[bool] = mapped_column(default=True, nullable=False)

    #: The L5X schema revision the generator writes against, e.g. ``1.0``. Held
    #: on the profile so a package can be regenerated for an older Studio
    #: install without editing the generator.
    l5x_schema_revision: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    #: Generator switches that are genuinely per-project (comment style, tag
    #: sort order, folder layout). Anything validated or queried gets a column.
    options: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    #: The profile used when a run does not name one.
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)


class ValidationRun(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One execution of the deterministic validators over a project revision.

    Pinned to ``revision_number`` rather than to "the project as it is now": a
    run is a statement about a specific state of the model. A summary that
    silently referred to a later edit would be worse than no summary, because it
    would be believed.

    The counts are stored rather than derived from :class:`ValidationResult` so
    that a run whose results were later waived still reports what it originally
    found. Waiving a result is a human act on the result row; it does not rewrite
    history on the run.
    """

    __tablename__ = "validation_runs"

    revision_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    status: Mapped[ValidationRunStatus] = mapped_column(
        default=ValidationRunStatus.RUNNING, nullable=False, index=True
    )

    #: The task that ran the validators, when a task did. A run started by hand
    #: from the UI has neither this nor ``triggered_by_id`` set to a task.
    triggered_by_task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    triggered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: :class:`ValidationDiscipline` values this run was asked to cover. Recorded
    #: because a clean result across three disciplines must never be read as a
    #: clean result across all of them.
    disciplines: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    information_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: How many checks actually executed. A run that reported nothing because it
    #: ran nothing looks identical to a clean run without this.
    checks_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Plain-language summary shown above the result list.
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    results: Mapped[list[ValidationResult]] = relationship(
        back_populates="validation_run", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def is_blocking(self) -> bool:
        """Whether this run stops a release candidate from being produced."""
        # Only ERROR blocks a release. A warning is information for a human to
        # weigh, and a gate that treats every warning as a blocker trains people
        # to bypass the gate — which costs far more than the warnings were ever
        # worth. UNSUPPORTED and HUMAN_REVIEW_REQUIRED are surfaced in the
        # package limitations instead; they are honest statements of scope, not
        # defects to be cleared before shipping.
        return self.error_count > 0


class ValidationResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One thing a validator found.

    Not an engineering object: a result is an observation about the model at a
    moment, not part of it, so it has no revision, no approval state and no
    supersede chain. It is deleted with its run.
    """

    __tablename__ = "validation_results"
    __table_args__ = (
        Index(
            "ix_validation_results_validation_run_id_severity",
            "validation_run_id",
            "severity",
        ),
        Index("ix_validation_results_project_id_code", "project_id", "code"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Stable check identifier, e.g. ``logic.duplicate_destructive_output``.
    #: These are published API — clients branch on them and customers write
    #: waiver policies against them — so a code is added or retired, never
    #: quietly repurposed to mean something else.
    code: Mapped[str] = mapped_column(String(60), nullable=False)

    discipline: Mapped[ValidationDiscipline] = mapped_column(nullable=False, index=True)
    severity: Mapped[ValidationSeverity] = mapped_column(nullable=False)

    #: Written to be shown to an engineer verbatim. Not a template key, not a
    #: log line: whatever is stored here is what a human reads and acts on.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: Machine-readable specifics — the two conflicting addresses, the operand
    #: that would not resolve, the limit that was exceeded.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)

    #: What the result is about, polymorphic for the same reason evidence links
    #: are: a result can point at any of forty-odd object kinds. Both are null
    #: for a project-wide finding such as "no test cases exist".
    object_type: Mapped[ObjectType | None] = mapped_column(nullable=True)
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)

    #: Where to look, in the words the engineer uses, e.g.
    #: ``MainProgram / Conveyor_Interlocks / Rung 12``. Denormalised on purpose:
    #: a validation result nobody can locate is not actionable, and the label has
    #: to keep working after the object is renamed, moved or deleted.
    object_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    #: The concrete next step, not a restatement of the problem.
    suggested_action: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Set when no amount of further checking can settle this — the platform has
    #: gone as far as evidence allows and a qualified person has to choose.
    requires_human_decision: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: A waived result stays in the row set and stays visible. It is never
    #: deleted, because "an engineer decided to accept this" is exactly the sort
    #: of thing a later reader needs to see.
    is_waived: Mapped[bool] = mapped_column(default=False, nullable=False)
    waived_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    waiver_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    validation_run: Mapped[ValidationRun] = relationship(back_populates="results")


class ReleaseCandidate(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One assembled package, at the level it has actually been proved to.

    A candidate is the unit that gets approved, hashed and handed over. It is
    immutable once released: to change anything, generate the next candidate and
    point this one's ``superseded_by_candidate_id`` at it.

    ``approved_by_id`` and ``approved_at`` come from
    :class:`~app.models.core.EngineeringObjectMixin` and are not redeclared here;
    the release approval is the object approval, recorded alongside
    ``approval_state``. ``status`` tracks the pipeline (generating, generated,
    released); ``approval_state`` tracks whether a human has signed it.
    """

    __tablename__ = "release_candidates"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "candidate_number",
            name="uq_release_candidates_project_id_candidate_number",
        ),
    )

    revision_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    #: Monotonic per project. Human-facing: "RC 4" is how people will refer to
    #: this in email, so it must never be reused or renumbered.
    candidate_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    status: Mapped[ReleaseCandidateStatus] = mapped_column(
        default=ReleaseCandidateStatus.DRAFT, nullable=False, index=True
    )

    export_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("export_profiles.id", ondelete="SET NULL"), nullable=True
    )
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("validation_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The highest level this package has actually attained. Never derived from
    #: any other field, and never raised by a check at a lower level passing.
    validation_level: Mapped[ValidationLevel] = mapped_column(
        default=ValidationLevel.GENERATED, nullable=False, index=True
    )

    #: NOT_ATTEMPTED is the honest default and the normal end state: without a
    #: configured licensed workstation nothing here can be attempted at all,
    #: and UNAVAILABLE says so rather than leaving a blank that reads as a pass.
    import_verification_status: Mapped[ImportVerificationStatus] = mapped_column(
        default=ImportVerificationStatus.NOT_ATTEMPTED, nullable=False
    )
    import_verification_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Which build of our generator wrote these files. The first question asked
    #: when a package behaves differently from one generated last month.
    generator_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: The target platform exactly as it stood at generation time — controller
    #: family, firmware, Studio version, schema revision, restricted-domain
    #: settings. Snapshotted rather than referenced: this is what stops a package
    #: being reinterpreted against a profile that somebody changed afterwards,
    #: which is the failure mode that makes a checksum meaningless.
    target_profile_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONDict, default=dict, nullable=False
    )
    #: ``{template_id: version}`` for every template that expanded into this
    #: package, for the same reason.
    template_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONDict, default=dict, nullable=False
    )

    package_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    package_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: What this package does not do, and what the model contained that we could
    #: not represent. Both ship inside the package: a limitation the customer
    #: only discovers at the import dialog is a limitation we hid.
    limitations: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    unsupported_items: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: The candidate that replaced this one. Distinct from the mixin's generic
    #: ``superseded_by_id`` because this one is a real foreign key: the release
    #: chain has to be walkable and must not point at a row that never existed.
    superseded_by_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("release_candidates.id", ondelete="SET NULL"), nullable=True
    )

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    artifacts: Mapped[list[ExportArtifact]] = relationship(
        back_populates="release_candidate",
        cascade="all, delete-orphan",
        order_by="ExportArtifact.import_order",
        lazy="selectin",
    )
    import_dependencies: Mapped[list[ImportDependency]] = relationship(
        cascade="all, delete-orphan", lazy="select"
    )
    manifest: Mapped[ReleaseManifest | None] = relationship(
        back_populates="release_candidate", cascade="all, delete-orphan", lazy="selectin"
    )
    verification_jobs: Mapped[list[WorkstationVerificationJob]] = relationship(
        back_populates="release_candidate", cascade="all, delete-orphan", lazy="select"
    )

    @property
    def is_import_ready_candidate_only(self) -> bool:
        """True until something has actually imported this package on a workstation.

        While this is True the package is an *import-ready candidate* and must be
        labelled that way everywhere it appears — the UI, the manifest, the
        covering email. Calling files "verified" because our own validators liked
        them is precisely the overclaim this product exists to avoid, and the
        distinction is only defensible if it is stated before anyone asks.
        """
        return (
            VALIDATION_LEVEL_ORDER[self.validation_level]
            < VALIDATION_LEVEL_ORDER[ValidationLevel.ROCKWELL_IMPORT_VERIFIED]
        )


class ExportArtifact(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """One generated file inside a release candidate.

    Each file carries its own ``validation_level``, because they do not all reach
    the same one: a tag CSV can be schema-checked and round-tripped while an HMI
    export produced by a partner plugin has only been generated. Rolling them up
    to a single level on the candidate would quietly promote the weakest file to
    the strength of the strongest.
    """

    __tablename__ = "export_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "release_candidate_id",
            "relative_path",
            name="uq_export_artifacts_release_candidate_id_relative_path",
        ),
    )

    release_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("release_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[ExportArtifactKind] = mapped_column(nullable=False, index=True)

    #: Path within the package, e.g. ``01_Controller/Programs/MainProgram.L5X``.
    #: The numeric prefixes are the import order made visible in a file browser,
    #: for the engineer who opens the zip instead of reading the manifest.
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    validation_level: Mapped[ValidationLevel] = mapped_column(
        default=ValidationLevel.GENERATED, nullable=False
    )

    generator_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    generator_version: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    #: The canonical object this file was generated from, polymorphic so any
    #: object kind can be the source. This is the link that answers "which model
    #: object produced this rung" from the file side.
    source_object_type: Mapped[ObjectType | None] = mapped_column(nullable=True)
    source_object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True, index=True)

    template_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    template_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    #: Position in the import sequence. Load-bearing: importing a program before
    #: the UDTs it references fails in Studio 5000 with an error that does not
    #: name the missing type, so getting this wrong costs the customer an hour.
    import_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Files from partner-owned delivery plugins are marked, because we do not
    #: warrant code we did not generate and the manifest has to say which is
    #: which.
    is_produced_by_plugin: Mapped[bool] = mapped_column(default=False, nullable=False)
    plugin_name: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Per-file generation warnings, shipped in the package rather than logged
    #: and forgotten.
    warnings: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)

    release_candidate: Mapped[ReleaseCandidate] = relationship(back_populates="artifacts")


class ImportDependency(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Why one artifact has to be imported before another.

    ``import_order`` on :class:`ExportArtifact` is the answer; this table is the
    reason. Storing the reason separately means the order can be recomputed when
    the package changes, and — more importantly — that a customer who imports in
    a different order can be told exactly what will break rather than being told
    to follow the numbers.

    Not an engineering object: an edge in a generated graph, rebuilt with each
    candidate and deleted with it.
    """

    __tablename__ = "import_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id",
            "depends_on_artifact_id",
            "kind",
            name="uq_import_dependencies_artifact_id_depends_on_artifact_id_kind",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("release_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("export_artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null when the prerequisite is not in this package — an AOI the customer
    #: already has, a module defined in a controller file we did not generate.
    depends_on_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("export_artifacts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: Always written, even when ``depends_on_artifact_id`` resolves. Kept as
    #: text so a dependency on something outside the package is still stated
    #: plainly instead of vanishing into a null foreign key.
    depends_on_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    kind: Mapped[ImportDependencyKind] = mapped_column(nullable=False)
    #: One sentence an engineer can act on, e.g. "MainProgram references
    #: UDT_ConveyorState, which is defined in 00_DataTypes/UDT_ConveyorState.L5X".
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Two foreign keys point at the same table, so each side has to name its
    # own; SQLAlchemy cannot guess which edge is which.
    artifact: Mapped[ExportArtifact] = relationship(
        foreign_keys="ImportDependency.artifact_id", lazy="selectin"
    )
    depends_on_artifact: Mapped[ExportArtifact | None] = relationship(
        foreign_keys="ImportDependency.depends_on_artifact_id", lazy="selectin"
    )


class ReleaseManifest(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """The immutable record of what was released.

    Written once, at freeze, and never edited. Any change at all — one more
    artifact, a corrected filename, a different approver — produces a new release
    candidate with its own manifest. That is the whole reason ``content_sha256``
    means anything: a hash over a document that can be revised in place proves
    only that somebody hashed something, once, at a time nobody recorded.

    ``content`` is the manifest as written to ``00_Manifest/manifest.json``,
    stored rather than regenerated on demand, so what the customer holds and what
    the database says are the same bytes and can be compared as such.
    """

    __tablename__ = "release_manifests"
    __table_args__ = (
        UniqueConstraint(
            "release_candidate_id",
            name="uq_release_manifests_release_candidate_id",
        ),
    )

    release_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("release_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Schema version of the manifest document itself, so a reader written for
    #: an older package knows whether it can parse this one.
    manifest_version: Mapped[str] = mapped_column(String(20), default="", nullable=False)

    content: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    artifact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Hash of ``00_Manifest/checksums.txt``, which in turn hashes every file.
    #: Two levels so a single value verifies the whole package.
    checksum_file_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    frozen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    frozen_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Who approved what, denormalised into the manifest so the package carries
    #: its own provenance and stays readable after it leaves this system.
    approval_summary: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    #: Hash of the shipped evidence index, tying every generated object in the
    #: package back to the basis it was generated from.
    evidence_index_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    release_candidate: Mapped[ReleaseCandidate] = relationship(back_populates="manifest")


class WorkstationVerificationJob(UUIDPrimaryKeyMixin, EngineeringObjectMixin, TimestampMixin, Base):
    """A queued request to the optional licensed Rockwell workstation adapter.

    The adapter is off by default and most deployments will never have one. It is
    the only route to ``ROCKWELL_IMPORT_VERIFIED``: nothing this backend does
    offline can substitute for an actual Studio 5000 import, and a job that never
    ran leaves the candidate at the level it honestly reached.

    ``approved_by_id`` and ``approved_at`` come from
    :class:`~app.models.core.EngineeringObjectMixin` and are not redeclared. They
    are load-bearing here rather than ceremonial: a verification job drives
    licensed software against generated files on a real machine, and it must not
    start without a named human having approved it. ``requested_by_id`` is
    separate on purpose — asking for a run and authorising one are different
    acts, and collapsing them would let a request approve itself.
    """

    __tablename__ = "workstation_verification_jobs"

    release_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("release_candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which configured adapter this job is for. A site may have more than one
    #: workstation, at different Studio versions.
    adapter_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)

    status: Mapped[ImportVerificationStatus] = mapped_column(
        default=ImportVerificationStatus.QUEUED, nullable=False, index=True
    )

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Where the adapter unpacked the package on the workstation. Recorded so a
    #: failed run can be reproduced by hand on the same machine.
    working_directory: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Structured messages the import reported, one entry per diagnostic.
    diagnostics: Mapped[list[Any]] = mapped_column(JSONList, default=list, nullable=False)
    #: A bounded slice of the adapter log. Deliberately an excerpt: the full log
    #: lives on the workstation, and a database row is not a log store.
    log_excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Fifteen minutes. An import that has not finished by then is stuck behind a
    #: dialog nobody can see, and a job without a timeout holds the site's single
    #: licensed workstation indefinitely.
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)

    release_candidate: Mapped[ReleaseCandidate] = relationship(back_populates="verification_jobs")
