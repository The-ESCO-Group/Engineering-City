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
    MOTION_MNEMONIC_PATTERN,
    RESTRICTED_INSTRUCTIONS,
    VARIADIC_OPERAND_LIMIT,
    DestinationCoverage,
    InstructionSpec,
    destination_coverage,
    destructive_operands,
    is_known,
    is_restricted,
    resolved_destructive_operands,
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
        "XPY",
        "LN",
        "LOG",
        "SIN",
        "COS",
        "TAN",
        "ASN",
        "ACS",
        "ATN",
        "DEG",
        "RAD",
        "TRN",
        "TOD",
        "FRD",
    ),
    InstructionCategory.MOVE_LOGICAL: (
        "MOV",
        "MVM",
        "COP",
        "CPS",
        "FLL",
        "SWPB",
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
        "FOR",
        "BRK",
        "UID",
        "UIE",
        "EVENT",
    ),
    InstructionCategory.SPECIAL: (
        "GSV",
        "IOT",
        "BSL",
        "BSR",
        "FFL",
        "FFU",
        "LFL",
        "LFU",
        "SQI",
        "SQO",
        "SQL",
        "AVE",
        "STD",
        "SRT",
        "SIZE",
        "FAL",
        "FSC",
        "PID",
        "ALMD",
        "ALMA",
        "CONCAT",
        "MID",
        "INSERT",
        "DELETE",
        "FIND",
        "LEN",
        "UPPER",
        "LOWER",
        "STOD",
        "STOR",
        "DTOS",
        "RTOS",
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


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.mnemonic)
def test_destination_coverage_is_stated_and_explained(spec: InstructionSpec) -> None:
    """An entry whose tuple is not the whole truth has to say so in its notes.

    The flag is what a caller branches on; the notes are what the engineer reads
    when the report says the check could not decide. An entry carrying the flag
    and no explanation leaves the second half of that unanswered.
    """
    assert isinstance(spec.destination_coverage, DestinationCoverage)
    if spec.destination_coverage is DestinationCoverage.COMPLETE:
        return
    notes = spec.notes.lower()
    assert any(
        word in notes for word in ("range", "mask", "masked", "operand_dependent", "depends")
    ), f"{spec.mnemonic} declares {spec.destination_coverage} and never says why."


def test_operand_dependent_entries_all_have_a_resolver() -> None:
    """The only two are JSR and SBR, and both are resolvable from their operands.

    A third added without teaching :func:`resolved_destructive_operands` about
    it would fall back to the empty tuple, which is the exact failure — "writes
    nothing" for something that writes — this field exists to stop.
    """
    declared = {
        spec.mnemonic
        for spec in ALL_SPECS
        if spec.destination_coverage is DestinationCoverage.OPERAND_DEPENDENT
    }
    assert declared == {"JSR", "SBR"}
    for mnemonic in declared:
        assert resolved_destructive_operands(mnemonic, ["A", "1", "B", "C"])


def test_no_mnemonic_is_both_catalogued_and_restricted() -> None:
    """The two tables are answers to different questions and must not overlap.

    A mnemonic in both would be generated by anything that looks it up in the
    catalogue and refused by anything that checks the restriction table, and
    which happened would depend on the order of two lines in a caller.
    """
    assert not set(INSTRUCTIONS) & set(RESTRICTED_INSTRUCTIONS)


@pytest.mark.parametrize("mnemonic", sorted(INSTRUCTIONS))
def test_a_catalogued_mnemonic_is_never_caught_by_the_motion_pattern(mnemonic: str) -> None:
    """``MCR``, ``MOV`` and friends must not be re-read as motion instructions."""
    assert not is_restricted(mnemonic), (
        f"{mnemonic} is catalogued as generatable but answers is_restricted()."
    )


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


def test_the_safety_reason_admits_the_name_collision() -> None:
    """ESTOP is a legal Add-On Instruction name on a standard controller.

    The mnemonic alone cannot tell a GuardLogix safety instruction from an
    integrator's AOI, and the refusal an engineer reads has to say so or they
    will go looking for safety logic that does not exist.
    """
    reason = restriction_reason("ESTOP")
    assert "Add-On Instruction" in reason
    assert "name collision" in reason


def test_messaging_reason_names_the_actual_hazard() -> None:
    assert "another device" in restriction_reason("MSG")


# ---------------------------------------------------------------------------
# Motion: the named list and the pattern that backs it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mnemonic",
    # Every one of these is a stock Logix motion instruction that the nine-entry
    # list this catalogue used to hold did not name.
    ["MDO", "MDF", "MAG", "MAFR", "MASD", "MASR", "MGS", "MGSD", "MGSR", "MCS", "MCT", "MRP"],
)
def test_motion_instructions_beyond_the_original_nine_are_refused(mnemonic: str) -> None:
    assert is_restricted(mnemonic)
    assert spec_for(mnemonic) is None
    assert is_known(mnemonic), "A refused instruction is still one we recognise."
    assert "Motion" in restriction_reason(mnemonic)


def test_the_direct_drive_reason_names_its_own_hazard() -> None:
    """MDO commands a drive with no profile at all; the generic text understates it."""
    assert "no profile" in restriction_reason("MDO")


@pytest.mark.parametrize("mnemonic", ["MDCC", "MAJH", "MCPM", "MSAT"])
def test_an_unlisted_motion_shaped_mnemonic_is_refused_by_pattern(mnemonic: str) -> None:
    """The Logix motion set outgrows any hand-kept list, so the shape backs it up.

    Without this an unlisted motion instruction is reported as "never heard of
    it" — an UNSUPPORTED note with no human decision required — for a rung that
    commands a servo axis.
    """
    assert mnemonic not in RESTRICTED_INSTRUCTIONS
    assert is_restricted(mnemonic)
    assert is_known(mnemonic)
    reason = restriction_reason(mnemonic)
    assert "by its mnemonic rather than by name" in reason
    assert mnemonic in reason


@pytest.mark.parametrize(
    "mnemonic",
    # Ordinary mnemonics that begin with M, and the shapes an Add-On Instruction
    # takes. None of these may be swallowed by the motion pattern.
    ["MOV", "MUL", "MOD", "MVM", "MEQ", "MCR", "MID", "MOTOR_START", "MC_Pump", "MAGFLOW", "M"],
)
def test_the_motion_pattern_does_not_swallow_ordinary_mnemonics(mnemonic: str) -> None:
    caught_by_shape = "by its mnemonic rather than by name" in restriction_reason(mnemonic)
    assert not caught_by_shape, f"{mnemonic} was read as a motion instruction by shape."


@pytest.mark.parametrize("mnemonic", sorted(RESTRICTED_INSTRUCTIONS))
def test_a_named_restriction_never_falls_through_to_the_pattern(mnemonic: str) -> None:
    """``MSG`` matches the motion shape; its own reason must still win."""
    assert restriction_reason(mnemonic) == RESTRICTED_INSTRUCTIONS[mnemonic]


def test_the_motion_pattern_is_narrow_enough_to_state() -> None:
    """Three to five characters, letters only. Asserted so a widening is deliberate."""
    assert MOTION_MNEMONIC_PATTERN.fullmatch("MAM")
    assert MOTION_MNEMONIC_PATTERN.fullmatch("MCLM")
    assert not MOTION_MNEMONIC_PATTERN.fullmatch("MA")
    assert not MOTION_MNEMONIC_PATTERN.fullmatch("MAGFLOW")
    assert not MOTION_MNEMONIC_PATTERN.fullmatch("MA_JOG")
    assert not MOTION_MNEMONIC_PATTERN.fullmatch("MOV")


# ---------------------------------------------------------------------------
# Controller configuration
# ---------------------------------------------------------------------------


def test_ssv_is_refused_and_says_what_it_reaches() -> None:
    """SSV writes controller, task, program and module configuration from a rung.

    Catalogued as neither known nor restricted, it came back "never heard of
    it", and the engineer was told the platform did not recognise a name rather
    than that the rung reconfigures the controller.
    """
    assert is_restricted("SSV")
    assert is_known("SSV")
    assert spec_for("SSV") is None
    reason = restriction_reason("SSV")
    assert "watchdog" in reason.lower()
    assert "never generated" in reason


def test_gsv_is_catalogued_and_writes_its_fourth_operand() -> None:
    """GSV's counterpart is read-only and ordinary, so it is generated, not refused."""
    spec = spec_for("GSV")
    assert spec is not None
    assert not is_restricted("GSV")
    assert spec.destructive_operand_indexes == (3,)
    assert (spec.min_operands, spec.max_operands) == (4, 4)


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
        # Added because their absence removed them from duplicate-destination
        # detection altogether: nothing else in the backend records a write.
        ("FLL", (1,)),
        ("GSV", (3,)),
        ("SWPB", (2,)),
        ("TOD", (1,)),
        ("FRD", (1,)),
        ("TRN", (1,)),
        ("XPY", (2,)),
        ("SIN", (1,)),
        ("BSL", (0, 1)),
        ("BSR", (0, 1)),
        ("FFL", (1, 2)),
        ("FFU", (0, 1, 2)),
        ("LFL", (1, 2)),
        ("LFU", (0, 1, 2)),
        ("SQI", (3,)),
        ("SQO", (2, 3)),
        ("SQL", (0, 2)),
        ("AVE", (1, 2)),
        ("STD", (1, 2)),
        ("SRT", (0, 1)),
        ("SIZE", (2,)),
        ("FAL", (0, 4)),
        ("FSC", (0,)),
        ("PID", (0, 3)),
        ("ALMD", (0,)),
        ("ALMA", (0,)),
        ("CONCAT", (2,)),
        ("MID", (3,)),
        ("FIND", (3,)),
        ("LEN", (1,)),
        ("DTOS", (1,)),
        # Reads and reports; writes nothing.
        ("IOT", ()),
        ("UID", ()),
        ("UIE", ()),
        ("EVENT", ()),
        ("BRK", ()),
        ("FOR", (1,)),
    ],
)
def test_destructive_operands(mnemonic: str, expected: tuple[int, ...]) -> None:
    assert destructive_operands(mnemonic) == expected


def test_the_sequencer_output_writes_its_destination_and_its_control() -> None:
    """The reading mistake this entry prevents: the destination is operand 2.

    An SQO drives real outputs from a step table, so a second rung writing that
    destination is the machine commanded from two places at once.
    """
    assert destructive_operands("SQO") == (2, 3)
    spec = spec_for("SQO")
    assert spec is not None
    assert spec.requires_human_review


def test_the_file_sort_writes_the_array_it_is_given() -> None:
    """SRT sorts in place: nothing in its operand list looks like a destination."""
    assert destructive_operands("SRT") == (0, 1)


# ---------------------------------------------------------------------------
# Destinations a fixed tuple cannot state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mnemonic", ["COP", "CPS", "FLL"])
def test_a_range_destination_is_declared_partial(mnemonic: str) -> None:
    """Operand 1 is the first element of a range, not the whole destination.

    ``COP(Master[0],Active[0],20)`` writes Active[0] through Active[19]. A
    caller that reads the tuple as the whole answer will not see the collision
    with a ``MOV`` into Active[5], so the entry has to say the tuple is partial
    rather than leave that to be discovered.
    """
    assert destination_coverage(mnemonic) is DestinationCoverage.PARTIAL
    spec = spec_for(mnemonic)
    assert spec is not None
    assert "range" in spec.notes.lower()


@pytest.mark.parametrize("mnemonic", ["MVM", "BTD"])
def test_a_masked_destination_is_declared_partial(mnemonic: str) -> None:
    assert destination_coverage(mnemonic) is DestinationCoverage.PARTIAL


@pytest.mark.parametrize("mnemonic", ["MOV", "OTE", "ADD", "TON", "GSV", "SQO"])
def test_an_ordinary_destination_is_declared_complete(mnemonic: str) -> None:
    assert destination_coverage(mnemonic) is DestinationCoverage.COMPLETE


def test_destination_coverage_is_none_for_an_unknown_mnemonic() -> None:
    """ "Nothing is known" and "the tuple is the whole truth" must stay distinct."""
    assert destination_coverage("ESCO_MOTOR_AOI") is None


@pytest.mark.parametrize("mnemonic", ["JSR", "SBR"])
def test_a_variadic_destination_is_declared_operand_dependent(mnemonic: str) -> None:
    assert destination_coverage(mnemonic) is DestinationCoverage.OPERAND_DEPENDENT
    assert destructive_operands(mnemonic) == ()


@pytest.mark.parametrize(
    ("operands", "expected"),
    [
        # JSR(Scale_Routine, 1, Raw_Count, Scaled_Value): one input, so operand
        # 3 is a return parameter and is written by the call.
        (["Scale_Routine", "1", "Raw_Count", "Scaled_Value"], (3,)),
        (["Scale_Routine", "0", "Out_A", "Out_B"], (2, 3)),
        (["Scale_Routine", "2", "In_A", "In_B", "Out"], (4,)),
        # Nothing passed back.
        (["Scale_Routine", "1", "Raw_Count"], ()),
        (["Scale_Routine", "0"], ()),
        # The count is not a literal, so which operands are returns cannot be
        # worked out. Claiming any would name an *input* as a destination.
        (["Scale_Routine", "Param_Count", "A", "B"], ()),
        (["Scale_Routine"], ()),
    ],
)
def test_jsr_return_parameters_are_resolved_from_the_input_count(
    operands: list[str], expected: tuple[int, ...]
) -> None:
    assert resolved_destructive_operands("JSR", operands) == expected


def test_sbr_writes_every_parameter_it_receives() -> None:
    assert resolved_destructive_operands("SBR", ["Speed_SP", "Mode"]) == (0, 1)
    assert resolved_destructive_operands("SBR", []) == ()


@pytest.mark.parametrize("mnemonic", ["MOV", "OTE", "COP"])
def test_resolving_a_fixed_instruction_answers_the_fixed_tuple(mnemonic: str) -> None:
    assert resolved_destructive_operands(mnemonic, ["A", "B", "C"]) == destructive_operands(
        mnemonic
    )


def test_resolving_an_unknown_mnemonic_claims_nothing() -> None:
    assert resolved_destructive_operands("ESCO_MOTOR_AOI", ["A", "B"]) == ()


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


@pytest.mark.parametrize(
    "mnemonic",
    [
        "OTL",
        "OTU",
        "JMP",
        "TND",
        "MCR",
        "AFI",
        "SFR",
        "SFP",
        # Drives a sequencer out of step, which is one of the three hazards the
        # review flag was written for.
        "SQI",
        "SQO",
        "SQL",
        # Skips the rest of the routine, or stops every periodic task in the
        # controller — the same class as TND and AFI.
        "FOR",
        "BRK",
        "UID",
        # Commands a final control element from a loop nobody here tuned.
        "PID",
    ],
)
def test_dangerous_instructions_are_flagged_for_review(mnemonic: str) -> None:
    spec = spec_for(mnemonic)
    assert spec is not None
    assert spec.requires_human_review


@pytest.mark.parametrize(
    "mnemonic", ["XIC", "XIO", "OTE", "MOV", "TON", "ADD", "EQU", "GSV", "COP", "CONCAT", "UIE"]
)
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


def test_scl_is_not_claimed_for_plc5() -> None:
    """SCL is an SLC 500 and MicroLogix instruction; PLC-5 has SCP instead.

    ``supports`` is exported as a general answer about family support, so a
    ``True`` here is a false capability claim a migration report would repeat.
    """
    assert not supports("SCL", ControllerFamily.PLC5)
    assert supports("SCP", ControllerFamily.PLC5)


def test_micro800_is_not_claimed() -> None:
    """Connected Components Workbench is a different instruction set."""
    assert ControllerFamily.MICRO800 not in LOGIX_FAMILIES
    assert not supports("XIC", ControllerFamily.MICRO800)
