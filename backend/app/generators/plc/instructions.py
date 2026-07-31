"""The Logix ladder instructions this platform is willing to generate and check.

This is a catalogue, not a compiler. Each entry states the few things a
validator has to know to check an instruction without a hand-written branch per
mnemonic: how many operands it takes, **which operand positions it writes**,
what it is called, and whether this platform will emit it at all.

The destructive positions are the point of the exercise. "Two rungs drive the
same coil" is one of the very few defects a validator can find with certainty —
the last rung scanned wins, silently — and it is only findable if something
knows that ``OTE`` writes operand 0 while ``XIC`` writes nothing, that ``MOV``
writes its *second* operand and ``CPT`` its *first*. That knowledge lives here,
once, rather than in every caller that walks a rung.

Reading an unknown mnemonic
---------------------------
:func:`spec_for` returns ``None`` for a mnemonic this catalogue does not
describe, and that is neither an error nor permission to carry on. The caller's
job is to record the instruction as unsupported **naming the mnemonic**, so the
gap is visible in the record instead of looking like an instruction that was
checked and found clean. A validator that silently ignores what it does not
recognise is worse than one that refuses to run: the one that refuses is
obviously broken, while the one that skips quietly reports a clean rung it never
read. The intended shape at a call site is::

    spec = spec_for(mnemonic)
    if spec is None:
        if is_restricted(mnemonic):
            report_restricted(mnemonic, restriction_reason(mnemonic))
        else:
            report_unsupported(mnemonic)
        return

The same applies to :func:`destructive_operands`, which answers ``()`` for an
unknown mnemonic. That empty tuple is "this catalogue has nothing to say", not
"this instruction writes nothing", and a caller that has not first checked
:func:`is_known` will mistake one for the other and lose a duplicate-destination
defect. Likewise :func:`supports` answers ``False`` for an unknown mnemonic: an
unrecognised instruction is not a supported one.

What ``operand_types`` claims, and what it does not
---------------------------------------------------
An empty tuple at a position means **any type**, and it is used freely. A wrong
constraint is worse than no constraint: it produces confident validation errors
against logic that is perfectly correct, which teaches engineers to ignore the
validator. So a constraint appears only where the instruction genuinely admits
one set of types — the ``TIMER`` of a ``TON``, the ``COUNTER`` of a ``CTU``, the
bit of an ``XIC`` — and everywhere else the position is left open and the reason
is written into ``notes``. Arithmetic is the large case: Logix converts between
numeric types by documented rules and the accepted set moves with firmware
revision, so nothing here asserts what ``ADD`` will take.

Two rules make the constraints that do exist safe to apply:

* They describe the operand **as evaluated**, after bit selectors, array
  subscripts and member access. ``Status_Word.3`` is a ``BOOL`` operand of a
  ``DINT`` tag. A validator that has not resolved the selector must skip the
  check rather than compare against the base tag's type.
* They apply only to operands whose kind is in :data:`TYPED_OPERAND_KINDS`. An
  :attr:`~app.core.enums.OperandKind.EXPRESSION`, a
  :attr:`~app.core.enums.OperandKind.ROUTINE_REFERENCE` or anything
  :attr:`~app.core.enums.OperandKind.UNRESOLVED` has no position type to check.

Vocabulary shared with the parser
---------------------------------
:mod:`app.parsers.l5x_parser` tokenises rung text into a mnemonic over operand
*strings* and nothing more, and this catalogue describes exactly that. Three
consequences a caller must hold onto:

* Branches are flattened into the instruction sequence by the tokeniser, so the
  operand *positions* described here survive but the rung's parallel structure
  does not. ``is_output`` says an instruction terminates a rung branch; it does
  not say where that branch is.
* Logix writes an unspecified operand as ``?`` — ``TON(T_Dwell,?,?)`` is normal
  in an export. Such a position is present and untyped, which is one more reason
  the preset and accumulator of a timer carry no constraint.
* A label or routine name is an operand like any other to the parser, so
  ``JMP(Restart)`` leaves ``Restart`` in ``unresolved_references``: no ``<Tags>``
  entry declares a label. A validator using this catalogue should suppress the
  unresolved-reference finding for the positions each spec's ``notes`` names,
  and must not "fix" it by inventing a tag.

Restricted instructions
-----------------------
:data:`RESTRICTED_INSTRUCTIONS` is a separate table of mnemonics this platform
recognises and refuses to emit — motion, messaging, and the GuardLogix safety
instruction set. These are read and reported like anything else; what they never
do is come out of a generator without a qualified workflow
(:class:`~app.core.enums.RestrictedDomainMode`). They are deliberately absent
from :data:`INSTRUCTIONS`: a generator that only ever emits what it can look up
in the catalogue cannot emit one of these by accident, which is a stronger
guarantee than a flag somebody has to remember to check.

The safety list is explicitly **not** claimed to be complete, and nothing rests
on it being complete. A safety-rated controller or a safety program puts the
whole project into manual-review-only mode by classification, long before any
individual mnemonic is looked up; the list is here so that a safety instruction
met inside an otherwise standard project is named and refused rather than
reported as merely unrecognised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.enums import BaseDataType, ControllerFamily, InstructionCategory, OperandKind

__all__ = [
    "INSTRUCTIONS",
    "LEGACY_FAMILIES",
    "LOGIX_FAMILIES",
    "RESTRICTED_INSTRUCTIONS",
    "TYPED_OPERAND_KINDS",
    "VARIADIC_OPERAND_LIMIT",
    "InstructionSpec",
    "destructive_operands",
    "is_known",
    "is_restricted",
    "restriction_reason",
    "spec_for",
    "supports",
]

#: ``max_operands`` for an instruction whose operand list has no fixed upper
#: bound — ``JSR`` and its parameter list, and the ``SBR``/``RET`` pair that
#: receives and returns them. A large integer rather than ``None`` so that every
#: caller's arity check stays one comparison instead of a special case, and so
#: an out-of-range destructive index is still a detectable mistake.
VARIADIC_OPERAND_LIMIT: Final[int] = 1000

#: The Logix families whose ladder instruction set this catalogue describes, and
#: the default for :attr:`InstructionSpec.supported_families`. Micro800 is
#: deliberately excluded: Connected Components Workbench is a different
#: instruction set, and claiming ``XIC`` generates for it would be a claim this
#: platform cannot support.
LOGIX_FAMILIES: Final[frozenset[ControllerFamily]] = frozenset(
    {
        ControllerFamily.CONTROLLOGIX,
        ControllerFamily.COMPACTLOGIX,
        ControllerFamily.GUARDLOGIX,
        ControllerFamily.COMPACT_GUARDLOGIX,
    }
)

#: Pre-Logix families. Used only by instructions that exist there and not in
#: Logix ladder, which a migration project will meet in its source material.
LEGACY_FAMILIES: Final[frozenset[ControllerFamily]] = frozenset(
    {ControllerFamily.SLC500, ControllerFamily.PLC5}
)

#: Operand kinds an ``operand_types`` entry can be checked against. An operand
#: of any other kind has no positional type to compare — an expression has a
#: result type but not an operand type, a routine reference names no data at
#: all, and an unresolved operand is the thing to report rather than type-check.
TYPED_OPERAND_KINDS: Final[frozenset[OperandKind]] = frozenset(
    {OperandKind.TAG, OperandKind.LITERAL}
)


@dataclass(frozen=True, slots=True)
class InstructionSpec:
    """What this platform knows about one ladder mnemonic.

    Frozen because the catalogue is shared, read from every validator, and must
    not be edited in place by a caller that wants a slightly different rule.
    """

    #: Canonical, upper-case. Lookups accept any case; this is the stored form.
    mnemonic: str
    #: Human name, as Rockwell documents it ("Examine If Closed").
    name: str
    category: InstructionCategory
    min_operands: int
    #: :data:`VARIADIC_OPERAND_LIMIT` when the operand list is open-ended.
    max_operands: int

    #: Operand positions the instruction *writes*. The one field this module
    #: exists for; see the module docstring.
    destructive_operand_indexes: tuple[int, ...] = ()

    #: Acceptable base types per position. An empty tuple at a position, or a
    #: tuple shorter than ``max_operands``, means "any" — an honest answer, and
    #: the common one.
    operand_types: tuple[tuple[BaseDataType, ...], ...] = ()

    #: True when the instruction terminates a rung branch rather than
    #: conditioning it. Not the same as "writes something": ``ONS`` writes its
    #: storage bit and still conditions the rung, ``JMP`` writes nothing and
    #: still terminates it.
    is_output: bool = False

    supported_families: frozenset[ControllerFamily] = LOGIX_FAMILIES

    #: What a reader needs to know that the fields above cannot say — above all,
    #: *why* a position carries no type constraint.
    notes: str = ""

    #: True where misuse is dangerous rather than merely wrong: logic that can
    #: silently skip outputs, latch one on, or drive a sequencer out of step.
    #: The instruction is still generated; a human is told to look at it.
    requires_human_review: bool = False

    def __post_init__(self) -> None:
        """Fail at import time rather than let a malformed entry ship.

        A catalogue is only worth trusting if it cannot quietly disagree with
        itself, and every one of these mistakes is silent at the call site: a
        destructive index past the end of the operand list simply never matches
        an operand, so the defect it was meant to catch stops being reported.
        """
        if self.mnemonic != self.mnemonic.upper() or not self.mnemonic:
            raise ValueError(f"Mnemonic must be non-empty and upper-case: {self.mnemonic!r}")
        if self.min_operands < 0 or self.max_operands < self.min_operands:
            raise ValueError(
                f"{self.mnemonic}: operand bounds {self.min_operands}..{self.max_operands} "
                "are not a range."
            )
        if len(self.operand_types) > self.max_operands:
            raise ValueError(
                f"{self.mnemonic}: {len(self.operand_types)} operand type entries for at "
                f"most {self.max_operands} operands."
            )
        for index in self.destructive_operand_indexes:
            if not 0 <= index < self.min_operands:
                raise ValueError(
                    f"{self.mnemonic}: destructive operand index {index} is outside the "
                    f"{self.min_operands} operand(s) the instruction requires."
                )
        if len(set(self.destructive_operand_indexes)) != len(self.destructive_operand_indexes):
            raise ValueError(f"{self.mnemonic}: repeated destructive operand index.")
        if not self.supported_families:
            raise ValueError(f"{self.mnemonic}: an instruction no family supports is not usable.")

    @property
    def is_restricted(self) -> bool:
        """Whether this mnemonic is refused for generation.

        Looked up against :data:`RESTRICTED_INSTRUCTIONS` rather than stored, so
        the two tables cannot drift apart: a mnemonic added to the restricted
        table starts reporting itself restricted even if a spec for it exists.
        """
        return self.mnemonic in RESTRICTED_INSTRUCTIONS

    def accepts_operand_count(self, count: int) -> bool:
        """Whether ``count`` operands is a legal arity for this instruction."""
        return self.min_operands <= count <= self.max_operands


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# Ordered by category, and within a category the way Rockwell's reference orders
# them, so a reader comparing this file against the manual can go line by line.

_SPECS: Final[tuple[InstructionSpec, ...]] = (
    # -- bit ----------------------------------------------------------------
    InstructionSpec(
        mnemonic="XIC",
        name="Examine If Closed",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        operand_types=((BaseDataType.BOOL,),),
        notes=(
            "Conditions the rung on the operand being 1. The BOOL constraint is on the "
            "operand as evaluated: 'Status_Word.3' is a BOOL operand of a DINT tag."
        ),
    ),
    InstructionSpec(
        mnemonic="XIO",
        name="Examine If Open",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        operand_types=((BaseDataType.BOOL,),),
        notes=(
            "Conditions the rung on the operand being 0. Which of XIC and XIO is "
            "fail-safe for a given signal depends on how the field device is wired, "
            "which no export states; that judgement is not made here."
        ),
    ),
    InstructionSpec(
        mnemonic="OTE",
        name="Output Energize",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.BOOL,),),
        is_output=True,
        notes=(
            "Writes its operand every scan, in both directions. Two OTE instructions "
            "on one tag is the duplicate-destination defect this catalogue exists to "
            "make findable."
        ),
    ),
    InstructionSpec(
        mnemonic="OTL",
        name="Output Latch",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.BOOL,),),
        is_output=True,
        requires_human_review=True,
        notes=(
            "Sets the bit and leaves it set when the rung goes false, across a fault "
            "and across a power cycle. A latched output with no reachable OTU is an "
            "output nothing turns off, so every use is put in front of a human."
        ),
    ),
    InstructionSpec(
        mnemonic="OTU",
        name="Output Unlatch",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.BOOL,),),
        is_output=True,
        requires_human_review=True,
        notes=(
            "Clears the bit and leaves it clear. Reviewed with its OTL: the pair is "
            "only correct as a pair, and clearing a bit some other rung latches is a "
            "race a reader cannot see from either rung alone."
        ),
    ),
    InstructionSpec(
        mnemonic="ONS",
        name="One Shot",
        category=InstructionCategory.BIT,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.BOOL,),),
        notes=(
            "An input instruction that nonetheless writes its storage bit. The bit "
            "must not be used anywhere else — sharing it makes both one-shots fire "
            "wrongly — which is a duplicate-destination check, not a special case."
        ),
    ),
    InstructionSpec(
        mnemonic="OSR",
        name="One Shot Rising",
        category=InstructionCategory.BIT,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(0, 1),
        operand_types=((BaseDataType.BOOL,), (BaseDataType.BOOL,)),
        is_output=True,
        notes=(
            "Logix form: storage bit then output bit, both written. The PLC-5 and SLC "
            "form of OSR is a single-operand input instruction; this spec describes "
            "the Logix one, which is why its families are Logix only."
        ),
    ),
    InstructionSpec(
        mnemonic="OSF",
        name="One Shot Falling",
        category=InstructionCategory.BIT,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(0, 1),
        operand_types=((BaseDataType.BOOL,), (BaseDataType.BOOL,)),
        is_output=True,
        notes="Storage bit then output bit, both written. Fires on the falling edge.",
    ),
    # -- timer and counter --------------------------------------------------
    InstructionSpec(
        mnemonic="TON",
        name="Timer On Delay",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.TIMER,),),
        is_output=True,
        notes=(
            "Timer, preset, accumulator. The instruction writes the whole TIMER "
            "structure, so the destructive position is the structure and not .ACC. "
            "Preset and accumulator carry no type: an export commonly writes them as "
            "'?', and a constraint would fail on a rung Logix accepts."
        ),
    ),
    InstructionSpec(
        mnemonic="TOF",
        name="Timer Off Delay",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.TIMER,),),
        is_output=True,
        notes=(
            "Timer, preset, accumulator. A TOF times while its rung is false, so a "
            "TOF inside a branch that is itself conditioned reads very differently "
            "from a TON in the same place. Positions as for TON."
        ),
    ),
    InstructionSpec(
        mnemonic="RTO",
        name="Retentive Timer On",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.TIMER,),),
        is_output=True,
        notes=(
            "Timer, preset, accumulator. Retains its accumulator when the rung goes "
            "false, so it is only complete with a RES that clears it; a retentive "
            "timer nothing resets is a timer that fires once and never again."
        ),
    ),
    InstructionSpec(
        mnemonic="CTU",
        name="Count Up",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.COUNTER,),),
        is_output=True,
        notes=(
            "Counter, preset, accumulator. Writes the COUNTER structure. Preset and "
            "accumulator untyped for the same reason as the timers."
        ),
    ),
    InstructionSpec(
        mnemonic="CTD",
        name="Count Down",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.COUNTER,),),
        is_output=True,
        notes=(
            "Counter, preset, accumulator. A CTU and a CTD on one counter are a "
            "deliberate pair rather than a duplicate destination, so a "
            "duplicate-destination check has to allow that combination."
        ),
    ),
    InstructionSpec(
        mnemonic="RES",
        name="Reset",
        category=InstructionCategory.TIMER_COUNTER,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.TIMER, BaseDataType.COUNTER, BaseDataType.CONTROL),),
        is_output=True,
        notes=(
            "Clears the accumulator and the status bits of the structure it names. "
            "The three structure types are the ones Logix accepts; a RES on anything "
            "else is a real error rather than a limitation of this catalogue."
        ),
    ),
    # -- compare ------------------------------------------------------------
    InstructionSpec(
        mnemonic="EQU",
        name="Equal To",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes=(
            "Source A, source B. Untyped: Logix converts between numeric types by "
            "documented rules and also compares strings and structures, so no single "
            "set of acceptable types is true here."
        ),
    ),
    InstructionSpec(
        mnemonic="NEQ",
        name="Not Equal To",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes="Source A, source B. Untyped, as EQU.",
    ),
    InstructionSpec(
        mnemonic="LES",
        name="Less Than",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes="Source A, source B. Untyped, as EQU.",
    ),
    InstructionSpec(
        mnemonic="GRT",
        name="Greater Than",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes=(
            "Source A, source B. Untyped, as EQU. Whether the threshold in operand 1 "
            "is the right threshold is a question for the surrounding documents, not "
            "for a catalogue."
        ),
    ),
    InstructionSpec(
        mnemonic="LEQ",
        name="Less Than or Equal To",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes="Source A, source B. Untyped, as EQU.",
    ),
    InstructionSpec(
        mnemonic="GEQ",
        name="Greater Than or Equal To",
        category=InstructionCategory.COMPARE,
        min_operands=2,
        max_operands=2,
        notes="Source A, source B. Untyped, as EQU.",
    ),
    InstructionSpec(
        mnemonic="CMP",
        name="Compare",
        category=InstructionCategory.COMPARE,
        min_operands=1,
        max_operands=1,
        notes=(
            "One operand, and it is an expression rather than a tag: the parser keeps "
            "it as a single verbatim string with its inner parentheses intact, and it "
            "is an OperandKind.EXPRESSION. Nothing positional can be checked in it, "
            "and the tags it reads are only findable by parsing the expression."
        ),
    ),
    InstructionSpec(
        mnemonic="LIM",
        name="Limit Test",
        category=InstructionCategory.COMPARE,
        min_operands=3,
        max_operands=3,
        notes=(
            "Low limit, test value, high limit — the tested value is the *middle* "
            "operand, which is the reading mistake this entry exists to prevent. "
            "Untyped, as EQU. Logix defines the result when low exceeds high "
            "(the test inverts), so that ordering is not an error to report."
        ),
    ),
    InstructionSpec(
        mnemonic="MEQ",
        name="Masked Equal To",
        category=InstructionCategory.COMPARE,
        min_operands=3,
        max_operands=3,
        notes=(
            "Source, mask, compare. Untyped: the mask is written as a literal in any "
            "radix as often as it is a tag."
        ),
    ),
    # -- compute and math ---------------------------------------------------
    InstructionSpec(
        mnemonic="ADD",
        name="Add",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source A, source B, destination. Untyped: the numeric types Logix "
            "accepts and how it converts between them move with firmware revision, "
            "and a wrong constraint here would fail correct arithmetic."
        ),
    ),
    InstructionSpec(
        mnemonic="SUB",
        name="Subtract",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A minus source B into destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="MUL",
        name="Multiply",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A, source B, destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="DIV",
        name="Divide",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source A divided by source B into destination. Untyped, as ADD. Whether "
            "source B can be zero is a question about the logic around the rung, and "
            "this catalogue does not answer it."
        ),
    ),
    InstructionSpec(
        mnemonic="MOD",
        name="Modulo",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A modulo source B into destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="SQR",
        name="Square Root",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="NEG",
        name="Negate",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="ABS",
        name="Absolute Value",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="CPT",
        name="Compute",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(0,),
        is_output=True,
        notes=(
            "Destination *first*, then the expression — the reverse of ADD and the "
            "reason destructive positions are stored per mnemonic rather than assumed "
            "to be last. The expression is one verbatim operand "
            "(OperandKind.EXPRESSION); the tags it reads are not positional."
        ),
    ),
    InstructionSpec(
        mnemonic="SCL",
        name="Scale",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        supported_families=LEGACY_FAMILIES,
        notes=(
            "PLC-5 and SLC form: source, rate, offset, destination. Not a Logix "
            "ladder instruction — Logix scales in an FBD block or in arithmetic — so "
            "this entry exists to read and report a migration source, and "
            "supports() answers False for every Logix family. The operand order is "
            "stated from the legacy manuals and is the part of this entry held with "
            "least confidence, so no position carries a type."
        ),
    ),
    InstructionSpec(
        mnemonic="SCP",
        name="Scale with Parameters",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=6,
        max_operands=6,
        destructive_operand_indexes=(5,),
        is_output=True,
        supported_families=LEGACY_FAMILIES,
        notes=(
            "Legacy form: input, input minimum, input maximum, scaled minimum, "
            "scaled maximum, output. Same standing as SCL — recognised for migration "
            "sources, never emitted for a Logix target, untyped throughout."
        ),
    ),
    # -- move and logical ---------------------------------------------------
    InstructionSpec(
        mnemonic="MOV",
        name="Move",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination. Untyped: MOV converts between numeric types, so a "
            "constraint would reject conversions Logix performs."
        ),
    ),
    InstructionSpec(
        mnemonic="MVM",
        name="Masked Move",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source, mask, destination. Only the masked bits are written, so two MVM "
            "instructions on one destination are not necessarily a conflict — "
            "whether they are depends on whether the masks overlap, which a "
            "duplicate-destination check on tag names alone cannot decide."
        ),
    ),
    InstructionSpec(
        mnemonic="COP",
        name="Copy File",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination, length — destination is the *middle* operand. "
            "Length counts destination elements, not bytes; COP does no type "
            "conversion and will happily reinterpret the bits, which is why a "
            "generated COP between unlike types is worth a look."
        ),
    ),
    InstructionSpec(
        mnemonic="CPS",
        name="Synchronous Copy File",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination, length, as COP, but the copy cannot be interrupted "
            "by a higher-priority task or an I/O update. That makes it the right "
            "instruction for a coherent snapshot and a scan-time cost everywhere else."
        ),
    ),
    InstructionSpec(
        mnemonic="CLR",
        name="Clear",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=1,
        max_operands=1,
        destructive_operand_indexes=(0,),
        is_output=True,
        notes="Writes zero into its single operand. Untyped: it clears any numeric type.",
    ),
    InstructionSpec(
        mnemonic="BTD",
        name="Bit Field Distribute",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source, source bit, destination, destination bit, length. The bit "
            "numbers and the length are immediate values as often as tags, so no "
            "position carries a type. Only the named bit field of the destination is "
            "written, with the same consequence for duplicate-destination checks as "
            "MVM."
        ),
    ),
    InstructionSpec(
        mnemonic="AND",
        name="Bitwise AND",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source A, source B, destination. Bitwise over integers, not a boolean "
            "operator: a rung that wants boolean AND uses series contacts instead."
        ),
    ),
    InstructionSpec(
        mnemonic="OR",
        name="Bitwise OR",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A, source B, destination. Bitwise, as AND.",
    ),
    InstructionSpec(
        mnemonic="XOR",
        name="Bitwise Exclusive OR",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A, source B, destination. Bitwise, as AND.",
    ),
    InstructionSpec(
        mnemonic="NOT",
        name="Bitwise NOT",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Bitwise complement, not a boolean negation.",
    ),
    # -- program control ----------------------------------------------------
    InstructionSpec(
        mnemonic="JSR",
        name="Jump To Subroutine",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=2,
        max_operands=VARIADIC_OPERAND_LIMIT,
        is_output=True,
        notes=(
            "Routine name, input parameter count, then the input parameters and the "
            "return parameters. Operand 0 is a routine reference, not a tag — the "
            "L5X parser resolves it against the routines the export declares, and a "
            "validator must not report it as an unresolved tag. Nothing is recorded "
            "as destructive: the return parameters *are* written, but which operands "
            "they are depends on the count in operand 1, so a fixed index tuple "
            "cannot say it. A validator that needs the return destinations has to "
            "read that count; treating JSR as writing nothing is a known gap, "
            "written down here rather than left to be discovered. JXR, which the "
            "parser also treats as calling a routine, is deliberately not "
            "catalogued: external routines are outside what this platform generates, "
            "so it comes back unknown and is reported as unsupported."
        ),
    ),
    InstructionSpec(
        mnemonic="SBR",
        name="Subroutine",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=VARIADIC_OPERAND_LIMIT,
        notes=(
            "The parameter list a called routine receives, first instruction of the "
            "first rung. *Every* operand is written when the routine is called, and "
            "a fixed index tuple cannot express 'all of them' for a variadic "
            "instruction, so this entry records none and says so: a validator that "
            "cares about SBR destinations must special-case it rather than trust the "
            "empty tuple."
        ),
    ),
    InstructionSpec(
        mnemonic="RET",
        name="Return",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=VARIADIC_OPERAND_LIMIT,
        is_output=True,
        notes=(
            "Returns to the calling JSR, passing back the values of its operands. "
            "Its own operands are sources; the destinations are the JSR's return "
            "parameters, which is why nothing here is destructive."
        ),
    ),
    InstructionSpec(
        mnemonic="JMP",
        name="Jump To Label",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=1,
        max_operands=1,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Operand 0 is a label name, not a tag: no <Tags> entry declares it, so "
            "the L5X parser reports it in unresolved_references and a validator "
            "should suppress that here rather than invent a tag. Human review "
            "because a jump skips every rung between it and its label, leaving those "
            "outputs in their last state — an interlock can be bypassed by logic that "
            "reads, rung by rung, as correct."
        ),
    ),
    InstructionSpec(
        mnemonic="LBL",
        name="Label",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=1,
        max_operands=1,
        notes=(
            "The target of a JMP, and an input instruction: it must be the first "
            "instruction on its rung. Operand 0 is a label name with the same "
            "unresolved-reference consequence as JMP's."
        ),
    ),
    InstructionSpec(
        mnemonic="TND",
        name="Temporary End",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Ends the scan of the routine where it stands. Human review for the same "
            "reason as JMP: every rung after it is skipped this scan, and a TND left "
            "in from commissioning is invisible in any single rung."
        ),
    ),
    InstructionSpec(
        mnemonic="MCR",
        name="Master Control Reset",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Bounds a zone whose non-retentive outputs are de-energised when the "
            "zone is false. Human review without exception: an MCR zone is read by "
            "people as a stop function, and Rockwell states plainly that it is not a "
            "substitute for a hard-wired emergency stop. It removes no power."
        ),
    ),
    InstructionSpec(
        mnemonic="AFI",
        name="Always False Instruction",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        requires_human_review=True,
        notes=(
            "An input instruction that is always false, so the rung it sits on never "
            "executes. Legitimate while commissioning and dangerous when left "
            "behind: the logic is present, reviewable and dead, which is exactly how "
            "a disabled interlock survives a code review."
        ),
    ),
    InstructionSpec(
        mnemonic="NOP",
        name="No Operation",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        notes="Does nothing and takes no operands. Kept so a placeholder rung parses.",
    ),
    InstructionSpec(
        mnemonic="EOT",
        name="End Of Transition",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=1,
        max_operands=1,
        operand_types=((BaseDataType.BOOL,),),
        is_output=True,
        notes=(
            "Returns the state of its data bit to an SFC transition that called this "
            "routine. It reads the bit rather than writing it, so nothing is "
            "destructive. Only meaningful in a routine an SFC calls; this platform "
            "generates ladder, so it is catalogued to be read rather than emitted."
        ),
    ),
    InstructionSpec(
        mnemonic="SFR",
        name="SFC Reset",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=2,
        max_operands=2,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Routine name then step name, neither of them a tag — the parser has no "
            "declaration to resolve them against and will report them as unresolved "
            "references. It writes SFC execution state rather than a named operand, "
            "so no position is destructive. Human review because restarting a "
            "sequence at a chosen step mid-cycle can command the next motion from a "
            "state the machine is not actually in."
        ),
    ),
    InstructionSpec(
        mnemonic="SFP",
        name="SFC Pause",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=2,
        max_operands=2,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Routine name then target state. Same unresolved-reference consequence "
            "as SFR, and the same reason for review: pausing a sequence leaves "
            "outputs energised in whatever state the active step set them to, which "
            "is not the same as stopping the machine."
        ),
    ),
)

#: The catalogue, keyed by canonical upper-case mnemonic. Restricted mnemonics
#: are deliberately absent; see :data:`RESTRICTED_INSTRUCTIONS`.
INSTRUCTIONS: Final[dict[str, InstructionSpec]] = {spec.mnemonic: spec for spec in _SPECS}

if len(INSTRUCTIONS) != len(_SPECS):  # pragma: no cover - guards a typo at import time
    raise ValueError("Duplicate mnemonic in the instruction catalogue.")


# ---------------------------------------------------------------------------
# Recognised, and refused
# ---------------------------------------------------------------------------
# Every reason is written to be shown to an engineer as-is: "this instruction is
# not supported" tells them nothing they can act on.

_MOTION_REASON: Final[str] = (
    "Motion instruction. Commanding a servo axis is a restricted domain: a "
    "plausible-looking generated move is a machine moving at the wrong time, in "
    "the wrong direction, or into a person, and reviewing it competently is a "
    "motion-engineering qualification rather than general controls experience. "
    "Read and reported; never generated without a qualified workflow."
)

_SAFETY_REASON: Final[str] = (
    "GuardLogix safety instruction. Safety logic is certified as a system: its "
    "content, its review and its validation are all part of a functional-safety "
    "process this platform is not a participant in. Generating one would put "
    "unreviewed logic behind a safety label, which is worse than generating "
    "nothing. Read and reported; never generated."
)

#: Mnemonics this platform recognises and refuses to emit, mapped to the reason
#: shown to the engineer. Parsing, reporting and citing them is expected; only
#: generation is refused.
RESTRICTED_INSTRUCTIONS: Final[dict[str, str]] = {
    # -- motion -------------------------------------------------------------
    "MAM": f"Motion Axis Move. {_MOTION_REASON}",
    "MAJ": f"Motion Axis Jog. {_MOTION_REASON}",
    "MAS": (
        "Motion Axis Stop. Refused even though it stops rather than starts an "
        "axis: a generated stop that decelerates on the wrong profile, or that a "
        f"reviewer trusts as a safety function, is its own hazard. {_MOTION_REASON}"
    ),
    "MSO": f"Motion Servo On. {_MOTION_REASON}",
    "MSF": f"Motion Servo Off. {_MOTION_REASON}",
    "MAH": f"Motion Axis Home. {_MOTION_REASON}",
    "MCD": f"Motion Change Dynamics. {_MOTION_REASON}",
    "MCLM": f"Motion Coordinated Linear Move. {_MOTION_REASON}",
    "MAPC": f"Motion Axis Position Cam. {_MOTION_REASON}",
    # -- messaging ----------------------------------------------------------
    "MSG": (
        "Message. It reaches another device: a MSG reads or writes data in a "
        "controller, drive or gateway this platform has never seen, over a path "
        "it cannot verify from an offline export. A generated MSG can write to "
        "the wrong node, and the failure shows up on equipment nobody was "
        "watching. Read and reported; never generated."
    ),
}

#: GuardLogix safety instruction mnemonics. **Not claimed to be exhaustive**,
#: and nothing depends on it being so: a safety-rated controller or a safety
#: program puts the project into manual-review-only mode by classification long
#: before a mnemonic is looked up, and a safety instruction that is missing from
#: this list is still unknown to the catalogue and so still never generated.
#: The list is here so that one met inside an otherwise standard project is
#: named and refused rather than reported as merely unrecognised.
_SAFETY_MNEMONICS: Final[tuple[str, ...]] = (
    "CROUT",
    "DCA",
    "DCM",
    "DCS",
    "DCST",
    "DCSTL",
    "DIN",
    "ENPEN",
    "EPMS",
    "ESTOP",
    "FPMS",
    "LC",
    "RIN",
    "ROUT",
    "SMAT",
    "THRS",
    "THRSE",
)

RESTRICTED_INSTRUCTIONS.update(dict.fromkeys(_SAFETY_MNEMONICS, _SAFETY_REASON))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _canonical(mnemonic: str) -> str:
    """Upper-case, whitespace-stripped. Lookups accept any case; this is the form."""
    return mnemonic.strip().upper()


def spec_for(mnemonic: str) -> InstructionSpec | None:
    """The spec for ``mnemonic``, or ``None`` if this catalogue does not describe it.

    ``None`` covers three different situations, and the caller has to tell them
    apart: a restricted instruction (:func:`is_restricted`), an add-on
    instruction defined by the project rather than by Rockwell, and a mnemonic
    nobody here has heard of. All three end in the same place — the instruction
    is recorded as unsupported, by name — but only the first has a reason worth
    showing to an engineer.
    """
    return INSTRUCTIONS.get(_canonical(mnemonic))


def is_known(mnemonic: str) -> bool:
    """Whether this module can say anything at all about ``mnemonic``.

    True for catalogued instructions *and* for restricted ones: refusing to
    generate ``MAM`` is knowing what it is. False therefore means "never heard
    of it", which is the state that has to be reported rather than skipped.
    """
    canonical = _canonical(mnemonic)
    return canonical in INSTRUCTIONS or canonical in RESTRICTED_INSTRUCTIONS


def is_restricted(mnemonic: str) -> bool:
    """Whether ``mnemonic`` is recognised and refused for generation."""
    return _canonical(mnemonic) in RESTRICTED_INSTRUCTIONS


def restriction_reason(mnemonic: str) -> str:
    """Why ``mnemonic`` is refused, or ``""`` when it is not restricted.

    The text is written for an engineer to read unedited, because a refusal
    without a reason is indistinguishable from a broken tool.
    """
    return RESTRICTED_INSTRUCTIONS.get(_canonical(mnemonic), "")


def destructive_operands(mnemonic: str) -> tuple[int, ...]:
    """Operand positions ``mnemonic`` writes.

    ``()`` for an unknown mnemonic, which is **not** the claim that it writes
    nothing — an unrecognised instruction may well drive the coil two rungs
    above are also driving. Call :func:`is_known` first and report the mnemonic
    when it is not; otherwise a duplicate-destination check quietly stops
    covering everything it does not recognise.
    """
    spec = INSTRUCTIONS.get(_canonical(mnemonic))
    return spec.destructive_operand_indexes if spec is not None else ()


def supports(mnemonic: str, family: ControllerFamily) -> bool:
    """Whether ``mnemonic`` is generated for ``family``.

    False for an unknown or restricted mnemonic: neither is a supported
    instruction, and only :func:`spec_for` and :func:`is_restricted` together
    can tell a caller which of the two it has.
    """
    spec = INSTRUCTIONS.get(_canonical(mnemonic))
    if spec is None or spec.is_restricted:
        return False
    return family in spec.supported_families
