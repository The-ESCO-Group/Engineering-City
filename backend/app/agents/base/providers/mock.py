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
a tag referenced but never declared. Inventing anything beyond that would make the
demo lie about what the platform can do.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agents.base.provider import ChatMessage, ProviderResponse, estimate_tokens
from app.core.enums import ProviderKind
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

    Reading requirements out of the narrative documents is deliberately not
    attempted. "Is this sentence a requirement or is it background" is the one
    judgement this agent exists to make, and a fixed rule that guessed at it
    would fill the model with untestable items — exactly the failure the real
    prompt spends most of its length warning against.

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

    for interlock in (context.get("graph") or {}).get("documented_interlocks") or []:
        if not interlock.get("is_current", True):
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

    rationale = (
        f"Transcribed {transcribed} documented interlock(s) into requirements and "
        f"raised {gaps} clarification(s) for rows the schedule left incomplete. No "
        f"requirement was read out of narrative text: deciding that a sentence is a "
        f"requirement rather than background is a judgement this provider does not make."
    )

    if not operations:
        return {
            "rationale": rationale,
            "actions": [
                {
                    "action_type": "complete_task",
                    "summary": "Nothing new to record from the interlock schedule.",
                    "confidence": 0.0,
                    "payload": {
                        "outcome": (
                            "Every documented interlock is already recorded as a "
                            "requirement or a clarification, or none carried an "
                            "evidence anchor to cite."
                        ),
                        "limitations": [
                            "Only the interlock schedule was read. The project "
                            "description and the narrative documents were not, so "
                            "this is not a complete requirements model.",
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
                    f"and {gaps} incomplete row(s) as clarifications."
                ),
                "evidence_anchor_ids": sorted(set(cited)),
                "confidence": _confidence(0.5),
                "payload": {
                    "title": "Requirements transcribed from the interlock schedule",
                    "summary": (
                        "One requirement per fully specified documented interlock, one "
                        "clarification per incomplete row. Each cites the schedule row "
                        "it was read from."
                    ),
                    "rationale": rationale,
                    "base_revision_number": int(context.get("base_revision_number") or 0),
                    "operations": operations,
                    "warnings": [
                        "Transcribed by the deterministic provider from structured "
                        "schedule rows. The narrative documents were not read, so the "
                        "requirements model is incomplete by construction.",
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
    """
    identifier = str(interlock.get("identifier") or "")
    trip_tag = str(interlock.get("trip_tag") or "")
    condition = str(interlock.get("trip_condition") or "")
    action = str(interlock.get("action") or "")
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
                "note": f"Row {identifier} of the interlock schedule.",
            }
        ],
        "description": f"Transcribed from the schedule row for {identifier}.",
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
        for requirement in unimplemented[:_MAX_MOCK_CLARIFICATIONS]
    ]

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
                    f"{len(existing.get('routines') or [])} routine(s)) and asked which "
                    f"pattern implements each of {len(unimplemented)} citable "
                    f"requirement(s) that no network yet claims."
                ),
                "limitations": [
                    "No template was selected and no logic was proposed. Matching a "
                    "requirement to an approved control pattern is a judgement, and a "
                    "fixed rule that made it would produce logic whose pattern nobody "
                    "chose.",
                    MOCK_WARNING,
                ],
            },
        }
    )

    return {
        "rationale": (
            f"{len(templates)} approved template(s) are available for this target. None "
            f"was selected: matching one to a requirement is the judgement this provider "
            f"does not make."
        ),
        "actions": actions,
    }


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
