# Fixtures

**Everything in this directory is invented.** There is no ESCO customer data in
this repository and none may be added.

Regenerate with:

```bash
python scripts/backend/make_fixtures.py
```

The generator verifies every artifact is readable by the library that will parse
it, so a fixture that has silently broken fails at generation time rather than
inside a test.

## `demo_project/` — Conveyor Line 3

A fictional three-conveyor line (CV101 feed, CV102 transfer, CV103 discharge)
with a surge hopper and a pneumatic diverter.

| File | Kind | Purpose |
| --- | --- | --- |
| `Conveyor_Line3.L5X` | L5X | Controller, 3 modules, 1 UDT, 1 AOI, 5 routines, 16 rungs, controller + program tags |
| `Interlock_Schedule.xlsx` | XLSX | Interlock table, a named table, and a **hidden** `Superseded` sheet |
| `IO_List.csv` | CSV | 13 points across DI/DO/AI |
| `Alarm_List.csv` | CSV | **Semicolon**-delimited, to exercise delimiter sniffing |
| `HMI_Alarm_Export.xml` | XML | Generic XML with nested elements and repeated siblings |
| `Functional_Description.docx` | DOCX | Headings, paragraphs, bullets, a table, document properties |
| `Testing_Notes.pdf` | PDF | Real text layer; records the two test failures below |
| `Scanned_Drawing.pdf` | PDF | **No text layer** — must produce an OCR-required warning, never a silent empty parse |
| `Site_Notes_Ambiguous.txt` | TEXT | Deliberately too vague to support any finding |
| `Line3_Project_Package.zip` | ZIP | Benign package with nested paths |

## Ground truth: the designed discrepancies

This is what a correct review must produce. Every case is deliberate.

| Interlock | Designed state | Expected finding |
| --- | --- | --- |
| **ILK-001** E-stop | Documented and implemented consistently, de-energise-to-trip both sides | **none** — a correctly matched interlock, so a review that flags it is producing a false positive |
| **ILK-002** Motor overload | Documented and implemented for CV101 and CV102 | **none** — matched |
| **ILK-003** CV102 jam | In the schedule, the functional description and the I/O list (`CV102_JAM` is wired), but **no rung implements it** | `missing_logic` / `missing_in_logic`, high severity — the test notes record it as FAIL, punch item PL3-014 |
| **ILK-004** Guard door | Schedule and description say `GUARD_DOOR_CLOSED`; the logic and I/O list use **`GRD_DR_CLSD`** | `naming_mismatch` — the interlock *is* implemented, so severity should be low/medium, not high |
| **ILK-005** Low air pressure | Description and schedule specify **< 60 psi**; rung 0 of `Conveyor_Interlocks` compares `GRT(AIR_PRESS_PSI, 40.0)` | `interlock_discrepancy` (contradictory threshold) — the test notes confirm dropout at 39 psi, punch item PL3-015 |
| **ILK-006** | Withdrawn in the hidden `Superseded` sheet | not a finding in itself; the **hidden sheet** should raise a parser warning |
| **ILK-007** Hopper level | In the schedule with **no trip tag, no threshold, no fail-safe**; the description lists it as an open item | `documentation_gap` / ambiguous — must **not** be turned into a technical claim about the logic |
| *(none)* CV103 VFD fault | `XIC(CV103_VFD_FLT)OTL(CV103_Status.Interlocked)` exists in logic with **no schedule entry** | `undocumented_logic` |

### Additional planted defects

| Where | What | Expected finding |
| --- | --- | --- |
| `Alarm_Logic` rung 2 | `OTE(ALM_HOPPER_HIGH)` — the tag is **never declared** in the export | `unresolved_reference` |
| `Conveyor_Control` rung 3 | Byte-identical duplicate of rung 1, "left in place during commissioning" | `duplicate_pattern` |
| `Conveyor_Control` rung 2 | CV103 run has **no `LINE_PERMISSIVE` contact**, unlike CV101 and CV102 | `missing_logic` — CV103 can run with the line permissive dropped |
| `Interlock_Schedule.xlsx` | `Superseded` sheet is hidden | parser warning, not a finding |
| `Scanned_Drawing.pdf` | No extractable text | `parser_limitation` warning; **must not** be reported as "reviewed and clean" |
| `Site_Notes_Ambiguous.txt` | Vague hearsay, no tags, no thresholds | **no finding may cite this alone** — doing so would be fabrication |

### What a review must *not* do

The fixture is built to catch over-reach as much as under-reach:

* flag ILK-001 or ILK-002 (both correct) as discrepancies;
* raise ILK-004 to high severity — the interlock exists, only the name differs;
* assert a threshold for ILK-007, which has none;
* claim `Scanned_Drawing.pdf` was reviewed;
* derive a technical finding from `Site_Notes_Ambiguous.txt` alone;
* claim runtime or firmware behaviour from an L5X export.
