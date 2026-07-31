"""Deterministic emission of Rockwell import candidates.

This package reads the canonical engineering model and writes files. It never
calls a model, never reads a clock and never invents an identifier, because the
property everything downstream rests on is that **the same approved revision
always produces byte-identical artifacts** — that is what makes a checksum in a
release manifest worth recording and what lets a regenerated package be diffed
against an approved one.

What comes out is an **import-ready candidate**. The highest level anything here
can reach is :attr:`~app.core.enums.ValidationLevel.ROUND_TRIP_PARSED`: our own
parser reads the generated L5X back and finds the rungs we wrote. Proving that
Studio 5000 will import a file needs the licensed-workstation adapter, which is
out of scope for this package and off by default, so nothing here sets
``ROCKWELL_IMPORT_VERIFIED`` and nothing here is labelled verified.

The modules, in dependency order:

``xml_writer``
    Escaping, CDATA and deterministic serialisation. One place knows how.
``l5x``
    Controller, program, routine, UDT and Add-On Instruction component exports,
    rendered from the logic IR — and the refusals that stop a file being written
    when the model would produce one that fails at import.
``csv_exports``
    Tag, comment and cross-reference CSV.
``import_order``
    The dependency graph, the order it implies, and ``IMPORT_ORDER.md``.
``package``
    The directory layout, the manifest, the checksums and the release rows.
"""

from app.generators.rockwell.csv_exports import (
    emit_comment_csv,
    emit_cross_reference_csv,
    emit_tag_csv,
)
from app.generators.rockwell.import_order import (
    ImportConstraint,
    ImportItem,
    ImportOrderCycleError,
    render_import_order_markdown,
    resolve_import_order,
)
from app.generators.rockwell.l5x import (
    ControllerContext,
    GenerationRefused,
    LogicScope,
    RefusalReason,
    RockwellGenerationError,
    emit_aoi_reference,
    emit_controller_project,
    emit_program,
    emit_routine,
    emit_udt,
    render_instruction_text,
    render_rung_text,
)
from app.generators.rockwell.package import (
    GENERATOR_NAME,
    GENERATOR_VERSION,
    IMPORT_READY_CANDIDATE,
    MANIFEST_VERSION,
    ApprovalRecord,
    ChangeLogEntry,
    DeliverySlot,
    EvidenceIndexEntry,
    GeneratedFile,
    Limitation,
    PackageOptions,
    ProgramContent,
    ProjectModel,
    ReleasePackage,
    RoutineContent,
    TargetProfile,
    TaskContent,
    UdtContent,
    UnsupportedItem,
    ValidationSummary,
    build_release_package,
)

__all__ = [
    "GENERATOR_NAME",
    "GENERATOR_VERSION",
    "IMPORT_READY_CANDIDATE",
    "MANIFEST_VERSION",
    "ApprovalRecord",
    "ChangeLogEntry",
    "ControllerContext",
    "DeliverySlot",
    "EvidenceIndexEntry",
    "GeneratedFile",
    "GenerationRefused",
    "ImportConstraint",
    "ImportItem",
    "ImportOrderCycleError",
    "Limitation",
    "LogicScope",
    "PackageOptions",
    "ProgramContent",
    "ProjectModel",
    "RefusalReason",
    "ReleasePackage",
    "RockwellGenerationError",
    "RoutineContent",
    "TargetProfile",
    "TaskContent",
    "UdtContent",
    "UnsupportedItem",
    "ValidationSummary",
    "build_release_package",
    "emit_aoi_reference",
    "emit_comment_csv",
    "emit_controller_project",
    "emit_cross_reference_csv",
    "emit_program",
    "emit_routine",
    "emit_tag_csv",
    "emit_udt",
    "render_import_order_markdown",
    "render_instruction_text",
    "render_rung_text",
    "resolve_import_order",
]
