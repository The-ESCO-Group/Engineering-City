# Evidence

Every technical finding in this product cites evidence. Not "the platform
analysed your project and found 14 issues", but "rung 0 of
`MainProgram/Conveyor_Interlocks` compares `AIR_PRESS_PSI` against `40.0`, while
row 9 of the interlock schedule specifies `< 60.0`" — with both quotations, both
locations, and a way for an engineer to open each one and check.

That is the whole product. An unsourced claim from a model is worthless here, and
worse than worthless if someone acts on it.

## The five kinds of evidence

`EvidenceKind` distinguishes things that must never be blurred together:

| Kind | Means | Example |
| --- | --- | --- |
| `direct` | Quoted from a source file | The rung text as exported |
| `inference` | Derived from direct evidence | "This rung has no permissive contact" |
| `missing_expected` | Looked for, not found | No rung anywhere references `CV102_JAM` |
| `parser_warning` | The parser could not read something | A hidden worksheet |
| `unsupported_content` | Deliberately not interpreted | A PDF page with no text layer |

`missing_expected` is the subtle one. "We found nothing" and "we looked for this
specific thing and it was absent" are different claims, and only the second can
support a finding. An anchor of this kind carries an `expectation` saying exactly
what was sought.

## Anchors

An `EvidenceAnchor` is a re-locatable pointer into one file.

| Field | Purpose |
| --- | --- |
| `anchor_key` | Parser-assigned, deterministic, unique within its file |
| `anchor_type` | The addressing scheme (`l5x_rung`, `xlsx_cell`, …) |
| `evidence_kind` | One of the five above |
| `source_label` | Which file, including archive member path |
| `display_location` | What a human reads |
| `structured_location` | What software matches on |
| `excerpt` | The quoted content |
| `content_hash` | SHA-256 of the excerpt at creation time |
| `parser_version` | Which parser produced it |

### display_location vs structured_location

Two audiences, two representations of the same place:

```
display_location:    "MainProgram / Conveyor_Interlocks / Rung 0"
structured_location: {"program": "MainProgram",
                      "routine": "Conveyor_Interlocks",
                      "rung": 0}
```

`display_location` must be followable. "Paragraph 12" is not — twelfth of what?
"`Functional_Description.docx` / 3. Interlocks and Permissives / paragraph 12"
is.

### Anchor keys

Deterministic and hierarchical, so a re-parse of the same bytes produces the same
keys and the anchors are updated in place rather than replaced. Findings that
cite them keep working across a parser upgrade.

| Parser | Key formats |
| --- | --- |
| L5X | `controller:<name>` · `program:<p>` · `routine:<p>/<r>` · `rung:<p>/<r>/<n>` · `instruction:<p>/<r>/<n>/<i>` · `tag:<scope>/<name>` · `module:<name>` · `aoi:<name>` |
| XLSX | `workbook` · `sheet:<name>` · `cell:<sheet>!<ref>` · `range:<sheet>!<a1>:<b2>` |
| CSV | `row:<n>` · `cell:<n>:<column>` |
| DOCX | `heading:<i>` · `paragraph:<i>` · `tablecell:<t>:<r>:<c>` |
| PDF | `page:<n>` · `textrange:<n>:<start>-<end>` |
| XML | `element:<xpath>` |
| Text | `file` · `line:<n>` · `section:<start>-<end>` |

Uniqueness is enforced by `(file_id, anchor_key)`. A parser that emits a
duplicate key is a bug: it makes one citation ambiguous. The persistence layer
logs it and keeps the first.

### content_hash

Fixes the excerpt at the moment the anchor was created. If a file is re-uploaded
and its content changed, the hash no longer matches and the discrepancy is
detectable — rather than an old finding silently re-pointing at new text that may
say something entirely different.

## The evidence graph

Individual anchors cite one file. A *discrepancy* exists between two, so
`app/core/services/evidence_graph.py` joins every parsed artifact in a project
into one structure: tags, documented interlocks, rungs, I/O points, alarms,
narrative statements, and the parser limitations carried forward from each file.

**The graph assembles facts and never draws conclusions.** Concretely:

* an interlock whose trip tag matches no declared tag gets `matched_tag=None`
  plus ranked `candidate_tags` — the graph does not decide it is a naming
  mismatch;
* a tag referenced but never declared gets `declared_in_logic=False` — the graph
  does not decide that is a fault;
* a documented threshold and a logic comparison are both recorded verbatim — the
  graph does not decide they contradict.

Each of those is an engineering judgement: an agent's to make, with evidence and
a stated confidence, and a human's to approve.

### Candidate matching

When a documented trip tag has no exact match, near-misses are offered for
consideration. Two measures, because the interesting real case is a contraction:

* `difflib` ratio over the normalised names;
* a subsequence check on word initials, so `GUARD_DOOR_CLOSED` surfaces
  `GRD_DR_CLSD` at 0.786 where sequence similarity alone would rank it too low.

Candidates are capped at five and thresholded at `SIMILARITY_THRESHOLD`. A
permissive list is not "thorough" — it buries the real match in noise, and an
agent will faithfully turn noise into findings an engineer has to disprove.

### Things the graph is careful about

Each of these exists because getting it wrong produces confident nonsense:

**UDT members.** `CV101_Status.Running` has no declaration of its own and never
will — it is a member of the declared tag `CV101_Status`. Without
`base_declared`, six UDT members read as undeclared references alongside the one
genuine case, and an engineer has to disprove all seven.

**Numeric literals.** `GRT(AIR_PRESS_PSI, 40.0)` has one tag and one constant.
Recording `40.0` as an undeclared tag manufactures a finding from nothing.

**Hidden sheets.** A row on a hidden worksheet is usually superseded. Dropping it
hides content from the engineer; counting it as current invents a requirement
that was deliberately withdrawn. It is kept, flagged `from_hidden_sheet`, and
excluded from the live count.

**Unreadable files.** A PDF with no text layer reaches the graph as a limitation,
so an agent can say "one drawing could not be read" instead of reviewing the
project as though it were empty.

**Missing inputs.** A project with no interlock schedule is a different thing
from a project with no discrepancies. `missing_inputs` names what was never
supplied.

## What evidence cannot establish

Stated here because agents are told it and reports repeat it:

* **An L5X export is not a controller.** It describes offline logic at export
  time. It cannot establish what a controller is currently running, its firmware
  behaviour, or whether the exported program was ever downloaded.
* **A parser records structure, not meaning.** `GRT(AIR_PRESS_PSI, 40.0)` is a
  mnemonic and two operands. That it constitutes a low-pressure trip at 40 psi is
  an interpretation.
* **Absence in an export is not absence in the plant.** A tag not declared in one
  export may exist in another program, another controller, or hard-wired logic
  that never appears in software at all.
* **A document says what it says.** That a functional description specifies 60
  psi does not make 60 psi correct — only documented.
