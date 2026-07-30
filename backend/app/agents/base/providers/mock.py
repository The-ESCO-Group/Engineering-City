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
}
