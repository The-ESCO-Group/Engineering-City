# Line 4 Packaging Conveyor Upgrade — ground truth

**Everything in this directory is invented.** There is no ESCO customer data in
this repository and none may be added. The plant, the customer, the purchase
order and every tag are fabricated; the fixture exists so the review pipeline can
be judged against a scenario whose correct answer is written down in advance.

Regenerate with:

```bash
python scripts/backend/make_line4_fixture.py
```

The generator is deterministic — no clock, no randomness — so two runs produce
byte-identical files. `backend/tests/test_line4_fixture.py` asserts that, and
asserts every claim this document makes about the bytes. **This file is the
contract. If it and the fixture disagree, the test fails.**

---

## The plant

A case-packaging line with a single accumulation zone.

| Item | Detail |
| --- | --- |
| CV401 | Infeed belt conveyor, driven by MTR401 |
| CV402 | Accumulation belt conveyor, driven by MTR402 |
| CV403 | Divert belt conveyor, driven by MTR403 |
| CV404 | Unpowered gravity roller discharge to the palletiser |
| MTR401–MTR403 | VFD-driven, run command + fault contact + speed reference |
| CYL401 | Pneumatic reject diverter on CV403, extend/retract proximity switches |
| CYL402 | Pneumatic stop gate at the head of CV404, open proximity switch |
| PE401 / PE402 | Accumulation zone entry / discharge photo-eyes |
| Controller | CompactLogix `5069-L330ER`, MajorRev 33 / MinorRev 11 as exported |
| I/O | `5069-IB16` slot 1, `5069-OB16` slot 2, `5069-IF8` slot 3, `5069-OF4` slot 4 |
| HMI | FactoryTalk View ME on a PanelView Plus 7 Standard 1000 |

## Artifacts

| File | Kind | What it is |
| --- | --- | --- |
| `Project_Description.docx` | DOCX | The customer's own brief. Prose, one deliberate ambiguity, and detail that appears nowhere else |
| `Functional_Description.docx` | DOCX | Modes, sequences, permissives, reset behaviour, interlock table |
| `IO_List.csv` | CSV | 26 points across DI/DO/AI/AO. Header on line 2; data on lines 3–28 |
| `Interlock_Matrix.xlsx` | XLSX | `Interlock Matrix` (7 interlocks) and `Cause and Effect`, both declared Excel tables, header on row 1, data on rows 2–8 |
| `Conveyor_Line4_Base.L5X` | L5X | The **existing** controller: 5 modules, 38 tags, 1 program, 6 routines, 24 rungs |
| `HMI_Screens.xml` | XML | FactoryTalk-shaped display export: 3 displays, 29 bound graphic elements |
| `Alarm_Register.csv` | CSV | 7 alarms from the **existing** terminal application. Header on line 2; data on lines 3–9 |
| `Approved_Templates.txt` | TEXT | Which ESCO templates the customer approved for this job |

### The one distinction the whole fixture turns on

`Conveyor_Line4_Base.L5X` is the controller **as it exists today**. Everything
else describes the **upgrade**. So:

* the `Scope` column of `Interlock Matrix` says `Existing` or `New (this upgrade)`;
* the `Status` column of `IO_List.csv` does the same for each point;
* `Alarm_Register.csv` is the *existing* register, not the upgrade's.

An `Existing` item that is missing from the base export is a **defect**. A
`New (this upgrade)` item that is missing from the base export is **expected**,
and reporting it is a false positive.

---

## The seven planted issues

Exactly seven. Nothing else in this fixture is wrong.

### 1. Missing interlock — ILK-404 accumulation zone full

| | |
| --- | --- |
| **Documented** | `Interlock_Matrix.xlsx`, sheet `Interlock Matrix`, **row 5** — `ILK-404`, trip tag `PE402_BLOCKED`, condition "PE402_BLOCKED = 1 continuously for 5 s", action "Stop CV401 infeed", scope **`Existing`**. Repeated on sheet `Cause and Effect`, **row 5**, and in `Functional_Description.docx` section 4 |
| **Wired** | `IO_List.csv` **row 10** — `PE402_BLOCKED`, `DI_401` slot 1 channel 7, `Local:1:I.Data.7`, status `Existing` |
| **Implemented** | Nowhere. The string `PE402` does not occur anywhere in `Conveyor_Line4_Base.L5X`: no tag declares it, no rung references it |
| **Expected finding** | `missing_in_logic` / `missing_logic` — an interlock that is documented as existing and is wired to a physical input is not implemented in the controller. Cite the matrix row, the I/O row and the absence |
| **Severity** | **High.** Cases back up into the divert zone with no automatic stop |

The absence must be cited as an absence — a `missing_expected` style claim — not
as "the rung is wrong". There is no rung.

### 2. Duplicate I/O assignment — `Local:1:I.Data.10`

| | |
| --- | --- |
| **Where** | `IO_List.csv` **row 13** (`GD401_CLOSED`, divert-zone guard door closed) and **row 18** (`MTR403_FAULT`, CV403 divert drive fault). Both read `DI_401`, slot `1`, channel `10`, address `Local:1:I.Data.10` |
| **Expected finding** | `duplicate_assignment` — two different field devices assigned to one physical channel. Only one can be wired; the other has no input |
| **Severity** | **High.** Both signals are safety-relevant: `GD401_CLOSED` drops the line permissive and `MTR403_FAULT` is one of the three drive fault contacts in the same rung |

Both rows are internally consistent (channel and address agree), so this is one
finding, not two. `Local:1:I.Data.10` is the only address that appears twice in
the file; every other address is unique. Both tags *are* declared in the L5X and
both *are* used in `Line4_Control / Permissives / Rung 0`, so the defect is in
the I/O assignment alone and not in the logic.

### 3. Inconsistent tag name — the CV401 run command

| | |
| --- | --- |
| **I/O list** | `IO_List.csv` **row 20** — device tag `MTR401_RUN_CMD`, `DO_401` slot 2 channel 0, `Local:2:O.Data.0`, "CV401 infeed drive run command" |
| **L5X** | Controller tag `CV401_Run_Output`, described as "CV401 infeed drive run command. Wired to DO_401 channel 0 (Local:2:O.Data.0)", written by `Line4_Control / Conveyor_Control / Rung 0` |
| **Expected finding** | `naming_mismatch` — the same signal carries two names across the document set |
| **Severity** | **Low to medium.** The signal *is* implemented and correctly wired. Raising this to high would be over-reach |

The two names do not normalise together: `normalise_tag_name` gives
`"mtr401 run cmd"` and `"cv401 run output"`. That is deliberate — a mismatch a
normaliser silently joins is not a mismatch anyone has to report.

What identifies them as one signal is that the descriptions match and the L5X
tag names the same physical address. What identifies it as a *defect* rather
than a house style is that the two sibling signals on the same card,
`MTR402_RUN_CMD` (row 21) and `MTR403_RUN_CMD` (row 22), carry the **same** name
in both documents.

### 4. Alarm trigger never driven — `ALM_CYL402_FAIL`

| | |
| --- | --- |
| **Defined** | `Alarm_Register.csv` **row 9** — `ALM-401-07`, trigger tag `ALM_CYL402_FAIL`, "CV404 stop gate failed to open", severity Medium, display `Line4_Alarms` |
| **Declared** | `Conveyor_Line4_Base.L5X`, controller tag `ALM_CYL402_FAIL`, BOOL |
| **Bound** | `HMI_Screens.xml`, display `Line4_Alarms`, element `ind_AlmStopGate` |
| **Driven** | Never. No `OTE`, `OTL` or `OTU` anywhere in the export targets it; the string `ALM_CYL402_FAIL` does not appear in any rung |
| **Expected finding** | `unreachable_alarm` / `missing_logic` — an alarm whose trigger tag no rung ever writes can never annunciate |
| **Severity** | **Medium** |

The input that would drive it *is* wired and *is* used: `CYL402_OPEN_PROX`
(`IO_List.csv` row 19) is read by `Line4_Control / Alarms / Rung 5`, which sets
the indication bit `CYL402_OPEN`. Only the fail-to-open detection is missing.
`Functional_Description.docx` section 6 states the alarm exists. A review that
reports this as one finding about CYL402 is correct; it is not two.

### 5. HMI binding to a missing tag — `Accum_Case_Count`

| | |
| --- | --- |
| **Where** | `HMI_Screens.xml`, display `Line4_Overview` (Number 10), element `<NumericDisplay Name="num_CasesAccumulated">`, connection `Value`, expression `{::[Line4]Accum_Case_Count}` |
| **Target** | `Accum_Case_Count` is declared nowhere — not in the L5X (controller or program scope), not in `IO_List.csv`, not in any other artifact |
| **Expected finding** | `unresolved_reference` — an HMI object bound to a tag the controller does not have. At runtime the object shows an error, not a value |
| **Severity** | **Medium** |

It is the **only** unresolvable binding in the file. All 28 others resolve,
including the two program-scope ones
(`{::[Line4]Program:Line4_Control.ManualMode}` and `...AutoMode`) and the
structure member `{::[Line4]CYL401_Extend_TMR.ACC}`.

### 6. Ambiguous requirement — the accumulation release

**Where:** `Project_Description.docx`, section *3. Scope of work*, second
paragraph:

> When the palletiser calls for product, CV402 shall release the accumulated
> cases to the discharge, and the stop gate shall close again once the zone is
> empty.

Two readings, both of which a controls engineer would defend:

* **Reading A — slug release.** The gate opens once and stays open until PE402
  reports the zone empty, discharging the whole accumulated queue back-to-back
  at line speed. No per-case timing is needed; the palletiser infeed has to be
  able to take cases nose-to-tail.
* **Reading B — metered release.** "Release the accumulated cases" describes the
  outcome, not one continuous opening: the gate cycles once per case until the
  zone is empty. This needs a per-case cycle time and a case-present interlock at
  the gate, and it changes the gate's duty cycle and the palletiser handshake.

Nothing settles it. `Functional_Description.docx` section 8 records it as not
settled, and the existing stop-gate logic
(`Line4_Control / Conveyor_Control / Rung 4`) implements neither — it holds the
gate open while the palletiser call is present, and the project description says
in the very next paragraph that this logic is to be replaced.

| | |
| --- | --- |
| **Expected finding** | `ambiguous_requirement` / `documentation_gap` — a clarification addressed to the customer, quoting the sentence and setting out both readings |
| **Severity** | **Medium.** It blocks design, not safety |

A review must **not** pick one reading and report the other as a defect, and must
not turn this into a technical claim about the existing logic.

### 7. Correctly implemented device — CYL401 reject diverter

**This is the false-positive control and the most important of the seven. A
correct review produces no findings about CYL401.**

The chain is complete and consistent end to end:

| Stage | Where | What it says |
| --- | --- | --- |
| Brief | `Project_Description.docx` §2 | "double-acting with extend and retract proximity switches … raise a fault if it does not confirm extension within **1.5** seconds" |
| Functional description | `Functional_Description.docx` §5 | Extend condition, the **1.5 s** confirm, the latched fault dropping the solenoid and stopping CV403, and the reset conditions |
| Interlock matrix | `Interlock_Matrix.xlsx`, `Interlock Matrix` **row 4** | `ILK-403`, trip tag `CYL401_EXT_PROX`, "not made within **1.5** s of CYL401_SOL energised", scope `Existing`, notes "Confirmation timer preset **1500** ms" |
| Cause and effect | `Interlock_Matrix.xlsx`, `Cause and Effect` **row 4** | Stop CV403, inhibit CYL401, alarm `ALM_CYL401_FAIL` |
| I/O | `IO_List.csv` rows **11**, **12**, **23** | `CYL401_EXT_PROX` `Local:1:I.Data.8`, `CYL401_RET_PROX` `Local:1:I.Data.9`, `CYL401_SOL` `Local:2:O.Data.3` |
| Logic | `Line4_Control / Diverter_Control` rungs **0–3** | Rung 0 energises `CYL401_SOL`; rung 1 `TON(CYL401_Extend_TMR,1500,0)`; rung 2 `OTL(ALM_CYL401_FAIL)`; rung 3 `OTU(ALM_CYL401_FAIL)` on reset PB **and** Manual **and** `CYL401_RET_PROX` |
| Logic | `Line4_Control / Conveyor_Control / Rung 2` | `XIO(ALM_CYL401_FAIL)` stops CV403 on a latched diverter fault |
| HMI | `HMI_Screens.xml`, display `Line4_Diverter` | Solenoid, extended, retracted, fault and reject-request indicators, plus the timer accumulator |
| Alarm | `Alarm_Register.csv` **row 8** | `ALM-401-06`, trigger `ALM_CYL401_FAIL`, "CV403 diverter failed to extend within **1.5** s — CV403 stopped", display `Line4_Diverter`, interlock `ILK-403` |

Every tag name is identical in every document. The confirm time is **1.5 s /
1500 ms** in all five places it appears. The fail-safe direction is consistent:
energise to trip, and the fault drops the solenoid so the cylinder returns on
spring.

Any finding raised against CYL401 is a false positive.

---

## What a correct review must **not** report

The fixture is built to catch over-reach as much as under-reach. None of the
following is a defect:

1. **Anything about CYL401.** See issue 7.
2. **ILK-405 (low plant air pressure) missing from the L5X.** Its `Scope` is
   `New (this upgrade)`, and `AIR_PRESS_PSI` (`IO_List.csv` row 25) has status
   `New (this upgrade)`. The base export is not supposed to contain it.
3. **ILK-406 (case jam at the CV402 zone entry) missing from the L5X.** Same
   reason: `Scope` is `New (this upgrade)`.
4. **`AIR_PRESS_PSI` having no controller tag.** New point, new interlock; the
   `5069-IF8` in slot 3 is described in the L5X as spare, which agrees.
5. **The jog-from-HMI request, the pressure transmitter and the jam detection
   in `Project_Description.docx` §3 not being implemented.** They are the scope
   of work, not omissions from it.
6. **`AutoMode` being written and never read by another rung.** It is a mode
   indication bit, bound on `Line4_Overview` as `ind_ModeAuto`.
7. **ILK-401 or ILK-402.** Both are documented and implemented consistently,
   both de-energise-to-trip on both sides, and both drop `LINE_PERMISSIVE` in
   `Line4_Control / Permissives / Rung 0`.
8. **ILK-407.** All three drive fault contacts are in that same rung, and each
   has an alarm rung in `Line4_Control / Alarms`.
9. **The `Cause and Effect` cells reading `new alarm to be added`.** They mark
   scope, not a missing alarm definition.
10. **The `ESCO-TPL-*` identifiers in `Approved_Templates.txt` having no
    counterpart in this repository.** That file records what the customer
    approved; it is an input, not a claim about the template registry.
11. **Runtime or firmware behaviour.** An L5X is an offline export. It says
    nothing about what a controller is running, and `MajorRev`/`MinorRev` are
    attributes of the export.

## Severity summary

| # | Issue | Class | Severity |
| --- | --- | --- | --- |
| 1 | ILK-404 documented and wired, never implemented | `missing_in_logic` | High |
| 2 | `Local:1:I.Data.10` assigned twice | `duplicate_assignment` | High |
| 3 | `MTR401_RUN_CMD` vs `CV401_Run_Output` | `naming_mismatch` | Low–medium |
| 4 | `ALM_CYL402_FAIL` never written | `unreachable_alarm` | Medium |
| 5 | `{::[Line4]Accum_Case_Count}` unresolvable | `unresolved_reference` | Medium |
| 6 | Accumulation release sentence | `ambiguous_requirement` | Medium |
| 7 | CYL401 diverter | **no finding** | — |

## Parser expectations

Every artifact parses cleanly with the real parsers. These counts are asserted
by `backend/tests/test_line4_fixture.py`, so a parser change that alters them is
visible rather than silent.

| File | Parser | Anchors | Warnings | Notes |
| --- | --- | --- | --- | --- |
| `Project_Description.docx` | `docx` | 26 | 0 | |
| `Functional_Description.docx` | `docx` | 60 | 0 | 1 table |
| `IO_List.csv` | `csv` | 265 | 2 | Both warnings are the parser's standing encoding and delimiter disclosures; 26 rows, 0 ragged, 1 unsupported item for the `#` banner line |
| `Interlock_Matrix.xlsx` | `xlsx` | 165 | 0 | Both tables are `named_table`; no hidden sheet, no merged cell, no formula |
| `Conveyor_Line4_Base.L5X` | `l5x` | 146 | 0 | 24 rungs, 38 tags, **0 unresolved references**, 0 unparsed rungs, 7 alarm references |
| `HMI_Screens.xml` | `xml` | 101 | 0 | 2 unsupported items, both standing capability boundaries |
| `Alarm_Register.csv` | `csv` | 70 | 2 | Same two disclosures; 7 rows, 0 ragged, 1 unsupported item for the `#` banner line |
| `Approved_Templates.txt` | `text` | 45 | 0 | 4 sections, every title `underlined` — none inferred |

The L5X having **zero** unresolved references matters: issues 4 and 5 are about
tags that are missing from *other* documents' point of view, and neither shows up
as a dangling operand inside the export. A review that finds them has to compare
files, which is the capability this fixture exists to test.
