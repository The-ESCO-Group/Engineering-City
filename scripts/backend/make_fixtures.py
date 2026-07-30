"""Generate the synthetic demonstration fixture set.

Everything here is invented. There is no ESCO customer data in this repository
and none may be added: the fixtures exist so parser and agent behaviour can be
tested against content whose *correct answer is known in advance*.

The set describes a fictional three-conveyor line and deliberately contains one
of each discrepancy class the review agents must find:

===========  ==========================  ===================================
Interlock    Designed discrepancy        What a correct review should report
===========  ==========================  ===================================
ILK-001      none - correctly matched    matched, no finding
ILK-002      none - correctly matched    matched, no finding
ILK-003      documented, absent in logic missing_in_logic
ILK-004      tag renamed in logic        naming_mismatch
ILK-005      threshold disagrees         contradictory
ILK-006      -                           undocumented logic (in L5X only)
ILK-007      no tag, no threshold        ambiguous / unsupported
===========  ==========================  ===================================

Run from the repository root::

    python scripts/backend/make_fixtures.py
"""

from __future__ import annotations

import argparse
import csv
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "demo_project"

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))


# ---------------------------------------------------------------------------
# L5X - synthetic Studio 5000 export
# ---------------------------------------------------------------------------
L5X = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="32.00"
    TargetName="Conveyor_Line3" TargetType="Controller" ContainsContext="false"
    ExportDate="Mon Jan 06 09:14:22 2025" ExportOptions="References DecoratedData Context">
<Controller Use="Target" Name="Conveyor_Line3" ProcessorType="1756-L83E"
    MajorRev="32" MinorRev="11" TimeSlice="20"
    ProjectCreationDate="Mon Nov 04 08:00:00 2024"
    LastModifiedDate="Mon Jan 06 09:12:04 2025">
  <Description><![CDATA[SYNTHETIC FIXTURE - Conveyor Line 3. Not a real project.]]></Description>

  <DataTypes Use="Context">
    <DataType Name="ConveyorStatus" Family="NoFamily" Class="User">
      <Description><![CDATA[Per-conveyor status word.]]></Description>
      <Members>
        <Member Name="Running" DataType="BOOL" Dimension="0" Radix="Decimal"
            Hidden="false" ExternalAccess="Read/Write"/>
        <Member Name="Faulted" DataType="BOOL" Dimension="0" Radix="Decimal"
            Hidden="false" ExternalAccess="Read/Write"/>
        <Member Name="Interlocked" DataType="BOOL" Dimension="0" Radix="Decimal"
            Hidden="false" ExternalAccess="Read/Write"/>
        <Member Name="SpeedFeedback" DataType="REAL" Dimension="0" Radix="Float"
            Hidden="false" ExternalAccess="Read/Write"/>
      </Members>
    </DataType>
  </DataTypes>

  <Modules Use="Context">
    <Module Name="Local" CatalogNumber="1756-L83E" Vendor="1" ProductType="14"
        ProductCode="168" Major="32" Minor="11" ParentModule="Local"
        ParentModPortId="1" Inhibited="false" MajorFault="true"/>
    <Module Name="DI_01" CatalogNumber="1756-IB16" Vendor="1" ProductType="7"
        ProductCode="4" Major="3" Minor="1" ParentModule="Local"
        ParentModPortId="1" Inhibited="false" MajorFault="false">
      <Description><![CDATA[Digital input card - line 3 safety and status inputs.]]></Description>
    </Module>
    <Module Name="DO_01" CatalogNumber="1756-OB16E" Vendor="1" ProductType="7"
        ProductCode="5" Major="3" Minor="1" ParentModule="Local"
        ParentModPortId="1" Inhibited="false" MajorFault="false">
      <Description><![CDATA[Digital output card - conveyor run commands.]]></Description>
    </Module>
    <Module Name="AI_01" CatalogNumber="1756-IF8" Vendor="1" ProductType="10"
        ProductCode="20" Major="3" Minor="1" ParentModule="Local"
        ParentModPortId="1" Inhibited="false" MajorFault="false">
      <Description><![CDATA[Analog input card - air pressure and hopper level.]]></Description>
    </Module>
  </Modules>

  <AddOnInstructionDefinitions Use="Context">
    <AddOnInstructionDefinition Name="MotorStarter" Revision="1.2"
        RevisionExtension="a" ExecutePrescan="false" ExecutePostscan="false"
        ExecuteEnableInFalse="false" CreatedDate="2024-11-04T08:10:00.000Z"
        SoftwareRevision="v32.00">
      <Description><![CDATA[Seals in a motor run command while all permissives hold.]]></Description>
      <Parameters>
        <Parameter Name="EnableIn" TagType="Base" DataType="BOOL" Usage="Input"
            Radix="Decimal" Required="false" Visible="false" ExternalAccess="Read Only"/>
        <Parameter Name="EnableOut" TagType="Base" DataType="BOOL" Usage="Output"
            Radix="Decimal" Required="false" Visible="false" ExternalAccess="Read Only"/>
        <Parameter Name="StartCmd" TagType="Base" DataType="BOOL" Usage="Input"
            Radix="Decimal" Required="true" Visible="true" ExternalAccess="Read/Write"/>
        <Parameter Name="StopCmd" TagType="Base" DataType="BOOL" Usage="Input"
            Radix="Decimal" Required="true" Visible="true" ExternalAccess="Read/Write"/>
        <Parameter Name="Permissive" TagType="Base" DataType="BOOL" Usage="Input"
            Radix="Decimal" Required="true" Visible="true" ExternalAccess="Read/Write"/>
        <Parameter Name="RunOut" TagType="Base" DataType="BOOL" Usage="Output"
            Radix="Decimal" Required="true" Visible="true" ExternalAccess="Read/Write"/>
      </Parameters>
      <Routines>
        <Routine Name="Logic" Type="RLL">
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[Seal-in with permissive break.]]></Comment>
              <Text><![CDATA[XIC(StopCmd)XIC(Permissive)[XIC(StartCmd),XIC(RunOut)]OTE(RunOut);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>
      </Routines>
    </AddOnInstructionDefinition>
  </AddOnInstructionDefinitions>

  <Tags Use="Context">
    <Tag Name="ESTOP_OK" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Emergency stop circuit healthy. De-energises on trip.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="CV101_OL_TRIP" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV101 motor overload relay tripped.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="CV102_OL_TRIP" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV102 motor overload relay tripped.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="GRD_DR_CLSD" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Guard door closed and latched.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="AIR_PRESS_PSI" TagType="Base" DataType="REAL" Radix="Float"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Plant air pressure at the line 3 header, psi.]]></Description>
      <Data Format="Decorated"><DataValue DataType="REAL" Radix="Float" Value="82.5"/></Data>
    </Tag>
    <Tag Name="CV103_VFD_FLT" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV103 variable frequency drive fault contact.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="HOPPER_LEVEL_PCT" TagType="Base" DataType="REAL" Radix="Float"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Surge hopper level, percent of full.]]></Description>
      <Data Format="Decorated"><DataValue DataType="REAL" Radix="Float" Value="45.0"/></Data>
    </Tag>
    <Tag Name="LINE_PERMISSIVE" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Master permissive for all line 3 conveyors.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="CV101_Status" TagType="Base" DataType="ConveyorStatus"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV101 status word.]]></Description>
    </Tag>
    <Tag Name="CV102_Status" TagType="Base" DataType="ConveyorStatus"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV102 status word.]]></Description>
    </Tag>
    <Tag Name="CV103_Status" TagType="Base" DataType="ConveyorStatus"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[CV103 status word.]]></Description>
    </Tag>
    <Tag Name="ALM_ESTOP" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Alarm: emergency stop actuated.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
    <Tag Name="ALM_CV101_OL" TagType="Base" DataType="BOOL" Radix="Decimal"
        Constant="false" ExternalAccess="Read/Write">
      <Description><![CDATA[Alarm: CV101 motor overload.]]></Description>
      <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
    </Tag>
  </Tags>

  <Programs Use="Context">
    <Program Name="MainProgram" TestEdits="false" MainRoutineName="MainRoutine"
        Disabled="false" UseAsFolder="false">
      <Description><![CDATA[Line 3 control.]]></Description>
      <Tags>
        <Tag Name="ModeSelect" TagType="Base" DataType="DINT" Radix="Decimal"
            Constant="false" ExternalAccess="Read/Write">
          <Description><![CDATA[0 = Off, 1 = Manual, 2 = Auto.]]></Description>
          <Data Format="Decorated"><DataValue DataType="DINT" Radix="Decimal" Value="0"/></Data>
        </Tag>
        <Tag Name="AutoMode" TagType="Base" DataType="BOOL" Radix="Decimal"
            Constant="false" ExternalAccess="Read/Write">
          <Description><![CDATA[Auto mode active.]]></Description>
          <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
        </Tag>
        <Tag Name="ManualMode" TagType="Base" DataType="BOOL" Radix="Decimal"
            Constant="false" ExternalAccess="Read/Write">
          <Description><![CDATA[Manual mode active.]]></Description>
          <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
        </Tag>
        <Tag Name="CV101_Start_PB" TagType="Base" DataType="BOOL" Radix="Decimal"
            Constant="false" ExternalAccess="Read/Write">
          <Description><![CDATA[CV101 start pushbutton.]]></Description>
          <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="0"/></Data>
        </Tag>
        <Tag Name="CV101_Stop_PB" TagType="Base" DataType="BOOL" Radix="Decimal"
            Constant="false" ExternalAccess="Read/Write">
          <Description><![CDATA[CV101 stop pushbutton, normally closed.]]></Description>
          <Data Format="Decorated"><DataValue DataType="BOOL" Radix="Decimal" Value="1"/></Data>
        </Tag>
        <Tag Name="CV101_Starter" TagType="Base" DataType="MotorStarter"
            Constant="false" ExternalAccess="Read/Write"/>
      </Tags>
      <Routines>
        <Routine Name="MainRoutine" Type="RLL">
          <Description><![CDATA[Scheduler.]]></Description>
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[Evaluate mode selection first.]]></Comment>
              <Text><![CDATA[JSR(Mode_Logic,0);]]></Text>
            </Rung>
            <Rung Number="1" Type="N">
              <Comment><![CDATA[Then interlocks, which gate every run command.]]></Comment>
              <Text><![CDATA[JSR(Conveyor_Interlocks,0);]]></Text>
            </Rung>
            <Rung Number="2" Type="N">
              <Comment><![CDATA[Then the conveyor run logic.]]></Comment>
              <Text><![CDATA[JSR(Conveyor_Control,0);]]></Text>
            </Rung>
            <Rung Number="3" Type="N">
              <Comment><![CDATA[Alarms last so they reflect this scan.]]></Comment>
              <Text><![CDATA[JSR(Alarm_Logic,0);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>

        <Routine Name="Mode_Logic" Type="RLL">
          <Description><![CDATA[Operating mode selection.]]></Description>
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[Manual mode when the selector is in position 1.]]></Comment>
              <Text><![CDATA[EQU(ModeSelect,1)OTE(ManualMode);]]></Text>
            </Rung>
            <Rung Number="1" Type="N">
              <Comment><![CDATA[Auto mode when the selector is in position 2.]]></Comment>
              <Text><![CDATA[EQU(ModeSelect,2)OTE(AutoMode);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>

        <Routine Name="Conveyor_Interlocks" Type="RLL">
          <Description><![CDATA[Line 3 interlocks. Master permissive is built here.]]></Description>
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[ILK-001 Emergency stop. ESTOP_OK de-energises on trip,
so the permissive drops on loss of the circuit. Fail-safe.]]></Comment>
              <Text><![CDATA[XIC(ESTOP_OK)XIC(GRD_DR_CLSD)XIO(CV101_OL_TRIP)XIO(CV102_OL_TRIP)GRT(AIR_PRESS_PSI,40.0)OTE(LINE_PERMISSIVE);]]></Text>
            </Rung>
            <Rung Number="1" Type="N">
              <Comment><![CDATA[ILK-002 CV101 overload latches the conveyor interlocked bit.]]></Comment>
              <Text><![CDATA[XIC(CV101_OL_TRIP)OTL(CV101_Status.Interlocked);]]></Text>
            </Rung>
            <Rung Number="2" Type="N">
              <Comment><![CDATA[ILK-002 CV102 overload latches the conveyor interlocked bit.]]></Comment>
              <Text><![CDATA[XIC(CV102_OL_TRIP)OTL(CV102_Status.Interlocked);]]></Text>
            </Rung>
            <Rung Number="3" Type="N">
              <Comment><![CDATA[CV103 drive fault stops CV103. No entry for this exists in the
interlock schedule.]]></Comment>
              <Text><![CDATA[XIC(CV103_VFD_FLT)OTL(CV103_Status.Interlocked);]]></Text>
            </Rung>
            <Rung Number="4" Type="N">
              <Comment><![CDATA[Interlock reset requires manual mode and no active trip.]]></Comment>
              <Text><![CDATA[XIC(ManualMode)XIO(CV101_OL_TRIP)XIO(CV102_OL_TRIP)OTU(CV101_Status.Interlocked);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>

        <Routine Name="Conveyor_Control" Type="RLL">
          <Description><![CDATA[Run commands for CV101 to CV103.]]></Description>
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[CV101 run via the MotorStarter AOI, gated by the permissive.]]></Comment>
              <Text><![CDATA[MotorStarter(CV101_Starter,CV101_Start_PB,CV101_Stop_PB,LINE_PERMISSIVE,CV101_Status.Running);]]></Text>
            </Rung>
            <Rung Number="1" Type="N">
              <Comment><![CDATA[CV102 follows CV101 in auto.]]></Comment>
              <Text><![CDATA[XIC(AutoMode)XIC(CV101_Status.Running)XIC(LINE_PERMISSIVE)XIO(CV102_Status.Interlocked)OTE(CV102_Status.Running);]]></Text>
            </Rung>
            <Rung Number="2" Type="N">
              <Comment><![CDATA[CV103 discharge conveyor. NOTE: no LINE_PERMISSIVE contact
on this rung.]]></Comment>
              <Text><![CDATA[XIC(AutoMode)XIC(CV102_Status.Running)XIO(CV103_Status.Interlocked)OTE(CV103_Status.Running);]]></Text>
            </Rung>
            <Rung Number="3" Type="N">
              <Comment><![CDATA[Duplicate of rung 1 left in place during commissioning.]]></Comment>
              <Text><![CDATA[XIC(AutoMode)XIC(CV101_Status.Running)XIC(LINE_PERMISSIVE)XIO(CV102_Status.Interlocked)OTE(CV102_Status.Running);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>

        <Routine Name="Alarm_Logic" Type="RLL">
          <Description><![CDATA[Alarm bits for the HMI.]]></Description>
          <RLLContent>
            <Rung Number="0" Type="N">
              <Comment><![CDATA[Emergency stop alarm.]]></Comment>
              <Text><![CDATA[XIO(ESTOP_OK)OTE(ALM_ESTOP);]]></Text>
            </Rung>
            <Rung Number="1" Type="N">
              <Comment><![CDATA[CV101 overload alarm.]]></Comment>
              <Text><![CDATA[XIC(CV101_OL_TRIP)OTE(ALM_CV101_OL);]]></Text>
            </Rung>
            <Rung Number="2" Type="N">
              <Comment><![CDATA[Hopper level high alarm. ALM_HOPPER_HIGH is referenced here
but is not declared anywhere in this export.]]></Comment>
              <Text><![CDATA[GRT(HOPPER_LEVEL_PCT,90.0)OTE(ALM_HOPPER_HIGH);]]></Text>
            </Rung>
          </RLLContent>
        </Routine>
      </Routines>
    </Program>
  </Programs>

  <Tasks Use="Context">
    <Task Name="MainTask" Type="CONTINUOUS" Priority="10" Watchdog="500"
        DisableUpdateOutputs="false" InhibitTask="false">
      <Description><![CDATA[Continuous task for line 3.]]></Description>
      <ScheduledPrograms>
        <ScheduledProgram Name="MainProgram"/>
      </ScheduledPrograms>
    </Task>
  </Tasks>
</Controller>
</RSLogix5000Content>
"""


# ---------------------------------------------------------------------------
# Interlock schedule (XLSX)
# ---------------------------------------------------------------------------
INTERLOCK_HEADERS = [
    "Interlock ID",
    "Description",
    "Trip Tag",
    "Trip Condition",
    "Action",
    "Affected Equipment",
    "Fail-Safe Behaviour",
    "Notes",
]

INTERLOCK_ROWS: list[list[str]] = [
    [
        "ILK-001",
        "Emergency stop actuated",
        "ESTOP_OK",
        "ESTOP_OK = 0",
        "Stop all line 3 conveyors",
        "CV101, CV102, CV103",
        "De-energise to trip (circuit healthy = 1)",
        "Hard-wired safety relay; PLC logic is indication and permissive only.",
    ],
    [
        "ILK-002",
        "Conveyor motor overload",
        "CV101_OL_TRIP",
        "CV101_OL_TRIP = 1",
        "Stop CV101 and latch interlocked",
        "CV101",
        "Energise to trip",
        "Manual reset required in Manual mode.",
    ],
    [
        "ILK-003",
        "Downstream jam detected",
        "CV102_JAM",
        "CV102_JAM = 1",
        "Stop CV101 and CV102",
        "CV101, CV102",
        "Energise to trip",
        "Photo-eye blocked longer than 3 s.",
    ],
    [
        "ILK-004",
        "Guard door open",
        "GUARD_DOOR_CLOSED",
        "GUARD_DOOR_CLOSED = 0",
        "Drop line permissive",
        "CV101, CV102, CV103",
        "De-energise to trip",
        "Interlocked gate switch on the drive-end guard.",
    ],
    [
        "ILK-005",
        "Low plant air pressure",
        "AIR_PRESS_PSI",
        "AIR_PRESS_PSI < 60.0",
        "Drop line permissive",
        "CV101, CV102, CV103",
        "Energise to trip",
        "Diverter cannot actuate reliably below 60 psi.",
    ],
    [
        "ILK-007",
        "Surge hopper level abnormal",
        "",
        "",
        "Stop feed conveyor",
        "CV101",
        "",
        "Threshold to be confirmed by process engineering.",
    ],
]


def write_interlock_schedule(path: Path) -> None:
    """Interlock schedule with a hidden revision sheet, to exercise that warning."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.table import Table, TableStyleInfo

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Interlocks"

    sheet["A1"] = "SYNTHETIC FIXTURE - Conveyor Line 3 Interlock Schedule"
    sheet["A1"].font = Font(bold=True, size=13)
    sheet["A2"] = "Document: SYN-L3-ILK-001  Rev B  (invented data, not a real project)"
    sheet.merge_cells("A1:H1")

    header_row = 4
    for column, title in enumerate(INTERLOCK_HEADERS, start=1):
        cell = sheet.cell(row=header_row, column=column, value=title)
        cell.font = Font(bold=True)

    for offset, row in enumerate(INTERLOCK_ROWS, start=1):
        for column, value in enumerate(row, start=1):
            sheet.cell(row=header_row + offset, column=column, value=value)

    last_row = header_row + len(INTERLOCK_ROWS)
    table = Table(displayName="InterlockSchedule", ref=f"A{header_row}:H{last_row}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    sheet.add_table(table)

    for column, width in zip("ABCDEFGH", (12, 30, 22, 26, 32, 22, 34, 44), strict=True):
        sheet.column_dimensions[column].width = width

    revisions = workbook.create_sheet("Revisions")
    revisions.append(["Rev", "Date", "By", "Change"])
    revisions.append(["A", "2024-11-08", "SYNTHETIC", "Initial issue."])
    revisions.append(["B", "2025-01-03", "SYNTHETIC", "Added ILK-007 pending threshold."])

    superseded = workbook.create_sheet("Superseded")
    superseded.append(["Interlock ID", "Description", "Status"])
    superseded.append(["ILK-006", "Reserved - do not reuse", "withdrawn"])
    # A hidden sheet must be reported as a warning rather than silently skipped:
    # content an engineer cannot see is exactly what a review should flag.
    superseded.sheet_state = "hidden"

    workbook.save(path)


# ---------------------------------------------------------------------------
# I/O list (CSV)
# ---------------------------------------------------------------------------
IO_ROWS: list[list[str]] = [
    ["Tag", "Description", "Type", "Module", "Address", "Signal", "Engineering Units"],
    ["ESTOP_OK", "E-stop circuit healthy", "DI", "DI_01", "DI_01:I.Data.0", "24VDC", ""],
    ["GRD_DR_CLSD", "Guard door closed", "DI", "DI_01", "DI_01:I.Data.1", "24VDC", ""],
    ["CV101_OL_TRIP", "CV101 overload tripped", "DI", "DI_01", "DI_01:I.Data.2", "24VDC", ""],
    ["CV102_OL_TRIP", "CV102 overload tripped", "DI", "DI_01", "DI_01:I.Data.3", "24VDC", ""],
    ["CV103_VFD_FLT", "CV103 VFD fault", "DI", "DI_01", "DI_01:I.Data.4", "24VDC", ""],
    ["CV101_Start_PB", "CV101 start pushbutton", "DI", "DI_01", "DI_01:I.Data.5", "24VDC", ""],
    ["CV101_Stop_PB", "CV101 stop pushbutton (NC)", "DI", "DI_01", "DI_01:I.Data.6", "24VDC", ""],
    ["CV101_RUN_CMD", "CV101 run command", "DO", "DO_01", "DO_01:O.Data.0", "24VDC", ""],
    ["CV102_RUN_CMD", "CV102 run command", "DO", "DO_01", "DO_01:O.Data.1", "24VDC", ""],
    ["CV103_RUN_CMD", "CV103 run command", "DO", "DO_01", "DO_01:O.Data.2", "24VDC", ""],
    [
        "AIR_PRESS_PSI",
        "Line 3 header air pressure",
        "AI",
        "AI_01",
        "AI_01:I.Ch0Data",
        "4-20mA",
        "psi",
    ],
    ["HOPPER_LEVEL_PCT", "Surge hopper level", "AI", "AI_01", "AI_01:I.Ch1Data", "4-20mA", "%"],
    # Present in the I/O list but absent from the L5X: the interlock the schedule
    # documents as ILK-003 was wired but never implemented in logic.
    ["CV102_JAM", "CV102 jam photo-eye blocked", "DI", "DI_01", "DI_01:I.Data.7", "24VDC", ""],
]


def write_io_list(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["# SYNTHETIC FIXTURE - Conveyor Line 3 I/O list. Invented data."])
        writer.writerows(IO_ROWS)


# ---------------------------------------------------------------------------
# Alarm list (CSV, semicolon-delimited to exercise delimiter sniffing)
# ---------------------------------------------------------------------------
def write_alarm_list(path: Path) -> None:
    rows = [
        ["Alarm Tag", "Message", "Severity", "Group", "Source Tag"],
        ["ALM_ESTOP", "Emergency stop actuated", "Critical", "Safety", "ESTOP_OK"],
        ["ALM_CV101_OL", "CV101 motor overload", "High", "Conveyors", "CV101_OL_TRIP"],
        ["ALM_CV102_OL", "CV102 motor overload", "High", "Conveyors", "CV102_OL_TRIP"],
        ["ALM_HOPPER_HIGH", "Surge hopper level high", "Medium", "Process", "HOPPER_LEVEL_PCT"],
        ["ALM_CV102_JAM", "CV102 jam detected", "High", "Conveyors", "CV102_JAM"],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# HMI alarm export (XML)
# ---------------------------------------------------------------------------
HMI_XML = """<?xml version="1.0" encoding="utf-8"?>
<AlarmConfiguration Version="12.0" Project="SYNTHETIC_Line3_HMI">
  <Comment>SYNTHETIC FIXTURE. Invented HMI alarm export, not a real project.</Comment>
  <AlarmGroups>
    <AlarmGroup Name="Safety" DisplayOrder="1"/>
    <AlarmGroup Name="Conveyors" DisplayOrder="2"/>
    <AlarmGroup Name="Process" DisplayOrder="3"/>
  </AlarmGroups>
  <Alarms>
    <Alarm Name="ALM_ESTOP" Group="Safety" Severity="1" Latched="true"
        AckRequired="true" Expression="ESTOP_OK == 0">
      <Message>Emergency stop actuated - all line 3 conveyors stopped</Message>
    </Alarm>
    <Alarm Name="ALM_CV101_OL" Group="Conveyors" Severity="2" Latched="true"
        AckRequired="true" Expression="CV101_OL_TRIP == 1">
      <Message>CV101 motor overload - reset required in Manual</Message>
    </Alarm>
    <Alarm Name="ALM_CV102_OL" Group="Conveyors" Severity="2" Latched="true"
        AckRequired="true" Expression="CV102_OL_TRIP == 1">
      <Message>CV102 motor overload - reset required in Manual</Message>
    </Alarm>
    <Alarm Name="ALM_HOPPER_HIGH" Group="Process" Severity="3" Latched="false"
        AckRequired="false" Expression="HOPPER_LEVEL_PCT &gt; 90.0">
      <Message>Surge hopper level high</Message>
    </Alarm>
    <Alarm Name="ALM_CV102_JAM" Group="Conveyors" Severity="2" Latched="true"
        AckRequired="true" Expression="CV102_JAM == 1">
      <Message>CV102 jam detected - clear blockage before restart</Message>
    </Alarm>
    <Alarm Name="ALM_GUARD_OPEN" Group="Safety" Severity="1" Latched="true"
        AckRequired="true" Expression="GUARD_DOOR_CLOSED == 0">
      <Message>Guard door open - line permissive dropped</Message>
    </Alarm>
  </Alarms>
</AlarmConfiguration>
"""


# ---------------------------------------------------------------------------
# Functional description (DOCX)
# ---------------------------------------------------------------------------
def write_functional_description(path: Path) -> None:
    from docx import Document

    document = Document()
    document.core_properties.title = "Conveyor Line 3 Functional Description (SYNTHETIC)"
    document.core_properties.author = "SYNTHETIC FIXTURE"
    document.core_properties.comments = "Invented content for testing. Not a real project."

    document.add_heading("Conveyor Line 3 - Functional Description", level=0)
    document.add_paragraph(
        "SYNTHETIC FIXTURE. This document describes an invented conveyor line and "
        "exists only to exercise the review platform. It is not a real ESCO project."
    )

    document.add_heading("1. Scope", level=1)
    document.add_paragraph(
        "Line 3 comprises three belt conveyors, CV101 (feed), CV102 (transfer) and "
        "CV103 (discharge), together with a surge hopper and a pneumatic diverter."
    )

    document.add_heading("2. Operating Modes", level=1)
    document.add_paragraph(
        "A three-position selector provides Off, Manual and Auto. In Manual each "
        "conveyor is started and stopped individually from the local station. In Auto "
        "the conveyors start in discharge-to-feed sequence and stop in the reverse "
        "order."
    )
    document.add_paragraph("Off - all run commands are removed.", style="List Bullet")
    document.add_paragraph(
        "Manual - individual control, interlocks still apply.", style="List Bullet"
    )
    document.add_paragraph(
        "Auto - sequenced control from the line start command.", style="List Bullet"
    )

    document.add_heading("3. Interlocks and Permissives", level=1)
    document.add_paragraph(
        "All three conveyors are gated by a master line permissive. The permissive is "
        "dropped by any of: emergency stop actuation, guard door open, either "
        "conveyor motor overload, or low plant air pressure."
    )
    document.add_paragraph(
        "The emergency stop circuit is de-energise-to-trip: ESTOP_OK is TRUE only "
        "while the circuit is healthy, so a broken wire stops the line."
    )
    document.add_paragraph(
        "Low plant air pressure is defined as a header pressure below 60 psi. Below "
        "this value the diverter cannot be relied upon and the line permissive is "
        "dropped."
    )
    document.add_paragraph(
        "A jam detected by the CV102 photo-eye shall stop CV101 and CV102 while "
        "leaving CV103 running to clear the discharge."
    )

    document.add_heading("4. Interlock Summary", level=1)
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    headers = table.rows[0].cells
    for index, title in enumerate(("Interlock", "Condition", "Action", "Fail-safe")):
        headers[index].text = title
    for interlock, condition, action, failsafe in (
        ("ILK-001", "ESTOP_OK = 0", "Drop line permissive", "De-energise to trip"),
        ("ILK-002", "Motor overload", "Stop and latch the conveyor", "Energise to trip"),
        ("ILK-003", "CV102 jam", "Stop CV101 and CV102", "Energise to trip"),
        ("ILK-004", "Guard door open", "Drop line permissive", "De-energise to trip"),
        ("ILK-005", "Air pressure below 60 psi", "Drop line permissive", "Energise to trip"),
    ):
        cells = table.add_row().cells
        cells[0].text = interlock
        cells[1].text = condition
        cells[2].text = action
        cells[3].text = failsafe

    document.add_heading("5. Alarms", level=1)
    document.add_paragraph(
        "Alarms are annunciated on the line 3 HMI. Safety-group alarms latch and "
        "require acknowledgement."
    )

    document.add_heading("6. Open Items", level=1)
    document.add_paragraph(
        "The surge hopper level interlock (ILK-007) is not yet defined. The trip "
        "threshold and the affected equipment are to be confirmed by process "
        "engineering before commissioning."
    )

    document.save(path)


# ---------------------------------------------------------------------------
# PDFs, hand-assembled so byte offsets are exact and output is deterministic
# ---------------------------------------------------------------------------
def _build_pdf(objects: list[bytes]) -> bytes:
    """Assemble a minimal PDF with a correct cross-reference table.

    Written by hand rather than with a library so the fixture is byte-stable
    across environments, which keeps parser tests deterministic.
    """
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _text_page_content(lines: list[tuple[int, int, str]], font: str = "F1") -> bytes:
    parts = [b"BT\n"]
    for size, y, text in lines:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"/{font} {size} Tf 1 0 0 1 56 {y} Tm ({escaped}) Tj\n".encode("latin-1"))
    parts.append(b"ET")
    return b"".join(parts)


def write_testing_notes_pdf(path: Path) -> None:
    """A PDF with a real text layer, so extraction succeeds."""
    lines = [
        (14, 740, "SYNTHETIC FIXTURE - Conveyor Line 3 Pre-Commissioning Test Notes"),
        (10, 716, "Document SYN-L3-TST-001 Rev A. Invented content, not a real project."),
        (11, 686, "1. Emergency stop"),
        (10, 668, "   Actuated each station E-stop. Line permissive dropped in under 100 ms."),
        (10, 654, "   All three conveyors stopped. PASS."),
        (11, 626, "2. Motor overload"),
        (10, 608, "   Simulated CV101 overload. CV101 stopped and latched interlocked."),
        (10, 594, "   Reset accepted only in Manual mode. PASS."),
        (11, 566, "3. CV102 jam detection"),
        (10, 548, "   Blocked the CV102 photo-eye for 5 s. No conveyor stopped."),
        (10, 534, "   Expected CV101 and CV102 to stop per ILK-003. FAIL - raised as punch item"),
        (10, 520, "   PL3-014."),
        (11, 492, "4. Guard door"),
        (10, 474, "   Opened the drive-end guard. Line permissive dropped. PASS."),
        (11, 446, "5. Low air pressure"),
        (10, 428, "   Bled the header to 52 psi. Line kept running. Permissive dropped only"),
        (10, 414, "   at 39 psi. Threshold does not match the 60 psi in the functional"),
        (10, 400, "   description. Raised as punch item PL3-015."),
        (11, 372, "6. Surge hopper level"),
        (10, 354, "   Not tested. Interlock ILK-007 is undefined."),
    ]
    content = _text_page_content(lines)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
    ]
    path.write_bytes(_build_pdf(objects))


def write_scanned_pdf(path: Path) -> None:
    """A PDF with no text layer at all.

    Stands in for a scanned drawing. The PDF parser must report that manual
    review or OCR is required rather than silently returning an empty page: a
    quietly empty parse would let a review claim it examined a document it could
    not read.
    """
    content = (
        b"q 0.85 0.85 0.85 rg 56 520 500 220 re f Q\n"
        b"q 0.6 0.6 0.6 RG 2 w 56 520 500 220 re S Q\n"
        b"q 0.4 0.4 0.4 rg 96 700 180 8 re f 96 676 320 8 re f 96 652 240 8 re f Q\n"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
    ]
    path.write_bytes(_build_pdf(objects))


# ---------------------------------------------------------------------------
# Project package (ZIP)
# ---------------------------------------------------------------------------
def write_project_package(path: Path, members: dict[str, Path]) -> None:
    """A benign project package, as an engineer would actually send one."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in members.items():
            archive.write(source, arcname=arcname)
        archive.writestr(
            "README.txt",
            "SYNTHETIC FIXTURE package for Conveyor Line 3.\n"
            "Contains invented engineering documents for testing only.\n",
        )


# ---------------------------------------------------------------------------
# Ambiguous / low-value input, to exercise the 'unsupported' path
# ---------------------------------------------------------------------------
AMBIGUOUS_NOTES = """SYNTHETIC FIXTURE - loose site notes, Conveyor Line 3
=====================================================

Invented content. Not a real project.

- Ops mentioned the hopper "trips out sometimes" but nobody could say at what
  level. No tag was identified.
- Someone thought there used to be a second guard switch on the tail end. Not
  found on the I/O list or in the logic.
- Air pressure interlock: the panel drawing says 60 psi, the commissioning notes
  say it actually dropped out around 39 psi. Unresolved.
- CV103 drive fault behaviour is not written down anywhere.

These notes are deliberately vague. Nothing here is sufficient on its own to
support a technical finding, and a review that produced one from this alone
would be fabricating.
"""


def generate(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def record(path: Path) -> Path:
        written.append(path)
        return path

    l5x = record(target / "Conveyor_Line3.L5X")
    l5x.write_text(L5X, encoding="utf-8", newline="\n")

    interlocks = record(target / "Interlock_Schedule.xlsx")
    write_interlock_schedule(interlocks)

    io_list = record(target / "IO_List.csv")
    write_io_list(io_list)

    alarms = record(target / "Alarm_List.csv")
    write_alarm_list(alarms)

    hmi = record(target / "HMI_Alarm_Export.xml")
    hmi.write_text(HMI_XML, encoding="utf-8", newline="\n")

    functional = record(target / "Functional_Description.docx")
    write_functional_description(functional)

    testing = record(target / "Testing_Notes.pdf")
    write_testing_notes_pdf(testing)

    scanned = record(target / "Scanned_Drawing.pdf")
    write_scanned_pdf(scanned)

    notes = record(target / "Site_Notes_Ambiguous.txt")
    notes.write_text(AMBIGUOUS_NOTES, encoding="utf-8", newline="\n")

    package = record(target / "Line3_Project_Package.zip")
    write_project_package(
        package,
        {
            "logic/Conveyor_Line3.L5X": l5x,
            "documents/Interlock_Schedule.xlsx": interlocks,
            "documents/IO_List.csv": io_list,
        },
    )
    return written


def verify(paths: list[Path]) -> None:
    """Prove each generated artifact is readable by the library that will parse it."""
    from docx import Document
    from openpyxl import load_workbook
    from pypdf import PdfReader

    for path in paths:
        assert path.exists() and path.stat().st_size > 0, f"empty fixture: {path.name}"

    workbook = load_workbook(FIXTURES / "Interlock_Schedule.xlsx")
    assert "Interlocks" in workbook.sheetnames
    assert workbook["Superseded"].sheet_state == "hidden"

    document = Document(str(FIXTURES / "Functional_Description.docx"))
    assert any("60 psi" in p.text for p in document.paragraphs)

    text = "".join(
        page.extract_text() or "" for page in PdfReader(FIXTURES / "Testing_Notes.pdf").pages
    )
    assert "ILK-003" in text, "testing notes PDF has no extractable text layer"

    scanned = "".join(
        page.extract_text() or "" for page in PdfReader(FIXTURES / "Scanned_Drawing.pdf").pages
    )
    assert scanned.strip() == "", "scanned fixture must have no extractable text"

    with zipfile.ZipFile(FIXTURES / "Line3_Project_Package.zip") as archive:
        assert archive.testzip() is None
        assert "logic/Conveyor_Line3.L5X" in archive.namelist()

    from defusedxml.ElementTree import fromstring

    for xml_path in (FIXTURES / "Conveyor_Line3.L5X", FIXTURES / "HMI_Alarm_Export.xml"):
        fromstring(xml_path.read_bytes())

    with (FIXTURES / "IO_List.csv").open(encoding="utf-8") as handle:
        assert len(list(csv.reader(handle))) == len(IO_ROWS) + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=FIXTURES)
    args = parser.parse_args()

    written = generate(args.target)
    if args.target == FIXTURES:
        verify(written)

    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size:,} bytes)")
    print(f"\n{len(written)} synthetic fixtures generated and verified readable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
