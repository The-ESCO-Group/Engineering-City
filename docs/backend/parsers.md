# Parsers

A parser turns one stored file into a normalised document plus a set of evidence
anchors. It is handed a path and settings and nothing else — no database session,
no network, no subprocess, no way to write anywhere except the result it returns.

## The three rules

From `backend/app/parsers/base.py`. These are product requirements, not style
guidance.

### 1. Never guess silently

Content that cannot be interpreted becomes a `ParserWarning` or an
`UnsupportedItem`. Never an omission.

A review that reports nothing because the parser quietly failed is worse than one
that says it could not read the file — the first is indistinguishable from a
clean result. This is why `Scanned_Drawing.pdf` produces a `no_text_layer`
warning *and* an unsupported item rather than an empty page: "we could not read
this" has to survive all the way into the report.

### 2. Never claim more than the format supports

Parsers record structure and text. They do not interpret, evaluate or conclude.

`GRT(AIR_PRESS_PSI, 40.0)` is recorded as mnemonic `GRT` with operands
`["AIR_PRESS_PSI", "40.0"]`. Not "trips below 40 psi". Not "contradicts the
documented 60 psi". Those are conclusions, and they belong to an agent that can
attach evidence and a confidence, and to a human who can approve them.

### 3. Be deterministic

The same bytes always produce the same content, summary and anchors, in the same
order. `content_hash` depends on it, tests depend on it, and a review that cannot
be reproduced cannot be defended.

No set iteration order, no timestamps, no absolute paths, nothing
machine-dependent in the output.

## The interface

```python
class MyParser:
    name = "myformat"
    version = "1.0.0"
    handles = (FileKind.MYFORMAT,)

    def parse(self, context: ParseContext) -> ParseResult: ...
```

`ParseResult` carries:

| Field | Contents |
| --- | --- |
| `content` | The normalised document |
| `summary` | Counts and shape hints, shown without loading `content` |
| `warnings` | Non-fatal problems, each with a `WarningCode` and a location |
| `unsupported` | Content deliberately not interpreted |
| `anchors` | `AnchorDraft` citations back into the source |

`version` is bumped whenever output changes in a way that invalidates stored
anchors. `ParsedArtifact` records it, so a re-parse after an upgrade is
detectable and the anchors are refreshed rather than orphaned.

## Registry

`parser_for(kind)` resolves a parser by name at call time. A kind whose parser is
absent, or whose third-party dependency is not installed, raises
`CapabilityUnavailableError` naming what is missing — reported through
`/api/system/capabilities` rather than breaking startup.

`FileKind.ZIP` has no parser: archives are expanded at intake and their members
are parsed individually.

## The seven parsers

| Kind | Extracts | Deliberately does not |
| --- | --- | --- |
| `l5x` | Controller, data types, modules, add-on instructions, tags (both scopes), programs, routines, a flat rung list with tokenised instructions, tasks, alarm references, unresolved references | Compile or evaluate logic; interpret AOI bodies; claim anything about a running controller |
| `xlsx` | Workbook properties, worksheets, used ranges, cells, formulas, merged regions, named tables, candidate structured tables | Evaluate formulas; hide a hidden sheet |
| `csv` | Detected encoding and delimiter, headers, rows with real file line numbers, skipped comment lines | Pad or truncate ragged rows silently |
| `docx` | Document properties, heading hierarchy, paragraphs with section paths, tables as header-keyed records, list items | Invoke Word; guess a list level it cannot determine |
| `pdf` | Per-page and per-line text with page numbers | **Use OCR.** A page with no text layer is reported, never silently empty |
| `xml` | Elements in document order with unambiguous XPaths, attributes, namespaces | Resolve entities or DTDs — both are disabled |
| `text` | Encoding, lines, blank-line paragraphs, underlined sections, bullets | Force structure onto unstructured notes |

### Candidate tables

The most load-bearing piece of the XLSX parser. A real interlock schedule is a
header row somewhere below a title block, followed by data. `candidate_tables`
exposes each data row as a `header -> value` record *with its real spreadsheet
row number*, so a finding can cite `Interlocks!C7` rather than "row 4 of the
table".

A sheet with data but no identifiable header row produces a
`structure_unrecognised` warning naming the sheet. Inventing a header would be
rule 1 and rule 2 at once.

### The PDF rule

The PDF parser has one behaviour that matters more than the rest: it never
silently uses OCR, and it never returns a quietly empty page.

A scanned drawing yields `no_text_layer`, an unsupported item, and a page anchor
marked `unsupported_content` with an expectation saying manual review or OCR is
required. The gap is *citable*. An agent can say "one drawing was not readable"
instead of reviewing the project as though that drawing were blank.

## Safety

All XML — L5X, HMI exports, alarm files, and the OOXML inside `.docx`/`.xlsx` —
is untrusted input.

`app/security/xml_safety.py` is the only place XML is parsed. DTDs and entities
are disabled, so external-entity (XXE) and entity-expansion (billion laughs)
attacks are refused rather than mitigated. Document size and nesting depth are
bounded. One implementation to audit, and no path around it.

Beyond that: no parser opens a path other than `context.path`, makes a network
call, runs a subprocess, or invokes Office automation.

## Warning codes

`WarningCode` in `base.py`. Stable — clients and reports branch on them.

`hidden_content` · `empty_document` · `no_text_layer` · `low_text_yield` ·
`encoding_guessed` · `delimiter_guessed` · `ragged_rows` · `merged_cells` ·
`formula_not_evaluated` · `unresolved_reference` · `unparsed_instruction` ·
`truncated` · `structure_unrecognised` · `password_protected`

## Adding a parser

1. Implement the `Parser` protocol in `app/parsers/<kind>_parser.py`.
2. Register `(module, class)` in `_PARSER_CLASSES` in `registry.py`.
3. Add the extension to `EXTENSION_KINDS` in
   `app/security/file_validation.py` — intake is an allowlist, so an
   unregistered extension is refused before it reaches a parser.
4. Write tests against a committed synthetic fixture, including: the ground truth
   it encodes, a determinism check, an empty input, and a malformed input.
5. Document the content shape and anchor keys here and in
   `docs/backend/evidence.md`.
