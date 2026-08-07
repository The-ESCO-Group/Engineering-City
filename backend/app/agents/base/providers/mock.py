"""The deterministic provider.

Makes the offline demonstration and the whole test suite possible: no network, no
model, no cost, and the same answer every time.

**It is not a model and does not pretend to be one.** It is a fixed rule engine
that reads the structured context the runner hands it and emits the same kind of
actions a model would, so that everything downstream — action validation,
evidence checks, the approval gate, event emission — is exercised end to end.

That distinction matters and is surfaced, not hidden. Every response carries a
warning saying the output came from the deterministic provider, findings it
produces are capped at modest confidence, and
``GET /api/health`` reports which providers are enabled. Nobody should be able to
mistake a MockProvider run for an engineering review.

The rules deliberately only restate what the evidence graph already established:
a documented interlock with no matching tag, a near-match under a different name,
a tag referenced but never declared, a passage one document records as unsettled.
Inventing anything beyond that would make the demo lie about what the platform can
do.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from app.agents.base.provider import ChatMessage, ProviderResponse, estimate_tokens
from app.core.enums import ProviderKind, QuestionCategory
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Attached to every response so a run can never be mistaken for a model's work.
MOCK_WARNING = (
    "Produced by the deterministic MockProvider, not a language model. The output "
    "restates what the evidence graph already established and must not be read as "
    "an engineering review."
)

#: Ceiling on any confidence this provider reports. A fixed rule that cannot weigh
#: context has no business claiming high confidence.
MAX_MOCK_CONFIDENCE = 0.55


class MockProvider:
    """Deterministic, offline stand-in for a model."""

    kind = ProviderKind.MOCK

    def __init__(self, model: str = "mock-deterministic-1") -> None:
        self.model = model

    def complete_structured(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        context = _latest_context(messages)
        agent_kind = str(context.get("agent_kind", ""))

        builder = _BUILDERS.get(agent_kind, _build_default)
        data = builder(context)
        data.setdefault("rationale", "")
        data.setdefault("actions", [])
        data.setdefault("warnings", [])
        if MOCK_WARNING not in data["warnings"]:
            data["warnings"].append(MOCK_WARNING)

        prompt_text = system + "".join(message.content for message in messages)
        return ProviderResponse(
            data=data,
            provider=self.kind.value,
            model=self.model,
            prompt_tokens=estimate_tokens(prompt_text),
            completion_tokens=estimate_tokens(json.dumps(data)),
            latency_ms=0,
            warnings=[MOCK_WARNING],
        )


def _latest_context(messages: list[ChatMessage]) -> dict[str, Any]:
    """The structured context the runner embedded in the last user message."""
    for message in reversed(messages):
        if message.role != "user":
            continue
        try:
            parsed = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _dedupe_key(*parts: str) -> str:
    """Stable identity for a finding, so a re-run updates rather than duplicates."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def _confidence(value: float) -> float:
    return round(min(value, MAX_MOCK_CONFIDENCE), 2)


# ---------------------------------------------------------------------------
# Per-agent rules
# ---------------------------------------------------------------------------
def _build_default(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "rationale": (
            "The deterministic provider has no rule for this agent, so it reports "
            "nothing rather than inventing a result."
        ),
        "actions": [
            {
                "action_type": "report_no_findings",
                "summary": "No deterministic rule applies to this agent.",
                "confidence": 0.0,
                "payload": {
                    "scope_examined": "Nothing was examined.",
                    "limitations": (
                        "The MockProvider has no rule for this agent kind, so this "
                        "is not evidence that the project is clean."
                    ),
                },
            }
        ],
    }


def _build_project_manager(context: dict[str, Any]) -> dict[str, Any]:
    """Plan which specialist reviews the supplied evidence actually supports.

    Deliberately makes no technical judgement — that boundary is the Project
    Manager Agent's whole remit.
    """
    graph = context.get("graph") or {}
    counts = graph.get("counts") or {}
    missing = list(graph.get("missing_inputs") or [])
    unreadable = list(graph.get("unreadable_files") or [])

    actions: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []

    if counts.get("rungs"):
        plan.append(
            {
                "kind": "review_plc",
                "title": "Review the control logic",
                "description": "Examine routines, rungs, tags and alarm references.",
                "agent_kind": "plc_review",
            }
        )
    if counts.get("interlocks") and counts.get("rungs"):
        plan.append(
            {
                "kind": "review_interlocks",
                "title": "Compare documented interlocks against the logic",
                "description": "Map each documented interlock to the logic implementing it.",
                "agent_kind": "interlock_review",
            }
        )

    for entry in plan:
        actions.append(
            {
                "action_type": "create_task",
                "summary": f"Plan: {entry['title']}",
                "confidence": _confidence(0.5),
                "payload": {**entry, "depends_on_indices": [], "priority": 50},
            }
        )

    if missing:
        actions.append(
            {
                "action_type": "post_message",
                "summary": "Expected inputs that were not supplied",
                "confidence": 0.0,
                "payload": {
                    "body": (
                        "These expected inputs were not supplied: "
                        + "; ".join(missing)
                        + ". Their absence limits what this review can cover."
                    ),
                    "context": {"missing_inputs": missing},
                },
            }
        )

    if unreadable:
        actions.append(
            {
                "action_type": "post_message",
                "summary": "Files that could not be read",
                "confidence": 0.0,
                "payload": {
                    "body": (
                        "These files were supplied but could not be read, so nothing "
                        "in them has been reviewed: "
                        + "; ".join(str(item.get("filename", "")) for item in unreadable)
                    ),
                    "context": {"unreadable_files": unreadable},
                },
            }
        )

    # A document that parsed but yielded nothing readable — a scanned drawing
    # with no text layer — never appears in ``unreadable_files``, because the
    # parse succeeded. Saying so is the difference between "reviewed and clean"
    # and "could not be read", which must never look the same to an engineer.
    partially_read = sorted(
        {
            str(item.get("filename", ""))
            for item in graph.get("limitations") or []
            if str(item.get("code")) in {"no_text_layer", "low_text_yield"}
        }
    )
    if partially_read:
        actions.append(
            {
                "action_type": "post_message",
                "summary": "Documents that yielded no readable content",
                "confidence": 0.0,
                "payload": {
                    "body": (
                        "No text could be extracted from: "
                        + "; ".join(partially_read)
                        + ". Nothing in them has been reviewed; they need manual "
                        "review or OCR before this review can be called complete."
                    ),
                    "context": {"partially_read_files": partially_read},
                },
            }
        )

    if not plan:
        actions.append(
            {
                "action_type": "report_blocker",
                "summary": "There is nothing the specialists can review.",
                "confidence": 0.0,
                "payload": {
                    "reason": (
                        "Neither control logic nor an interlock schedule could be "
                        "read, so no specialist review can proceed."
                    ),
                    "missing_inputs": missing,
                    "resolvable_by_upload": True,
                },
            }
        )

    return {
        "rationale": (
            f"Planned {len(plan)} specialist review(s) from what was actually "
            f"supplied: {counts.get('rungs', 0)} rung(s) and "
            f"{counts.get('interlocks', 0)} documented interlock(s)."
        ),
        "actions": actions,
    }


def _build_plc_review(context: dict[str, Any]) -> dict[str, Any]:
    """Report structural observations about the logic, each with an anchor."""
    graph = context.get("graph") or {}
    anchors = context.get("anchor_index") or {}
    usage = graph.get("tag_usage") or {}
    actions: list[dict[str, Any]] = []

    for name in usage.get("referenced_but_not_declared", []):
        tag = _tag(graph, name)
        rung_keys = list(tag.get("rung_keys") or []) if tag else []
        evidence = [anchors[key] for key in rung_keys if key in anchors]
        if not evidence:
            continue
        actions.append(
            {
                "action_type": "create_finding",
                "summary": f"'{name}' is used in logic but never declared",
                "evidence_anchor_ids": evidence,
                "confidence": _confidence(0.5),
                "payload": {
                    "title": f"Unresolved reference: {name}",
                    "category": "unresolved_reference",
                    "severity": "medium",
                    "fact": (
                        f"'{name}' appears as an operand on {len(rung_keys)} rung(s) "
                        f"in this export and is not declared as a tag anywhere in it."
                    ),
                    "inference": (
                        "The reference may resolve against a tag defined outside this "
                        "export, or it may be genuinely undefined."
                    ),
                    "suggested_review_action": (
                        f"Confirm whether '{name}' is declared in the live project. An "
                        "export cannot show whether it exists there."
                    ),
                    "limitations": (
                        "Derived from an offline export by a deterministic rule. It "
                        "establishes nothing about what the controller is running."
                    ),
                    "evidence": [
                        {"anchor_id": anchor_id, "role": "logic"} for anchor_id in evidence
                    ],
                    "dedupe_key": _dedupe_key("plc", "unresolved", name),
                },
            }
        )

    duplicates = _duplicate_rungs(graph)
    for text, keys in duplicates:
        evidence = [anchors[key] for key in keys if key in anchors]
        if len(evidence) < 2:
            continue
        actions.append(
            {
                "action_type": "create_finding",
                "summary": "Two rungs have identical instruction text",
                "evidence_anchor_ids": evidence,
                "confidence": _confidence(0.5),
                "payload": {
                    "title": "Duplicate rung logic",
                    "category": "duplicate_pattern",
                    "severity": "low",
                    "fact": (
                        f"{len(evidence)} rungs carry byte-identical instruction text: {text[:160]}"
                    ),
                    "inference": (
                        "A duplicated rung is often left behind during commissioning. "
                        "Whether it is redundant or deliberate cannot be determined "
                        "from the export."
                    ),
                    "suggested_review_action": (
                        "Confirm with the author whether the duplicate is intended."
                    ),
                    "limitations": "Textual comparison only; no logic was evaluated.",
                    "evidence": [
                        {"anchor_id": anchor_id, "role": "logic"} for anchor_id in evidence
                    ],
                    "dedupe_key": _dedupe_key("plc", "duplicate", text),
                },
            }
        )

    if not actions:
        actions.append(
            {
                "action_type": "report_no_findings",
                "summary": "No structural observations from the deterministic rules.",
                "confidence": 0.0,
                "payload": {
                    "scope_examined": (
                        f"{graph.get('counts', {}).get('rungs', 0)} rung(s) across the "
                        "supplied export."
                    ),
                    "limitations": (
                        "Only fixed structural rules were applied. This is not "
                        "evidence that the logic is correct."
                    ),
                },
            }
        )

    return {
        "rationale": (
            "Applied fixed structural rules to the normalised export: undeclared "
            "operands and byte-identical rungs. No logic was evaluated."
        ),
        "actions": actions,
    }


def _build_interlock_review(context: dict[str, Any]) -> dict[str, Any]:
    """Compare each documented interlock against the logic, citing both sides."""
    graph = context.get("graph") or {}
    anchors = context.get("anchor_index") or {}
    actions: list[dict[str, Any]] = []
    matrix: list[dict[str, Any]] = []

    for requirement in graph.get("interlocks") or []:
        if not requirement.get("is_current", True):
            continue
        identifier = str(requirement.get("identifier", ""))
        requirement_anchors = [
            anchors[str(source.get("anchor_key"))]
            for source in requirement.get("sources") or []
            if str(source.get("anchor_key")) in anchors
        ]
        logic_anchors = [
            anchors[key] for key in requirement.get("related_rung_keys") or [] if key in anchors
        ]

        if not requirement.get("is_specified"):
            matrix.append(
                {
                    "interlock_id": identifier,
                    "comparison_result": "ambiguous",
                    "severity": "informational",
                    "confidence": 0.0,
                }
            )
            if requirement_anchors:
                actions.append(_documentation_gap(identifier, requirement, requirement_anchors))
            continue

        if requirement.get("matched_tag"):
            matrix.append(
                {
                    "interlock_id": identifier,
                    "comparison_result": "matched",
                    "severity": "informational",
                    "confidence": _confidence(0.5),
                }
            )
            continue

        # Only a *strong* candidate is treated as the same signal under another
        # name. Raw sequence similarity is not enough: CV102_Status scores 0.67
        # against CV102_JAM purely because they share a prefix, and accepting it
        # would turn a documented interlock that is genuinely absent from the
        # logic into a mere naming inconsistency — downgrading it out of the
        # severity band that requires a human decision. When the signal is weak
        # the honest answer is "not found", which escalates.
        candidates = [
            candidate
            for candidate in requirement.get("candidate_tags") or []
            if str(candidate.get("reason", "")) != "sequence similarity"
        ]
        if candidates and logic_anchors and requirement_anchors:
            actions.append(
                _naming_mismatch(
                    identifier, requirement, candidates[0], requirement_anchors, logic_anchors
                )
            )
            matrix.append(
                {
                    "interlock_id": identifier,
                    "comparison_result": "naming_mismatch",
                    "severity": "medium",
                    "confidence": _confidence(0.45),
                }
            )
            continue

        if requirement_anchors:
            actions.append(_missing_in_logic(identifier, requirement, requirement_anchors))
            matrix.append(
                {
                    "interlock_id": identifier,
                    "comparison_result": "missing_in_logic",
                    "severity": "high",
                    "confidence": _confidence(0.5),
                }
            )

    if not actions:
        actions.append(
            {
                "action_type": "report_no_findings",
                "summary": "Every documented interlock matched a declared tag.",
                "confidence": 0.0,
                "payload": {
                    "scope_examined": f"{len(matrix)} documented interlock(s).",
                    "limitations": (
                        "Name matching only. A matched name does not establish that "
                        "the logic implements the documented behaviour."
                    ),
                },
            }
        )

    return {
        "rationale": (
            f"Compared {len(matrix)} current documented interlock(s) against the "
            "declared tags and the rungs referencing them."
        ),
        "actions": actions,
        "discrepancy_matrix": matrix,
    }


def _missing_in_logic(
    identifier: str, requirement: dict[str, Any], requirement_anchors: list[str]
) -> dict[str, Any]:
    """A documented interlock with nothing in the logic that implements it.

    Rated high and flagged for approval. If the finding is wrong an engineer
    spends a minute dismissing it; if it is right and was not escalated, a
    documented protection is missing and nobody was told.
    """
    tag = requirement.get("trip_tag", "")
    return {
        "action_type": "create_finding",
        "summary": f"{identifier}: no logic references the documented trip tag",
        "evidence_anchor_ids": requirement_anchors,
        "confidence": _confidence(0.5),
        "requires_approval": True,
        "payload": {
            "title": f"{identifier} is documented but not implemented",
            "category": "missing_logic",
            "severity": "high",
            "fact": (
                f"The interlock schedule specifies {identifier} with trip tag "
                f"'{tag}' and condition '{requirement.get('trip_condition', '')}'. "
                f"No tag of that name is declared in the supplied export and no rung "
                f"references it."
            ),
            "inference": (
                "The documented interlock may not be implemented in the supplied "
                "logic, or it may be implemented under a different name, in another "
                "program, or in hard-wired circuitry outside this export."
            ),
            "suggested_review_action": (
                f"Confirm with the controls engineer whether {identifier} is "
                f"implemented, and where. If it is not, assess the safety impact "
                f"before the line runs."
            ),
            "limitations": (
                "Based on name matching against one export. Absence here is not "
                "absence in the plant."
            ),
            "evidence": [
                {"anchor_id": anchor_id, "role": "requirement"} for anchor_id in requirement_anchors
            ],
            "dedupe_key": _dedupe_key("interlock", "missing", identifier),
        },
    }


def _naming_mismatch(
    identifier: str,
    requirement: dict[str, Any],
    candidate: dict[str, Any],
    requirement_anchors: list[str],
    logic_anchors: list[str],
) -> dict[str, Any]:
    documented = requirement.get("trip_tag", "")
    implemented = candidate.get("name", "")
    return {
        "action_type": "create_finding",
        "summary": f"{identifier}: documented tag name differs from the logic",
        "evidence_anchor_ids": [*requirement_anchors, *logic_anchors],
        "confidence": _confidence(0.45),
        "payload": {
            "title": f"{identifier}: '{documented}' documented, '{implemented}' in logic",
            "category": "naming_mismatch",
            "severity": "medium",
            "fact": (
                f"The schedule names the trip tag '{documented}'. No tag of that name "
                f"is declared. A declared tag '{implemented}' is referenced by "
                f"{len(logic_anchors)} rung(s)."
            ),
            "inference": (
                "The two names may denote the same signal. That is a judgement about "
                "the plant, not something the documents establish."
            ),
            "suggested_review_action": (
                f"Confirm whether '{implemented}' is the implementation of "
                f"{identifier}. If so, align the schedule and the logic on one name."
            ),
            "limitations": (
                "Name similarity only. The interlock does appear to be implemented, "
                "so this is a documentation consistency issue rather than a missing "
                "protection."
            ),
            "evidence": [
                *(
                    {"anchor_id": anchor_id, "role": "requirement"}
                    for anchor_id in requirement_anchors
                ),
                *({"anchor_id": anchor_id, "role": "logic"} for anchor_id in logic_anchors),
            ],
            "dedupe_key": _dedupe_key("interlock", "naming", identifier),
        },
    }


def _documentation_gap(
    identifier: str, requirement: dict[str, Any], requirement_anchors: list[str]
) -> dict[str, Any]:
    missing = ", ".join(requirement.get("missing_fields") or [])
    return {
        "action_type": "create_finding",
        "summary": f"{identifier} is too underspecified to compare against logic",
        "evidence_anchor_ids": requirement_anchors,
        "confidence": _confidence(0.5),
        "payload": {
            "title": f"{identifier} is incompletely specified",
            "category": "documentation_gap",
            "severity": "low",
            "fact": (f"The schedule row for {identifier} leaves these fields blank: {missing}."),
            "inference": (
                "Without a trip tag and a condition there is nothing to compare "
                "against the logic, so no conclusion about implementation is possible."
            ),
            "suggested_review_action": (
                f"Complete the {identifier} row before the interlock can be reviewed."
            ),
            "limitations": (
                "This is a statement about the document only. It says nothing about "
                "whether any protection exists."
            ),
            "evidence": [
                {"anchor_id": anchor_id, "role": "requirement"} for anchor_id in requirement_anchors
            ],
            "dedupe_key": _dedupe_key("interlock", "gap", identifier),
        },
    }


def _build_senior_review(context: dict[str, Any]) -> dict[str, Any]:
    """Check each finding's evidence and lower confidence where it is thin.

    Only ever adds doubt. A rule engine has no basis for raising confidence in
    someone else's claim, and the review layer must not be able to manufacture
    certainty.
    """
    findings = context.get("findings") or []
    actions: list[dict[str, Any]] = []
    seen: dict[str, str] = {}

    for finding in findings:
        finding_id = str(finding.get("id"))
        title = str(finding.get("title", ""))
        evidence_count = int(finding.get("evidence_count") or 0)
        has_direct = bool(finding.get("has_direct_evidence"))
        limitations = list(finding.get("parser_limitations") or [])

        key = str(finding.get("dedupe_key") or title)
        if key in seen:
            actions.append(
                {
                    "action_type": "update_finding_review",
                    "summary": f"Duplicate of an earlier finding: {title}",
                    "confidence": _confidence(0.5),
                    "payload": {
                        "finding_id": finding_id,
                        "verdict": "duplicate",
                        "duplicate_of_finding_id": seen[key],
                        "notes": "Same identity as a finding already recorded.",
                    },
                }
            )
            continue
        seen[key] = finding_id

        if evidence_count == 0:
            verdict, notes, adjusted = (
                "unsupported",
                "No evidence anchor supports this finding, so it cannot be released.",
                0.0,
            )
        elif not has_direct:
            verdict, notes, adjusted = (
                "needs_more_evidence",
                "Every anchor is an inference; no direct quotation supports the claim.",
                0.2,
            )
        elif limitations:
            verdict, notes, adjusted = (
                "needs_more_evidence",
                "A parser limitation affects the evidence: " + "; ".join(limitations[:3]),
                0.3,
            )
        else:
            verdict, notes, adjusted = (
                "supported",
                "Evidence anchors resolve and include a direct quotation.",
                min(float(finding.get("confidence") or 0.0), MAX_MOCK_CONFIDENCE),
            )

        actions.append(
            {
                "action_type": "update_finding_review",
                "summary": f"{verdict}: {title}",
                "confidence": _confidence(0.5),
                "payload": {
                    "finding_id": finding_id,
                    "verdict": verdict,
                    "notes": notes,
                    "adjusted_confidence": round(
                        min(adjusted, float(finding.get("confidence") or 1.0)), 2
                    ),
                    "parser_limitations": limitations[:5],
                },
            }
        )

    return {
        "rationale": (
            f"Checked {len(findings)} finding(s) for evidence, duplication and parser "
            "limitations. Confidence was only ever reduced."
        ),
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# The production agents
#
# These three propose changes to the canonical model rather than findings, which
# raises the stakes: a finding is a sentence a human reads, and a merged
# operation is a row every later stage builds on. The rules below are therefore
# narrower than the review ones, not wider. Each proposed object is a
# transcription of something already recorded — a schedule row, an approved
# device, an accepted requirement — and where the honest answer is a judgement,
# these ask a question instead of making one up. Nothing here selects a control
# pattern, classifies a requirement, or decides what a device is for.
# ---------------------------------------------------------------------------
def _build_requirements_planning(context: dict[str, Any]) -> dict[str, Any]:
    """Record the interlock schedule's rows as requirements, and ask about the gaps.

    A documented interlock is the one place in an uploaded set where the customer
    wrote a requirement down in a structured, anchored form: an identifier, a
    trip tag, a condition and an action, each traceable to the row it came from.
    Restating those is transcription, which is all this provider does.

    Reading *requirements* out of the narrative documents is deliberately not
    attempted. "Is this sentence a requirement or is it background" is the one
    judgement this agent exists to make, and a fixed rule that guessed at it
    would fill the model with untestable items — exactly the failure the real
    prompt spends most of its length warning against.

    Asking a question about a narrative sentence is a different act, and
    :func:`_deferred_passages` is the one narrative rule here: it reports the
    passages a *companion* document has already recorded as unsettled. That is
    still transcription — the document set says the requirement is open, and this
    restates it — and a question, unlike a requirement, adds nothing to the model
    that a later stage would build on.

    Nothing here is accepted, confirmed or resolved: every requirement is left at
    its default ``proposed`` and every gap becomes an open clarification.
    """
    anchors = context.get("anchor_index") or {}
    existing = context.get("existing") or {}
    in_use = existing.get("references_in_use") or {}
    taken = {str(item) for item in in_use.get("requirement") or []}
    asked = {str(item.get("question", "")) for item in existing.get("open_clarifications") or []}

    operations: list[dict[str, Any]] = []
    cited: list[str] = []
    transcribed = 0
    gaps = 0
    deferred = 0

    for interlock in (context.get("graph") or {}).get("documented_interlocks") or []:
        # Scope is not a filter here, and that is the difference between this
        # consumer and the review ones. ``is_current`` answers "should the
        # supplied logic already implement this row", so it excludes a row scoped
        # to the upgrade — but a row the upgrade adds is exactly a requirement of
        # the upgrade, and dropping it leaves the interlocks this job was raised
        # to build absent from the requirements model. A hidden worksheet is
        # different: it is superseded or working content, and transcribing a row
        # from one would record as asked-for something an engineer opening the
        # workbook cannot even see.
        if interlock.get("from_hidden_sheet"):
            continue
        identifier = str(interlock.get("identifier") or "").strip()
        if not identifier:
            continue
        anchor = _first_anchor(interlock.get("sources") or [], anchors)
        if anchor is None:
            # No anchor means nothing to cite, and a requirement that cites
            # nothing makes an invention look traceable.
            continue

        if interlock.get("is_specified"):
            reference = f"REQ-{identifier}"[:64]
            if reference in taken:
                continue
            taken.add(reference)
            operations.append(_requirement_operation(reference, interlock, anchor))
            cited.append(anchor)
            transcribed += 1
            continue

        question = _gap_question(identifier, interlock)
        if question in asked:
            continue
        asked.add(question)
        operations.append(_clarification_operation(identifier, interlock, question, anchor))
        cited.append(anchor)
        gaps += 1

    for passage in _deferred_passages(context.get("documents") or [])[:_MAX_MOCK_CLARIFICATIONS]:
        question = _deferral_question(passage)
        if question in asked:
            continue
        asked.add(question)
        operations.append(_deferral_operation(passage, question))
        cited.extend((passage.passage_anchor, passage.notice_anchor))
        deferred += 1

    rationale = (
        f"Transcribed {transcribed} documented interlock(s) into requirements and "
        f"raised {gaps} clarification(s) for rows the schedule left incomplete, plus "
        f"{deferred} for passages a companion document records as unsettled. No "
        f"requirement was read out of narrative text: deciding that a sentence is a "
        f"requirement rather than background is a judgement this provider does not make."
    )

    if not operations:
        return {
            "rationale": rationale,
            "actions": [
                {
                    "action_type": "complete_task",
                    "summary": "Nothing new to record from the documents supplied.",
                    "confidence": 0.0,
                    "payload": {
                        "outcome": (
                            "Every documented interlock is already recorded as a "
                            "requirement or a clarification, or none carried an "
                            "evidence anchor to cite, and no document records a "
                            "passage of another as unsettled."
                        ),
                        "limitations": [
                            "Only the interlock schedule was read for requirements, and "
                            "the narrative documents only for passages another document "
                            "records as unsettled. This is not a complete requirements "
                            "model.",
                            MOCK_WARNING,
                        ],
                    },
                }
            ],
        }

    return {
        "rationale": rationale,
        "actions": [
            {
                "action_type": "propose_changeset",
                "summary": (
                    f"Record {transcribed} documented interlock(s) as requirements "
                    f"and {gaps + deferred} open question(s) as clarifications."
                ),
                "evidence_anchor_ids": sorted(set(cited)),
                "confidence": _confidence(0.5),
                "payload": {
                    "title": "Requirements transcribed from the interlock schedule",
                    "summary": (
                        "One requirement per fully specified documented interlock, one "
                        "clarification per incomplete row and per passage a companion "
                        "document records as unsettled. Each cites the row or paragraph "
                        "it was read from."
                    ),
                    "rationale": rationale,
                    "base_revision_number": int(context.get("base_revision_number") or 0),
                    "operations": operations,
                    "warnings": [
                        "Transcribed by the deterministic provider from structured "
                        "schedule rows. No requirement was read out of narrative text, "
                        "so the requirements model is incomplete by construction.",
                    ],
                },
            }
        ],
    }


def _requirement_operation(
    reference: str, interlock: dict[str, Any], anchor: str
) -> dict[str, Any]:
    """One schedule row as a requirement, with the row itself as source_text.

    ``kind`` is deliberately unset. The column defaults to ``other``, and an
    unclassified requirement is a classification nobody made — guessing
    ``safety`` from the word "guard" would hide that from the validator that
    reports it.

    The scope cell travels with the row for the same reason the other cells do:
    "this interlock exists today" and "this upgrade adds it" are different
    requirements to accept, and an engineer reading the transcription has to be
    able to tell which one they are signing for.
    """
    identifier = str(interlock.get("identifier") or "")
    trip_tag = str(interlock.get("trip_tag") or "")
    condition = str(interlock.get("trip_condition") or "")
    action = str(interlock.get("action") or "")
    scope_text = str(interlock.get("scope_text") or "").strip()
    scoped = f", scope {scope_text}" if scope_text else ""
    return {
        "operation_type": "create_object",
        "object_type": "requirement",
        "temp_key": f"req-{identifier}",
        "values": {
            "reference": reference,
            "title": f"{identifier}: {interlock.get('description') or trip_tag}"[:300],
            "statement": (
                f"When {condition} ({trip_tag}), the system shall {action}."
                if action
                else f"The system shall respond to {condition} ({trip_tag})."
            ),
            # The row as the schedule wrote it, cell by cell. Not tidied: the
            # whole point of keeping source_text beside statement is that an
            # engineer can see what changed between them.
            "source_text": " | ".join(
                value
                for value in (
                    identifier,
                    str(interlock.get("description") or ""),
                    trip_tag,
                    condition,
                    action,
                    str(interlock.get("failsafe") or ""),
                    scope_text,
                )
                if value
            ),
            "is_inferred": False,
            "confidence": _confidence(0.5),
        },
        "basis": [
            {
                "kind": "direct",
                "anchor_id": anchor,
                "note": f"Row {identifier} of the interlock schedule{scoped}.",
            }
        ],
        "description": f"Transcribed from the schedule row for {identifier}{scoped}.",
    }


def _clarification_operation(
    identifier: str, interlock: dict[str, Any], question: str, anchor: str
) -> dict[str, Any]:
    missing = ", ".join(str(item) for item in interlock.get("missing_fields") or [])
    return {
        "operation_type": "create_object",
        "object_type": "clarification",
        "temp_key": f"clarify-{identifier}",
        "values": {
            "question": question,
            "ambiguity": (
                f"The schedule row for {identifier} leaves {missing} blank. Nothing "
                f"can be designed or compared against it until those are stated, and "
                f"choosing a value here would invent a requirement the customer never "
                f"asked for."
            ),
            "blocks_generation": True,
        },
        "basis": [
            {
                "kind": "direct",
                "anchor_id": anchor,
                "note": f"The incomplete schedule row for {identifier}.",
            }
        ],
        "description": f"The schedule row for {identifier} is missing {missing}.",
    }


def _gap_question(identifier: str, interlock: dict[str, Any]) -> str:
    missing = ", ".join(str(item) for item in interlock.get("missing_fields") or []) or "fields"
    return f"What are the {missing} for interlock {identifier}?"


# ---------------------------------------------------------------------------
# Passages a companion document records as unsettled
#
# The one narrative rule in this provider, and the reason it is defensible is
# that it reads no meaning at all. It does not ask whether a sentence *sounds*
# ambiguous — a rule that did would fire on ordinary prose, because ordinary
# engineering prose is full of sentences that can be read two ways and are
# perfectly clear in context. It asks instead whether the document set already
# says the passage is open, which is a fact about the documents.
#
# Three independent textual conditions have to hold together, and no one of them
# is the signal on its own:
#
# 1. a statement records something as not settled, in one of the phrases an
#    author uses for exactly that and for nothing else;
# 2. the same statement cites where the unsettled thing was asked for — a
#    numbered section of a *different* document in this same upload, named in the
#    statement;
# 3. a word the notice used to name its subject appears in that section, so the
#    passage can be quoted rather than gestured at.
#
# Condition 1 alone is prose about anything. Condition 2 alone is an ordinary
# cross-reference. Condition 3 alone is a coincidence of vocabulary. Together
# they are one engineer writing down that another document's passage is open, and
# restating that is the same act as restating a schedule row.
#
# What the rule deliberately does not do is say what the competing readings are.
# The documents do not write them down, so the clarification says the problem is
# missing information — composing the alternatives here would invent the
# requirement the question exists to ask for.
# ---------------------------------------------------------------------------

#: How a document records, in its own voice, that something is not yet settled.
#: A closed set, and every alternative is an author saying so outright rather
#: than a word that turns up while describing a working plant. ``has to be
#: confirmed`` is excluded because it states an obligation on the plant, not an
#: open decision — "extension has to be confirmed within 1.5 s" is a
#: requirement, and reading it as a deferral would question a settled one.
_DEFERRAL = re.compile(
    r"\b(?:"
    r"(?:has|have|is|are|was|were)\s+(?:not\s+been|not\s+yet\s+been|still\s+not)\s+"
    r"(?:settled|agreed|decided|confirmed|specified|selected|chosen)"
    r"|(?<!has )(?<!have )(?<!had )to\s+be\s+(?:confirmed|advised|decided|agreed|settled)"
    r"|tb[cd]"
    r")\b",
    re.IGNORECASE,
)

#: A cross-reference to a numbered section: "section 3", "sections 4.2".
_SECTION_REFERENCE = re.compile(r"\bsections?\s+(\d+(?:\.\d+)*)", re.IGNORECASE)

#: The number a heading opens with, which is what a section reference names.
_SECTION_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)")

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

#: Words long enough to name a subject. Four characters and up excludes the
#: articles, conjunctions and prepositions without a stop list to maintain, and a
#: stop list is exactly where a rule like this starts being tuned to one fixture.
_SUBJECT_WORD = re.compile(r"[a-z]{4,}")

#: Statements quoted for one notice. A notice whose subject matches more of the
#: cited section than this is not naming a passage, and quoting half a section
#: gives the reader a question they cannot answer in a sentence.
_MAX_QUOTED_STATEMENTS = 2


class _DeferredPassage(NamedTuple):
    """A passage one document asks for and another records as not settled."""

    passage: str
    passage_location: str
    passage_anchor: str
    notice: str
    notice_location: str
    notice_anchor: str


def _deferred_passages(documents: list[Any]) -> list[_DeferredPassage]:
    """Every passage a companion document records as unsettled, with its notice.

    Order is the order the documents were supplied, so two runs over one upload
    produce the same questions in the same sequence.
    """
    readable = [document for document in documents if isinstance(document, dict)]
    titles = {
        str(document.get("filename") or ""): _document_title(str(document.get("filename") or ""))
        for document in readable
    }

    found: list[_DeferredPassage] = []
    for document in readable:
        filename = str(document.get("filename") or "")
        for statement in _statements(document):
            notice = str(statement.get("text") or "").strip()
            notice_anchor = str(statement.get("anchor_id") or "")
            if not notice_anchor or not _DEFERRAL.search(notice):
                continue

            numbers = set(_SECTION_REFERENCE.findall(notice))
            lowered = notice.casefold()
            named = [
                other
                for other in readable
                if str(other.get("filename") or "") != filename
                and titles[str(other.get("filename") or "")]
                and titles[str(other.get("filename") or "")] in lowered
            ]
            # One section of one document. Anything else and the citation does not
            # identify a passage, so there is nothing to quote back to the reader.
            if len(numbers) != 1 or len(named) != 1:
                continue

            subject = _subject_words(notice, titles)
            passages = _cited_passages(named[0], next(iter(numbers)), subject)
            if not passages:
                continue
            found.extend(
                _DeferredPassage(
                    passage=str(passage.get("text") or "").strip(),
                    passage_location=_statement_location(named[0], passage),
                    passage_anchor=str(passage.get("anchor_id") or ""),
                    notice=notice,
                    notice_location=_statement_location(document, statement),
                    notice_anchor=notice_anchor,
                )
                for passage in passages
            )
    return found


def _statements(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in document.get("statements") or [] if isinstance(item, dict)]


def _document_title(filename: str) -> str:
    """The document's name as prose, which is how another document cites it.

    ``Project_Description.docx`` is referred to as "the project description", so
    the stem with its separators opened out is the string to look for. Nothing
    smarter: a title read out of the file's own metadata is one the citing author
    never saw.
    """
    return re.sub(r"[_\-]+", " ", PurePosixPath(filename).stem).casefold().strip()


def _subject_words(notice: str, titles: dict[str, str]) -> set[str]:
    """What the notice calls the thing it is deferring.

    Taken from the sentence carrying the deferral phrase, not from the whole
    paragraph: a notice's second sentence is usually about the notice ("no logic
    has been written for it"), and its words would match passages that have
    nothing to do with the subject.

    The deferral phrase, the section reference and the cited document's name are
    removed first — all three are how the notice points, not what it points at.
    """
    sentence = next(
        (part for part in _SENTENCE_BREAK.split(notice) if _DEFERRAL.search(part)), notice
    )
    stripped = _DEFERRAL.sub(" ", _SECTION_REFERENCE.sub(" ", sentence)).casefold()
    for title in titles.values():
        if title:
            stripped = stripped.replace(title, " ")
    return set(_SUBJECT_WORD.findall(stripped))


def _cited_passages(
    document: dict[str, Any], number: str, subject: set[str]
) -> list[dict[str, Any]]:
    """The statements of one numbered section that the notice's subject names."""
    if not subject:
        return []
    scored: list[tuple[int, dict[str, Any]]] = [
        (
            len(subject & set(_SUBJECT_WORD.findall(str(statement.get("text") or "").casefold()))),
            statement,
        )
        for statement in _statements(document)
        if _section_number(str(statement.get("section") or "")) == number
        and statement.get("anchor_id")
    ]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return []
    named = [statement for score, statement in scored if score == best]
    return named if len(named) <= _MAX_QUOTED_STATEMENTS else []


def _section_number(section: str) -> str:
    """The number of the innermost heading a statement sits under."""
    match = _SECTION_NUMBER.match(section.split(" / ")[-1].strip())
    return match.group(1).rstrip(".") if match else ""


def _statement_location(document: dict[str, Any], statement: dict[str, Any]) -> str:
    section = str(statement.get("section") or "").split(" / ")[-1].strip()
    filename = str(document.get("filename") or "")
    return f"{filename} / {section}" if section else filename


def _deferral_question(passage: _DeferredPassage) -> str:
    return (
        f'Which behaviour is required by "{passage.passage[:600]}" '
        f"({passage.passage_location})? {passage.notice_location} records it as not "
        f"settled, so no reading of it can be taken as agreed."
    )


def _deferral_operation(passage: _DeferredPassage, question: str) -> dict[str, Any]:
    """The passage, the notice that defers it, and why no reading is offered."""
    return {
        "operation_type": "create_object",
        "object_type": "clarification",
        "temp_key": f"clarify-deferred-{_dedupe_key(passage.passage_anchor, passage.passage)[:16]}",
        "values": {
            "question": question,
            "ambiguity": (
                f"{passage.passage_location} asks for this behaviour and "
                f'{passage.notice_location} records it as unsettled: "{passage.notice[:600]}" '
                f"Neither document sets out the readings that compete, so this is missing "
                f"information rather than a stated ambiguity: there is no alternative "
                f"written down that could be chosen, and composing one here would invent "
                f"the requirement this question exists to ask for."
            ),
            # Designing against a passage the document set records as open means
            # picking a reading, and a reading picked with no author is exactly
            # the requirement nobody agreed to.
            "blocks_generation": True,
        },
        "basis": [
            {
                "kind": "direct",
                "anchor_id": passage.passage_anchor,
                "note": f"The passage {passage.notice_location} leaves unsettled.",
            },
            {
                "kind": "direct",
                "anchor_id": passage.notice_anchor,
                "note": f"{passage.notice_location}, the notice that leaves it unsettled.",
            },
        ],
        "description": (
            f"{passage.notice_location} records the passage cited from "
            f"{passage.passage_location} as not settled."
        ),
    }


def _build_tag_architecture(context: dict[str, Any]) -> dict[str, Any]:
    """Declare a tag per device signal, but only where a requirement asks for one.

    The naming is mechanical — ``{tag_prefix}_{signal name}`` is the standard's
    own composition rule — and the data type follows from the signal type. What
    is not mechanical is *why the tag should exist*, and that is the whole of the
    traceability rule: a tag with no stated basis is an object nobody can review.

    So the only basis this provider will state is an accepted requirement that
    names the device. Where no accepted requirement names it, the tag is not
    proposed and a clarification says so. That is the honest asymmetry: naming a
    tag is transcription, deciding that a device needs one is engineering.
    """
    base_revision = int(context.get("base_revision_number") or 0)
    declared = {str(tag.get("name", "")).casefold() for tag in context.get("existing_tags") or []}
    accepted = [
        requirement
        for requirement in context.get("requirements") or []
        if requirement.get("is_accepted")
    ]

    operations: list[dict[str, Any]] = []
    unsupported: list[str] = []

    for device in context.get("devices") or []:
        prefix = str(device.get("tag_prefix") or "").strip()
        if not prefix:
            continue
        requirement = _requirement_naming(accepted, prefix)
        if requirement is None:
            unsupported.append(prefix)
            continue

        for signal in device.get("signals") or []:
            name = f"{prefix}_{signal.get('name', '')}".strip("_")
            data_type = _DATA_TYPE_BY_SIGNAL_TYPE.get(str(signal.get("signal_type", "")))
            if not name or data_type is None or name.casefold() in declared:
                continue
            declared.add(name.casefold())
            operations.append(_tag_operation(name, device, signal, data_type, requirement))

    actions: list[dict[str, Any]] = []
    if operations:
        actions.append(
            {
                "action_type": "propose_changeset",
                "summary": f"Declare {len(operations)} tag(s) for signals on approved devices.",
                "confidence": _confidence(0.5),
                "payload": {
                    "title": "Tags for the signals of requirement-backed devices",
                    "summary": (
                        "One controller-scoped tag per signal of each device an accepted "
                        "requirement names, composed as {tag_prefix}_{signal} and typed "
                        "from the signal type."
                    ),
                    "rationale": (
                        "Naming follows the standard's composition rule and the data "
                        "type follows the signal type; both are mechanical. Each tag "
                        "cites the accepted requirement that names its device."
                    ),
                    "base_revision_number": base_revision,
                    "operations": operations,
                    "warnings": [
                        "Composed by the deterministic provider. No alias, group, UDT or "
                        "AOI reference was designed, and no existing tag was examined "
                        "against the naming standard.",
                    ],
                },
            }
        )

    for prefix in unsupported[:_MAX_MOCK_CLARIFICATIONS]:
        actions.append(
            {
                "action_type": "request_clarification",
                "summary": f"No accepted requirement names {prefix}.",
                "confidence": 0.0,
                "payload": {
                    "category": "tag_naming",
                    "question": (
                        f"Which accepted requirement do the tags for {prefix} come "
                        f"from, or should {prefix} be out of scope for this project?"
                    ),
                    "why_it_matters": (
                        f"No tag will be created for {prefix} until this is answered. "
                        f"A tag that exists for no stated reason cannot be reviewed, "
                        f"and everything generated from it would inherit the gap."
                    ),
                },
            }
        )

    actions.append(
        {
            "action_type": "complete_task",
            "summary": "Tag design pass by the deterministic provider.",
            "confidence": 0.0,
            "payload": {
                "outcome": (
                    f"Proposed {len(operations)} tag(s) and left {len(unsupported)} "
                    f"device(s) undeclared for want of an accepted requirement."
                ),
                "objects_created": {"tag": len(operations)},
                "limitations": [
                    "Only base tags for device signals were considered. Aliases, "
                    "groups, UDTs, AOI references and I/O alias tags were not.",
                    "No existing tag was checked against the naming standard, so this "
                    "is not evidence that the tag model is consistent.",
                    MOCK_WARNING,
                ],
            },
        }
    )

    return {
        "rationale": (
            f"Composed {len(operations)} tag name(s) mechanically from approved devices "
            f"and their signals. Every one cites the accepted requirement that names its "
            f"device; devices no requirement names produced a question, not a tag."
        ),
        "actions": actions,
    }


#: Signal types whose Logix data type follows without a judgement. A network or
#: virtual signal does not: its type depends on what the producer publishes,
#: which no rule here can read.
_DATA_TYPE_BY_SIGNAL_TYPE: dict[str, str] = {
    "discrete_input": "BOOL",
    "discrete_output": "BOOL",
    "analog_input": "REAL",
    "analog_output": "REAL",
}

#: Questions one mock turn will raise. A run that asks forty is a run nobody
#: answers, and the pattern is visible after a handful.
_MAX_MOCK_CLARIFICATIONS = 10

#: Recorded answers one turn restates in full before it starts counting. Each is
#: already on the record as a decision, so repeating three hundred of them buys
#: nothing and overflows the field the statement is written to — and an action
#: refused for length would take the whole statement with it, leaving a step that
#: produced nothing looking like one with nothing to say.
_MAX_MOCK_RESTATEMENTS = 3


def _requirement_naming(requirements: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    """The first accepted requirement whose wording names this device."""
    needle = prefix.casefold()
    for requirement in requirements:
        text = f"{requirement.get('title', '')} {requirement.get('statement', '')}".casefold()
        if needle in text:
            return requirement
    return None


def _tag_operation(
    name: str,
    device: dict[str, Any],
    signal: dict[str, Any],
    data_type: str,
    requirement: dict[str, Any],
) -> dict[str, Any]:
    description = str(signal.get("description") or "").strip()
    return {
        "operation_type": "create_object",
        "object_type": "tag",
        "temp_key": f"tag-{name}",
        "values": {
            "name": name,
            "scope": "controller",
            "base_data_type": data_type,
            "description": (description or f"{device.get('name', '')} {signal.get('name', '')}")[
                :250
            ],
            # The name it is being created under. Populated always, because it is
            # what makes a later rename explicable rather than a discrepancy
            # nobody can close.
            "source_name": name,
        },
        "basis": [
            {
                "kind": "inference",
                "requirement_reference": str(requirement.get("reference") or ""),
                "note": (
                    f"{requirement.get('reference')} names "
                    f"{device.get('tag_prefix')}; this tag carries its "
                    f"{signal.get('name')} signal."
                ),
            }
        ],
        "description": f"Signal {signal.get('name')} of device {device.get('tag_prefix')}.",
    }


def _build_plc_logic(context: dict[str, Any]) -> dict[str, Any]:
    """Select no control pattern, and say exactly why not.

    This is the one production agent whose central act — choosing which approved
    pattern implements a requirement, and with which parameters — is irreducibly
    a judgement. A fixed rule can match a device category to a template name, and
    the result would look like engineering and be a coincidence of naming. The
    expansion is then written into rungs that a reviewer reads as generated
    logic, and nothing downstream would record that a lookup table chose it.

    So it proposes nothing and asks. When generation is blocked for the project
    it repeats the block's own question, which is already written to be put to a
    human unedited.

    What it does not do is ask twice. A question a named human has answered
    arrives in ``answered_questions``, and an answer is a fact this provider may
    state: it says who answered, what they said, and whether those words name one
    of the approved templates. Saying which is the whole of what a rule engine can
    add to an answer — it still selects nothing — and it is what stops the human
    loop from being decorative, because the third asking of an answered question
    is not a question, it is a run that cannot move.
    """
    block = context.get("generation_blocked")
    if isinstance(block, dict):
        return {
            "rationale": (
                "Generation is blocked for this project, so no template was offered "
                "and none could be selected."
            ),
            "actions": [
                {
                    "action_type": "request_clarification",
                    "summary": "Generation is blocked for this project.",
                    "confidence": 0.0,
                    "payload": {
                        "category": str(block.get("question_category") or "scope"),
                        "question": str(block.get("question") or ""),
                        "why_it_matters": str(block.get("reason") or ""),
                    },
                },
                {
                    "action_type": "report_blocker",
                    "summary": "No logic can be proposed for this project.",
                    "confidence": 0.0,
                    "payload": {
                        "reason": str(block.get("reason") or ""),
                        "resolvable_by_upload": False,
                    },
                },
            ],
        }

    existing = context.get("existing") or {}
    templates = context.get("templates") or []
    # ``requirements`` is already the citable list — accepted, and with no open
    # blocking clarification — so the only filter left is whether some network
    # already claims to implement it.
    claimed = {
        str(network.get("requirement_reference") or "")
        for network in existing.get("logic_networks") or []
    }
    unimplemented = [
        requirement
        for requirement in context.get("requirements") or []
        if str(requirement.get("reference") or "") not in claimed
    ]

    answers = _template_selection_answers(context)
    outstanding: list[dict[str, Any]] = []
    restated: list[str] = []
    for requirement in unimplemented:
        reference = str(requirement.get("reference") or "")
        answer = _answer_naming(answers, reference)
        if answer is None:
            outstanding.append(requirement)
        else:
            restated.append(_restate_answer(answer, reference, templates))

    actions: list[dict[str, Any]] = [
        {
            "action_type": "request_clarification",
            "summary": f"Which approved pattern implements {requirement.get('reference')}?",
            "confidence": 0.0,
            "payload": {
                "category": "template_selection",
                "question": (
                    f"Which of the {len(templates)} approved template(s) implements "
                    f"{requirement.get('reference')}, and with which parameters?"
                ),
                "why_it_matters": (
                    "No logic will be proposed for this requirement until a pattern is "
                    "chosen. Choosing the nearest-sounding template would produce a "
                    "rung whose pattern nobody selected."
                ),
            },
        }
        for requirement in outstanding[:_MAX_MOCK_CLARIFICATIONS]
    ]

    if restated and outstanding:
        # Part answered. The unanswered questions carry the block on their own, so
        # the answers already in are stated where a human will read them next to
        # the ones still outstanding.
        actions.append(
            {
                "action_type": "post_message",
                "summary": f"{len(restated)} recorded answer(s) restated, not re-asked.",
                "confidence": 0.0,
                "payload": {
                    "body": _restated_summary(restated),
                    "context": {"answered": len(restated), "outstanding": len(outstanding)},
                },
            }
        )
    elif restated:
        # Nothing left to ask and nothing produced. Saying so once is the honest
        # end of this line of enquiry; asking the answered questions again would
        # put the same sentences back in front of the person who answered them.
        actions.append(
            {
                "action_type": "report_blocker",
                "summary": "Every requirement's selection question is already answered.",
                "confidence": 0.0,
                "payload": {
                    "reason": (
                        f"No logic was proposed. Every one of the {len(restated)} citable "
                        f"requirement(s) that no network claims already carries a recorded "
                        f"answer, and none of those answers gives this provider a pattern "
                        f"it may expand. " + _restated_summary(restated)
                    ),
                    "missing_inputs": [
                        str(requirement.get("reference") or "")
                        for requirement in unimplemented[:_MAX_MOCK_CLARIFICATIONS]
                    ],
                    "resolvable_by_upload": False,
                },
            }
        )

    actions.append(
        {
            "action_type": "complete_task",
            "summary": "No control pattern was selected.",
            "confidence": 0.0,
            "payload": {
                "outcome": (
                    f"Reported the controller organisation already recorded "
                    f"({len(existing.get('controller_tasks') or [])} task(s), "
                    f"{len(existing.get('programs') or [])} program(s), "
                    f"{len(existing.get('routines') or [])} routine(s)); asked which "
                    f"pattern implements each of {len(outstanding)} citable "
                    f"requirement(s) that no network yet claims, and restated the "
                    f"recorded answer for {len(restated)} more."
                ),
                "limitations": [
                    "No template was selected and no logic was proposed. Matching a "
                    "requirement to an approved control pattern is a judgement, and a "
                    "fixed rule that made it would produce logic whose pattern nobody "
                    "chose.",
                    *(
                        [
                            f"{len(restated)} requirement(s) have an answered selection "
                            "question whose recorded answer names no approved template "
                            "and no parameters, so the answer is stated and nothing is "
                            "expanded from it."
                        ]
                        if restated
                        else []
                    ),
                    MOCK_WARNING,
                ],
            },
        }
    )

    return {
        "rationale": (
            f"{len(templates)} approved template(s) are available for this target. None "
            f"was selected: matching one to a requirement is the judgement this provider "
            f"does not make. {len(outstanding)} requirement(s) were asked about and "
            f"{len(restated)} already carry a recorded answer, restated rather than "
            f"asked again."
        ),
        "actions": actions,
    }


def _restated_summary(restated: list[str]) -> str:
    """The recorded answers this turn states, and a count of the rest."""
    shown = " ".join(restated[:_MAX_MOCK_RESTATEMENTS])
    remaining = len(restated) - _MAX_MOCK_RESTATEMENTS
    if remaining > 0:
        shown += (
            f" {remaining} further requirement(s) carry a recorded answer that names no "
            "approved template either."
        )
    return shown


def _template_selection_answers(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Answered questions that were asked about choosing a template.

    Filtered by category rather than by wording: an answer to "which controller
    family is this" is a fact about the project and says nothing about which
    pattern implements a requirement, and reading it as though it did would be
    exactly the invention this provider exists not to make.
    """
    return [
        entry
        for entry in context.get("answered_questions") or []
        if isinstance(entry, dict)
        and str(entry.get("category") or "") == QuestionCategory.TEMPLATE_SELECTION.value
        and str(entry.get("answer") or "").strip()
    ]


def _answer_naming(answers: list[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    """The answered selection question that names this requirement, if any.

    Matched on the question rather than the answer because the question is the
    one this provider wrote, and it names exactly one requirement. The boundary
    matters: ``REQ-40`` must not collect the answer to a question about
    ``REQ-401``.
    """
    if not reference:
        return None
    pattern = re.compile(rf"{re.escape(reference)}(?![0-9A-Za-z_-])")
    for answer in answers:
        if pattern.search(str(answer.get("question") or "")):
            return answer
    return None


def _restate_answer(answer: dict[str, Any], reference: str, templates: list[dict[str, Any]]) -> str:
    """One recorded answer, restated as the fact it is.

    Never paraphrased and never acted on beyond saying whether it names something
    this provider could have expanded. The answer is a named human's decision, so
    the name, the reference and the decision it was recorded as are all part of
    the statement — an unattributed answer is not evidence of anything.
    """
    named = _template_named_in(str(answer.get("answer") or ""), templates)
    recorded = str(answer.get("decision_reference") or "")
    statement = (
        f"{reference}: {answer.get('reference')} was answered by "
        f"{answer.get('answered_by') or 'nobody the record names'} — "
        f"{_quote(str(answer.get('answer') or ''))}"
        + (f", recorded as {recorded}" if recorded else "")
        + "."
    )
    if named:
        return (
            f"{statement} The answer names approved template {named}; expanding it also "
            "needs that template's parameters, which the answer does not state and this "
            "provider does not compose from prose."
        )
    return (
        f"{statement} The answer names none of the {len(templates)} approved template(s) "
        "for this target, so there is still no pattern to expand."
    )


def _template_named_in(answer: str, templates: list[dict[str, Any]]) -> str:
    """The approved template an answer names, or ``""``.

    A containment test over the ids the context actually carries, so an id nobody
    loaded cannot be recognised here and an answer naming a pattern this
    installation does not have reads as naming none.
    """
    haystack = answer.casefold()
    for template in templates:
        for key in ("qualified_id", "template_id"):
            candidate = str(template.get(key) or "").strip()
            if candidate and candidate.casefold() in haystack:
                return str(template.get("qualified_id") or candidate)
    return ""


def _quote(text: str) -> str:
    collapsed = " ".join(text.split())
    return f"'{collapsed[:300]}'" if collapsed else "'' (no text was recorded)"


def _first_anchor(sources: list[Any], anchors: dict[str, Any]) -> str | None:
    """The first anchor id among a fact's sources, or ``None`` if none resolves."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        anchor = anchors.get(str(source.get("anchor_key", "")))
        if anchor:
            return str(anchor)
    return None


# ---------------------------------------------------------------------------
def _tag(graph: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in graph.get("tags") or []:
        if entry.get("name") == name:
            return entry
    return None


def _duplicate_rungs(graph: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Rung texts that appear on more than one rung, in document order."""
    by_text: dict[str, list[str]] = {}
    for rung in graph.get("logic") or []:
        text = str(rung.get("text") or "").strip()
        if not text:
            continue
        by_text.setdefault(text, []).append(str(rung.get("anchor_key")))
    return [(text, keys) for text, keys in by_text.items() if len(keys) > 1]


_BUILDERS = {
    "project_manager": _build_project_manager,
    "plc_review": _build_plc_review,
    "interlock_review": _build_interlock_review,
    "senior_review": _build_senior_review,
    "requirements_planning": _build_requirements_planning,
    "tag_architecture": _build_tag_architecture,
    "plc_logic": _build_plc_logic,
}
