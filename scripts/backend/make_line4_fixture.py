"""Generate the Line 4 Packaging Conveyor Upgrade demonstration fixture.

Everything here is invented. There is no ESCO customer data in this repository
and none may be added: the fixture exists so the whole review pipeline can be
judged against a scenario whose *correct answer is written down in advance*.

The plant is a fictional case-packaging line: four conveyors (CV401 infeed,
CV402 accumulation, CV403 divert, CV404 gravity discharge), three VFD-driven
motors (MTR401-MTR403), a pneumatic diverter (CYL401), a stop gate (CYL402) and
a two-eye accumulation zone (PE401 upstream, PE402 downstream), on a
CompactLogix 5069-L330ER with a FactoryTalk View ME terminal.

``Conveyor_Line4_Base.L5X`` is the controller **as it exists today**. The other
documents describe the upgrade. That distinction matters: an interlock the
matrix marks ``New (this upgrade)`` is *supposed* to be absent from the base
export, and reporting it as missing is a false positive.

Exactly seven issues are planted, no more:

====  ==========================================  ==============================
#     Planted issue                               Where it lives
====  ==========================================  ==============================
1     interlock documented + wired, no logic      ILK-404 / PE402_BLOCKED
2     duplicate physical I/O address              Local:1:I.Data.10, twice
3     inconsistent tag name across documents      MTR401_RUN_CMD vs
                                                  CV401_Run_Output
4     alarm trigger tag no rung ever writes       ALM_CYL402_FAIL
5     HMI binding to a tag that does not exist    Accum_Case_Count
6     ambiguous requirement, two readings         stop-gate release sentence
7     one correctly implemented device            CYL401 diverter - **no
                                                  finding may be raised**
====  ==========================================  ==============================

``fixtures/line4_packaging_conveyor/README.md`` is the contract; it states each
issue's exact location, the finding a correct review produces, its severity, and
what a correct review must **not** report. ``backend/tests/test_line4_fixture.py``
asserts those claims against these bytes.

Run from the repository root::

    python scripts/backend/make_line4_fixture.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import zipfile
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "line4_packaging_conveyor"

#: Fixed stamps. Nothing here may read the clock: a fixture that changes when it
#: is regenerated cannot be a ground truth, and the test asserts byte-equality
#: between two runs.
#: Naive on purpose: OOXML core properties carry local wall-clock stamps, and
#: python-docx and openpyxl both reject an aware datetime here.
DOC_CREATED = dt.datetime(2026, 2, 9, 8, 0, 0)
DOC_MODIFIED = dt.datetime(2026, 3, 2, 9, 0, 0)
#: ``zipfile`` stamps every member with the current local time. OOXML packages
#: are rewritten with this instead so a regenerated .docx/.xlsx is byte-stable.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

DISCLAIMER = (
    "SYNTHETIC FIXTURE. Invented content for testing the review platform. "
    "Not a real project, not a real customer, and not real plant data."
)

CONTROLLER = "Line4_Packaging"
PROCESSOR = "5069-L330ER"
PROGRAM = "Line4_Control"
HMI_SHORTCUT = "Line4"

#: The diverter confirm time. One number, quoted identically in the project
#: description, the functional description, the interlock matrix, the alarm
#: register and the L5X timer preset, because CYL401 is the false-positive
#: control and a disagreement here would turn it into an eighth issue.
DIVERTER_CONFIRM_SECONDS = "1.5"
DIVERTER_CONFIRM_MILLISECONDS = "1500"


# ---------------------------------------------------------------------------
# L5X - the existing controller export the upgrade extends
# ---------------------------------------------------------------------------
class Tag(NamedTuple):
    """One ``<Tag>`` element. ``value`` empty means no ``<Data>`` block."""

    name: str
    data_type: str
    description: str
    value: str = ""


class Rung(NamedTuple):
    comment: str
    text: str


class Routine(NamedTuple):
    name: str
    description: str
    rungs: tuple[Rung, ...]


MODULES: tuple[tuple[str, str, str], ...] = (
    ("Local", PROCESSOR, "CompactLogix 5380 controller, Line 4 packaging panel."),
    ("DI_401", "5069-IB16", "Slot 1 digital input card. All 16 channels are assigned."),
    ("DO_401", "5069-OB16", "Slot 2 digital output card. Drive run commands and solenoids."),
    ("AI_401", "5069-IF8", "Slot 3 analog input card. Spare; no channel is wired today."),
    ("AO_401", "5069-OF4", "Slot 4 analog output card. Drive speed references."),
)

CONTROLLER_TAGS: tuple[Tag, ...] = (
    Tag("ESTOP_OK", "BOOL", "Line 4 E-stop string healthy. De-energises on trip.", "0"),
    Tag("START_PB", "BOOL", "Line start pushbutton. DI_401 channel 1.", "0"),
    Tag("STOP_PB", "BOOL", "Line stop pushbutton, wired normally closed. DI_401 channel 2.", "1"),
    Tag("RESET_PB", "BOOL", "Fault reset pushbutton. DI_401 channel 3.", "0"),
    Tag("MODE_SEL_MANUAL", "BOOL", "Mode selector in the Manual position.", "0"),
    Tag("MODE_SEL_AUTO", "BOOL", "Mode selector in the Auto position.", "0"),
    Tag("PE401_BLOCKED", "BOOL", "PE401 upstream accumulation photo-eye blocked.", "0"),
    Tag("CYL401_EXT_PROX", "BOOL", "CYL401 diverter extended proximity switch.", "0"),
    Tag("CYL401_RET_PROX", "BOOL", "CYL401 diverter retracted proximity switch.", "0"),
    Tag("GD401_CLOSED", "BOOL", "Divert-zone guard door closed and latched.", "0"),
    Tag("REJECT_REQ", "BOOL", "Case reject request from the upstream checkweigher.", "0"),
    Tag("PLT_CALL", "BOOL", "Palletiser calling for product.", "0"),
    Tag("MTR401_FAULT", "BOOL", "CV401 infeed drive fault contact.", "0"),
    Tag("MTR402_FAULT", "BOOL", "CV402 accumulation drive fault contact.", "0"),
    Tag("MTR403_FAULT", "BOOL", "CV403 divert drive fault contact.", "0"),
    Tag("CYL402_OPEN_PROX", "BOOL", "CV404 stop gate open proximity switch.", "0"),
    # Planted issue 3: the I/O list calls this signal MTR401_RUN_CMD. Its two
    # siblings below keep the I/O list's names, which is what makes this one a
    # naming mismatch rather than a house style.
    Tag(
        "CV401_Run_Output",
        "BOOL",
        "CV401 infeed drive run command. Wired to DO_401 channel 0 (Local:2:O.Data.0).",
        "0",
    ),
    Tag(
        "MTR402_RUN_CMD",
        "BOOL",
        "CV402 accumulation drive run command. DO_401 channel 1.",
        "0",
    ),
    Tag("MTR403_RUN_CMD", "BOOL", "CV403 divert drive run command. DO_401 channel 2.", "0"),
    Tag("CYL401_SOL", "BOOL", "CYL401 diverter solenoid. Extends when energised.", "0"),
    Tag("CYL402_SOL", "BOOL", "CV404 stop gate solenoid. Opens the gate when energised.", "0"),
    Tag("CYL402_OPEN", "BOOL", "CV404 stop gate confirmed open. Indication only.", "0"),
    Tag("MTR401_SPEED_REF", "REAL", "CV401 drive speed reference, ft/min.", "0.0"),
    Tag("MTR402_SPEED_REF", "REAL", "CV402 drive speed reference, ft/min.", "0.0"),
    Tag("MTR403_SPEED_REF", "REAL", "CV403 drive speed reference, ft/min.", "0.0"),
    Tag("Line_Speed_SP", "REAL", "Line speed setpoint, ft/min.", "90.0"),
    Tag("LINE_PERMISSIVE", "BOOL", "Master permissive for every Line 4 conveyor.", "0"),
    Tag("LINE_RUNNING", "BOOL", "Line start command sealed in.", "0"),
    Tag("CYL401_Extend_TMR", "TIMER", "CYL401 diverter extend confirmation timer."),
    Tag("ALM_ESTOP", "BOOL", "Alarm: emergency stop actuated.", "0"),
    Tag("ALM_GD401_OPEN", "BOOL", "Alarm: divert-zone guard door open.", "0"),
    Tag("ALM_CYL401_FAIL", "BOOL", "Alarm: CYL401 diverter failed to extend.", "0"),
    Tag("ALM_MTR401_FAULT", "BOOL", "Alarm: CV401 drive fault.", "0"),
    Tag("ALM_MTR402_FAULT", "BOOL", "Alarm: CV402 drive fault.", "0"),
    Tag("ALM_MTR403_FAULT", "BOOL", "Alarm: CV403 drive fault.", "0"),
    # Planted issue 4: declared here, defined in the alarm register, bound on
    # the HMI - and no rung in this export ever writes it.
    Tag("ALM_CYL402_FAIL", "BOOL", "Alarm: CV404 stop gate failed to open.", "0"),
)

PROGRAM_TAGS: tuple[Tag, ...] = (
    Tag("ManualMode", "BOOL", "Manual mode active.", "0"),
    Tag("AutoMode", "BOOL", "Auto mode active.", "0"),
)

ROUTINES: tuple[Routine, ...] = (
    Routine(
        "MainRoutine",
        "Scheduler for the Line 4 packaging conveyors.",
        (
            Rung("Mode selection first.", "JSR(Mode_Logic,0);"),
            Rung(
                "Then the permissives, which gate every run command.",
                "JSR(Permissives,0);",
            ),
            Rung("Then the conveyor run logic.", "JSR(Conveyor_Control,0);"),
            Rung("Then the diverter.", "JSR(Diverter_Control,0);"),
            Rung("Alarms last so they reflect this scan.", "JSR(Alarms,0);"),
        ),
    ),
    Routine(
        "Mode_Logic",
        "Operating mode selection from the three-position selector.",
        (
            Rung(
                "Manual mode while the selector is in the Manual position.",
                "XIC(MODE_SEL_MANUAL)XIO(MODE_SEL_AUTO)OTE(ManualMode);",
            ),
            Rung(
                "Auto mode while the selector is in the Auto position.",
                "XIC(MODE_SEL_AUTO)XIO(MODE_SEL_MANUAL)OTE(AutoMode);",
            ),
        ),
    ),
    Routine(
        "Permissives",
        "Master line permissive and the line start/stop seal-in.",
        (
            Rung(
                "ILK-401 emergency stop, ILK-402 divert-zone guard door and ILK-407 "
                "conveyor drive faults. ESTOP_OK and GD401_CLOSED are both "
                "de-energise-to-trip, so the permissive drops on loss of either circuit.",
                "XIC(ESTOP_OK)XIC(GD401_CLOSED)"
                "XIO(MTR401_FAULT)XIO(MTR402_FAULT)XIO(MTR403_FAULT)"
                "OTE(LINE_PERMISSIVE);",
            ),
            Rung(
                "Line start/stop seal-in. STOP_PB is wired normally closed, so the "
                "seal breaks on a broken wire.",
                "XIC(LINE_PERMISSIVE)XIC(STOP_PB)"
                "[XIC(START_PB),XIC(LINE_RUNNING)]"
                "OTE(LINE_RUNNING);",
            ),
        ),
    ),
    Routine(
        "Conveyor_Control",
        "Run commands and speed references for CV401 to CV403, and the CV404 stop gate.",
        (
            Rung(
                "CV401 infeed run command.",
                "XIC(LINE_RUNNING)XIC(LINE_PERMISSIVE)XIO(MTR401_FAULT)OTE(CV401_Run_Output);",
            ),
            Rung(
                "CV402 accumulation conveyor. Runs while PE401 sees product at the zone entry.",
                "XIC(LINE_RUNNING)XIC(LINE_PERMISSIVE)XIO(MTR402_FAULT)"
                "XIC(PE401_BLOCKED)OTE(MTR402_RUN_CMD);",
            ),
            Rung(
                "CV403 divert conveyor. A latched diverter fault stops it, per ILK-403.",
                "XIC(LINE_RUNNING)XIC(LINE_PERMISSIVE)XIO(MTR403_FAULT)"
                "XIO(ALM_CYL401_FAIL)OTE(MTR403_RUN_CMD);",
            ),
            Rung(
                "All three drives take the same line speed reference.",
                "XIC(LINE_RUNNING)"
                "MOV(Line_Speed_SP,MTR401_SPEED_REF)"
                "MOV(Line_Speed_SP,MTR402_SPEED_REF)"
                "MOV(Line_Speed_SP,MTR403_SPEED_REF);",
            ),
            Rung(
                "CV404 stop gate. The gate is held open while the palletiser call is present.",
                "XIC(LINE_RUNNING)XIC(LINE_PERMISSIVE)XIC(PLT_CALL)OTE(CYL402_SOL);",
            ),
        ),
    ),
    Routine(
        "Diverter_Control",
        "CYL401 reject diverter: extend, confirm, fault and reset.",
        (
            Rung(
                "CYL401 extends on a reject request while the line is running and no "
                "diverter fault is latched.",
                "XIC(LINE_RUNNING)XIC(LINE_PERMISSIVE)XIC(REJECT_REQ)"
                "XIO(ALM_CYL401_FAIL)OTE(CYL401_SOL);",
            ),
            Rung(
                "ILK-403 the diverter must confirm extension within "
                f"{DIVERTER_CONFIRM_SECONDS} s of the solenoid being energised.",
                "XIC(CYL401_SOL)XIO(CYL401_EXT_PROX)"
                f"TON(CYL401_Extend_TMR,{DIVERTER_CONFIRM_MILLISECONDS},0);",
            ),
            Rung(
                "ILK-403 timer done latches the diverter fault, which drops the "
                "solenoid on rung 0 and stops CV403 in Conveyor_Control rung 2.",
                "XIC(CYL401_Extend_TMR.DN)OTL(ALM_CYL401_FAIL);",
            ),
            Rung(
                "ILK-403 reset. The diverter fault clears only in Manual, with the "
                "reset pushbutton, and only once the retract proximity switch "
                "confirms the diverter is home.",
                "XIC(RESET_PB)XIC(ManualMode)XIC(CYL401_RET_PROX)OTU(ALM_CYL401_FAIL);",
            ),
        ),
    ),
    Routine(
        "Alarms",
        "Alarm bits for the FactoryTalk View ME terminal.",
        (
            Rung("ILK-401 emergency stop alarm.", "XIO(ESTOP_OK)OTE(ALM_ESTOP);"),
            Rung(
                "ILK-402 divert-zone guard door alarm.",
                "XIO(GD401_CLOSED)OTE(ALM_GD401_OPEN);",
            ),
            Rung("ILK-407 CV401 drive fault alarm.", "XIC(MTR401_FAULT)OTE(ALM_MTR401_FAULT);"),
            Rung("ILK-407 CV402 drive fault alarm.", "XIC(MTR402_FAULT)OTE(ALM_MTR402_FAULT);"),
            Rung("ILK-407 CV403 drive fault alarm.", "XIC(MTR403_FAULT)OTE(ALM_MTR403_FAULT);"),
            Rung(
                "CV404 stop gate confirmed open, for indication on the overview screen.",
                "XIC(CYL402_SOL)XIC(CYL402_OPEN_PROX)OTE(CYL402_OPEN);",
            ),
        ),
    ),
)


def _tag_xml(tag: Tag, indent: str) -> str:
    radix = {"REAL": "Float", "BOOL": "Decimal"}.get(tag.data_type, "")
    radix_attribute = f' Radix="{radix}"' if radix else ""
    lines = [
        f'{indent}<Tag Name="{tag.name}" TagType="Base" DataType="{tag.data_type}"'
        f"{radix_attribute}",
        f'{indent}    Constant="false" ExternalAccess="Read/Write">',
        f"{indent}  <Description><![CDATA[{tag.description}]]></Description>",
    ]
    if tag.value:
        lines.append(
            f'{indent}  <Data Format="Decorated"><DataValue DataType="{tag.data_type}"'
            f' Radix="{radix}" Value="{tag.value}"/></Data>'
        )
    lines.append(f"{indent}</Tag>")
    return "\n".join(lines)


def _routine_xml(routine: Routine) -> str:
    lines = [
        f'        <Routine Name="{routine.name}" Type="RLL">',
        f"          <Description><![CDATA[{routine.description}]]></Description>",
        "          <RLLContent>",
    ]
    for number, rung in enumerate(routine.rungs):
        lines.extend(
            (
                f'            <Rung Number="{number}" Type="N">',
                f"              <Comment><![CDATA[{rung.comment}]]></Comment>",
                f"              <Text><![CDATA[{rung.text}]]></Text>",
                "            </Rung>",
            )
        )
    lines.extend(("          </RLLContent>", "        </Routine>"))
    return "\n".join(lines)


def build_l5x() -> str:
    """Assemble the export from the tables above.

    Built rather than written out as one literal so every rung, tag and module
    is a single addressable record: the README cites rung numbers, and a rung
    numbered by its position in this table cannot drift away from the number the
    file actually carries.
    """
    modules = "\n".join(
        f'    <Module Name="{name}" CatalogNumber="{catalog}" Vendor="1"\n'
        f'        ParentModule="Local" Inhibited="false" MajorFault="false">\n'
        f"      <Description><![CDATA[{description}]]></Description>\n"
        f"    </Module>"
        for name, catalog, description in MODULES
    )
    controller_tags = "\n".join(_tag_xml(tag, "    ") for tag in CONTROLLER_TAGS)
    program_tags = "\n".join(_tag_xml(tag, "        ") for tag in PROGRAM_TAGS)
    routines = "\n".join(_routine_xml(routine) for routine in ROUTINES)
    description = (
        "SYNTHETIC FIXTURE - Line 4 packaging conveyors, as installed. "
        "Invented data; not a real project."
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="33.00"
    TargetName="{CONTROLLER}" TargetType="Controller" ContainsContext="false"
    ExportDate="Mon Mar 02 09:00:00 2026" ExportOptions="References DecoratedData Context">
<Controller Use="Target" Name="{CONTROLLER}" ProcessorType="{PROCESSOR}"
    MajorRev="33" MinorRev="11" TimeSlice="20"
    ProjectCreationDate="Tue Apr 16 07:30:00 2019"
    LastModifiedDate="Mon Mar 02 08:55:00 2026">
  <Description><![CDATA[{description}]]></Description>

  <Modules Use="Context">
{modules}
  </Modules>

  <Tags Use="Context">
{controller_tags}
  </Tags>

  <Programs Use="Context">
    <Program Name="{PROGRAM}" TestEdits="false" MainRoutineName="MainRoutine"
        Disabled="false" UseAsFolder="false">
      <Description><![CDATA[Line 4 packaging conveyor control.]]></Description>
      <Tags>
{program_tags}
      </Tags>
      <Routines>
{routines}
      </Routines>
    </Program>
  </Programs>

  <Tasks Use="Context">
    <Task Name="MainTask" Type="CONTINUOUS" Priority="10" Watchdog="500"
        DisableUpdateOutputs="false" InhibitTask="false">
      <Description><![CDATA[Continuous task for the Line 4 packaging conveyors.]]></Description>
      <ScheduledPrograms>
        <ScheduledProgram Name="{PROGRAM}"/>
      </ScheduledPrograms>
    </Task>
  </Tasks>
</Controller>
</RSLogix5000Content>
"""


# ---------------------------------------------------------------------------
# I/O list (CSV)
# ---------------------------------------------------------------------------
IO_HEADERS = [
    "Module",
    "Slot",
    "Channel",
    "Address",
    "Signal Type",
    "Device Tag",
    "Description",
    "Engineering Units",
    "Range",
    "Status",
]

DI = "DI 24VDC sinking"
DO = "DO 24VDC sourcing"
AI = "AI 4-20 mA"
AO = "AO 4-20 mA"

#: One row per wired point. Channel and Address always agree, including on the
#: duplicated row, so the collision is a single defect rather than two.
IO_ROWS: list[list[str]] = [
    [
        "DI_401",
        "1",
        "0",
        "Local:1:I.Data.0",
        DI,
        "ESTOP_OK",
        "Line 4 E-stop string healthy",
        "",
        "0 = tripped / 1 = healthy",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "1",
        "Local:1:I.Data.1",
        DI,
        "START_PB",
        "Line start pushbutton",
        "",
        "0 = released / 1 = pressed",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "2",
        "Local:1:I.Data.2",
        DI,
        "STOP_PB",
        "Line stop pushbutton (normally closed)",
        "",
        "0 = pressed / 1 = released",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "3",
        "Local:1:I.Data.3",
        DI,
        "RESET_PB",
        "Fault reset pushbutton",
        "",
        "0 = released / 1 = pressed",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "4",
        "Local:1:I.Data.4",
        DI,
        "MODE_SEL_MANUAL",
        "Mode selector in Manual",
        "",
        "0 = not selected / 1 = selected",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "5",
        "Local:1:I.Data.5",
        DI,
        "MODE_SEL_AUTO",
        "Mode selector in Auto",
        "",
        "0 = not selected / 1 = selected",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "6",
        "Local:1:I.Data.6",
        DI,
        "PE401_BLOCKED",
        "PE401 upstream accumulation photo-eye blocked",
        "",
        "0 = clear / 1 = blocked",
        "Existing",
    ],
    # Planted issue 1: wired here and documented as ILK-404, never implemented.
    [
        "DI_401",
        "1",
        "7",
        "Local:1:I.Data.7",
        DI,
        "PE402_BLOCKED",
        "PE402 downstream accumulation photo-eye blocked",
        "",
        "0 = clear / 1 = blocked",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "8",
        "Local:1:I.Data.8",
        DI,
        "CYL401_EXT_PROX",
        "CYL401 diverter extended proximity switch",
        "",
        "0 = not made / 1 = made",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "9",
        "Local:1:I.Data.9",
        DI,
        "CYL401_RET_PROX",
        "CYL401 diverter retracted proximity switch",
        "",
        "0 = not made / 1 = made",
        "Existing",
    ],
    # Planted issue 2, first half.
    [
        "DI_401",
        "1",
        "10",
        "Local:1:I.Data.10",
        DI,
        "GD401_CLOSED",
        "Divert-zone guard door closed and latched",
        "",
        "0 = open / 1 = closed",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "11",
        "Local:1:I.Data.11",
        DI,
        "REJECT_REQ",
        "Case reject request from the upstream checkweigher",
        "",
        "0 = accept / 1 = reject",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "12",
        "Local:1:I.Data.12",
        DI,
        "PLT_CALL",
        "Palletiser calling for product",
        "",
        "0 = no call / 1 = call",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "13",
        "Local:1:I.Data.13",
        DI,
        "MTR401_FAULT",
        "CV401 infeed drive fault contact",
        "",
        "0 = healthy / 1 = faulted",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "14",
        "Local:1:I.Data.14",
        DI,
        "MTR402_FAULT",
        "CV402 accumulation drive fault contact",
        "",
        "0 = healthy / 1 = faulted",
        "Existing",
    ],
    # Planted issue 2, second half: the same physical channel as GD401_CLOSED.
    [
        "DI_401",
        "1",
        "10",
        "Local:1:I.Data.10",
        DI,
        "MTR403_FAULT",
        "CV403 divert drive fault contact",
        "",
        "0 = healthy / 1 = faulted",
        "Existing",
    ],
    [
        "DI_401",
        "1",
        "15",
        "Local:1:I.Data.15",
        DI,
        "CYL402_OPEN_PROX",
        "CV404 stop gate open proximity switch",
        "",
        "0 = not made / 1 = made",
        "Existing",
    ],
    # Planted issue 3: the L5X calls this signal CV401_Run_Output.
    [
        "DO_401",
        "2",
        "0",
        "Local:2:O.Data.0",
        DO,
        "MTR401_RUN_CMD",
        "CV401 infeed drive run command",
        "",
        "0 = stop / 1 = run",
        "Existing",
    ],
    [
        "DO_401",
        "2",
        "1",
        "Local:2:O.Data.1",
        DO,
        "MTR402_RUN_CMD",
        "CV402 accumulation drive run command",
        "",
        "0 = stop / 1 = run",
        "Existing",
    ],
    [
        "DO_401",
        "2",
        "2",
        "Local:2:O.Data.2",
        DO,
        "MTR403_RUN_CMD",
        "CV403 divert drive run command",
        "",
        "0 = stop / 1 = run",
        "Existing",
    ],
    [
        "DO_401",
        "2",
        "3",
        "Local:2:O.Data.3",
        DO,
        "CYL401_SOL",
        "CYL401 diverter solenoid (extends when energised)",
        "",
        "0 = retract / 1 = extend",
        "Existing",
    ],
    [
        "DO_401",
        "2",
        "4",
        "Local:2:O.Data.4",
        DO,
        "CYL402_SOL",
        "CV404 stop gate solenoid (opens when energised)",
        "",
        "0 = closed / 1 = open",
        "Existing",
    ],
    [
        "AI_401",
        "3",
        "0",
        "Local:3:I.Ch0Data",
        AI,
        "AIR_PRESS_PSI",
        "PT401 plant air header pressure at the Line 4 FRL",
        "psi",
        "0 to 150",
        "New (this upgrade)",
    ],
    [
        "AO_401",
        "4",
        "0",
        "Local:4:O.Ch0Data",
        AO,
        "MTR401_SPEED_REF",
        "CV401 drive speed reference",
        "ft/min",
        "0 to 120",
        "Existing",
    ],
    [
        "AO_401",
        "4",
        "1",
        "Local:4:O.Ch1Data",
        AO,
        "MTR402_SPEED_REF",
        "CV402 drive speed reference",
        "ft/min",
        "0 to 120",
        "Existing",
    ],
    [
        "AO_401",
        "4",
        "2",
        "Local:4:O.Ch2Data",
        AO,
        "MTR403_SPEED_REF",
        "CV403 drive speed reference",
        "ft/min",
        "0 to 120",
        "Existing",
    ],
]


def _banner(subject: str) -> str:
    """A ``#`` banner line carrying no candidate delimiter.

    A comma anywhere in it would make ``csv.writer`` quote the whole line, and a
    line that starts with a quote is not a comment: it would be read as a
    one-column header row and every real row after it as ragged.
    """
    return f"# SYNTHETIC FIXTURE - {subject} Invented data. Not a real project."


def write_io_list(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([_banner("Line 4 packaging conveyor upgrade I/O list.")])
        writer.writerow(IO_HEADERS)
        writer.writerows(IO_ROWS)


# ---------------------------------------------------------------------------
# Alarm register (CSV)
# ---------------------------------------------------------------------------
ALARM_HEADERS = [
    "Alarm ID",
    "Trigger Tag",
    "Message",
    "Severity",
    "Group",
    "Latched",
    "Ack Required",
    "Associated Display",
    "Interlock ID",
]

ALARM_ROWS: list[list[str]] = [
    [
        "ALM-401-01",
        "ALM_ESTOP",
        "Line 4 emergency stop actuated - all conveyors stopped",
        "Critical",
        "Safety",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "ILK-401",
    ],
    [
        "ALM-401-02",
        "ALM_GD401_OPEN",
        "Divert-zone guard door open - line permissive dropped",
        "Critical",
        "Safety",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "ILK-402",
    ],
    [
        "ALM-401-03",
        "ALM_MTR401_FAULT",
        "CV401 infeed drive fault",
        "High",
        "Drives",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "ILK-407",
    ],
    [
        "ALM-401-04",
        "ALM_MTR402_FAULT",
        "CV402 accumulation drive fault",
        "High",
        "Drives",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "ILK-407",
    ],
    [
        "ALM-401-05",
        "ALM_MTR403_FAULT",
        "CV403 divert drive fault",
        "High",
        "Drives",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "ILK-407",
    ],
    [
        "ALM-401-06",
        "ALM_CYL401_FAIL",
        f"CV403 diverter failed to extend within {DIVERTER_CONFIRM_SECONDS} s - CV403 stopped",
        "High",
        "Machine",
        "Yes",
        "Yes",
        "Line4_Diverter",
        "ILK-403",
    ],
    # Planted issue 4: no rung in the export writes ALM_CYL402_FAIL.
    [
        "ALM-401-07",
        "ALM_CYL402_FAIL",
        "CV404 stop gate failed to open",
        "Medium",
        "Machine",
        "Yes",
        "Yes",
        "Line4_Alarms",
        "",
    ],
]


def write_alarm_register(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [_banner("alarm register from the existing FactoryTalk View ME application.")]
        )
        writer.writerow(ALARM_HEADERS)
        writer.writerows(ALARM_ROWS)


# ---------------------------------------------------------------------------
# Interlock matrix (XLSX): the schedule, plus a cause-and-effect sheet
# ---------------------------------------------------------------------------
INTERLOCK_HEADERS = [
    "Interlock ID",
    "Description",
    "Initiating Device",
    "Trip Tag",
    "Trip Condition",
    "Action",
    "Affected Equipment",
    "Fail-Safe Behaviour",
    "Reset",
    "Scope",
    "Notes",
]

_EXISTING = "Existing"
_NEW = "New (this upgrade)"

INTERLOCK_ROWS: list[list[str]] = [
    [
        "ILK-401",
        "Emergency stop actuated",
        "ES401 station",
        "ESTOP_OK",
        "ESTOP_OK = 0",
        "Drop line permissive; stop CV401, CV402 and CV403",
        "CV401, CV402, CV403",
        "De-energise to trip (circuit healthy = 1)",
        "Manual, at the E-stop station",
        _EXISTING,
        "Hard-wired safety relay. PLC logic is indication and permissive only.",
    ],
    [
        "ILK-402",
        "Divert-zone guard door open",
        "GD401",
        "GD401_CLOSED",
        "GD401_CLOSED = 0",
        "Drop line permissive; stop CV401, CV402 and CV403",
        "CV401, CV402, CV403",
        "De-energise to trip",
        "Manual reset pushbutton",
        _EXISTING,
        "Interlocked gate switch on the divert-zone guard.",
    ],
    [
        "ILK-403",
        "Diverter fails to extend",
        "CYL401",
        "CYL401_EXT_PROX",
        f"Extend proximity not made within {DIVERTER_CONFIRM_SECONDS} s of CYL401_SOL energised",
        "Latch the diverter fault; drop CYL401_SOL; stop CV403",
        "CV403, CYL401",
        "Energise to trip",
        "Manual reset in Manual mode, with CYL401 confirmed home",
        _EXISTING,
        f"Confirmation timer preset {DIVERTER_CONFIRM_MILLISECONDS} ms.",
    ],
    # Planted issue 1: existing scope, wired, documented here and in the
    # functional description, and implemented nowhere.
    [
        "ILK-404",
        "Accumulation zone full",
        "PE402",
        "PE402_BLOCKED",
        "PE402_BLOCKED = 1 continuously for 5 s",
        "Stop CV401 infeed",
        "CV401",
        "Energise to trip",
        "Automatic once the zone clears",
        _EXISTING,
        "Prevents cases backing up into the divert zone.",
    ],
    [
        "ILK-405",
        "Low plant air pressure",
        "PT401",
        "AIR_PRESS_PSI",
        "AIR_PRESS_PSI < 65.0 psi",
        "Inhibit CYL401 and CYL402; stop CV403",
        "CV403, CYL401, CYL402",
        "Energise to trip",
        "Automatic once pressure is restored",
        _NEW,
        "PT401 is new. Not expected in the existing controller export.",
    ],
    [
        "ILK-406",
        "Case jam at the CV402 zone entry",
        "PE401 and PE402",
        "PE401_BLOCKED",
        "PE401_BLOCKED = 1 for 10 s with PE402_BLOCKED = 0",
        "Stop CV401 and CV402",
        "CV401, CV402",
        "Energise to trip",
        "Manual reset pushbutton",
        _NEW,
        "New logic. Not expected in the existing controller export.",
    ],
    [
        "ILK-407",
        "Conveyor drive fault",
        "MTR401, MTR402, MTR403",
        "MTR401_FAULT, MTR402_FAULT, MTR403_FAULT",
        "Any drive fault contact = 1",
        "Drop line permissive; stop CV401, CV402 and CV403",
        "CV401, CV402, CV403",
        "Energise to trip",
        "Manual reset pushbutton",
        _EXISTING,
        "All three drive fault contacts are in the same permissive rung.",
    ],
]

CAUSE_EFFECT_HEADERS = [
    "Interlock ID",
    "Cause",
    "Drop Line Permissive",
    "Stop CV401",
    "Stop CV402",
    "Stop CV403",
    "Inhibit CYL401",
    "Close CYL402",
    "Alarm Tag",
]

_X = "X"
_NONE = ""

CAUSE_EFFECT_ROWS: list[list[str]] = [
    ["ILK-401", "Emergency stop actuated", _X, _X, _X, _X, _X, _X, "ALM_ESTOP"],
    ["ILK-402", "Divert-zone guard door open", _X, _X, _X, _X, _X, _X, "ALM_GD401_OPEN"],
    [
        "ILK-403",
        f"Diverter fails to extend within {DIVERTER_CONFIRM_SECONDS} s",
        _NONE,
        _NONE,
        _NONE,
        _X,
        _X,
        _NONE,
        "ALM_CYL401_FAIL",
    ],
    [
        "ILK-404",
        "Accumulation zone full (PE402 blocked 5 s)",
        _NONE,
        _X,
        _NONE,
        _NONE,
        _NONE,
        _NONE,
        "no alarm required",
    ],
    [
        "ILK-405",
        "Plant air pressure below 65 psi",
        _NONE,
        _NONE,
        _NONE,
        _X,
        _X,
        _X,
        "new alarm to be added",
    ],
    [
        "ILK-406",
        "Case jam at the CV402 zone entry",
        _NONE,
        _X,
        _X,
        _NONE,
        _NONE,
        _NONE,
        "new alarm to be added",
    ],
    [
        "ILK-407",
        "Any conveyor drive fault",
        _X,
        _X,
        _X,
        _X,
        _X,
        _X,
        "ALM_MTR401_FAULT / ALM_MTR402_FAULT / ALM_MTR403_FAULT",
    ],
]


def write_interlock_matrix(path: Path) -> None:
    """Two visible sheets, each a declared Excel table.

    Declared rather than inferred so the parser reads ``named_table`` and not a
    guessed header row, and with no banner rows above the header so no populated
    row falls outside a table. Both would only be parser warnings, but this
    fixture is the false-positive control: noise here is noise a review has to
    decide about.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workbook = Workbook()
    workbook.properties.title = "SYNTHETIC FIXTURE - Line 4 interlock matrix"
    workbook.properties.creator = "SYNTHETIC FIXTURE"
    workbook.properties.lastModifiedBy = "SYNTHETIC FIXTURE"
    workbook.properties.description = DISCLAIMER
    workbook.properties.created = DOC_CREATED
    workbook.properties.modified = DOC_MODIFIED

    def fill(sheet: object, headers: list[str], rows: list[list[str]], name: str) -> None:
        for column, title in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=column, value=title)  # type: ignore[attr-defined]
            cell.font = Font(bold=True)
        for offset, row in enumerate(rows, start=2):
            for column, value in enumerate(row, start=1):
                if value == "":
                    continue
                sheet.cell(row=offset, column=column, value=value)  # type: ignore[attr-defined]
        last_column = chr(ord("A") + len(headers) - 1)
        table = Table(displayName=name, ref=f"A1:{last_column}{len(rows) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        sheet.add_table(table)  # type: ignore[attr-defined]

    matrix = workbook.active
    assert matrix is not None
    matrix.title = "Interlock Matrix"
    fill(matrix, INTERLOCK_HEADERS, INTERLOCK_ROWS, "InterlockMatrix")
    for column, width in zip(
        "ABCDEFGHIJK", (12, 32, 26, 40, 52, 50, 24, 42, 46, 20, 58), strict=True
    ):
        matrix.column_dimensions[column].width = width

    effects = workbook.create_sheet("Cause and Effect")
    fill(effects, CAUSE_EFFECT_HEADERS, CAUSE_EFFECT_ROWS, "CauseAndEffect")
    for column, width in zip("ABCDEFGHI", (12, 44, 22, 12, 12, 12, 16, 14, 52), strict=True):
        effects.column_dimensions[column].width = width

    workbook.save(path)
    normalise_zip(path)


# ---------------------------------------------------------------------------
# Deterministic OOXML packaging
# ---------------------------------------------------------------------------
#: ``docProps/core.xml`` inside an OOXML package.
_CORE_PROPERTIES = "docProps/core.xml"
_MODIFIED_ELEMENT = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")
_MODIFIED_VALUE = DOC_MODIFIED.strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")


def normalise_zip(path: Path) -> None:
    """Rewrite an OOXML package so the same content always gives the same bytes.

    Two things in the package read the clock and neither is content. ``zipfile``
    stamps every member it writes with the current local time, so the members
    are rewritten in their original order with a fixed timestamp and fixed
    attributes. Separately, ``openpyxl`` overwrites the workbook's *modified*
    core property at save time whatever it was set to, so that one element is
    put back to the fixed stamp here.
    """
    with zipfile.ZipFile(path) as source:
        members = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    members = [
        (
            name,
            _MODIFIED_ELEMENT.sub(rb"\g<1>" + _MODIFIED_VALUE + rb"\g<2>", data)
            if name == _CORE_PROPERTIES
            else data,
        )
        for name, data in members
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            target.writestr(info, data)
    path.write_bytes(buffer.getvalue())


# ---------------------------------------------------------------------------
# HMI display export (XML), FactoryTalk View ME shaped
# ---------------------------------------------------------------------------
class Element(NamedTuple):
    """One graphic element on a display, and the tag its connection reads."""

    element_type: str
    name: str
    caption: str
    connection: str
    expression: str


def _binding(tag: str) -> str:
    return f"{{::[{HMI_SHORTCUT}]{tag}}}"


OVERVIEW_ELEMENTS: tuple[Element, ...] = (
    Element("MultiStateIndicator", "ind_LineRunning", "Line running", "Indicator", "LINE_RUNNING"),
    Element(
        "MultiStateIndicator", "ind_LinePermissive", "Permissive", "Indicator", "LINE_PERMISSIVE"
    ),
    Element("MultiStateIndicator", "ind_EstopHealthy", "E-stop healthy", "Indicator", "ESTOP_OK"),
    Element("MultiStateIndicator", "ind_GuardClosed", "Guard closed", "Indicator", "GD401_CLOSED"),
    Element(
        "MultiStateIndicator",
        "ind_ModeManual",
        "Manual",
        "Indicator",
        f"Program:{PROGRAM}.ManualMode",
    ),
    Element(
        "MultiStateIndicator", "ind_ModeAuto", "Auto", "Indicator", f"Program:{PROGRAM}.AutoMode"
    ),
    Element(
        "MultiStateIndicator", "ind_CV401Run", "CV401 running", "Indicator", "CV401_Run_Output"
    ),
    Element("MultiStateIndicator", "ind_CV402Run", "CV402 running", "Indicator", "MTR402_RUN_CMD"),
    Element("MultiStateIndicator", "ind_CV403Run", "CV403 running", "Indicator", "MTR403_RUN_CMD"),
    Element("MultiStateIndicator", "ind_PE401", "PE401 blocked", "Indicator", "PE401_BLOCKED"),
    Element("MultiStateIndicator", "ind_GateOpen", "Stop gate open", "Indicator", "CYL402_OPEN"),
    Element("NumericDisplay", "num_LineSpeed", "Line speed ft/min", "Value", "Line_Speed_SP"),
    # Planted issue 5: Accum_Case_Count is declared nowhere - not in the L5X,
    # not in the I/O list, not in any other document.
    Element(
        "NumericDisplay", "num_CasesAccumulated", "Cases accumulated", "Value", "Accum_Case_Count"
    ),
    Element("MomentaryPushButton", "btn_Start", "Start", "Value", "START_PB"),
    Element("MomentaryPushButton", "btn_Stop", "Stop", "Value", "STOP_PB"),
    Element("MomentaryPushButton", "btn_Reset", "Reset", "Value", "RESET_PB"),
)

DIVERTER_ELEMENTS: tuple[Element, ...] = (
    Element(
        "MultiStateIndicator",
        "ind_DiverterSolenoid",
        "Solenoid energised",
        "Indicator",
        "CYL401_SOL",
    ),
    Element(
        "MultiStateIndicator", "ind_DiverterExtended", "Extended", "Indicator", "CYL401_EXT_PROX"
    ),
    Element(
        "MultiStateIndicator", "ind_DiverterRetracted", "Retracted", "Indicator", "CYL401_RET_PROX"
    ),
    Element(
        "MultiStateIndicator", "ind_DiverterFault", "Diverter fault", "Indicator", "ALM_CYL401_FAIL"
    ),
    Element(
        "MultiStateIndicator", "ind_RejectRequest", "Reject requested", "Indicator", "REJECT_REQ"
    ),
    Element(
        "NumericDisplay", "num_ExtendElapsed", "Extend elapsed ms", "Value", "CYL401_Extend_TMR.ACC"
    ),
)

ALARM_ELEMENTS: tuple[Element, ...] = (
    Element("MultiStateIndicator", "ind_AlmEstop", "Emergency stop", "Indicator", "ALM_ESTOP"),
    Element(
        "MultiStateIndicator", "ind_AlmGuard", "Guard door open", "Indicator", "ALM_GD401_OPEN"
    ),
    Element(
        "MultiStateIndicator", "ind_AlmMtr401", "CV401 drive fault", "Indicator", "ALM_MTR401_FAULT"
    ),
    Element(
        "MultiStateIndicator", "ind_AlmMtr402", "CV402 drive fault", "Indicator", "ALM_MTR402_FAULT"
    ),
    Element(
        "MultiStateIndicator", "ind_AlmMtr403", "CV403 drive fault", "Indicator", "ALM_MTR403_FAULT"
    ),
    Element(
        "MultiStateIndicator", "ind_AlmDiverter", "Diverter fault", "Indicator", "ALM_CYL401_FAIL"
    ),
    Element(
        "MultiStateIndicator", "ind_AlmStopGate", "Stop gate fault", "Indicator", "ALM_CYL402_FAIL"
    ),
)

DISPLAYS: tuple[tuple[str, int, str, tuple[Element, ...]], ...] = (
    ("Line4_Overview", 10, "Line 4 packaging conveyor overview.", OVERVIEW_ELEMENTS),
    ("Line4_Diverter", 20, "CYL401 reject diverter detail.", DIVERTER_ELEMENTS),
    ("Line4_Alarms", 30, "Line 4 alarm summary.", ALARM_ELEMENTS),
)


def build_hmi_xml() -> str:
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<FactoryTalkViewMEProject Name="Line4_Packaging_HMI" ProductVersion="14.00"',
        '    Terminal="PanelView Plus 7 Standard 1000" ExportFormat="GraphicDisplayXML">',
        f"  <Description>{DISCLAIMER}</Description>",
        "  <RSLinxShortcuts>",
        f'    <Shortcut Name="{HMI_SHORTCUT}" Processor="{PROCESSOR}"'
        r' Path="AB_ETHIP-1\192.0.2.20\Backplane\0"/>',
        "  </RSLinxShortcuts>",
        "  <Displays>",
    ]
    for name, number, description, elements in DISPLAYS:
        parts.extend(
            (
                f'    <Display Name="{name}" Number="{number}" Width="800" Height="600"'
                ' Type="Replace">',
                f"      <Description>{description}</Description>",
                "      <GraphicElements>",
            )
        )
        for index, element in enumerate(elements):
            top = 60 + index * 34
            parts.extend(
                (
                    f'        <{element.element_type} Name="{element.name}" Left="20"'
                    f' Top="{top}" Width="240" Height="30">',
                    f"          <Caption>{element.caption}</Caption>",
                    f'          <Connection Name="{element.connection}"'
                    f' Expression="{_binding(element.expression)}"/>',
                    f"        </{element.element_type}>",
                )
            )
        parts.extend(("      </GraphicElements>", "    </Display>"))
    parts.extend(("  </Displays>", "</FactoryTalkViewMEProject>", ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Project description (DOCX) - the customer's own brief
# ---------------------------------------------------------------------------
#: Planted issue 6. Two readings a controls engineer would actually argue over,
#: written out in the fixture README. Kept as a constant so the test can assert
#: the exact sentence survives into the document.
AMBIGUOUS_SENTENCE = (
    "When the palletiser calls for product, CV402 shall release the accumulated "
    "cases to the discharge, and the stop gate shall close again once the zone is "
    "empty."
)


def _set_properties(document: object, title: str) -> None:
    properties = document.core_properties  # type: ignore[attr-defined]
    properties.title = title
    properties.author = "SYNTHETIC FIXTURE"
    properties.last_modified_by = "SYNTHETIC FIXTURE"
    properties.comments = DISCLAIMER
    properties.category = "Synthetic test fixture"
    properties.revision = 1
    properties.created = DOC_CREATED
    properties.modified = DOC_MODIFIED


def write_project_description(path: Path) -> None:
    from docx import Document

    document = Document()
    _set_properties(document, "Line 4 Packaging Conveyor Upgrade - Project Description")

    document.add_heading("Line 4 Packaging Conveyor Upgrade", level=0)
    document.add_paragraph(DISCLAIMER)
    document.add_paragraph(
        "Project description issued with purchase order SYN-PO-4471. Prepared by the "
        "plant engineering group; this is the brief as written, not an engineered "
        "specification."
    )

    document.add_heading("1. Background", level=1)
    document.add_paragraph(
        "Line 4 packs cases of dry goods and hands them to the palletiser at the end "
        "of the building. The line has been in service since 2019 and runs three "
        "shifts. Throughput is 48 cases per minute at present, and cases are nominally "
        "300 by 200 by 250 mm and weigh between 8 and 18 kg."
    )
    document.add_paragraph(
        "The complaint from operations is that the line surges. When the palletiser "
        "pauses the cases back up onto the divert conveyor, the reject diverter cannot "
        "clear the lane, and an operator has to stop the line by hand. We lose about "
        "twenty minutes a shift to this."
    )

    document.add_heading("2. Existing equipment", level=1)
    document.add_paragraph(
        "CV401 infeed, CV402 accumulation, CV403 divert and CV404 gravity discharge. "
        "CV401 to CV403 are belt conveyors driven by MTR401, MTR402 and MTR403, each "
        "on its own variable frequency drive. CV404 is an unpowered gravity roller "
        "section feeding the palletiser."
    )
    document.add_paragraph(
        "A pneumatic diverter, CYL401, sits on CV403 and pushes rejected cases into "
        "the reject lane. It is double-acting with extend and retract proximity "
        "switches, and it has always been set up to raise a fault if it does not "
        f"confirm extension within {DIVERTER_CONFIRM_SECONDS} seconds. The reject lane "
        "is emptied by hand once a shift.",
    )
    document.add_paragraph(
        "A stop gate, CYL402, holds cases back at the head of CV404. Two photo-eyes, "
        "PE401 at the entry to the accumulation zone and PE402 at its discharge, watch "
        "the zone."
    )
    document.add_paragraph(
        "The controller is a CompactLogix 5069-L330ER in the Line 4 panel, and the "
        "operator terminal is a PanelView Plus 7 Standard 1000 running FactoryTalk "
        "View ME."
    )

    document.add_heading("3. Scope of work", level=1)
    document.add_paragraph(
        "We want the accumulation zone to do the work the operators are doing by hand."
    )
    document.add_paragraph(AMBIGUOUS_SENTENCE)
    document.add_paragraph(
        "The existing stop-gate logic, which simply holds the gate open for as long as "
        "the palletiser call is present, is to be replaced by whatever the new "
        "accumulation control needs."
    )
    document.add_paragraph(
        "Add a pressure transmitter on the air header at the line 4 filter-regulator "
        "so the line stops cleanly when the compressor trips instead of leaving the "
        "diverter half out.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Add jam detection at the entry to the accumulation zone.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Let maintenance jog each conveyor from the terminal when the selector is in "
        "Manual. They currently have to go to the drive isolators.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Everything else on the line is to be left as it is. We are not changing the "
        "drives, the safety circuit or the guard.",
        style="List Bullet",
    )

    document.add_heading("4. Control system", level=1)
    document.add_paragraph(
        "Keep the existing controller and the existing I/O. Slot 3 is an empty analog "
        "input card with spare channels, so the pressure transmitter can go there "
        "without new hardware."
    )
    document.add_paragraph(
        "Use our standard templates. The approved list is attached to this description."
    )

    document.add_heading("5. HMI", level=1)
    document.add_paragraph(
        "New alarms go on the existing terminal. We are not buying a second terminal, "
        "and the alarm wording has to match what the operators already read on the "
        "other lines."
    )

    document.add_heading("6. Commissioning", level=1)
    document.add_paragraph(
        "The changeover has to happen in a single twelve-hour window on a Sunday. The "
        "line must be running for the Monday morning shift. If the work cannot be "
        "finished in that window we need to be able to roll back to the existing "
        "program before the shift starts."
    )

    document.save(path)
    normalise_zip(path)


# ---------------------------------------------------------------------------
# Functional description (DOCX)
# ---------------------------------------------------------------------------
FUNCTIONAL_INTERLOCKS: tuple[tuple[str, str, str, str], ...] = (
    ("ILK-401", "ESTOP_OK = 0", "Drop line permissive", "De-energise to trip"),
    ("ILK-402", "GD401_CLOSED = 0", "Drop line permissive", "De-energise to trip"),
    (
        "ILK-403",
        f"CYL401 not extended within {DIVERTER_CONFIRM_SECONDS} s",
        "Latch diverter fault and stop CV403",
        "Energise to trip",
    ),
    ("ILK-404", "PE402 blocked for 5 s", "Stop CV401 infeed", "Energise to trip"),
    ("ILK-405", "Air pressure below 65 psi", "Inhibit CYL401 and CYL402", "Energise to trip"),
    ("ILK-406", "PE401 blocked for 10 s", "Stop CV401 and CV402", "Energise to trip"),
    ("ILK-407", "Any drive fault contact = 1", "Drop line permissive", "Energise to trip"),
)


def write_functional_description(path: Path) -> None:
    from docx import Document

    document = Document()
    _set_properties(document, "Line 4 Packaging Conveyor Upgrade - Functional Description")

    document.add_heading("Line 4 Packaging Conveyor - Functional Description", level=0)
    document.add_paragraph(DISCLAIMER)
    document.add_paragraph(
        "Document SYN-L4-FDS-001 Rev B. Describes the line as it operates today and "
        "the behaviour this upgrade adds. Behaviour marked existing is already in the "
        "controller; behaviour marked new is in the scope of this upgrade."
    )

    document.add_heading("1. Scope", level=1)
    document.add_paragraph(
        "Line 4 comprises CV401 infeed, CV402 accumulation, CV403 divert and CV404 "
        "gravity discharge, driven by MTR401, MTR402 and MTR403 on variable frequency "
        "drives. A pneumatic diverter CYL401 sits on CV403 and a stop gate CYL402 "
        "holds cases at the head of CV404. Photo-eyes PE401 and PE402 watch the "
        "accumulation zone entry and its discharge."
    )

    document.add_heading("2. Operating modes", level=1)
    document.add_paragraph(
        "A three-position selector provides Off, Manual and Auto. The selector "
        "positions arrive on two discrete inputs; neither input made is Off."
    )
    document.add_paragraph(
        "Off - no run command is issued.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Manual, existing - the conveyors are started individually at the drive "
        "isolators. Interlocks still apply, and a latched fault can be reset only in "
        "this mode.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Manual, new - each conveyor may also be jogged from the terminal while the "
        "selector is in Manual.",
        style="List Bullet",
    )
    document.add_paragraph(
        "Auto - the line runs from the line start pushbutton and stops on the line "
        "stop pushbutton.",
        style="List Bullet",
    )

    document.add_heading("3. Start, stop and permissives", level=1)
    document.add_paragraph(
        "A master line permissive gates every conveyor run command. The permissive is "
        "made only while the emergency stop string is healthy, the divert-zone guard "
        "door is closed and latched, and none of the three drive fault contacts is "
        "made."
    )
    document.add_paragraph(
        "The emergency stop string and the guard door switch are both de-energise to "
        "trip, so a broken wire drops the permissive and stops the line."
    )
    document.add_paragraph(
        "The line start pushbutton seals in a line running bit while the permissive "
        "holds. The stop pushbutton is wired normally closed, so releasing the seal "
        "does not depend on a signal arriving."
    )

    document.add_heading("4. Interlocks", level=1)
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    headers = table.rows[0].cells
    for index, title in enumerate(("Interlock", "Condition", "Action", "Fail-safe")):
        headers[index].text = title
    for interlock, condition, action, failsafe in FUNCTIONAL_INTERLOCKS:
        cells = table.add_row().cells
        cells[0].text = interlock
        cells[1].text = condition
        cells[2].text = action
        cells[3].text = failsafe
    document.add_paragraph(
        "ILK-401, ILK-402, ILK-403, ILK-404 and ILK-407 are existing behaviour. "
        "ILK-405 and ILK-406 are added by this upgrade."
    )

    document.add_heading("5. Reject diverter, CYL401", level=1)
    document.add_paragraph(
        "The diverter extends when the upstream checkweigher requests a reject, while "
        "the line is running, the permissive holds and no diverter fault is latched. "
        "The solenoid extends the cylinder when energised and the cylinder returns on "
        "spring when it is de-energised."
    )
    document.add_paragraph(
        "If the extended proximity switch is not made within "
        f"{DIVERTER_CONFIRM_SECONDS} seconds of the solenoid being energised, the "
        "diverter fault latches. The latched fault drops the solenoid and stops CV403."
    )
    document.add_paragraph(
        "The diverter fault is unlatched by the reset pushbutton, only while the "
        "selector is in Manual, and only while the retracted proximity switch confirms "
        "the diverter is home. It is annunciated on the terminal as a machine alarm."
    )

    document.add_heading("6. Stop gate, CYL402", level=1)
    document.add_paragraph(
        "The stop gate opens while the palletiser is calling for product and the line "
        "is running. The gate solenoid opens the gate when energised, so the gate "
        "closes on loss of air or loss of power."
    )
    document.add_paragraph(
        "An open proximity switch confirms the gate has opened and is shown on the "
        "overview screen. A stop gate fault alarm is annunciated when the gate fails "
        "to open on a palletiser call."
    )

    document.add_heading("7. Alarms and reset", level=1)
    document.add_paragraph(
        "Alarms are annunciated on the existing FactoryTalk View ME terminal. Alarms "
        "in the safety group latch and require acknowledgement. Latched machine faults "
        "are unlatched by the reset pushbutton in Manual only, once the initiating "
        "condition has cleared."
    )

    document.add_heading("8. To be confirmed", level=1)
    document.add_paragraph(
        "The release behaviour requested in section 3 of the project description has "
        "not been settled. It is not specified here and no logic has been written for "
        "it."
    )

    document.save(path)
    normalise_zip(path)


# ---------------------------------------------------------------------------
# Approved templates (plain text)
# ---------------------------------------------------------------------------
APPROVED_TEMPLATES = """\
ESCO Standard Template Approval - Line 4 Packaging Conveyor Upgrade
==================================================================

SYNTHETIC FIXTURE. Invented content for testing the review platform. Not a real
project, not a real customer, and not real plant data.

Document SYN-L4-TPL-001 Rev A, issued with purchase order SYN-PO-4471.

Approved for use on this job
----------------------------

ESCO-TPL-CONV-VFD-002    VFD-driven belt conveyor start and stop, with seal-in,
                         a drive fault contact and a permissive break.
ESCO-TPL-CYL-SGL-004     Single-solenoid pneumatic cylinder with extend and
                         retract proximity confirmation and a fail-to-move
                         timer.
ESCO-TPL-MODE-SEL-001    Three-position Off, Manual and Auto mode selection from
                         two discrete selector inputs.
ESCO-TPL-ALM-STD-003     Standard latched alarm, unlatched by the reset
                         pushbutton in Manual mode only.
ESCO-TPL-PERM-LINE-001   Master line permissive built from de-energise to trip
                         safety contacts.
ESCO-TPL-HMI-ME-006      FactoryTalk View ME screen set for a single line, with
                         an overview, a machine detail screen and an alarm
                         summary.

Not approved for this job
-------------------------

ESCO-TPL-CONV-SOFT-005   Controller-side soft-start ramp profile. The drives
                         ramp in the drive, and the customer has asked that the
                         ramp stay there.
ESCO-TPL-ACCUM-ZONE-007  Zero-pressure accumulation with per-zone release. The
                         line has a single accumulation zone and per-zone
                         release has not been approved for it.
ESCO-TPL-SAFETY-GM-011   Safety-instruction guard monitoring. The guard and the
                         emergency stop string stay hard-wired on this line and
                         are out of scope.

Conditions of approval
----------------------

- Templates are approved as written. Any deviation needs written agreement from
  the controls engineer named on the purchase order before it goes into a
  release.
- Alarm messages must use the wording already in the terminal application. New
  alarms are added to that application; no new terminal is supplied.
- Tag names in generated logic follow the device tags in the issued I/O list.
"""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
#: Artifact filenames, in the order a reviewer would read them. Exported so the
#: test can assert the fixture README names every one of them.
ARTIFACTS = (
    "Project_Description.docx",
    "Functional_Description.docx",
    "IO_List.csv",
    "Interlock_Matrix.xlsx",
    "Conveyor_Line4_Base.L5X",
    "HMI_Screens.xml",
    "Alarm_Register.csv",
    "Approved_Templates.txt",
)


def generate(target: Path) -> list[Path]:
    """Write every artifact into ``target`` and return the paths written."""
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def record(name: str) -> Path:
        path = target / name
        written.append(path)
        return path

    write_project_description(record("Project_Description.docx"))
    write_functional_description(record("Functional_Description.docx"))
    write_io_list(record("IO_List.csv"))
    write_interlock_matrix(record("Interlock_Matrix.xlsx"))
    record("Conveyor_Line4_Base.L5X").write_text(build_l5x(), encoding="utf-8", newline="\n")
    record("HMI_Screens.xml").write_text(build_hmi_xml(), encoding="utf-8", newline="\n")
    write_alarm_register(record("Alarm_Register.csv"))
    record("Approved_Templates.txt").write_text(APPROVED_TEMPLATES, encoding="utf-8", newline="\n")
    return written


def verify(target: Path) -> None:
    """Prove the planted issues are in the bytes, at generation time.

    The fixture README states where each issue lives and the test module asserts
    those claims in detail. These checks are the cheap subset that must never be
    allowed to pass silently: a generator that stopped planting an issue would
    otherwise produce a clean fixture and a green ground truth document.
    """
    from docx import Document
    from openpyxl import load_workbook

    for path in (target / name for name in ARTIFACTS):
        assert path.exists() and path.stat().st_size > 0, f"empty artifact: {path.name}"

    l5x = (target / "Conveyor_Line4_Base.L5X").read_text(encoding="utf-8")
    # Issue 1: nothing about PE402 exists in the logic at all.
    assert "PE402" not in l5x, "ILK-404 must be absent from the base export"
    # Issue 3: the L5X keeps its own name for the CV401 run command.
    assert "CV401_Run_Output" in l5x
    assert "MTR401_RUN_CMD" not in l5x
    # Issue 4: the tag is declared and never written.
    assert 'Name="ALM_CYL402_FAIL"' in l5x
    assert "OTE(ALM_CYL402_FAIL)" not in l5x
    assert "OTL(ALM_CYL402_FAIL)" not in l5x
    # Issue 7: the correctly implemented device is complete.
    for expected in (
        "CYL401_SOL",
        "CYL401_EXT_PROX",
        "CYL401_RET_PROX",
        f"TON(CYL401_Extend_TMR,{DIVERTER_CONFIRM_MILLISECONDS},0)",
        "OTL(ALM_CYL401_FAIL)",
        "OTU(ALM_CYL401_FAIL)",
    ):
        assert expected in l5x, f"CYL401 chain is incomplete: {expected} missing"

    # Issue 2: exactly one physical address is used twice.
    addresses = [row[3] for row in IO_ROWS]
    duplicated = sorted({item for item in addresses if addresses.count(item) > 1})
    assert duplicated == ["Local:1:I.Data.10"], f"unexpected duplicate addresses: {duplicated}"

    # Issue 5: exactly one HMI binding names a tag nothing declares.
    declared = {tag.name for tag in CONTROLLER_TAGS} | {tag.name for tag in PROGRAM_TAGS}
    bound = {base_tag(element.expression) for _, _, _, elements in DISPLAYS for element in elements}
    assert bound - declared == {"Accum_Case_Count"}, f"unexpected HMI bindings: {bound - declared}"

    # Issue 6: the ambiguous sentence survived into the document.
    document = Document(str(target / "Project_Description.docx"))
    assert any(AMBIGUOUS_SENTENCE in paragraph.text for paragraph in document.paragraphs)

    workbook = load_workbook(target / "Interlock_Matrix.xlsx")
    assert workbook.sheetnames == ["Interlock Matrix", "Cause and Effect"]
    assert all(sheet.sheet_state == "visible" for sheet in workbook.worksheets)

    from defusedxml.ElementTree import fromstring

    for name in ("Conveyor_Line4_Base.L5X", "HMI_Screens.xml"):
        fromstring((target / name).read_bytes())


def base_tag(expression: str) -> str:
    """The controller tag an HMI expression reads.

    ``Program:Line4_Control.ManualMode`` names a program-scope tag and
    ``CYL401_Extend_TMR.ACC`` names a member of a structure; both resolve to the
    tag the controller declares, which is what a binding can be checked against.
    """
    text = expression.strip()
    if text.startswith("Program:"):
        text = text.split(".", 1)[1] if "." in text else text
    for separator in (".", "["):
        text = text.split(separator, 1)[0]
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=FIXTURES)
    args = parser.parse_args()

    written = generate(args.target)
    verify(args.target)

    for path in written:
        print(f"wrote {path.name} ({path.stat().st_size:,} bytes)")
    print(f"\n{len(written)} artifacts generated; seven planted issues verified present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
