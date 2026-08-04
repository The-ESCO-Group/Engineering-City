"""Things this platform claims are deterministic, checked by running them twice.

A checksum over output that is not reproducible is theatre. Every guarantee in
here was, at some point, true only by accident — a ``set`` that happened not to
appear in a shipped template, a database that happened to return rows in
insertion order — so each one is now pinned by a test that would have caught it.

The template checks run in **separate processes with different**
``PYTHONHASHSEED`` **values**, because that is the only way to see this class of
defect. CPython randomises string hashing per interpreter start, so a digest that
depends on the iteration order of a ``set`` is perfectly stable within one
process and different in the next one. Reasoning about it is not evidence;
comparing the bytes two processes produced is.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from app.core.enums import BaseDataType
from app.generators.plc.template_registry import (
    Template,
    load_templates,
    render_parameter_value,
)
from app.models.project import Project
from app.models.tags import UDTDefinition, UDTMember, udt_checksum

BACKEND = Path(__file__).resolve().parents[1]

#: Seeds to run the registry under. 0 disables randomisation entirely, and the
#: rest were chosen because they produced three distinct digests for the same
#: file when the checksum still fell back to ``str()``.
HASH_SEEDS = ("0", "1", "7", "555", "12345")

#: Loads a template root and prints one canonical line per template and per load
#: error. Run as ``python -c``, so it must not depend on anything in the test
#: process — the point is that a second interpreter agrees with the first.
_REPORT = """
import pathlib, sys
from app.generators.plc.template_registry import default_template_root, load_templates

root = default_template_root() if sys.argv[1] == "-" else pathlib.Path(sys.argv[1])
registry = load_templates(root)
lines = [t.qualified_id() + " " + t.checksum for t in registry.all()]
lines += [
    "! " + e.path + " " + e.message
    for e in sorted(registry.load_errors, key=lambda e: (e.path, e.message))
]
sys.stdout.write("\\n".join(lines))
"""


@pytest.fixture
def template_body() -> dict[str, object]:
    """A minimal, valid template body these tests mutate.

    Deliberately its own copy rather than the registry suite's fixture: these
    tests are about what the loader does with awkward *values*, and they must not
    start failing because somebody added a field to a shared fixture.
    """
    return {
        "id": "esco.test.pattern",
        "version": "1.0.0",
        "category": "motor_control",
        "display_name": "Test pattern",
        "description": "A minimal template used by the determinism tests.",
        "supported_families": ["compactlogix"],
        "approval_level": "standard",
        "restricted_domains": [],
        "parameters": [
            {
                "name": "device_tag",
                "type": "identifier",
                "required": True,
                "description": "Device tag stem",
            }
        ],
        "required_tags": [
            {
                "role": "command",
                "suffix": "_Cmd_Start",
                "data_type": "BOOL",
                "description": "Operator start request",
            }
        ],
        "generated_objects": {
            "routines": [
                {
                    "name": "R_Devices",
                    "language": "ladder",
                    "description": "Device control blocks",
                }
            ],
            "rungs": [
                {
                    "routine": "R_Devices",
                    "title": "{device_tag} run request",
                    "comment": "Three-wire start/stop.",
                    "elements": [
                        {"instruction": "XIC", "operand": "{device_tag}_Cmd_Start"},
                        {
                            "instruction": "OTE",
                            "operand": "{device_tag}_Cmd_Run",
                            "kind": "output",
                        },
                    ],
                }
            ],
        },
        "engineering_notes": "Test pattern; not an approved ESCO control pattern.",
        "restrictions": ["Does nothing. It exists so the loader has something to load."],
        "tests": [],
    }


def _registry_report(root: str, seed: str) -> str:
    """The registry, as rendered by a fresh interpreter started with ``seed``."""
    environment = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": str(BACKEND),
        # The load errors carry an em dash, and a Windows console defaults to a
        # code page that cannot encode it.
        "PYTHONIOENCODING": "utf-8",
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", _REPORT, root],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _report_under_every_seed(root: str) -> dict[str, str]:
    return {seed: _registry_report(root, seed) for seed in HASH_SEEDS}


# ---------------------------------------------------------------------------
# Template checksums
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_the_shipped_templates_hash_the_same_in_every_process() -> None:
    """The digests an approval records must be the ones the next worker recomputes.

    An expansion approved in one worker process records
    ``esco.motor.standard@1.0.0 (sha256 ...)``. If the next worker recomputes
    that digest differently, an unmodified file reports as drifted and the
    reviewer is sent to diff a template nobody touched.
    """
    reports = _report_under_every_seed("-")
    baseline = reports[HASH_SEEDS[0]]

    assert baseline.strip(), "the shipped template root loaded nothing"
    for seed, report in reports.items():
        assert report == baseline, f"PYTHONHASHSEED={seed} produced a different registry"


@pytest.mark.slow
def test_a_set_in_a_template_cannot_move_the_digest_between_processes(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """The specific hole: a value ``json`` cannot serialise reaching the digest.

    ``yaml.safe_load`` produces a Python ``set`` for the standard ``!!set`` tag,
    and ``str()`` of a set of strings renders in hash order. Stringifying it —
    which is what ``json.dumps(default=str)`` does — made the digest of a file
    that loads with zero errors differ on every interpreter start.

    The file is written once, here, so the only thing varying between the child
    processes is how the registry canonicalises what it read.
    """
    parameters = template_body["parameters"]
    assert isinstance(parameters, list)
    parameters.append(
        {
            "name": "keywords",
            "type": "string",
            "required": False,
            "default": {"motor", "starter", "vfd", "contactor", "overload"},
            "description": "Search keywords",
        }
    )
    (tmp_path / "pattern.yaml").write_text(
        yaml.safe_dump(template_body, sort_keys=False), encoding="utf-8"
    )
    assert "!!set" in (tmp_path / "pattern.yaml").read_text(encoding="utf-8")

    reports = _report_under_every_seed(str(tmp_path))
    baseline = reports[HASH_SEEDS[0]]

    assert baseline.startswith("! pattern.yaml"), baseline
    for seed, report in reports.items():
        assert report == baseline, f"PYTHONHASHSEED={seed} disagreed about pattern.yaml"


# ---------------------------------------------------------------------------
# Numeric parameters
# ---------------------------------------------------------------------------
@pytest.fixture
def scaling_template(tmp_path: Path, template_body: dict[str, object]) -> Template:
    """A pattern with one ``type: number`` parameter substituted into a rung."""
    parameters = template_body["parameters"]
    assert isinstance(parameters, list)
    parameters.append(
        {
            "name": "eu_max",
            "type": "number",
            "required": False,
            "default": 100.0,
            "description": "Engineering value at full scale",
        }
    )
    objects = template_body["generated_objects"]
    assert isinstance(objects, dict)
    objects["rungs"].append(  # type: ignore[index]
        {
            "routine": "R_Devices",
            "title": "{device_tag} scale",
            "comment": "Linear interpolation to engineering units.",
            "elements": [
                {
                    "instruction": "CPT",
                    "operands": [
                        "{device_tag}_PV",
                        "({device_tag}_Raw * {eu_max} / 32767)",
                    ],
                    "kind": "output",
                }
            ],
        }
    )
    (tmp_path / "pattern.yaml").write_text(
        yaml.safe_dump(template_body, sort_keys=False), encoding="utf-8"
    )
    template = load_templates(tmp_path).get("esco.test.pattern")
    assert template is not None
    return template


def test_the_same_number_in_two_spellings_renders_one_rung(scaling_template: Template) -> None:
    """``100`` and ``100.0`` are the same engineering value and must generate the same text.

    Both pass ``validate_parameters``, and a browser client cannot even express
    the second: ``JSON.stringify(100.0)`` is ``"100"``, so an expansion stored as
    ``100.0`` and re-submitted from the UI came back as an int. Rendering with
    ``str()`` carried that into the operand, which moved the L5X bytes and the
    release manifest checksum for a change nobody made.
    """
    from app.agents.review.plc_logic import render_ladder_preview

    as_int = render_ladder_preview(scaling_template, {"device_tag": "PT_101", "eu_max": 100})
    as_float = render_ladder_preview(scaling_template, {"device_tag": "PT_101", "eu_max": 100.0})

    assert as_int == as_float
    assert "(PT_101_Raw * 100.0 / 32767)" in as_int


def test_an_omitted_number_renders_from_its_default_the_same_way(
    scaling_template: Template,
) -> None:
    """Supplying the default explicitly must not change a single byte."""
    from app.agents.review.plc_logic import render_ladder_preview

    omitted = render_ladder_preview(scaling_template, {"device_tag": "PT_101"})
    supplied = render_ladder_preview(scaling_template, {"device_tag": "PT_101", "eu_max": 100})

    assert omitted == supplied


def test_a_whole_number_keeps_its_decimal_point() -> None:
    """Not the rule ``l5x._number`` uses for a scan rate, and deliberately so.

    These values land inside CPT expressions. Logix evaluates an expression whose
    operands are all integers with integer arithmetic, so rendering a calibrated
    range of ``100.0`` as ``100`` would turn ``Raw * 100 / 32767`` into a
    truncating divide — a scaling defect introduced while fixing a formatting one.
    """
    assert render_parameter_value("number", 100) == "100.0"
    assert render_parameter_value("number", 100.0) == "100.0"
    assert render_parameter_value("number", 12.5) == "12.5"
    assert render_parameter_value("integer", 32767) == "32767"


def test_canonical_parameters_normalises_only_what_it_declares(
    scaling_template: Template,
) -> None:
    canonical = scaling_template.canonical_parameters(
        {"device_tag": "PT_101", "eu_max": 100, "typo": 7}
    )

    assert canonical["eu_max"] == 100.0
    assert isinstance(canonical["eu_max"], float)
    assert canonical["device_tag"] == "PT_101"
    # Left alone so ``validate_parameters`` still reports it. Dropping it here
    # would expand the template on the old default while the caller believed
    # their value had been used.
    assert canonical["typo"] == 7


# ---------------------------------------------------------------------------
# UDT checksums
# ---------------------------------------------------------------------------
#: The ESCO standard motor interface.
_ESCO_MOTOR: tuple[tuple[str, BaseDataType], ...] = (
    ("Cmd_Start", BaseDataType.BOOL),
    ("Cmd_Stop", BaseDataType.BOOL),
    ("Sts_Running", BaseDataType.BOOL),
    ("Sts_Faulted", BaseDataType.BOOL),
    ("Run_Hours", BaseDataType.REAL),
)


def _make_udt(
    db: Session,
    project: Project,
    name: str,
    members: tuple[tuple[str, BaseDataType], ...],
    *,
    ordinal_step: int = 1,
) -> UDTDefinition:
    """Build a type the way the changeset engine does: by column values, one flush each."""
    udt = UDTDefinition(project_id=project.id, name=name, family="motor")
    db.add(udt)
    db.flush()
    for index, (member_name, data_type) in enumerate(members):
        db.add(
            UDTMember(
                project_id=project.id,
                udt_id=udt.id,
                name=member_name,
                base_data_type=data_type,
                ordinal=index * ordinal_step,
                description=f"{member_name} on {name}",
            )
        )
        db.flush()
    return udt


def test_a_udt_written_one_member_at_a_time_ends_with_a_real_checksum(
    db: Session, project: Project
) -> None:
    """The field documents an integrity guarantee, so something must provide it.

    The changeset engine builds each row from column values and flushes it on its
    own, never touching the ``members`` relationship, so nothing that relied on
    that collection would see the members at all.
    """
    udt = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)

    assert udt.checksum != ""
    assert len(udt.checksum) == 64
    assert udt.checksum == udt_checksum(udt.members)


def test_a_member_inserted_in_the_middle_changes_the_checksum(
    db: Session, project: Project
) -> None:
    """The defect the field exists to catch.

    Two controllers each carry an ``ESCO_Motor``; one has had a ``Cmd_Jog``
    inserted at ordinal 4, shifting the bit packing of every member after it. If
    both rows hold ``""`` the one comparison that would have made this obvious
    reads as two identical values.
    """
    baseline = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)
    diverged = _make_udt(
        db,
        project,
        "ESCO_Motor_LineB",
        (*_ESCO_MOTOR[:4], ("Cmd_Jog", BaseDataType.BOOL), *_ESCO_MOTOR[4:]),
    )

    assert baseline.checksum != diverged.checksum


def test_two_identical_member_lists_hash_alike(db: Session, project: Project) -> None:
    """Which is what makes a difference mean something."""
    first = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)
    second = _make_udt(db, project, "ESCO_Motor_Copy", _ESCO_MOTOR)

    # Both non-empty first. Two empty strings are also equal, and that is exactly
    # how this field passed for a comparison it was never making.
    assert first.checksum != ""
    assert first.checksum == second.checksum


def test_renumbering_ordinals_without_reordering_is_not_divergence(
    db: Session, project: Project
) -> None:
    """Order is identity; the ordinal numbers are bookkeeping."""
    dense = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)
    sparse = _make_udt(db, project, "ESCO_Motor_Sparse", _ESCO_MOTOR, ordinal_step=10)

    assert dense.checksum != ""
    assert dense.checksum == sparse.checksum


def test_rewording_a_member_description_is_not_divergence(db: Session, project: Project) -> None:
    """A re-worded comment is not a change to the shape a faceplate binds against."""
    udt = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)
    before = udt.checksum
    assert before != ""

    udt.members[0].description = "Operator start request, from the HMI faceplate."
    db.flush()

    assert udt.checksum == before


def test_removing_a_member_updates_the_checksum(db: Session, project: Project) -> None:
    """A soft-deleted member is not part of the type any more."""
    from app.core.clock import utcnow

    udt = _make_udt(db, project, "ESCO_Motor", _ESCO_MOTOR)
    before = udt.checksum

    udt.members[-1].deleted_at = utcnow()
    db.flush()

    assert udt.checksum != before
    assert udt.checksum == udt_checksum(udt.members)


def test_a_type_with_no_members_still_carries_a_computed_digest(
    db: Session, project: Project
) -> None:
    """Empty is a member list, not an absent one, and must not read as ``""``."""
    udt = _make_udt(db, project, "ESCO_Empty", ())

    assert udt.checksum == udt_checksum([])
    assert udt.checksum != ""
