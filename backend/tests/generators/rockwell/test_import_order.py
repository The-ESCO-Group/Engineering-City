"""Import order: the sequence, its determinism, and what a cycle does.

A cycle has to be an error naming the cycle. Emitting an arbitrary order instead
would produce a package whose numbers are confidently wrong, and the failure
would surface at the customer's import dialog with nothing to point at.
"""

from __future__ import annotations

import pytest

from app.core.enums import ExportArtifactKind, ImportDependencyKind
from app.generators.rockwell.import_order import (
    ImportConstraint,
    ImportItem,
    ImportOrderCycleError,
    render_import_order_markdown,
    resolve_import_order,
)
from app.generators.rockwell.l5x import RockwellGenerationError

UDT = ImportItem(
    key="01_Controller/UDTs/ConveyorStatus.L5X",
    label="User-defined type ConveyorStatus",
    kind=ExportArtifactKind.UDT_L5X,
    rank=20,
)
AOI = ImportItem(
    key="01_Controller/AOIs/MotorStarter.L5X",
    label="Add-On Instruction interface MotorStarter",
    kind=ExportArtifactKind.AOI_L5X,
    rank=30,
)
TAGS = ImportItem(
    key="01_Controller/Controller_Tags.csv",
    label="Controller-scope tags",
    kind=ExportArtifactKind.TAG_CSV,
    rank=40,
)
PROGRAM = ImportItem(
    key="01_Controller/Programs/MainProgram.L5X",
    label="Program MainProgram",
    kind=ExportArtifactKind.PROGRAM_L5X,
    rank=50,
)
ROUTINE = ImportItem(
    key="01_Controller/Routines/MainProgram-Conveyor_Control.L5X",
    label="Routine MainProgram / Conveyor_Control",
    kind=ExportArtifactKind.ROUTINE_L5X,
    rank=60,
)


def constraint(
    item: ImportItem,
    depends_on: ImportItem | None,
    kind: ImportDependencyKind,
    reason: str = "because",
    label: str = "",
) -> ImportConstraint:
    return ImportConstraint(
        item_key=item.key,
        depends_on_key=None if depends_on is None else depends_on.key,
        depends_on_label=label or (depends_on.label if depends_on else "external"),
        kind=kind,
        reason=reason,
    )


def positions(ordered: tuple[ImportItem, ...]) -> dict[str, int]:
    return {item.key: index for index, item in enumerate(ordered)}


class TestOrdering:
    def test_a_data_type_comes_before_the_tags_that_use_it(self) -> None:
        ordered = resolve_import_order(
            [TAGS, UDT],
            [constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE)],
        )
        index = positions(ordered)
        assert index[UDT.key] < index[TAGS.key]

    def test_an_aoi_comes_before_the_routine_that_calls_it(self) -> None:
        ordered = resolve_import_order(
            [ROUTINE, AOI],
            [constraint(ROUTINE, AOI, ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION)],
        )
        index = positions(ordered)
        assert index[AOI.key] < index[ROUTINE.key]

    def test_a_program_comes_before_its_routines(self) -> None:
        ordered = resolve_import_order(
            [ROUTINE, PROGRAM],
            [constraint(ROUTINE, PROGRAM, ImportDependencyKind.REQUIRES_PROGRAM)],
        )
        index = positions(ordered)
        assert index[PROGRAM.key] < index[ROUTINE.key]

    def test_a_full_chain_orders_end_to_end(self) -> None:
        ordered = resolve_import_order(
            [ROUTINE, PROGRAM, TAGS, AOI, UDT],
            [
                constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE),
                constraint(TAGS, AOI, ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION),
                constraint(PROGRAM, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE),
                constraint(ROUTINE, PROGRAM, ImportDependencyKind.REQUIRES_PROGRAM),
                constraint(ROUTINE, TAGS, ImportDependencyKind.REQUIRES_TAG),
            ],
        )
        assert [item.key for item in ordered] == [
            UDT.key,
            AOI.key,
            TAGS.key,
            PROGRAM.key,
            ROUTINE.key,
        ]

    def test_the_order_does_not_depend_on_the_order_the_edges_arrived(self) -> None:
        edges = [
            constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE),
            constraint(ROUTINE, PROGRAM, ImportDependencyKind.REQUIRES_PROGRAM),
            constraint(ROUTINE, TAGS, ImportDependencyKind.REQUIRES_TAG),
        ]
        items = [ROUTINE, PROGRAM, TAGS, AOI, UDT]
        first = resolve_import_order(items, edges)
        second = resolve_import_order(list(reversed(items)), list(reversed(edges)))
        assert [item.key for item in first] == [item.key for item in second]

    def test_a_constraint_with_no_target_orders_nothing(self) -> None:
        ordered = resolve_import_order(
            [AOI],
            [
                constraint(
                    AOI,
                    None,
                    ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION,
                    label="The compiled definition of MotorStarter",
                )
            ],
        )
        assert [item.key for item in ordered] == [AOI.key]


class TestCycles:
    def test_a_cycle_raises_naming_the_files_in_it(self) -> None:
        with pytest.raises(ImportOrderCycleError) as caught:
            resolve_import_order(
                [PROGRAM, ROUTINE],
                [
                    constraint(ROUTINE, PROGRAM, ImportDependencyKind.REQUIRES_PROGRAM),
                    constraint(PROGRAM, ROUTINE, ImportDependencyKind.REQUIRES_TAG),
                ],
            )
        message = str(caught.value)
        assert PROGRAM.key in message
        assert ROUTINE.key in message
        assert set(caught.value.cycle) == {PROGRAM.key, ROUTINE.key}

    def test_a_longer_cycle_names_every_member(self) -> None:
        with pytest.raises(ImportOrderCycleError) as caught:
            resolve_import_order(
                [UDT, TAGS, PROGRAM],
                [
                    constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE),
                    constraint(UDT, PROGRAM, ImportDependencyKind.ORDERED_AFTER),
                    constraint(PROGRAM, TAGS, ImportDependencyKind.REQUIRES_TAG),
                ],
            )
        assert set(caught.value.cycle) == {UDT.key, TAGS.key, PROGRAM.key}

    def test_a_self_edge_is_ignored_rather_than_reported_as_a_cycle(self) -> None:
        ordered = resolve_import_order(
            [UDT], [constraint(UDT, UDT, ImportDependencyKind.ORDERED_AFTER)]
        )
        assert [item.key for item in ordered] == [UDT.key]


class TestBadInput:
    def test_two_files_at_one_path_are_refused(self) -> None:
        with pytest.raises(RockwellGenerationError, match="claim the same path"):
            resolve_import_order([UDT, UDT], [])

    def test_a_constraint_naming_an_absent_file_is_refused(self) -> None:
        with pytest.raises(RockwellGenerationError, match="not in this package"):
            resolve_import_order(
                [TAGS], [constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE)]
            )


class TestMarkdown:
    def test_every_constraint_contributes_its_reason(self) -> None:
        edges = [
            constraint(
                TAGS,
                UDT,
                ImportDependencyKind.REQUIRES_DATA_TYPE,
                reason="Controller_Tags.csv declares a tag of type ConveyorStatus.",
            ),
            constraint(
                ROUTINE,
                None,
                ImportDependencyKind.REQUIRES_ADDON_INSTRUCTION,
                reason="Import the customer's own definition first.",
                label="The compiled definition of MotorStarter",
            ),
        ]
        ordered = resolve_import_order([TAGS, UDT, ROUTINE], edges)
        text = render_import_order_markdown(
            ordered, edges, release_name="ESCO_Project_Release_7"
        ).decode("utf-8")
        assert "# Import order - ESCO_Project_Release_7" in text
        assert "import-ready candidates" in text
        assert "Controller_Tags.csv declares a tag of type ConveyorStatus." in text
        assert "requires_data_type" in text
        assert "The compiled definition of MotorStarter" in text
        assert "## Prerequisites this package does not contain" in text
        assert f"1. `{UDT.key}`" in text

    def test_a_file_with_no_prerequisite_says_so(self) -> None:
        text = render_import_order_markdown(
            resolve_import_order([UDT], []), [], release_name="R1"
        ).decode("utf-8")
        assert "No prerequisite inside this package." in text
        assert "None. Every prerequisite named above is a file in this package." in text

    def test_the_rendering_is_byte_identical_across_calls(self) -> None:
        edges = [constraint(TAGS, UDT, ImportDependencyKind.REQUIRES_DATA_TYPE)]
        ordered = resolve_import_order([TAGS, UDT], edges)
        assert render_import_order_markdown(
            ordered, edges, release_name="R1"
        ) == render_import_order_markdown(ordered, edges, release_name="R1")
