"""Fail-closed boundary for the not-yet-authoritative instruction branch id."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.base.actions import ChangeOperationProposal, ProposeChangesetPayload
from app.core.enums import (
    ChangeOperationType,
    InstructionCategory,
    MergeOutcome,
    ObjectType,
    ProgramType,
)
from app.core.services.canonical_import import import_latest_parsed_artifacts
from app.engineering import changesets, merge
from app.models.plc import LogicBranch, LogicInstruction, LogicRung, Program, Routine
from app.models.project import Project
from app.models.revision import MergeResult


def _rung(db: Session, project: Project, label: str) -> LogicRung:
    program = Program(
        project_id=project.id,
        name=f"{label}Program",
        program_type=ProgramType.NORMAL,
        ordinal=0,
    )
    db.add(program)
    db.flush()
    routine = Routine(
        project_id=project.id,
        program_id=program.id,
        name=f"{label}Routine",
        ordinal=0,
    )
    db.add(routine)
    db.flush()
    rung = LogicRung(project_id=project.id, routine_id=routine.id, ordinal=0)
    db.add(rung)
    db.flush()
    return rung


def _branch(db: Session, project: Project, rung: LogicRung) -> LogicBranch:
    branch = LogicBranch(
        project_id=project.id,
        rung_id=rung.id,
        level=1,
        leg_index=0,
        ordinal=0,
    )
    db.add(branch)
    db.flush()
    return branch


def _merge(
    db: Session,
    project: Project,
    operation: ChangeOperationProposal,
) -> MergeResult:
    change_set = changesets.build_changeset(
        db,
        project=project,
        payload=ProposeChangesetPayload(
            title="Attempt to attach instruction branch",
            summary="Exercise the branch identity trust boundary.",
            base_revision_number=project.current_revision_number,
            operations=[operation],
        ),
        author_agent="test_branch_boundary",
    )
    changesets.submit(db, change_set)
    return merge.merge_changeset(db, changeset=change_set)


def test_create_logic_cannot_attach_a_branch_from_another_project(
    db: Session, project: Project
) -> None:
    """A real foreign branch id is still refused before an instruction is inserted."""
    target_rung = _rung(db, project, "Target")
    other_project = Project(name="Unrelated controls project")
    db.add(other_project)
    db.flush()
    foreign_branch = _branch(db, other_project, _rung(db, other_project, "Foreign"))

    result = _merge(
        db,
        project,
        ChangeOperationProposal(
            operation_type=ChangeOperationType.CREATE_LOGIC,
            object_type=ObjectType.LOGIC_INSTRUCTION,
            temp_key="new-xic",
            values={
                "rung_id": str(target_rung.id),
                "ordinal": 0,
                "mnemonic": "XIC",
                "category": InstructionCategory.BIT.value,
                "branch_level": 1,
                "branch_index": 0,
                "branch_id": str(foreign_branch.id),
            },
        ),
    )
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert result.validation_error_count == 1
    assert "branch_id" in result.explanation
    assert "not writable through a change set" in result.explanation
    assert db.scalar(select(func.count()).select_from(LogicInstruction)) == 0


def test_update_field_cannot_attach_a_branch_from_another_rung(
    db: Session, project: Project
) -> None:
    """The same-project case is held closed too; a foreign key alone is insufficient."""
    target_rung = _rung(db, project, "Target")
    other_branch = _branch(db, project, _rung(db, project, "Other"))
    instruction = LogicInstruction(
        project_id=project.id,
        rung_id=target_rung.id,
        ordinal=0,
        mnemonic="XIC",
        category=InstructionCategory.BIT,
    )
    db.add(instruction)
    db.flush()

    result = _merge(
        db,
        project,
        ChangeOperationProposal(
            operation_type=ChangeOperationType.UPDATE_FIELD,
            object_type=ObjectType.LOGIC_INSTRUCTION,
            object_id=instruction.id,
            based_on_object_revision=instruction.object_revision,
            field_name="branch_id",
            values={"branch_id": str(other_branch.id)},
        ),
    )
    db.flush()

    assert result.outcome is MergeOutcome.VALIDATION_FAILED
    assert result.validation_error_count == 1
    assert "branch_id" in result.explanation
    db.refresh(instruction)
    assert instruction.branch_id is None
    assert instruction.object_revision == 1


def test_canonical_import_leaves_branch_identity_unset(
    db: Session, ingested_project: Project
) -> None:
    """Importer compatibility stays on the legacy coordinate representation."""
    import_latest_parsed_artifacts(db, ingested_project.id)
    instructions = list(
        db.scalars(
            select(LogicInstruction).where(LogicInstruction.project_id == ingested_project.id)
        )
    )

    assert instructions
    assert all(instruction.branch_id is None for instruction in instructions)
