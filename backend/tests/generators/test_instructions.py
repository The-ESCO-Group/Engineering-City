"""The instruction catalogue, checked against itself and against a few facts.

Two kinds of test live here and they are worth telling apart.

The consistency tests are the ones that keep the catalogue trustworthy as it
grows: a destructive operand index outside the instruction's own operand list is
silent at runtime — it simply never matches an operand, so the duplicate-coil
defect it was meant to catch stops being reported and nothing says why.

The specific assertions are the ones that would catch a plausible edit. That MOV
writes its *second* operand and CPT its *first* is exactly the sort of thing a
tidy-up "corrects" into consistency, and the whole point of storing destructive
positions per mnemonic is that they are not consistent.
"""

from __future__ import annotations

import pytest

from app.core.enums import BaseDataType, ControllerFamily, InstructionCategory
from app.generators.plc.instructions import (
    INSTRUCTIONS,
    LOGIX_FAMILIES,
    RESTRICTED_INSTRUCTIONS,
    VARIADIC_OPERAND_LIMIT,
    InstructionSpec,
    destructive_operands,
    is_known,
    is_restricted,
    restriction_reason,
    spec_for,
    supports,
)

#: The coverage the platform commits to, by category. Written out rather than
#: derived from the catalogue, so deleting an entry fails a test instead of
#: quietly shrinking what this list means.
REQUIRED_COVERAGE: dict[InstructionCategory, tuple[str, ...]] = {
    InstructionCategory.BIT: ("XIC", "XIO", "OTE", "OTL", "OTU", "ONS", "OSR", "OSF"),
    InstructionCategory.TIMER_COUNTER: ("TON", "TOF", "RTO", "CTU", "CTD", "RES"),
    InstructionCategory.COMPARE: (
        "EQU",
        "NEQ",
        "LES",
        "GRT",
        "LEQ",
        "GEQ",
        "CMP",
        "LIM",
        "MEQ",
    ),
    InstructionCategory.COMPUTE_MATH: (
        "ADD",
        "SUB",
        "MUL",
        "DIV",
        "MOD",
        "SQR",
        "NEG",
        "ABS",
        "CPT",
        "SCL",
        "SCP",
    ),
    InstructionCategory.MOVE_LOGICAL: (
        "MOV",
        "MVM",
        "COP",
        "CPS",
        "CLR",
        "BTD",
        "AND",
        "OR",
        "XOR",
        "NOT",
    ),
    InstructionCategory.PROGRAM_CONTROL: (
        "JSR",
        "SBR",
        "RET",
        "JMP",
        "LBL",
        "TND",
        "MCR",
        "AFI",
        "NOP",
        "EOT",
        "SFR",
        "SFP",
    ),
}

ALL_SPECS: list[InstructionSpec] = list(INSTRUCTIONS.values())


# ---------------------------------------------------------------------------
# Internal consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_key_matches_canonical_mnemonic(spec: InstructionSpec) -> None:
    assert INSTRUCTIONS[spec.mnemonic] is spec
    assert spec.mnemonic == spec.mnemonic.upper().strip()
    assert spec.mnemonic


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_operand_bounds_are_a_range(spec: InstructionSpec) -> None:
    assert spec.min_operands >= 0
    assert spec.min_operands <= spec.max_operands
    assert spec.max_operands <= VARIADIC_OPERAND_LIMIT


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_destructive_indexes_are_within_max_operands(spec: InstructionSpec) -> None:
    for index in spec.destructive_operand_indexes:
        assert 0 <= index < spec.max_operands, (
            f"{spec.mnemonic} claims to write operand {index} but takes at most "
            f"{spec.max_operands}; the position would never match an operand."
        )


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_destructive_indexes_are_within_min_operands(spec: InstructionSpec) -> None:
    """An instruction cannot write an operand it does not require.

    Stricter than the ``max_operands`` check on purpose: a written position is
    never optional, so an index in the optional range means the operand order
    has been misread.
    """
    for index in spec.destructive_operand_indexes:
        assert index < spec.min_operands, (
            f"{spec.mnemonic} writes operand {index}, which is beyond the "
            f"{spec.min_operands} operand(s) it requires."
        )


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_destructive_indexes_are_unique(spec: InstructionSpec) -> None:
    indexes = spec.destructive_operand_indexes
    assert len(set(indexes)) == len(indexes)


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_operand_types_fit_the_operand_list(spec: InstructionSpec) -> None:
    assert len(spec.operand_types) <= spec.max_operands
    for position in spec.operand_types:
        assert isinstance(position, tuple)
        for data_type in position:
            assert isinstance(data_type, BaseDataType)


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_every_spec_is_documented(spec: InstructionSpec) -> None:
    """A catalogue entry with no notes cannot say why a position is untyped."""
    assert spec.name.strip()
    assert spec.notes.strip()


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_supported_families_are_real_and_non_empty(spec: InstructionSpec) -> None:
    assert spec.supported_families
    for family in spec.supported_families:
        assert isinstance(family, ControllerFamily)


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_category_is_one_the_catalogue_claims(spec: InstructionSpec) -> None:
    """Nothing in the catalogue is filed as UNSUPPORTED or as an AOI.

    Those two categories describe instructions this module deliberately says
    nothing about, so an entry carrying one would contradict its own presence.
    """
    assert spec.category not in {
        InstructionCategory.UNSUPPORTED,
        InstructionCategory.ADDON_INSTRUCTION,
    }


def test_frozen_spec_cannot_be_edited_in_place() -> None:
    spec = INSTRUCTIONS["OTE"]
    with pytest.raises(AttributeError):
        spec.mnemonic = "XIC"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"mnemonic": "ote"}, "upper-case"),
        ({"min_operands": 3, "max_operands": 1}, "not a range"),
        ({"destructive_operand_indexes": (4,)}, "outside"),
        ({"destructive_operand_indexes": (0, 0)}, "repeated"),
        ({"supported_families": frozenset()}, "no family supports"),
    ],
)
def test_malformed_spec_is_rejected_at_construction(
    kwargs: dict[str, object], fragment: str
) -> None:
    """A bad entry fails loudly at import rather than validating nothing."""
    base: dict[str, object] = {
        "mnemonic": "OTE",
        "name": "Output Energize",
        "category": InstructionCategory.BIT,
        "min_operands": 1,
        "max_operands": 1,
        "destructive_operand_indexes": (0,),
    }
    with pytest.raises(ValueError, match=fragment):
        InstructionSpec(**{**base, **kwargs})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "mnemonic"),
    [(category, mnemonic) for category, group in REQUIRED_COVERAGE.items() for mnemonic in group],
)
def test_required_instruction_is_catalogued(category: InstructionCategory, mnemonic: str) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None, f"{mnemonic} is missing from the catalogue."
    assert spec.category is category


def test_catalogue_holds_nothing_beyond_the_declared_coverage() -> None:
    """The catalogue and the list this test file declares stay the same set.

    An instruction added without a line here is an instruction whose category
    nobody wrote down, and the test that follows the coverage table would keep
    passing while the catalogue said something new.
    """
    declared = {mnemonic for group in REQUIRED_COVERAGE.values() for mnemonic in group}
    assert set(INSTRUCTIONS) == declared


# ---------------------------------------------------------------------------
# Restricted instructions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mnemonic", sorted(RESTRICTED_INSTRUCTIONS))
def test_restricted_mnemonic_is_absent_or_flagged(mnemonic: str) -> None:
    spec = spec_for(mnemonic)
    assert spec is None or spec.is_restricted, (
        f"{mnemonic} is catalogued as generatable and restricted at the same time."
    )


@pytest.mark.parametrize("mnemonic", sorted(RESTRICTED_INSTRUCTIONS))
def test_restricted_mnemonic_states_a_reason(mnemonic: str) -> None:
    reason = restriction_reason(mnemonic)
    # Long enough to be a reason rather than a label: a refusal with no reason
    # is indistinguishable from a broken tool.
    assert len(reason) > 40
    assert reason == RESTRICTED_INSTRUCTIONS[mnemonic]


@pytest.mark.parametrize(
    "mnemonic",
    ["MAM", "MAJ", "MAS", "MSO", "MSF", "MAH", "MCD", "MCLM", "MAPC", "MSG"],
)
def test_motion_and_messaging_are_refused(mnemonic: str) -> None:
    assert is_restricted(mnemonic)
    assert spec_for(mnemonic) is None
    assert is_known(mnemonic), "A refused instruction is still one we recognise."
    assert not supports(mnemonic, ControllerFamily.CONTROLLOGIX)


def test_safety_instructions_are_refused() -> None:
    for mnemonic in ("ESTOP", "THRS", "DCS", "CROUT", "SMAT"):
        assert is_restricted(mnemonic)
        assert "safety" in restriction_reason(mnemonic).lower()


def test_messaging_reason_names_the_actual_hazard() -> None:
    assert "another device" in restriction_reason("MSG")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("written", ["mov", "MOV", "Mov", "  mov  "])
def test_lookup_is_case_insensitive(written: str) -> None:
    spec = spec_for(written)
    assert spec is not None
    assert spec is INSTRUCTIONS["MOV"]
    assert spec.mnemonic == "MOV", "The canonical form is upper-case."
    assert is_known(written)
    assert destructive_operands(written) == (1,)
    assert supports(written, ControllerFamily.CONTROLLOGIX)


@pytest.mark.parametrize("written", ["mam", "MAM", "Mam"])
def test_restricted_lookup_is_case_insensitive(written: str) -> None:
    assert is_restricted(written)
    assert restriction_reason(written)


def test_unknown_mnemonic_is_none_and_not_an_error() -> None:
    assert spec_for("ESCO_MOTOR_AOI") is None
    assert not is_known("ESCO_MOTOR_AOI")
    assert not is_restricted("ESCO_MOTOR_AOI")
    assert restriction_reason("ESCO_MOTOR_AOI") == ""
    assert not supports("ESCO_MOTOR_AOI", ControllerFamily.CONTROLLOGIX)


def test_unknown_mnemonic_reports_no_destructive_operands() -> None:
    """``()`` here means "nothing known", not "writes nothing".

    Asserted so the contract is written down where a caller will read it: the
    only safe way to use this answer is to have checked :func:`is_known` first.
    """
    assert destructive_operands("ESCO_MOTOR_AOI") == ()
    assert not is_known("ESCO_MOTOR_AOI")


# ---------------------------------------------------------------------------
# Specific, high-value facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mnemonic", "expected"),
    [
        ("OTE", (0,)),
        ("OTL", (0,)),
        ("OTU", (0,)),
        ("ONS", (0,)),
        ("OSR", (0, 1)),
        ("OSF", (0, 1)),
        ("XIC", ()),
        ("XIO", ()),
        ("MOV", (1,)),
        ("MVM", (2,)),
        ("COP", (1,)),
        ("CPS", (1,)),
        ("CLR", (0,)),
        ("BTD", (2,)),
        ("TON", (0,)),
        ("TOF", (0,)),
        ("RTO", (0,)),
        ("CTU", (0,)),
        ("CTD", (0,)),
        ("RES", (0,)),
        ("ADD", (2,)),
        ("SUB", (2,)),
        ("MUL", (2,)),
        ("DIV", (2,)),
        ("CPT", (0,)),
        ("JSR", ()),
        ("EQU", ()),
        ("GRT", ()),
        ("JMP", ()),
        ("AFI", ()),
    ],
)
def test_destructive_operands(mnemonic: str, expected: tuple[int, ...]) -> None:
    assert destructive_operands(mnemonic) == expected


def test_destination_position_is_not_uniform() -> None:
    """The reason this table exists at all, asserted so nobody 'tidies' it."""
    assert destructive_operands("MOV") == (1,)
    assert destructive_operands("ADD") == (2,)
    assert destructive_operands("CPT") == (0,)
    assert destructive_operands("COP") == (1,)


@pytest.mark.parametrize(
    ("mnemonic", "is_output"),
    [
        ("XIC", False),
        ("XIO", False),
        ("ONS", False),
        ("LBL", False),
        ("AFI", False),
        ("NOP", False),
        ("OTE", True),
        ("OSR", True),
        ("TON", True),
        ("MOV", True),
        ("JSR", True),
        ("JMP", True),
    ],
)
def test_output_flag(mnemonic: str, is_output: bool) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert spec.is_output is is_output


@pytest.mark.parametrize(
    ("mnemonic", "expected"),
    [
        ("XIC", ((BaseDataType.BOOL,),)),
        ("TON", ((BaseDataType.TIMER,),)),
        ("CTU", ((BaseDataType.COUNTER,),)),
        ("RES", ((BaseDataType.TIMER, BaseDataType.COUNTER, BaseDataType.CONTROL),)),
        # Untyped on purpose: a wrong constraint on arithmetic would reject
        # conversions Logix performs.
        ("ADD", ()),
        ("MOV", ()),
        ("EQU", ()),
    ],
)
def test_operand_types(mnemonic: str, expected: tuple[tuple[BaseDataType, ...], ...]) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert spec.operand_types == expected


@pytest.mark.parametrize(
    ("mnemonic", "minimum", "maximum"),
    [
        ("XIC", 1, 1),
        ("OSR", 2, 2),
        ("TON", 3, 3),
        ("LIM", 3, 3),
        ("BTD", 5, 5),
        ("SCP", 6, 6),
        ("TND", 0, 0),
        ("AFI", 0, 0),
        ("JSR", 2, VARIADIC_OPERAND_LIMIT),
        ("SBR", 0, VARIADIC_OPERAND_LIMIT),
        ("RET", 0, VARIADIC_OPERAND_LIMIT),
    ],
)
def test_operand_counts(mnemonic: str, minimum: int, maximum: int) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert (spec.min_operands, spec.max_operands) == (minimum, maximum)
    assert spec.accepts_operand_count(minimum)
    assert not spec.accepts_operand_count(minimum - 1)


@pytest.mark.parametrize("mnemonic", ["OTL", "OTU", "JMP", "TND", "MCR", "AFI", "SFR", "SFP"])
def test_dangerous_instructions_are_flagged_for_review(mnemonic: str) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert spec.requires_human_review


@pytest.mark.parametrize("mnemonic", ["XIC", "XIO", "OTE", "MOV", "TON", "ADD", "EQU"])
def test_everyday_instructions_are_not_flagged_for_review(mnemonic: str) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert not spec.requires_human_review


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", sorted(LOGIX_FAMILIES))
@pytest.mark.parametrize("mnemonic", ["XIC", "OTE", "TON", "MOV", "ADD", "JSR"])
def test_core_instructions_generate_for_every_logix_family(
    mnemonic: str, family: ControllerFamily
) -> None:
    assert supports(mnemonic, family)


@pytest.mark.parametrize("mnemonic", ["SCL", "SCP"])
def test_legacy_scaling_is_not_generated_for_logix(mnemonic: str) -> None:
    """Recognised so a migration source can be read; never emitted for Logix."""
    spec = spec_for(mnemonic)
    assert spec is not None
    assert not supports(mnemonic, ControllerFamily.CONTROLLOGIX)
    assert supports(mnemonic, ControllerFamily.SLC500)
    assert supports(mnemonic, ControllerFamily.PLC5)


def test_micro800_is_not_claimed() -> None:
    """Connected Components Workbench is a different instruction set."""
    assert ControllerFamily.MICRO800 not in LOGIX_FAMILIES
    assert not supports("XIC", ControllerFamily.MICRO800)
