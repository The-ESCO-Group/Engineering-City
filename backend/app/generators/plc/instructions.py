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

Nothing outside this module fills the gap. ``LogicOperand.is_destination``
reads like a second opinion about what an instruction writes, and
:meth:`~app.validation.base.ValidationContext.destructive_writes` falls back to
it, but no code path in the backend ever sets it — it is declared with
``default=False`` and only ever read. So for a mnemonic this catalogue does not
describe, *nothing* records the write. That is why a stock instruction missing
from here is not a cosmetic gap: it removes the instruction from
duplicate-destination detection entirely.

What ``destructive_operand_indexes`` cannot say
-----------------------------------------------
A fixed tuple of positions is the right model for most of the instruction set
and the wrong model for some of it, and the difference has to be readable rather
than guessed at. :attr:`InstructionSpec.destination_coverage` says which case an
entry is in:

* :attr:`DestinationCoverage.COMPLETE` — the tuple names every operand written,
  and the whole of each. The great majority.
* :attr:`DestinationCoverage.PARTIAL` — the tuple names the right operands but
  not the extent of the write. ``MVM`` and ``BTD`` write only the bits their
  mask selects; ``COP``, ``CPS`` and ``FLL`` name the *first element* of a
  destination range whose length is another operand. A duplicate-destination
  check on operand text alone will neither over- nor under-report these
  reliably, and it should say so rather than answer confidently.
* :attr:`DestinationCoverage.OPERAND_DEPENDENT` — which positions are written
  depends on the operand list itself. ``SBR`` writes all of them; ``JSR`` writes
  the return parameters, which begin after the input-parameter count in operand
  1. :func:`resolved_destructive_operands` works those out from the operands, so
  a caller has something better than the empty tuple to use.

What this catalogue does not cover
----------------------------------
Stated so the boundary is a decision rather than an omission a reader has to
infer. Absent on purpose, and reported as unknown instructions when met: the
serial-port ASCII set (``ABL``, ``ACB``, ``ARD``, ``AWT`` and the rest), the
diagnostic pair ``DDT``/``FBC``, ``JXR`` (see the ``JSR`` entry), and the
legacy PLC-5/SLC instruction set beyond the ``SCL``/``SCP`` pair a migration
source needs. In each case the operand order is not held here with enough
confidence to name a destructive position, and a guessed position is a
confident wrong answer where "unknown" is an honest one.

Operand counts, and where they are deliberately loose
------------------------------------------------------
``min_operands``/``max_operands`` exist so a generated instruction with the
wrong number of operands is refused rather than exported. For a handful of
entries — ``PID`` and the alarm instructions above all — the exact arity Logix
writes in neutral text is not held here with confidence, while the position it
*writes* is. Those entries carry deliberately wide bounds and say so in their
notes: refusing a legal rung costs more than accepting an odd one, and the
destructive position is the field this module exists for.

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
recognises and refuses to emit — motion, messaging, controller configuration and
the GuardLogix safety instruction set. These are read and reported like anything
else; what they never do is come out of a generator without a qualified workflow
(:class:`~app.core.enums.RestrictedDomainMode`). They are deliberately absent
from :data:`INSTRUCTIONS`: a generator that only ever emits what it can look up
in the catalogue cannot emit one of these by accident, which is a stronger
guarantee than a flag somebody has to remember to check.

**No list here is claimed to be complete**, and the two that matter are backed
differently.

The safety list is not exhaustive and nothing rests on it being so. A
safety-rated controller or a safety program puts the whole project into
manual-review-only mode by classification, long before any individual mnemonic
is looked up; the list is here so that a safety instruction met inside an
otherwise standard project is named and refused rather than reported as merely
unrecognised. It cuts the other way too: ``ESTOP``, ``LC``, ``DIN`` and friends
are legal Add-On Instruction names on a standard controller, where the safety
instruction set does not exist, so a match is not by itself proof of safety
logic — the refusal text says as much.

The motion list *is* backed, by :data:`MOTION_MNEMONIC_PATTERN`. The Logix
motion set runs to roughly fifty mnemonics and grows with firmware, and a
hand-kept list that falls behind fails silently: an unlisted motion instruction
comes back "never heard of it" and is reported as a capability gap rather than
as a refused restricted domain, with no human decision required. So an
``M``-prefixed mnemonic shaped like a motion instruction, which this catalogue
does not otherwise describe, is treated as motion by pattern. That fails the
other way — towards a named refusal a human can overrule — which is the right
direction for a servo axis. The explicit list is still kept, and kept long,
because it is what :func:`restriction_reason` names the instruction from and
what a caller enumerating :data:`RESTRICTED_INSTRUCTIONS` shows a reader.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.enums import BaseDataType, ControllerFamily, InstructionCategory, OperandKind

__all__ = [
    "INSTRUCTIONS",
    "LEGACY_FAMILIES",
    "LOGIX_FAMILIES",
    "MOTION_MNEMONIC_PATTERN",
    "RESTRICTED_INSTRUCTIONS",
    "TYPED_OPERAND_KINDS",
    "VARIADIC_OPERAND_LIMIT",
    "DestinationCoverage",
    "InstructionSpec",
    "destination_coverage",
    "destructive_operands",
    "is_known",
    "is_restricted",
    "resolved_destructive_operands",
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


class DestinationCoverage(StrEnum):
    """How much of what an instruction writes its destructive tuple can say.

    Kept as data rather than as prose in ``notes`` because a caller has to
    *branch* on it: a duplicate-destination check that treats a ``COP`` the same
    way it treats a ``MOV`` reports a clean result it has not earned, and one
    that treats a ``JSR`` as writing nothing loses the return parameters. Prose
    the caller cannot read is not a mitigation.
    """

    #: The tuple names every operand written, and the whole of each.
    COMPLETE = "complete"
    #: The right operands, but not the extent of the write — a masked field
    #: (``MVM``, ``BTD``) or the first element of a range (``COP``, ``FLL``).
    PARTIAL = "partial"
    #: Which positions are written depends on the operand list itself
    #: (``JSR``, ``SBR``). :func:`resolved_destructive_operands` works them out.
    OPERAND_DEPENDENT = "operand_dependent"


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

    #: Whether the tuple above is the whole truth. See
    #: :class:`DestinationCoverage`; anything other than ``COMPLETE`` means a
    #: caller relying on the tuple alone is under-reporting.
    destination_coverage: DestinationCoverage = DestinationCoverage.COMPLETE

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
        return is_restricted(self.mnemonic)

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
        supported_families=frozenset({ControllerFamily.SLC500}),
        notes=(
            "SLC 500 form: source, rate, offset, destination. Not a Logix ladder "
            "instruction — Logix scales in an FBD block or in arithmetic — so this "
            "entry exists to read and report a migration source, and supports() "
            "answers False for every Logix family. SLC 500 only, and not PLC-5: the "
            "PLC-5 family provides SCP and, as far as this catalogue can establish, "
            "no SCL. SCL is met most often on MicroLogix, which ControllerFamily has "
            "no member for, so a MicroLogix source has no family here to claim. The "
            "operand order is stated from the legacy manuals and is the part of this "
            "entry held with least confidence, so no position carries a type."
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
    InstructionSpec(
        mnemonic="XPY",
        name="X To The Power Of Y",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes="Source A raised to source B, into destination. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="LN",
        name="Natural Log",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination. Untyped, as ADD. Whether the source can reach zero "
            "or go negative is a question about the surrounding logic; this "
            "catalogue does not answer it."
        ),
    ),
    InstructionSpec(
        mnemonic="LOG",
        name="Log Base 10",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as ADD; the domain caveat of LN applies.",
    ),
    InstructionSpec(
        mnemonic="SIN",
        name="Sine",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source in radians, destination. Untyped, as ADD. Nothing here checks the "
            "unit: a source in degrees is a real defect this catalogue cannot see."
        ),
    ),
    InstructionSpec(
        mnemonic="COS",
        name="Cosine",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source in radians, destination. Untyped, as SIN.",
    ),
    InstructionSpec(
        mnemonic="TAN",
        name="Tangent",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source in radians, destination. Untyped, as SIN.",
    ),
    InstructionSpec(
        mnemonic="ASN",
        name="Arc Sine",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination in radians. Untyped, as SIN.",
    ),
    InstructionSpec(
        mnemonic="ACS",
        name="Arc Cosine",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination in radians. Untyped, as SIN.",
    ),
    InstructionSpec(
        mnemonic="ATN",
        name="Arc Tangent",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination in radians. Untyped, as SIN.",
    ),
    InstructionSpec(
        mnemonic="DEG",
        name="Radians To Degrees",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source in radians, destination in degrees. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="RAD",
        name="Degrees To Radians",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source in degrees, destination in radians. Untyped, as ADD.",
    ),
    InstructionSpec(
        mnemonic="TRN",
        name="Truncate",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination. Removes the fractional part without rounding, "
            "which is not what an engineer reading 'truncate' next to a scaled "
            "analogue value always expects. Untyped, as ADD."
        ),
    ),
    InstructionSpec(
        mnemonic="TOD",
        name="Convert To BCD",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes=(
            "Source, destination. The destination holds a BCD bit pattern rather than "
            "the number it displays, so arithmetic on it afterwards is a defect. "
            "Untyped: the conversion is defined over the integer types."
        ),
    ),
    InstructionSpec(
        mnemonic="FRD",
        name="Convert From BCD",
        category=InstructionCategory.COMPUTE_MATH,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source BCD pattern, destination integer. Untyped, as TOD.",
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
        destination_coverage=DestinationCoverage.PARTIAL,
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
        destination_coverage=DestinationCoverage.PARTIAL,
        is_output=True,
        notes=(
            "Source, destination, length — destination is the *middle* operand, and "
            "it names the *first element of a range*: COP writes operand 2 "
            "consecutive elements starting there, so 'COP(Master[0],Active[0],20)' "
            "writes Active[0] through Active[19] and the destructive tuple names only "
            "the first of them. A duplicate-destination check that compares operand "
            "text alone will not see the collision with a MOV into Active[5]; the "
            "coverage is PARTIAL for exactly that reason. Length counts destination "
            "elements, not bytes; COP does no type conversion and will happily "
            "reinterpret the bits, which is why a generated COP between unlike types "
            "is worth a look."
        ),
    ),
    InstructionSpec(
        mnemonic="CPS",
        name="Synchronous Copy File",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(1,),
        destination_coverage=DestinationCoverage.PARTIAL,
        is_output=True,
        notes=(
            "Source, destination, length, as COP — including that the destination is "
            "the first element of a range operand 2 elements long, which the "
            "destructive tuple cannot state. The difference from COP is that the copy "
            "cannot be interrupted by a higher-priority task or an I/O update, which "
            "makes it the right instruction for a coherent snapshot and a scan-time "
            "cost everywhere else."
        ),
    ),
    InstructionSpec(
        mnemonic="FLL",
        name="Fill File",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(1,),
        destination_coverage=DestinationCoverage.PARTIAL,
        is_output=True,
        notes=(
            "Source, destination, length. One value written into operand 2 "
            "consecutive elements starting at the destination, so the destructive "
            "tuple names the first element of a range exactly as COP's does. Untyped: "
            "FLL fills any numeric type and a structure alike."
        ),
    ),
    InstructionSpec(
        mnemonic="SWPB",
        name="Swap Byte",
        category=InstructionCategory.MOVE_LOGICAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source, order mode, destination. The order mode is a keyword rather than "
            "a tag or a number, so it carries no type and a validator must not report "
            "it as an unresolved reference."
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
        destination_coverage=DestinationCoverage.PARTIAL,
        is_output=True,
        notes=(
            "Source, source bit, destination, destination bit, length. The bit "
            "numbers and the length are immediate values as often as tags, so no "
            "position carries a type. Only the named bit field of the destination is "
            "written — a masked write in all but name — with the same consequence for "
            "duplicate-destination checks as MVM, which is why the coverage is PARTIAL."
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
        destination_coverage=DestinationCoverage.OPERAND_DEPENDENT,
        is_output=True,
        notes=(
            "Routine name, input parameter count, then the input parameters and the "
            "return parameters. Operand 0 is a routine reference, not a tag — the "
            "L5X parser resolves it against the routines the export declares, and a "
            "validator must not report it as an unresolved tag. The return "
            "parameters *are* written, but which operands they are depends on the "
            "count in operand 1, so no fixed index tuple can name them: the coverage "
            "is OPERAND_DEPENDENT and resolved_destructive_operands() works them out "
            "from the operand list. JXR, which the parser also treats as calling a "
            "routine, is deliberately not catalogued: external routines are outside "
            "what this platform generates, so it comes back unknown and is reported "
            "as unsupported."
        ),
    ),
    InstructionSpec(
        mnemonic="SBR",
        name="Subroutine",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=VARIADIC_OPERAND_LIMIT,
        destination_coverage=DestinationCoverage.OPERAND_DEPENDENT,
        notes=(
            "The parameter list a called routine receives, first instruction of the "
            "first rung. *Every* operand is written when the routine is called, and "
            "a fixed index tuple cannot express 'all of them' for a variadic "
            "instruction. The coverage is OPERAND_DEPENDENT rather than an empty "
            "tuple a caller would read as 'writes nothing', and "
            "resolved_destructive_operands() answers with every position the "
            "instruction was given."
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
    InstructionSpec(
        mnemonic="FOR",
        name="For",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(1,),
        is_output=True,
        requires_human_review=True,
        notes=(
            "Routine name, index, initial value, terminal value, step size. Operand 0 "
            "names a routine rather than a tag, with the same unresolved-reference "
            "consequence as JSR's. The index is written on every pass, which is the "
            "destructive position. Human review because the whole loop runs inside a "
            "single scan: a terminal value or step that the surrounding logic can "
            "make large, zero or the wrong sign turns one rung into a watchdog fault, "
            "and nothing in the rung itself shows it."
        ),
    ),
    InstructionSpec(
        mnemonic="BRK",
        name="Break",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Leaves the FOR loop that called this routine. Writes nothing. Human "
            "review for the reason TND has: everything after it in the routine is "
            "skipped for that pass, and the outputs it would have written stay as "
            "they were."
        ),
    ),
    InstructionSpec(
        mnemonic="UID",
        name="User Interrupt Disable",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        is_output=True,
        requires_human_review=True,
        notes=(
            "Disables the execution of periodic and event tasks until a UIE re-enables "
            "them. Writes nothing, and is still one of the most consequential "
            "instructions in the set: a UID whose UIE is skipped — by a JMP, by a TND, "
            "by a fault — leaves every periodic task in the controller stopped while "
            "the continuous task carries on looking healthy. Every use goes in front "
            "of a human, and it is reviewed with its UIE rather than on its own."
        ),
    ),
    InstructionSpec(
        mnemonic="UIE",
        name="User Interrupt Enable",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=0,
        max_operands=0,
        is_output=True,
        notes=(
            "Re-enables the tasks a UID disabled. Writes nothing. Harmless alone, "
            "which is why the review flag sits on UID; what matters is that every "
            "path out of the protected region reaches one of these."
        ),
    ),
    InstructionSpec(
        mnemonic="EVENT",
        name="Trigger Event Task",
        category=InstructionCategory.PROGRAM_CONTROL,
        min_operands=1,
        max_operands=1,
        is_output=True,
        notes=(
            "Operand 0 names a task, not a tag: no <Tags> entry declares it, so the "
            "parser reports it unresolved and a validator should suppress that here "
            "rather than invent a tag. It writes no operand — what it does is run "
            "another task, so the effect of this rung is entirely outside the routine "
            "it is written in."
        ),
    ),
    # -- input/output and controller data ------------------------------------
    InstructionSpec(
        mnemonic="GSV",
        name="Get System Value",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        notes=(
            "Class name, instance name, attribute name, destination. Only operand 3 "
            "is a tag: the first three name a CIP object class, an instance and an "
            "attribute, and the instance is routinely written empty "
            "('GSV(WallClockTime,,LocalDateTime,Now[0])'). A validator must not "
            "report positions 0..2 as unresolved tags. Reading controller state is "
            "not restricted; writing it is — see SSV."
        ),
    ),
    InstructionSpec(
        mnemonic="IOT",
        name="Immediate Output",
        category=InstructionCategory.SPECIAL,
        min_operands=1,
        max_operands=1,
        is_output=True,
        notes=(
            "Sends the named output tag to its module now instead of at the end of "
            "the task. The operand is a source, not a destination — IOT writes no "
            "tag — but the rung's effect is felt on the wire mid-scan, which is the "
            "part a reader misses."
        ),
    ),
    # -- array (file) and shift ----------------------------------------------
    InstructionSpec(
        mnemonic="BSL",
        name="Bit Shift Left",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(0, 1),
        operand_types=((), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "Array, control, source bit, length. Both the array and the CONTROL "
            "structure are written every time the rung goes true. Untyped at the "
            "array position: a bit-shift register is a DINT array as often as it is "
            "anything else."
        ),
    ),
    InstructionSpec(
        mnemonic="BSR",
        name="Bit Shift Right",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(0, 1),
        operand_types=((), (BaseDataType.CONTROL,)),
        is_output=True,
        notes="Array, control, source bit, length. Writes both, as BSL.",
    ),
    InstructionSpec(
        mnemonic="FFL",
        name="FIFO Load",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "Source, FIFO, control, length, position. The FIFO array and the CONTROL "
            "structure are both written. FFL and its FFU share the control tag by "
            "design, so the pair is not a duplicate destination — a "
            "duplicate-destination check has to allow that combination the way it "
            "allows CTU with CTD."
        ),
    ),
    InstructionSpec(
        mnemonic="FFU",
        name="FIFO Unload",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(0, 1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "FIFO, destination, control, length, position. Three written positions: "
            "the unloaded value goes to the destination, the CONTROL is updated, and "
            "the FIFO array itself is written because the remaining elements shift "
            "down. Pairs with FFL."
        ),
    ),
    InstructionSpec(
        mnemonic="LFL",
        name="LIFO Load",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes="Source, LIFO, control, length, position. Writes both, as FFL.",
    ),
    InstructionSpec(
        mnemonic="LFU",
        name="LIFO Unload",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(0, 1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "LIFO, destination, control, length, position. Writes all three, as FFU; "
            "the difference from FFU is only which end of the stack is unloaded."
        ),
    ),
    InstructionSpec(
        mnemonic="SQI",
        name="Sequencer Input",
        category=InstructionCategory.SPECIAL,
        min_operands=6,
        max_operands=6,
        destructive_operand_indexes=(3,),
        operand_types=((), (), (), (BaseDataType.CONTROL,)),
        is_output=False,
        requires_human_review=True,
        notes=(
            "Array, mask, source, control, length, position. An input instruction: it "
            "compares rather than commands, and the only thing it writes is the "
            "CONTROL structure. Reviewed with the SQO it belongs to — a sequencer "
            "whose input and output steps disagree drives the machine out of step, "
            "and neither rung shows it alone."
        ),
    ),
    InstructionSpec(
        mnemonic="SQO",
        name="Sequencer Output",
        category=InstructionCategory.SPECIAL,
        min_operands=6,
        max_operands=6,
        destructive_operand_indexes=(2, 3),
        operand_types=((), (), (), (BaseDataType.CONTROL,)),
        is_output=True,
        requires_human_review=True,
        notes=(
            "Array, mask, destination, control, length, position. Writes the "
            "destination — through the mask, so only the masked bits of it — and the "
            "CONTROL. The position operand mirrors the control's .POS and is not "
            "recorded as a separate destination. Human review without exception: an "
            "SQO drives real outputs from a step table, and a table one step out of "
            "phase with the machine is a plausible-looking rung commanding the wrong "
            "actuator."
        ),
    ),
    InstructionSpec(
        mnemonic="SQL",
        name="Sequencer Load",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(0, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        requires_human_review=True,
        notes=(
            "Array, source, control, length, position. Loads the source into the step "
            "table, so the array and the CONTROL are written. Reviewed with its SQI "
            "and SQO: this is the instruction that edits the step table the other two "
            "are driven from."
        ),
    ),
    InstructionSpec(
        mnemonic="AVE",
        name="File Average",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "Array, destination, control, length, position. Writes the destination "
            "and the CONTROL. Runs over the whole array in one scan unless the "
            "control's mode says otherwise, which is a scan-time cost worth knowing "
            "about on a large array."
        ),
    ),
    InstructionSpec(
        mnemonic="STD",
        name="File Standard Deviation",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(1, 2),
        operand_types=((), (), (BaseDataType.CONTROL,)),
        is_output=True,
        notes="Array, destination, control, length, position. Writes both, as AVE.",
    ),
    InstructionSpec(
        mnemonic="SRT",
        name="File Sort",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(0, 1),
        operand_types=((), (BaseDataType.CONTROL,)),
        is_output=True,
        notes=(
            "Array, control, length, position. Sorts in place, so the array itself is "
            "the destination along with the CONTROL — which is the reading mistake "
            "this entry exists to prevent: nothing in the operand list looks like a "
            "destination."
        ),
    ),
    InstructionSpec(
        mnemonic="SIZE",
        name="Size In Elements",
        category=InstructionCategory.SPECIAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source array, dimension to vary, size. Only the size is written. Untyped: "
            "the dimension is an immediate value as often as a tag."
        ),
    ),
    InstructionSpec(
        mnemonic="FAL",
        name="File Arithmetic And Logic",
        category=InstructionCategory.SPECIAL,
        min_operands=6,
        max_operands=6,
        destructive_operand_indexes=(0, 4),
        destination_coverage=DestinationCoverage.PARTIAL,
        operand_types=((BaseDataType.CONTROL,),),
        is_output=True,
        notes=(
            "Control, length, position, mode, destination, expression. Writes the "
            "CONTROL and the destination. The destination is an array reference as "
            "often as a scalar, so like COP it can name the start of a range — but "
            "unlike COP the extent is governed by the length operand and the mode "
            "together, which this catalogue does not decode. The expression is one "
            "verbatim operand; the tags it reads are not positional."
        ),
    ),
    InstructionSpec(
        mnemonic="FSC",
        name="File Search And Compare",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=5,
        destructive_operand_indexes=(0,),
        operand_types=((BaseDataType.CONTROL,),),
        is_output=True,
        notes=(
            "Control, length, position, mode, expression. Only the CONTROL is "
            "written; the result of the search is its .FD and .POS members. The "
            "expression is one verbatim operand, as FAL's."
        ),
    ),
    InstructionSpec(
        mnemonic="PID",
        name="Proportional Integral Derivative",
        category=InstructionCategory.SPECIAL,
        min_operands=5,
        max_operands=10,
        destructive_operand_indexes=(0, 3),
        is_output=True,
        requires_human_review=True,
        notes=(
            "PID structure, process variable, tieback, control variable, master loop, "
            "in-hold bit, in-hold value, and the setpoint/process/output fields the "
            "faceplate displays. Two written positions and both matter: the PID "
            "structure itself and, above all, the control variable — a second rung "
            "writing that tag is a loop fighting whatever else drives the valve. The "
            "operand bounds are deliberately wide: the leading operands are stable "
            "across every form of this instruction seen here and the trailing display "
            "fields are not held with confidence, and refusing a legal PID would cost "
            "more than accepting an odd one. Human review because tuning, direction "
            "and the manual/auto path are engineering decisions no catalogue makes."
        ),
    ),
    # -- alarms ---------------------------------------------------------------
    InstructionSpec(
        mnemonic="ALMD",
        name="Digital Alarm",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=6,
        destructive_operand_indexes=(0,),
        is_output=True,
        notes=(
            "Alarm structure, input condition, then the program acknowledge, reset, "
            "disable and enable bits. Operand 0 is the ALMD structure and the "
            "instruction writes it; everything after it is read. The bounds are wide "
            "on purpose — the trailing program-control operands are not held with "
            "confidence here, while operand 0 is — see the module docstring."
        ),
    ),
    InstructionSpec(
        mnemonic="ALMA",
        name="Analog Alarm",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=8,
        destructive_operand_indexes=(0,),
        is_output=True,
        notes=(
            "Alarm structure then the analogue input and the program-control bits. "
            "Operand 0 is the ALMA structure and is written; the rest are read. "
            "Bounds wide for the same reason as ALMD's. The limits an ALMA alarms on "
            "live in the structure rather than in the operand list, so nothing "
            "positional here says what it trips at."
        ),
    ),
    # -- ASCII string --------------------------------------------------------
    InstructionSpec(
        mnemonic="CONCAT",
        name="String Concatenate",
        category=InstructionCategory.SPECIAL,
        min_operands=3,
        max_operands=3,
        destructive_operand_indexes=(2,),
        is_output=True,
        notes=(
            "Source A, source B, destination. Untyped: STRING and the user-defined "
            "string types are both legal and the catalogue does not distinguish them."
        ),
    ),
    InstructionSpec(
        mnemonic="MID",
        name="Middle String",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        notes="Source, quantity, start, destination. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="INSERT",
        name="Insert String",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        notes="Source A, source B, start, destination. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="DELETE",
        name="Delete String",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        notes="Source, quantity, start, destination. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="FIND",
        name="Find String",
        category=InstructionCategory.SPECIAL,
        min_operands=4,
        max_operands=4,
        destructive_operand_indexes=(3,),
        is_output=True,
        notes=(
            "Source, search, start, result. The result is a position rather than a "
            "string, and it is the only written operand."
        ),
    ),
    InstructionSpec(
        mnemonic="LEN",
        name="String Length",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source string, destination length. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="UPPER",
        name="Upper Case",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="LOWER",
        name="Lower Case",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source, destination. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="STOD",
        name="String To DINT",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source string, destination integer. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="STOR",
        name="String To REAL",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source string, destination real. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="DTOS",
        name="DINT To String",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source integer, destination string. Untyped, as CONCAT.",
    ),
    InstructionSpec(
        mnemonic="RTOS",
        name="REAL To String",
        category=InstructionCategory.SPECIAL,
        min_operands=2,
        max_operands=2,
        destructive_operand_indexes=(1,),
        is_output=True,
        notes="Source real, destination string. Untyped, as CONCAT.",
    ),
)

#: The catalogue, keyed by canonical upper-case mnemonic. Restricted mnemonics
#: are deliberately absent; see :data:`RESTRICTED_INSTRUCTIONS`.
INSTRUCTIONS: Final[dict[str, InstructionSpec]] = {spec.mnemonic: spec for spec in _SPECS}

if len(INSTRUCTIONS) != len(_SPECS):  # pragma: no cover - guards a typo at import time
    raise ValueError("Duplicate mnemonic in the instruction catalogue.")

#: The entries :func:`resolved_destructive_operands` knows how to work out. A
#: third OPERAND_DEPENDENT entry added without a resolver would fall through to
#: an empty tuple and silently write nothing, which is the failure this whole
#: field exists to stop, so it is refused at import instead.
_OPERAND_DEPENDENT_RESOLVED: Final[frozenset[str]] = frozenset({"JSR", "SBR"})

_declared_operand_dependent = {
    spec.mnemonic
    for spec in _SPECS
    if spec.destination_coverage is DestinationCoverage.OPERAND_DEPENDENT
}
if _declared_operand_dependent != _OPERAND_DEPENDENT_RESOLVED:  # pragma: no cover - import guard
    raise ValueError(
        "Every OPERAND_DEPENDENT entry needs a resolver in "
        "resolved_destructive_operands(): "
        f"{sorted(_declared_operand_dependent ^ _OPERAND_DEPENDENT_RESOLVED)}"
    )


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
    "nothing. Read and reported; never generated. Note that several of these "
    "mnemonics — ESTOP, LC, DIN, RIN, ROUT among them — are also legal Add-On "
    "Instruction names, and the GuardLogix safety instruction set does not exist "
    "on a standard controller: if this project declares an Add-On Instruction by "
    "this name, this is a name collision and not safety logic."
)

#: The motion mnemonics this catalogue can put a name to, mapped to that name.
#: **Not exhaustive** — the Logix motion set is around fifty mnemonics and grows
#: with firmware — which is why :data:`MOTION_MNEMONIC_PATTERN` backs it up. What
#: this table adds over the pattern is the instruction's name in the refusal
#: text, and an enumerable list for a caller that wants to show one.
_MOTION_INSTRUCTION_NAMES: Final[dict[str, str]] = {
    "MAAT": "Motion Apply Axis Tuning",
    "MAFR": "Motion Axis Fault Reset",
    "MAG": "Motion Axis Gear",
    "MAH": "Motion Axis Home",
    "MAHD": "Motion Apply Hookup Diagnostics",
    "MAJ": "Motion Axis Jog",
    "MAM": "Motion Axis Move",
    "MAOC": "Motion Arm Output Cam",
    "MAPC": "Motion Axis Position Cam",
    "MAR": "Motion Arm Registration",
    "MAS": "Motion Axis Stop",
    "MASD": "Motion Axis Shutdown",
    "MASR": "Motion Axis Shutdown Reset",
    "MATC": "Motion Axis Time Cam",
    "MAW": "Motion Arm Watch",
    "MCCD": "Motion Coordinated Change Dynamics",
    "MCCM": "Motion Coordinated Circular Move",
    "MCCP": "Motion Calculate Cam Profile",
    "MCD": "Motion Change Dynamics",
    "MCLM": "Motion Coordinated Linear Move",
    "MCS": "Motion Coordinated Stop",
    "MCSD": "Motion Coordinated Shutdown",
    "MCSR": "Motion Coordinated Shutdown Reset",
    "MCSV": "Motion Calculate Slave Values",
    "MCT": "Motion Coordinated Transform",
    "MCTP": "Motion Calculate Transform Position",
    "MDF": "Motion Direct Drive Off",
    "MDO": "Motion Direct Drive On",
    "MDOC": "Motion Disarm Output Cam",
    "MDR": "Motion Disarm Registration",
    "MDW": "Motion Disarm Watch",
    "MGS": "Motion Group Stop",
    "MGSD": "Motion Group Shutdown",
    "MGSP": "Motion Group Strobe Position",
    "MGSR": "Motion Group Shutdown Reset",
    "MRAT": "Motion Run Axis Tuning",
    "MRHD": "Motion Run Hookup Diagnostics",
    "MRP": "Motion Redefine Position",
    "MSF": "Motion Servo Off",
    "MSO": "Motion Servo On",
}

#: Reasons that replace the generic motion text where the instruction deserves
#: its own sentence.
_MOTION_SPECIFIC_REASONS: Final[dict[str, str]] = {
    "MAS": (
        "Motion Axis Stop. Refused even though it stops rather than starts an "
        "axis: a generated stop that decelerates on the wrong profile, or that a "
        f"reviewer trusts as a safety function, is its own hazard. {_MOTION_REASON}"
    ),
    "MDO": (
        "Motion Direct Drive On. It commands a drive output directly, with no "
        "profile, no deceleration and no end point — the axis moves for as long "
        f"as the rung is true. {_MOTION_REASON}"
    ),
}

#: Shape of a Logix motion mnemonic: ``M``, one of the six second letters the
#: motion set uses (Axis, Coordinated/Calculate, Direct/Disarm, Group, Run/
#: Redefine, Servo) and one to three more letters. Deliberately narrow — three
#: to five characters, letters only — so it does not swallow an Add-On
#: Instruction, whose names are longer and usually carry an underscore or a
#: digit. It is consulted **after** :data:`INSTRUCTIONS`, so ordinary mnemonics
#: of this shape (``MCR``) and named restrictions (``MSG``) are answered first.
#:
#: A short customer AOI that does match — ``MCP``, say — is reported as a
#: restricted motion instruction rather than as an unknown one. That is a
#: refusal a human reads and can overrule; the alternative, an unlisted motion
#: instruction reported as a mere capability gap with no human decision
#: required, is a servo axis nobody was asked about.
MOTION_MNEMONIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"M[ACDGRS][A-Z]{1,3}")

_MOTION_PATTERN_REASON: Final[str] = (
    "Recognised as a motion instruction by its mnemonic rather than by name: "
    "this catalogue does not hold an entry for it, and the Logix motion set is "
    "larger than any list here is kept up to date with. Treating an unrecognised "
    f"M-prefixed axis mnemonic as motion is deliberate. {_MOTION_REASON} If this "
    "is an Add-On Instruction of the customer's that happens to share the shape "
    "of a motion mnemonic, say so and the refusal can be overruled."
)

_CONTROLLER_CONFIGURATION_REASON: Final[str] = (
    "It writes the controller's own configuration rather than process data. "
    "The CIP objects reachable this way include a task's watchdog and its "
    "DisableUpdateOutputs flag, a program's Disabled flag, a module's mode, the "
    "controller's time synchronisation and a MESSAGE object's connection path — "
    "so a single rung can stop a program executing, extend a watchdog past the "
    "point it protects anything, or redirect a message to another node. None of "
    "that is visible in the tag list or the I/O schedule, and none of it is a "
    "decision a generator makes. Read and reported; never generated."
)

#: Mnemonics this platform recognises and refuses to emit, mapped to the reason
#: shown to the engineer. Parsing, reporting and citing them is expected; only
#: generation is refused.
RESTRICTED_INSTRUCTIONS: Final[dict[str, str]] = {
    # -- messaging ----------------------------------------------------------
    "MSG": (
        "Message. It reaches another device: a MSG reads or writes data in a "
        "controller, drive or gateway this platform has never seen, over a path "
        "it cannot verify from an offline export. A generated MSG can write to "
        "the wrong node, and the failure shows up on equipment nobody was "
        "watching. Read and reported; never generated."
    ),
    # -- controller configuration -------------------------------------------
    "SSV": f"Set System Value. {_CONTROLLER_CONFIGURATION_REASON}",
}

RESTRICTED_INSTRUCTIONS.update(
    {
        mnemonic: _MOTION_SPECIFIC_REASONS.get(mnemonic, f"{name}. {_MOTION_REASON}")
        for mnemonic, name in _MOTION_INSTRUCTION_NAMES.items()
    }
)

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
    return canonical in INSTRUCTIONS or is_restricted(canonical)


def _is_motion_by_pattern(canonical: str) -> bool:
    """Whether an uncatalogued mnemonic is shaped like a motion instruction.

    Only asked once the named tables have both said no, so it can never
    reinterpret ``MCR`` or ``MSG``. See :data:`MOTION_MNEMONIC_PATTERN` for why
    the answer errs towards refusing.
    """
    return (
        canonical not in INSTRUCTIONS
        and canonical not in RESTRICTED_INSTRUCTIONS
        and MOTION_MNEMONIC_PATTERN.fullmatch(canonical) is not None
    )


def is_restricted(mnemonic: str) -> bool:
    """Whether ``mnemonic`` is recognised and refused for generation.

    Answers True for a mnemonic this module holds no entry for but which is
    shaped like a Logix motion instruction; :data:`RESTRICTED_INSTRUCTIONS`
    therefore under-states what this function refuses, and a caller enumerating
    the table is showing a reader a list rather than the rule.
    """
    canonical = _canonical(mnemonic)
    return canonical in RESTRICTED_INSTRUCTIONS or _is_motion_by_pattern(canonical)


def restriction_reason(mnemonic: str) -> str:
    """Why ``mnemonic`` is refused, or ``""`` when it is not restricted.

    The text is written for an engineer to read unedited, because a refusal
    without a reason is indistinguishable from a broken tool. A mnemonic caught
    by the motion pattern rather than by name gets a reason that says so, so the
    engineer is told this was a judgement about the shape of the name and not a
    fact from a table.
    """
    canonical = _canonical(mnemonic)
    named = RESTRICTED_INSTRUCTIONS.get(canonical)
    if named is not None:
        return named
    if _is_motion_by_pattern(canonical):
        return f"{canonical}. {_MOTION_PATTERN_REASON}"
    return ""


def destructive_operands(mnemonic: str) -> tuple[int, ...]:
    """Operand positions ``mnemonic`` writes.

    ``()`` for an unknown mnemonic, which is **not** the claim that it writes
    nothing — an unrecognised instruction may well drive the coil two rungs
    above are also driving. Call :func:`is_known` first and report the mnemonic
    when it is not; otherwise a duplicate-destination check quietly stops
    covering everything it does not recognise.

    ``()`` is also the answer for ``JSR`` and ``SBR``, whose written positions
    depend on their operands. Check :func:`destination_coverage`, or call
    :func:`resolved_destructive_operands` with the operand list and get the
    right answer without having to.
    """
    spec = INSTRUCTIONS.get(_canonical(mnemonic))
    return spec.destructive_operand_indexes if spec is not None else ()


def destination_coverage(mnemonic: str) -> DestinationCoverage | None:
    """How far :func:`destructive_operands` can be trusted for ``mnemonic``.

    ``None`` for a mnemonic this catalogue does not describe — the case where
    nothing at all is known, which is not the same as ``COMPLETE``.
    """
    spec = INSTRUCTIONS.get(_canonical(mnemonic))
    return spec.destination_coverage if spec is not None else None


def _jsr_return_parameter_indexes(operands: Sequence[str]) -> tuple[int, ...]:
    """``JSR`` return-parameter positions, read off the operand list.

    ``JSR(Routine, N, in_1..in_N, out_1..out_M)``: everything after the ``N``
    input parameters is passed back and is written by the call. When operand 1
    is not a plain non-negative integer — a tag holding the count, an unreadable
    export, a truncated operand list — nothing is claimed. Guessing there would
    name the *input* parameters as destinations, which is a confident error
    against correct logic in the direction this module never goes.
    """
    if len(operands) < 2:
        return ()
    count = operands[1].strip()
    if not count.isdigit():
        return ()
    first_return = 2 + int(count)
    return tuple(range(first_return, len(operands)))


def resolved_destructive_operands(mnemonic: str, operands: Sequence[str]) -> tuple[int, ...]:
    """Operand positions ``mnemonic`` writes, given the operands it was written with.

    The same answer as :func:`destructive_operands` for every entry whose
    coverage is fixed, and the real answer for the two whose coverage is
    :attr:`DestinationCoverage.OPERAND_DEPENDENT`: ``SBR`` writes every operand
    it receives, and ``JSR`` writes the return parameters that follow its input
    list. ``()`` for an unknown mnemonic, with the same warning as
    :func:`destructive_operands`.

    ``operands`` is the operand *text* in ordinal order, which is what the
    parser records and what the count in a ``JSR`` has to be read from.
    """
    spec = INSTRUCTIONS.get(_canonical(mnemonic))
    if spec is None:
        return ()
    if spec.destination_coverage is not DestinationCoverage.OPERAND_DEPENDENT:
        return spec.destructive_operand_indexes
    if spec.mnemonic == "SBR":
        return tuple(range(len(operands)))
    if spec.mnemonic == "JSR":
        return _jsr_return_parameter_indexes(operands)
    # Unreachable while the two entries above are the only OPERAND_DEPENDENT
    # ones, and the import-time check below keeps it that way. Returning the
    # fixed tuple would quietly under-report a third one.
    raise NotImplementedError(  # pragma: no cover - guarded at import time
        f"{spec.mnemonic} is OPERAND_DEPENDENT and has no resolver."
    )


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
