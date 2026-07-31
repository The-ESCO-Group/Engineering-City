"""The assembled package: determinism, layout, manifest, checksums, honesty.

The determinism test is the one the rest of the release pipeline rests on. If
two generations of one approved revision differ by a single byte, the ``sha256``
in the manifest records nothing more than "somebody hashed something once", and
a regenerated package cannot be diffed against an approved one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.enums import (
    VALIDATION_LEVEL_ORDER,
    ExportArtifactKind,
    ImportVerificationStatus,
    ValidationLevel,
)
from app.generators.rockwell.package import (
    AUDIT_DIR,
    CONTROLLER_DIR,
    GENERATOR_NAME,
    GENERATOR_VERSION,
    IMPORT_READY_CANDIDATE,
    MANIFEST_DIR,
    ApprovalRecord,
    ChangeLogEntry,
    DeliverySlot,
    EvidenceIndexEntry,
    PackageOptions,
    ProjectModel,
    ReleasePackage,
    UnsupportedItem,
    ValidationSummary,
    build_release_package,
)
from app.models.release import ReleaseCandidate
from tests.generators.rockwell.conftest import build_demo_model, build_options

MANIFEST_PATH = f"{MANIFEST_DIR}/manifest.json"
CHECKSUMS_PATH = f"{MANIFEST_DIR}/checksums.sha256"
LIMITATIONS_PATH = f"{MANIFEST_DIR}/LIMITATIONS.md"


@pytest.fixture
def package(demo_model: ProjectModel, demo_options: PackageOptions) -> ReleasePackage:
    return build_release_package(demo_model, demo_options)


def paths(built: ReleasePackage) -> set[str]:
    return {file.relative_path for file in built.files}


class TestDeterminism:
    def test_two_generations_of_one_revision_are_byte_identical(self) -> None:
        first = build_release_package(build_demo_model(), build_options())
        second = build_release_package(build_demo_model(), build_options())
        assert [file.relative_path for file in first.files] == [
            file.relative_path for file in second.files
        ]
        for left, right in zip(first.files, second.files, strict=True):
            assert left.data == right.data, left.relative_path

    def test_the_manifest_itself_is_byte_identical(self) -> None:
        first = build_release_package(build_demo_model(), build_options())
        second = build_release_package(build_demo_model(), build_options())
        assert first.file_at(MANIFEST_PATH).data == second.file_at(MANIFEST_PATH).data
        assert first.file_at(CHECKSUMS_PATH).data == second.file_at(CHECKSUMS_PATH).data

    def test_determinism_survives_round_trip_verification(self, settings: Settings) -> None:
        first = build_release_package(build_demo_model(), build_options(settings=settings))
        second = build_release_package(build_demo_model(), build_options(settings=settings))
        for left, right in zip(first.files, second.files, strict=True):
            assert left.data == right.data, left.relative_path

    def test_no_file_carries_a_wall_clock_timestamp(self, package: ReleasePackage) -> None:
        for file in package.files:
            if file.relative_path.endswith(".L5X"):
                assert b"ExportDate" not in file.data, file.relative_path
            if file.relative_path.endswith(".csv"):
                assert not file.data.startswith(b"remark"), file.relative_path
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        assert manifest["determinism"]["wall_clock_timestamps"] == "omitted"
        assert "generated_at" not in manifest


class TestLayout:
    def test_the_brief_s_directory_structure_is_produced(self, package: ReleasePackage) -> None:
        assert package.root_name == "ESCO_Project_Release_7"
        present = paths(package)
        for expected in (
            MANIFEST_PATH,
            CHECKSUMS_PATH,
            LIMITATIONS_PATH,
            f"{MANIFEST_DIR}/target_profile.json",
            f"{MANIFEST_DIR}/validation_summary.json",
            f"{MANIFEST_DIR}/IMPORT_ORDER.md",
            f"{CONTROLLER_DIR}/Controller_Project.L5X",
            f"{CONTROLLER_DIR}/Controller_Tags.csv",
            f"{CONTROLLER_DIR}/UDTs/ConveyorStatus.L5X",
            f"{CONTROLLER_DIR}/AOIs/MotorStarter.L5X",
            f"{CONTROLLER_DIR}/Programs/MainProgram.L5X",
            f"{CONTROLLER_DIR}/Routines/MainProgram-Conveyor_Control.L5X",
            f"{CONTROLLER_DIR}/Routines/MainProgram-MainRoutine.L5X",
            f"{AUDIT_DIR}/change_log.json",
            f"{AUDIT_DIR}/approval_log.json",
            f"{AUDIT_DIR}/evidence_index.json",
        ):
            assert expected in present, expected

    def test_the_partner_directories_exist_even_when_empty(
        self, package: ReleasePackage, tmp_path: Path
    ) -> None:
        assert package.empty_directories == ("02_HMI", "03_Alarms", "04_Testing", "05_Reports")
        root = package.write(tmp_path)
        for directory in package.empty_directories:
            assert (root / directory).is_dir()

    def test_writing_reproduces_every_file_on_disk(
        self, package: ReleasePackage, tmp_path: Path
    ) -> None:
        root = package.write(tmp_path)
        for file in package.files:
            assert (root / file.relative_path).read_bytes() == file.data

    def test_a_plugin_output_is_marked_as_the_partner_s(self, demo_model: ProjectModel) -> None:
        built = build_release_package(
            demo_model,
            build_options(
                delivery_slots=(
                    DeliverySlot(
                        directory="02_HMI",
                        discipline="HMI",
                        plugin_name="esco.hmi.factorytalk",
                        is_present=True,
                        files=(("Displays.xml", b"<Displays/>"),),
                    ),
                    DeliverySlot(
                        directory="03_Alarms",
                        discipline="Alarms",
                        absence_reason="The alarm plugin was not installed on this host.",
                    ),
                )
            ),
        )
        hmi = built.file_at("02_HMI/Displays.xml")
        assert hmi.is_produced_by_plugin is True
        assert hmi.plugin_name == "esco.hmi.factorytalk"
        limitations = built.file_at(LIMITATIONS_PATH).data.decode("utf-8")
        assert "The alarm plugin was not installed on this host." in limitations
        assert "02_HMI" not in [item.code for item in built.limitations]


class TestVerificationLevels:
    def test_without_a_parser_nothing_claims_more_than_generated(
        self, package: ReleasePackage
    ) -> None:
        assert {file.validation_level for file in package.files} == {ValidationLevel.GENERATED}

    def test_with_the_parser_the_l5x_files_reach_round_trip_parsed(
        self, demo_model: ProjectModel, settings: Settings
    ) -> None:
        built = build_release_package(demo_model, build_options(settings=settings))
        l5x = [file for file in built.files if file.relative_path.endswith(".L5X")]
        assert l5x
        for file in l5x:
            assert file.validation_level is ValidationLevel.ROUND_TRIP_PARSED, file.relative_path
            assert file.warnings == ()

    def test_no_artifact_ever_claims_more_than_round_trip_parsed(
        self, demo_model: ProjectModel, settings: Settings
    ) -> None:
        built = build_release_package(demo_model, build_options(settings=settings))
        ceiling = VALIDATION_LEVEL_ORDER[ValidationLevel.ROUND_TRIP_PARSED]
        for file in built.files:
            assert VALIDATION_LEVEL_ORDER[file.validation_level] <= ceiling
        manifest = json.loads(built.file_at(MANIFEST_PATH).data)
        for artifact in manifest["artifacts"]:
            level = ValidationLevel(artifact["validation_level"])
            assert VALIDATION_LEVEL_ORDER[level] <= ceiling
        assert manifest["verification"]["rockwell_import_verified"] is False
        assert manifest["verification"]["highest_level_attained"] == "round_trip_parsed"
        # The package as a whole is only as strong as its weakest file.
        assert manifest["verification"]["package_level"] == "generated"

    def test_the_package_level_is_the_weakest_file_not_the_strongest(
        self, demo_model: ProjectModel, settings: Settings
    ) -> None:
        built = build_release_package(demo_model, build_options(settings=settings))
        assert built.validation_level is ValidationLevel.GENERATED


class TestManifest:
    def test_every_recorded_sha256_matches_the_bytes_on_disk(
        self, package: ReleasePackage, tmp_path: Path
    ) -> None:
        root = package.write(tmp_path)
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        assert manifest["artifacts"]
        for artifact in manifest["artifacts"]:
            on_disk = (root / artifact["relative_path"]).read_bytes()
            assert hashlib.sha256(on_disk).hexdigest() == artifact["sha256"]
            assert len(on_disk) == artifact["size_bytes"]

    def test_the_checksum_file_covers_every_file_except_itself(
        self, package: ReleasePackage, tmp_path: Path
    ) -> None:
        root = package.write(tmp_path)
        lines = package.file_at(CHECKSUMS_PATH).data.decode("utf-8").splitlines()
        listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
        assert listed.keys() == paths(package) - {CHECKSUMS_PATH}
        for relative, digest in listed.items():
            assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest

    def test_the_manifest_records_the_generator_revision_and_target(
        self, package: ReleasePackage
    ) -> None:
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        assert manifest["generator"] == {"name": GENERATOR_NAME, "version": GENERATOR_VERSION}
        assert manifest["project"]["revision_number"] == 7
        assert manifest["target"]["target_studio_version"] == "32.03"
        assert manifest["target"]["controller_family"] == "controllogix"
        assert manifest["target"]["restricted_domain_modes"] == {"motion": "manual_review_only"}

    def test_every_template_that_expanded_into_the_package_is_recorded(
        self, package: ReleasePackage
    ) -> None:
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        assert {"id": "esco.motor.standard", "version": "v1"} in manifest["templates"]

    def test_the_import_order_is_in_the_manifest_with_its_reasons(
        self, package: ReleasePackage
    ) -> None:
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        sequence = [entry["path"] for entry in manifest["import_order"]]
        assert sequence[0] == f"{CONTROLLER_DIR}/Controller_Project.L5X"
        assert sequence.index(f"{CONTROLLER_DIR}/UDTs/ConveyorStatus.L5X") < sequence.index(
            f"{CONTROLLER_DIR}/Controller_Tags.csv"
        )
        assert sequence.index(f"{CONTROLLER_DIR}/AOIs/MotorStarter.L5X") < sequence.index(
            f"{CONTROLLER_DIR}/Routines/MainProgram-Conveyor_Control.L5X"
        )
        assert sequence.index(f"{CONTROLLER_DIR}/Programs/MainProgram.L5X") < sequence.index(
            f"{CONTROLLER_DIR}/Routines/MainProgram-Conveyor_Control.L5X"
        )
        routine = next(
            entry
            for entry in manifest["import_order"]
            if entry["path"].endswith("MainProgram-Conveyor_Control.L5X")
        )
        assert any(
            "Conveyor_Control" in dependency["reason"] for dependency in routine["depends_on"]
        )

    def test_the_cross_reference_report_is_not_in_the_import_order(
        self, package: ReleasePackage
    ) -> None:
        manifest = json.loads(package.file_at(MANIFEST_PATH).data)
        sequence = [entry["path"] for entry in manifest["import_order"]]
        assert f"{CONTROLLER_DIR}/Cross_Reference.csv" not in sequence
        assert package.file_at(f"{CONTROLLER_DIR}/Cross_Reference.csv").import_order == 0

    def test_approvals_limitations_and_unsupported_items_are_recorded(
        self, demo_model: ProjectModel
    ) -> None:
        built = build_release_package(
            demo_model,
            build_options(
                approvals=(
                    ApprovalRecord(
                        object_type="logic_rung",
                        object_label="MainProgram / Conveyor_Control / Rung 0",
                        decision="approved",
                        approver="A. Engineer",
                        approved_at="2026-07-30T09:00:00Z",
                        note="Seal-in reviewed against SPEC-14.",
                    ),
                ),
                unsupported_items=(
                    UnsupportedItem(
                        kind="routine_body",
                        subject="Diagnostics",
                        detail="Structured text is read but not regenerated.",
                        location="MainProgram / Diagnostics",
                    ),
                ),
                change_log=(
                    ChangeLogEntry(
                        revision_number=7,
                        operation="update",
                        object_type="logic_rung",
                        object_label="Rung 0",
                        field_name="comment",
                        before="",
                        after="Seal-in with the master permissive.",
                        author="A. Engineer",
                    ),
                ),
                evidence_index=(
                    EvidenceIndexEntry(
                        object_type="logic_rung",
                        object_label="Rung 0",
                        basis_kind="requirement",
                        basis_label="REQ-014",
                        source_label="Functional Spec.docx",
                        anchor_key="req:REQ-014",
                    ),
                ),
                validation=ValidationSummary(
                    revision_number=7,
                    status="completed",
                    checks_run=42,
                    warning_count=2,
                    disciplines=("plc_logic", "tags"),
                    summary="Two naming warnings, nothing blocking.",
                ),
            ),
        )
        manifest = json.loads(built.file_at(MANIFEST_PATH).data)
        assert manifest["approvals"][0]["approver"] == "A. Engineer"
        assert manifest["approvals_summary"] == {"count": 1, "decisions": {"approved": 1}}
        assert manifest["unsupported_items"][0]["subject"] == "Diagnostics"
        assert manifest["validation"]["checks_run"] == 42
        assert {item["code"] for item in manifest["limitations"]} >= {
            "import_ready_candidate",
            "no_timestamps",
            "aoi_interface_only",
        }
        audit = json.loads(built.file_at(f"{AUDIT_DIR}/change_log.json").data)
        assert audit["entries"][0]["after"] == "Seal-in with the master permissive."
        evidence = json.loads(built.file_at(f"{AUDIT_DIR}/evidence_index.json").data)
        assert evidence["entries"][0]["basis_label"] == "REQ-014"


class TestLimitationsDocument:
    def test_it_says_plainly_that_these_are_import_ready_candidates(
        self, package: ReleasePackage
    ) -> None:
        text = package.file_at(LIMITATIONS_PATH).data.decode("utf-8")
        assert IMPORT_READY_CANDIDATE in text
        assert "No Studio 5000 import" in text
        assert "should be described as verified" in text

    def test_an_absent_delivery_plugin_is_explained_rather_than_left_as_an_empty_folder(
        self, package: ReleasePackage
    ) -> None:
        text = package.file_at(LIMITATIONS_PATH).data.decode("utf-8")
        for directory in ("02_HMI", "03_Alarms", "04_Testing", "05_Reports"):
            assert f"{directory} is empty" in text
        assert "not because nothing was needed" in text

    def test_an_add_on_instruction_without_a_definition_is_called_out(
        self, demo_model: ProjectModel
    ) -> None:
        from tests.generators.rockwell.conftest import make_aoi

        model = ProjectModel(
            project_name=demo_model.project_name,
            revision_number=demo_model.revision_number,
            controller=demo_model.controller,
            tasks=demo_model.tasks,
            programs=demo_model.programs,
            controller_tags=demo_model.controller_tags,
            udts=demo_model.udts,
            aoi_references=(make_aoi(is_available=False),),
            modules=demo_model.modules,
            logic_references=demo_model.logic_references,
        )
        built = build_release_package(model, build_options())
        text = built.file_at(LIMITATIONS_PATH).data.decode("utf-8")
        assert "aoi_definition_not_supplied" in text
        aoi_file = built.file_at(f"{CONTROLLER_DIR}/AOIs/MotorStarter.L5X")
        assert any("not supplied" in warning for warning in aoi_file.warnings)
        # That warning is about the definition, not about the round trip; it
        # must not be reported as a file our own parser could not read back.
        assert "round_trip_not_attained" not in text


class TestReleaseRows:
    def test_artifact_rows_carry_the_hashes_and_levels(self, package: ReleasePackage) -> None:
        project_id = uuid.UUID(int=1)
        candidate_id = uuid.UUID(int=2)
        rows = package.artifact_rows(project_id=project_id, release_candidate_id=candidate_id)
        assert len(rows) == len(package.files)
        by_path = {row.relative_path: row for row in rows}
        tags = by_path[f"{CONTROLLER_DIR}/Controller_Tags.csv"]
        assert tags.kind is ExportArtifactKind.TAG_CSV
        assert tags.sha256 == package.file_at(tags.relative_path).sha256
        assert tags.generator_name == GENERATOR_NAME
        assert tags.media_type == "text/csv"

    def test_dependency_rows_resolve_paths_to_stored_ids(self, package: ReleasePackage) -> None:
        ids = {
            file.relative_path: uuid.UUID(int=index + 10)
            for index, file in enumerate(package.files)
        }
        rows = package.dependency_rows(
            ids, project_id=uuid.UUID(int=1), release_candidate_id=uuid.UUID(int=2)
        )
        assert rows
        assert all(row.artifact_id is not None for row in rows)
        assert all(row.reason for row in rows)
        external = [row for row in rows if row.depends_on_artifact_id is None]
        assert external, "the Add-On Instruction the customer supplies is a dependency too"
        assert all(row.depends_on_label for row in external)

    def test_the_manifest_row_records_both_hash_levels(self, package: ReleasePackage) -> None:
        row = package.manifest_row(
            project_id=uuid.UUID(int=1), release_candidate_id=uuid.UUID(int=2)
        )
        manifest_bytes = package.file_at(MANIFEST_PATH).data
        assert row.content_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
        assert row.checksum_file_sha256 == package.file_at(CHECKSUMS_PATH).sha256
        assert row.artifact_count == len(package.files)
        assert row.approval_summary == {"count": 0, "decisions": {}}

    def test_the_candidate_is_never_marked_import_verified(self, package: ReleasePackage) -> None:
        candidate = ReleaseCandidate()
        package.apply_to_candidate(candidate)
        assert candidate.validation_level is ValidationLevel.GENERATED
        assert candidate.import_verification_status is ImportVerificationStatus.NOT_ATTEMPTED
        assert "no Studio 5000 import has been attempted" in candidate.import_verification_detail
        assert candidate.generator_version == GENERATOR_VERSION
        assert candidate.template_versions["esco.motor.standard"] == "v1"
        assert candidate.package_path == "ESCO_Project_Release_7"
