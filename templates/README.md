# ESCO control-pattern templates

A **template** is an ESCO control pattern written down: the tags a motor starter
needs, the rungs that implement a valve travel timeout, and — just as important
— what the pattern deliberately does *not* do.

Templates are treated as code. They live in this repository, they are reviewed
in pull requests, they carry a semantic version, and every expansion records the
template id, the version and a checksum, so a generated rung can always answer
*which template produced this, and has that template changed since?*

The loader is `backend/app/generators/plc/template_registry.py`. The tests are
`backend/tests/generators/test_template_registry.py`.

---

## Layout

| Directory | Contents |
| --- | --- |
| `templates/rockwell/controller/` | Controller and program layouts — the task, program and routine skeleton a project starts from. |
| `templates/patterns/` | Equipment control patterns — the reusable blocks that go inside those routines. |

One template per file. The filename is not significant; the `id` inside is.

### What ships today

**Controller / program layout**

| File | Id | Families |
| --- | --- | --- |
| `rockwell/controller/compactlogix_machine.yaml` | `esco.controller.compactlogix.machine` | CompactLogix |
| `rockwell/controller/controllogix_process.yaml` | `esco.controller.controllogix.process` | ControlLogix |

**Equipment patterns**

| File | Id | Approval level |
| --- | --- | --- |
| `patterns/motor_standard.yaml` | `esco.motor.standard` | standard |
| `patterns/vfd_standard.yaml` | `esco.vfd.standard` | standard |
| `patterns/valve_standard.yaml` | `esco.valve.standard` | standard |
| `patterns/analog_input_scaling.yaml` | `esco.analog.input.scaling` | standard |
| `patterns/permissive_interlock.yaml` | `esco.permissive.interlock` | **senior review required** |
| `patterns/fault_reset.yaml` | `esco.fault.reset` | **senior review required** |
| `patterns/mode_selection.yaml` | `esco.mode.selection` | standard |
| `patterns/sequence_state_machine.yaml` | `esco.sequence.state_machine` | **senior review required** |
| `patterns/communication_loss.yaml` | `esco.communication.loss` | **senior review required** |
| `patterns/diagnostic.yaml` | `esco.diagnostic.controller` | standard |

The patterns target the routine names the controller layouts create
(`R_Devices`, `R_Permissives`, `R_Interlocks`, `R_Sequence`, `R_Mode_Select`,
`R_Alarm_Latch`, `R_AI_Scaling`, `R_AI_Validity`, `R_Scan_Diagnostics`,
`R_Comms_Diagnostics`), so a layout and the patterns expanded into it line up
without anybody having to reconcile two naming schemes.

---

## The checksum is computed, never written

**There is no `checksum:` key in a template file, and adding one would not be
read.** The checksum is a SHA-256 computed at load time over the canonically
serialised template body — sorted keys, fixed separators — so:

* two loads of the same file always produce the same digest;
* any change to any value changes it;
* reformatting the YAML, reordering keys or changing trailing whitespace does
  not change it, because the digest is taken over the parsed structure rather
  than the raw bytes.

This is deliberate. A checksum a human can type into the file records that
person's intent, not the content of the file — the two disagree the first time
someone edits a template and forgets to update the digest, and from then on the
field is worse than useless because it looks authoritative. Deriving it from the
content means it cannot be wrong.

An expansion records `template_id`, `template_version` **and** the checksum. The
first two say which pattern was used; the checksum proves the file has not
drifted since. `esco.motor.standard@1.0.0` with a different checksum from the
one recorded is a template that was edited without its version being bumped, and
that is a finding.

---

## Versioning

`MAJOR.MINOR.PATCH`, strictly three numeric parts. No pre-release or build
suffixes: a template either is the approved pattern or it is a different version
of it.

| Change | Bump |
| --- | --- |
| Generated logic changes — a rung added, removed or reordered, an instruction or operand changed | **MAJOR** |
| A required tag is added, removed, renamed or has its data type changed | **MAJOR** |
| A parameter is added as required, removed, renamed, or its type changed | **MAJOR** |
| A restriction is added, i.e. the pattern turns out to do less than it claimed | **MAJOR** |
| An optional parameter is added with a default that preserves existing behaviour | MINOR |
| A test is added | MINOR |
| A supported controller family is added | MINOR |
| Wording only — description, comments, engineering notes, tag descriptions | PATCH |
| A default value changes | **MAJOR** — it changes behaviour for every expansion that did not state the value |

The rule behind the table: **if an existing project re-expanded at the new
version would get different logic, or would need a different tag list, it is a
MAJOR bump.** When in doubt, bump major. Versions are cheap; a silently changed
control pattern is not.

Old versions stay in the repository. A project pins the version it was reviewed
against, and an approved expansion must keep meaning the same thing after a
newer template lands. To ship a new version, add a new file (e.g.
`motor_standard_v2.yaml`) carrying the same `id` and the new `version`; the
registry indexes by id *and* version and `get(id)` with no version returns the
highest.

### Withdrawing a version, which is different from superseding one

**A version that turns out to be defective is replaced in place, not kept
alongside its replacement.** The rule above exists so that a *correct* pattern
somebody approved keeps meaning what it meant. It does not apply to a pattern
that was wrong: leaving a sequence engine that deadlocks at step 1, or an
interlock trip nothing can clear, in the tree as a pinnable version means the
next project can pin it — and the registry gives no signal that one version of
an id is withdrawn and another is not.

So: edit the file, bump the version, and say in `engineering_notes` what the
defect was, what it did on a machine, and that expansions of the old version
must be re-expanded rather than patched in the project. The note is the whole
record — the old file is gone, and a reviewer holding a project pinned to the
withdrawn version needs to be able to read what was wrong with it.

Eleven of the twelve templates here are at **2.0.0** for this reason: the 1.0.0
set was reviewed rung by rung as ladder rather than as YAML, and each of those
files carries at the top of its `engineering_notes` what the review found and
what it did on a machine. `esco.diagnostic.controller` is the one that came
through unchanged, and is still at 1.0.0.

---

## File format

Every template is a single YAML mapping. `yaml.safe_load` only — no tags, no
custom constructors, no `!!python/…`.

```yaml
id: esco.motor.standard          # dotted lower-case, stable forever
version: 1.0.0                   # MAJOR.MINOR.PATCH
category: motor_control          # a TemplateCategory member
display_name: Standard motor start/stop
description: >
  One paragraph: what this pattern is and what shape of equipment it is for.
supported_families: [compactlogix, controllogix]   # ControllerFamily members
approval_level: standard         # a TemplateApprovalLevel member
restricted_domains: []           # RestrictedDomain members; see below

parameters:
  - name: device_tag
    type: identifier             # see the parameter types below
    required: true
    description: Device tag stem, e.g. MTR_101
  - name: has_run_feedback
    type: boolean
    required: false
    default: true                # only on optional parameters
    description: The starter reports it is running

required_tags:
  - role: command                # a TagRole member
    suffix: _Cmd_Start           # appended to the device tag stem
    data_type: BOOL              # a BaseDataType member
    description: Operator start request, momentary
    when_parameter: has_run_feedback   # optional: only needed when …
    when_value: true                   # … this parameter holds this value
    units: Hz                          # optional, for analog tags

generated_objects:
  tasks: []                      # controller layouts only
  programs: []                   # controller layouts only
  routines:
    - name: R_Devices
      language: ladder           # a RoutineLanguage member
      description: Device control blocks
  rungs:
    - routine: R_Devices
      title: "{device_tag} run request"
      comment: Why this rung is shaped the way it is.
      when_parameter: has_run_feedback   # optional, same rule as tags
      when_value: true
      elements: [ … ]            # see below

engineering_notes: >
  The reasoning a reviewer needs: why the logic is arranged this way, what the
  common mistake is, what has to be set during commissioning.

restrictions:
  - Plain statements of what this pattern does NOT do. Required, non-empty.

tests:
  - name: Start requires the permissive
    given: The motor is stopped and _Permissive is false
    when: _Cmd_Start is pulsed
    then: _Cmd_Run stays false.
```

### Vocabulary

`category`, `supported_families`, `approval_level`, `restricted_domains`,
`role`, `data_type`, `language`, `type` (on a program) and `type` (on a task)
are all members of the published enums in `backend/app/core/enums.py`. A value
outside those enums is a **load error**, not a pass-through — the loader names
the offending key and lists what was allowed.

`approval_level` is one of `standard`, `senior_review_required`,
`qualified_review_required`. Anything where a mistake would be dangerous is at
least `senior_review_required`. A template that names a `restricted_domain`
must be `qualified_review_required`; the loader enforces that.

### Parameter types

`string`, `identifier`, `tag_reference`, `boolean`, `integer`, `number`, `enum`.

`identifier` must match a Logix identifier; `tag_reference` also permits
structure members, bit selectors and array subscripts; `enum` requires a
`choices` list. A **required** parameter must not carry a `default` — that
combination lets an expansion proceed on a value nobody chose, and the loader
rejects it.

`Template.validate_parameters(values)` returns a sorted list of problems and an
empty list when the values are acceptable. It reports a missing required
parameter, an **unknown** parameter (usually a rename, and ignoring it would
silently expand with the old default), a wrong type, and a value outside an
enum's choices.

### Placeholders

Operands, rung titles and rung comments carry `{parameter}` placeholders:
`"{device_tag}_Cmd_Start"`. The registry does **not** substitute them. It ships
the pattern; expansion is a separate step, and that is where a value gets
recorded against a requirement and a human decision.

### Rung elements, and branches

An element is either an instruction or a set of parallel branches.

```yaml
# an instruction with one operand
- {instruction: XIC, operand: "{device_tag}_Cmd_Start", kind: condition}

# an instruction with several operands, in argument order
- instruction: TON
  operands: ["{device_tag}_Tmr", "{timeout_ms}", "0"]
  kind: output

# parallel branches: a list of legs, each leg a list of elements
- branch:
    - - {instruction: XIC, operand: "{device_tag}_Cmd_Start", kind: condition}
      - {instruction: XIC, operand: "{device_tag}_Permissive", kind: condition}
    - - {instruction: XIC, operand: "{device_tag}_Cmd_Run", kind: condition}
```

`kind` is a `LogicElementKind` — in practice `condition` or `output`.
`branch_start`, `branch_level` and `branch_end` are **not** used in a template:
branch structure is the nested `branch` key, and the loader rejects an element
that tries to express it with markers instead.

Ladder is a tree, and the nesting is explicit for that reason. A flat list with
start/end markers has to be reconstructed by whatever reads it, is silently
wrong when the markers do not balance, and cannot express a branch inside a
branch — which the seal-in rung of every device pattern in this directory needs.
A branch must have at least two legs; one leg is just a series of elements, and
the loader says so.

An instruction mnemonic must be a plain upper-case identifier (`XIC`, `TON`,
`MOV`, or the name of an Add-On Instruction). Whether the mnemonic is one this
platform actually supports is the instruction catalogue's business, not the
registry's.

### Conditional tags and rungs

`when_parameter` / `when_value` gate a required tag or a whole rung on a
parameter value, which is how one template covers a motor with run feedback and
a motor without it. The alternative — two near-identical templates — makes the
difference between them something a reader has to diff rather than something
stated. `Template.required_tags_for(values)` and `Template.rungs_for(values)`
resolve them.

### Internal consistency

The loader refuses a template whose own parts do not line up: a rung targeting a
routine the template never creates, a program listing a routine that does not
exist, a task listing a program that does not exist, or a `when_parameter`
naming a parameter that was never declared. Catching those here means the
failure is a load error naming the file rather than a generator crashing part
way through an expansion.

For a controller layout that creates several programs, routines are addressed as
`Program/Routine` (`Machine_Control/MainRoutine`), because two programs may each
hold a `MainRoutine`. Equipment patterns declare no program and address routines
by name alone.

---

## A bad file is recorded, not raised

`load_templates()` never raises. A file that cannot be read, is not valid YAML,
or does not describe a valid template is recorded in
`TemplateRegistry.load_errors` with its path and the reason, and every other
template still loads. One broken template must not stop the platform from
starting — it degrades to a clearly-reported missing pattern, the same posture
as plugin discovery in `backend/app/agents/base/plugins.py`.

Errors are addressed to whoever has to fix them:

```
patterns/motor_standard.yaml: 'version' must be MAJOR.MINOR.PATCH
patterns/valve_standard.yaml: rung 'XV_305 open request' targets routine
  'R_Valves', which this template does not create
```

---

## Adding a template

1. **Check it is not a variant of one that exists.** A parameter on an existing
   template is almost always better than a second template: two patterns that
   differ in one rung drift apart, and nobody notices until they disagree.
2. **Write the file** into `templates/patterns/` (or
   `templates/rockwell/controller/` for a layout). Start at `version: 1.0.0`.
3. **Write `restrictions` first, and make them plain.** They are not a
   disclaimer. They are the part a reviewer reads to decide whether this pattern
   fits the equipment in front of them, and the part that stops it being used
   where it does not belong. State what the pattern cannot detect, not only what
   it does not generate.
4. **Set `approval_level` honestly.** If a mistake in this pattern could hurt
   somebody or wreck equipment, it is `senior_review_required`. Fault reset,
   permissives and interlocks, communication loss and the sequence engine all
   are, and each says why in its notes.
5. **Write `engineering_notes` for the reviewer, not the author.** Why the logic
   is arranged this way, what the common mistake is, what commissioning has to
   set. The permissive-in-the-seal-in-leg note in `motor_standard.yaml` is the
   model.
6. **Write `tests` as behaviour.** `given` / `when` / `then`, with a `then` that
   is checkable. These become the generated test plan, so a vague `then` costs
   somebody a commissioning day.
7. **Read your own rungs as ladder, in scan order.** The load errors above catch
   a file that does not parse; they say nothing about whether the logic works.
   The questions that have actually caught defects here: does the rung driving
   this OTE stay true, or is it true for one scan? Is every OTL matched by an
   OTU somebody can reach? Does latching a fault remove the condition that was
   driving its own alarm? Does a held command re-seal equipment on its own? Is
   any bit written by two OTEs? `test_template_registry.py` asserts each of
   those over every shipped template — a new pattern that gets one wrong fails
   the suite with the reason.
8. **Run the checks.**

   ```
   .venv\Scripts\python -m ruff check backend/app/generators backend/tests/generators
   .venv\Scripts\python -m pytest backend/tests/generators/test_template_registry.py -q
   ```

   The suite loads every shipped template and fails on any load error, so a
   template that does not parse fails the build rather than disappearing quietly
   at runtime.
9. **Add it to the table at the top of this file.**

### Changing an existing template

Bump the version per the table above, and remember that the checksum changes on
its own — you cannot and need not touch it. If the change is MAJOR *and the old
version was correct*, add the new version as a new file and leave the old one in
place: projects pin versions, and an approved expansion has to keep meaning what
it meant when it was approved. If the change is MAJOR because the old version
was **wrong**, edit the file in place and follow the withdrawal rule above.

---

## Using the registry

```python
from app.core.enums import ControllerFamily, TemplateCategory
from app.generators.plc.template_registry import get_registry, template_for

motor = template_for("esco.motor.standard")            # newest version
pinned = template_for("esco.motor.standard", "1.0.0")  # exactly this one

motor.qualified_id()                       # 'esco.motor.standard@1.0.0'
motor.checksum                             # sha256 of the canonical body
motor.supports(ControllerFamily.CONTROLLOGIX)
motor.validate_parameters({"device_tag": "MTR_101"})   # [] means acceptable

registry = get_registry()
registry.by_category(TemplateCategory.MOTOR_CONTROL)
registry.for_family(ControllerFamily.COMPACTLOGIX)
registry.load_errors                       # always check this
```

`load_templates(root=…)` reads a different directory, which is how the tests
exercise malformed files without putting one in the shipped tree. The default
root is discovered from `__file__` relative to the repository — templates ship
with the code, so the code is what knows where they are. There is no environment
variable, deliberately: a template set that changes with an operator's shell is
one nobody can reproduce a generated project from.

---

## What a template is not

* **Not a substitute for engineering judgement.** A template is a starting point
  that has been reviewed once. Whether it fits *this* machine is still a
  decision somebody has to make and record.
* **Not safety logic.** Nothing in this directory is safety-rated, and every
  pattern says so in its restrictions. Safety functions belong in a safety
  controller, in safety-rated logic, assessed by a qualified person against the
  machine's risk assessment.
* **Not complete logic.** The patterns generate a block; the project's
  conditions, schedules and per-step behaviour are still written per project.
* **Not a promise that the output imports.** Everything this platform can prove
  offline stops at round-trip parsing. Import into Studio 5000 is verified on a
  licensed workstation or it is not verified.
