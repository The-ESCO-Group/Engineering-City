"""Requirements and Planning Agent.

Turns the project description and the supporting documents into a structured,
traceable requirements model: requirements, acceptance criteria, assumptions,
design constraints and clarifications, each citing the passage it was read from.

The judgement this agent exists to make is *requirement or narrative*. A project
description contains both, and promoting background prose into a shall-statement
produces a model full of untestable items that then demand review effort, test
coverage and traceability nobody can supply. ``Requirement.confidence`` is
therefore about that distinction and nothing else — how sure the extractor is
that a sentence is a requirement at all, not how sure it is of the content — and
the prompt says so in those words.

Its remit is narrow on purpose. It cannot ``create_finding``: it establishes what
was asked for, not whether anything is correct, and a technical judgement it made
here would be one no reviewer had asked for. It cannot ``create_task_plan``
either — scheduling downstream work is the Project Manager Agent's, and the
"planning" in this agent's name is the requirements plan, not the task plan.

Above all it cannot accept its own work. An inferred requirement stays
:class:`~app.core.enums.RequirementStatus.PROPOSED`, an assumption stays
unconfirmed, a clarification stays open. A record that certifies itself records
nothing.

That last rule is enforced by :func:`refusal_reasons` rather than by the shared
machinery, because the shared machinery does not reach it: none of the five
object kinds this agent creates is in
:data:`~app.core.enums.TRACEABLE_OBJECT_TYPES`, so neither the action schema nor
the merge validator demands a basis for any of them, and nothing below this
module knows that an assumption without ``impact_if_wrong`` is a guess with a
reference number. The handler that wires this agent calls that function and
refuses the whole change set if it returns anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.actions import ChangeOperationProposal, ProposeChangesetPayload
from app.agents.base.agent import SHARED_RULES, AgentContext, BaseAgent
from app.core.enums import (
    ActionType,
    AgentKind,
    ChangeOperationType,
    ClarificationStatus,
    ObjectType,
    RequirementStatus,
)
from app.core.services.evidence_graph import EvidenceGraph
from app.engineering.revisions import current_revision_number
from app.models.profile import ProjectProfile
from app.models.requirements import (
    AcceptanceCriterion,
    Assumption,
    Clarification,
    DesignConstraint,
    Requirement,
)

#: The only canonical objects this agent's change set may name. Passed to
#: :func:`~app.engineering.changesets.build_changeset` as the scope the agent was
#: *given*, so the merge enforces it rather than trusting the proposal.
#:
#: Tags, logic and I/O are absent deliberately: deciding what the system must do
#: and deciding how to build it are separate acts with separate reviewers, and an
#: extractor that could also write a rung would be able to satisfy its own
#: requirement without anyone noticing the requirement was never agreed.
ALLOWED_OBJECT_TYPES: tuple[ObjectType, ...] = (
    ObjectType.REQUIREMENT,
    ObjectType.ACCEPTANCE_CRITERION,
    ObjectType.ASSUMPTION,
    ObjectType.CONSTRAINT,
    ObjectType.CLARIFICATION,
)

#: Object kinds this agent may not create without saying where they came from.
#:
#: The traceability rule the action schema already applies to tags, rungs and
#: alarms, extended to the objects those things will later be generated *from*.
#: A requirement with no source is not a requirement, it is an invention, and it
#: is a particularly expensive one: everything downstream cites it as its own
#: basis, so the invention propagates into logic that looks fully traceable.
#:
#: A clarification is exempt. It is raised precisely when something is missing or
#: unreadable, and an absence has no passage to point at; its ``question`` and
#: ``ambiguity`` carry the justification instead.
BASIS_REQUIRED_OBJECT_TYPES: frozenset[ObjectType] = frozenset(
    {
        ObjectType.REQUIREMENT,
        ObjectType.ACCEPTANCE_CRITERION,
        ObjectType.ASSUMPTION,
        ObjectType.CONSTRAINT,
    }
)

#: Fields whose value is a named human's decision, and the only value this agent
#: may propose for each.
#:
#: Every one of these is a self-certification route. An agent that could set a
#: requirement to ``accepted`` would be agreeing to its own reading of the brief;
#: one that could set ``is_confirmed`` would be confirming its own guess; one that
#: could write a ``resolution`` would be answering its own question. The result
#: in each case is a record that looks reviewed and was not.
HUMAN_ONLY_FIELDS: Mapping[ObjectType, Mapping[str, Any]] = {
    ObjectType.REQUIREMENT: {"status": RequirementStatus.PROPOSED},
    ObjectType.ACCEPTANCE_CRITERION: {"status": RequirementStatus.PROPOSED},
    ObjectType.ASSUMPTION: {"is_confirmed": False},
    ObjectType.CLARIFICATION: {"status": ClarificationStatus.OPEN, "resolution": ""},
}

#: Operation types that bring an object into existence.
_CREATING: frozenset[ChangeOperationType] = frozenset(
    {ChangeOperationType.CREATE_OBJECT, ChangeOperationType.CREATE_LOGIC}
)

#: Statements handed over per document. A functional description runs to
#: hundreds of paragraphs and the budget is spent on the ones that could be
#: requirements; truncation is reported in the context so the agent can state it
#: as a limitation rather than quietly review part of a document.
MAX_STATEMENTS_PER_DOCUMENT = 400

#: Existing objects shown back to the agent, so a re-run converges on what is
#: already recorded instead of proposing it again under a new reference.
MAX_EXISTING_OBJECTS = 500

PROMPT = f"""{SHARED_RULES}

You are the Requirements and Planning Agent.

You read the project description and the supporting documents and produce a
structured, traceable requirements model. You establish WHAT WAS ASKED FOR. You
do not judge whether any of it is correct, safe, complete or achievable — you
have no create_finding action and the platform refuses the attempt.

Note on rule 6 above: report_no_findings is not one of your actions. Your
equivalent of "I found nothing" is report_blocker when there is no description to
extract from at all, and otherwise complete_task naming what you read and what
you could not.

WHAT YOU PROPOSE
One propose_changeset, echoing base_revision_number from the context, whose
operations create only these object types:
- requirement — one thing the delivered system must do;
- acceptance_criterion — one condition that must hold before a requirement counts
  as met. Link it by setting requirement_id to "temp:" followed by the temp_key
  of the create that made the requirement;
- assumption — something you had to take as given because nobody said;
- constraint — a limit the design may not cross;
- clarification — a specific ambiguity, attached to what is ambiguous.
Proposing a tag, a rung, a device or an I/O point is outside your scope and the
merge will refuse it.

THE JUDGEMENT THAT MATTERS: IS THIS A REQUIREMENT, OR IS IT NARRATIVE?
A brief contains both. "Line 4 was commissioned in 2009 and runs three shifts" is
background. "The conveyor shall stop when the guard door opens" is a requirement.
Treating background as a requirement fills the model with untestable items that
then demand acceptance criteria, test cases and review time nobody can spend, and
it buries the requirements that are real.

`confidence` on a requirement means HOW SURE YOU ARE THAT THIS IS A REQUIREMENT
AT ALL. It does not mean how sure you are that the content is true. A sentence
you believe accurately describes the plant but doubt was meant as a requirement
is LOW confidence. Say which sentences you were unsure about.

WORDING: TWO FIELDS, TWO JOBS
- source_text is the customer's own words, copied exactly. Never write into it a
  sentence the document does not contain, and never tidy it.
- statement is the canonical wording this platform will quote downstream.
An engineer compares the two to catch a requirement that changed meaning while it
was being cleaned up, so a statement that says more than its source is the defect
this pair exists to expose.

CITE WHERE IT CAME FROM
Every requirement, acceptance criterion, assumption and constraint states a
basis: kind "direct" with the anchor_id of the paragraph, row or line it was read
from, or kind "human_decision" with the decision reference, or kind "inference"
with the requirement_reference it was derived from. Take anchor ids only from
anchor_index in the context, and list every one you cite in the action's
evidence_anchor_ids as well, so they are checked before anything is recorded.

NEVER FILL A GAP
If a threshold, a fail state, a reset behaviour, a mode, an interlock action or a
timing was never stated, you have three honest options and none of them is a
plausible default:
- request_clarification, when a human can answer it;
- record it as an assumption with impact_if_wrong stating what breaks if the
  assumption is wrong, and set made_by_agent to requirements_planning;
- leave it out and say so in complete_task limitations.
A number nobody wrote down is the worst thing you can produce here. Once it
reaches generated logic it is indistinguishable from something the customer asked
for, and no later reviewer can tell which it was.

WHAT YOU INFER IS MARKED, AND YOU NEVER ACCEPT IT YOURSELF
Anything you derived rather than read is is_inferred true, stays at status
"proposed", and carries an assumption saying what was assumed and what happens if
it is wrong. An assumption whose consequence nobody wrote down cannot be
prioritised, cannot be tested and will never be revisited.
You may not set a requirement or criterion to "accepted", may not set is_confirmed
on an assumption, and may not write a resolution or close a clarification. Those
are a named human's acts. A proposal that does any of them is refused whole.

AMBIGUITY NAMES BOTH READINGS
"This is ambiguous" leaves a human with nothing to decide. State the competing
readings — "does *stop* mean a controlled ramp-down or an immediate drive
disable" can be answered in one word. Two or more readings, or none at all when
the problem is missing information rather than ambiguity. Put the same wording in
the clarification object's ambiguity field.

WHAT TO EXTRACT
- functional requirements, with the customer's wording in source_text;
- constraints: existing panel space, fixed addresses, a controller that must stay
  on its firmware, a standard that must be followed;
- acceptance criteria, and mark one is_testable false rather than dropping it
  when the customer asked for something nothing can check;
- devices and equipment named, and the sequences or steps described;
- manual, automatic, maintenance and cleaning mode expectations, including what
  each mode is supposed to inhibit;
- interlock and alarm expectations, as stated — not as you would design them;
- what is missing or ambiguous.

RESTRICTED DOMAINS
If the description reaches into motion, safety logic, burner management, process
safety or high energy, record the requirement and name the domain in
restricted_domains. Recording what was asked for is right; deciding that this
platform may build it is not yours.

WHAT THIS EVIDENCE CANNOT ESTABLISH — do not claim any of it:
- that the description is complete, or that its silences are deliberate;
- that a behaviour is not required because nobody wrote it down;
- anything about the plant as built. You are reading a brief, not a site;
- that a requirement is achievable on the target platform, or affordable, or
  compatible with the existing installation.
"""


class RequirementsPlanningAgent(BaseAgent):
    kind = AgentKind.REQUIREMENTS_PLANNING

    @property
    def allowed_actions(self) -> set[ActionType]:
        return {
            ActionType.PROPOSE_CHANGESET,
            ActionType.REQUEST_CLARIFICATION,
            ActionType.POST_MESSAGE,
            ActionType.COMPLETE_TASK,
            ActionType.REPORT_BLOCKER,
        }

    @property
    def allowed_object_types(self) -> tuple[ObjectType, ...]:
        """The change-set scope this agent is granted.

        Exposed rather than inlined at the call site because the merge enforces
        the scope it was *given*, and the grant should be readable next to the
        action set it belongs with.
        """
        return ALLOWED_OBJECT_TYPES

    def system_prompt(self, context: AgentContext) -> str:
        return PROMPT

    def build_context(self, context: AgentContext) -> dict[str, Any]:
        db = context.db
        project = context.project
        graph = context.graph
        anchors = self.anchor_index(db, project.id)

        profile = db.execute(
            select(ProjectProfile).where(ProjectProfile.project_id == project.id)
        ).scalar_one_or_none()

        documents, truncated = _documents(graph, anchors)

        return {
            "agent_kind": self.kind.value,
            "base_revision_number": current_revision_number(db, project.id),
            "project": {
                "name": project.name,
                "client_reference": project.client_reference,
            },
            "profile": _profile_view(profile),
            "anchor_index": anchors,
            "documents": documents,
            "truncated_documents": truncated,
            "graph": {
                "counts": graph.counts(),
                # The interlock schedule and the alarm list are statements of
                # what was asked for, so they belong here. The I/O list does not:
                # it is a design output, and reading it would have this agent
                # derive requirements from somebody's answer rather than from the
                # question.
                "documented_interlocks": [item.as_dict() for item in graph.interlocks],
                "documented_alarms": graph.alarms,
                "missing_inputs": graph.missing_inputs,
                "unreadable_files": graph.unreadable_files,
                "limitations": [item.as_dict() for item in graph.limitations],
            },
            # Rungs and tags are withheld on purpose. Requirements come from what
            # the customer asked for, not from what somebody already built:
            # handed the existing logic, an extractor writes requirements that
            # the implementation satisfies by construction, and the gap between
            # brief and build — the thing this stage exists to find — closes
            # silently.
            "existing": _existing_objects(db, project.id),
        }


# ---------------------------------------------------------------------------
# Deterministic guards
# ---------------------------------------------------------------------------
def refusal_reasons(payload: ProposeChangesetPayload) -> list[str]:
    """Reasons this agent's own proposal must be refused before it is recorded.

    Three rules the shared machinery does not cover, each returning a sentence a
    human can read rather than a boolean, and all of them collected in one pass
    so an author sees every problem at once instead of one per round trip:

    * an object created with no stated basis (:data:`BASIS_REQUIRED_OBJECT_TYPES`);
    * a value only a named human may set (:data:`HUMAN_ONLY_FIELDS`);
    * an assumption that does not say what breaks if it is wrong.

    The scope check duplicates one the merge also performs, deliberately: the two
    gates fail differently. The merge refuses a change set that has already been
    recorded and attributed; this refuses it before it exists.
    """
    reasons: list[str] = []
    for ordinal, operation in enumerate(payload.operations):
        label = _operation_label(ordinal, operation)

        if operation.object_type not in ALLOWED_OBJECT_TYPES:
            reasons.append(
                f"{label} is outside this agent's scope; it establishes what was "
                f"asked for and does not design "
                f"{operation.object_type.value.replace('_', ' ')}s."
            )
            continue

        creating = operation.operation_type in _CREATING
        if (
            creating
            and operation.object_type in BASIS_REQUIRED_OBJECT_TYPES
            and not operation.basis
        ):
            reasons.append(
                f"{label} states no basis; an object recorded here becomes the "
                f"stated reason for everything generated from it, so one that "
                f"cites nothing makes an invention look traceable."
            )

        for field_name, human_value in HUMAN_ONLY_FIELDS.get(operation.object_type, {}).items():
            if field_name in operation.values and operation.values[field_name] != human_value:
                reasons.append(
                    f"{label} sets {field_name} to "
                    f"{operation.values[field_name]!r}; only a named human may do "
                    f"that, and an agent that accepts its own work leaves a record "
                    f"that looks reviewed and is not."
                )

        if (
            creating
            and operation.object_type is ObjectType.ASSUMPTION
            and not str(operation.values.get("impact_if_wrong", "")).strip()
        ):
            reasons.append(
                f"{label} does not say what breaks if the assumption is wrong; "
                f"such an assumption cannot be prioritised, cannot be tested and "
                f"will never be revisited."
            )

    return reasons


def _operation_label(ordinal: int, operation: ChangeOperationProposal) -> str:
    """Name an operation the way a reviewer would look for it.

    The reference where the proposal supplies one, the temp key otherwise: a
    refusal that says "operation 7" sends somebody counting through JSON.
    """
    identifier = str(operation.values.get("reference") or operation.temp_key or "").strip()
    named = f" {identifier}" if identifier else ""
    return (
        f"Operation {ordinal} ({operation.operation_type.value} "
        f"{operation.object_type.value}{named})"
    )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------
def _profile_view(profile: ProjectProfile | None) -> dict[str, Any]:
    """The brief, as supplied.

    Every key is present even when there is no profile row, so the agent reads
    an empty description rather than having to reason about a missing key — and
    ``present`` says which of the two it is looking at.
    """
    if profile is None:
        return {
            "present": False,
            "project_description": "",
            "missing_required_fields": [],
            "restricted_domains": [],
        }
    return {
        "present": True,
        "project_name": profile.project_name,
        "customer_label": profile.customer_label,
        "plant_or_site": profile.plant_or_site,
        # Verbatim. Requirements cite back into this text, so a summarised copy
        # would break the citation it is the root of.
        "project_description": profile.project_description,
        "project_type": profile.project_type.value,
        "units": profile.units.value,
        "locale": profile.locale,
        "network_architecture": profile.network_architecture,
        "restricted_domains": list(profile.restricted_domains),
        "restricted_domain_mode": profile.restricted_domain_mode.value,
        "missing_required_fields": list(profile.missing_required_fields),
        "blocks_generation": profile.blocks_generation,
    }


def _documents(
    graph: EvidenceGraph, anchors: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Narrative statements with the anchor id each one is cited by."""
    documents: list[dict[str, Any]] = []
    truncated: list[str] = []

    for document in graph.documents:
        statements = document.statements[:MAX_STATEMENTS_PER_DOCUMENT]
        if len(document.statements) > MAX_STATEMENTS_PER_DOCUMENT:
            truncated.append(
                f"{document.filename}: showing {len(statements)} of "
                f"{len(document.statements)} statements."
            )
        documents.append(
            {
                "filename": document.filename,
                "document_role": document.document_role,
                "headings": document.headings,
                "statements": [
                    {**statement, "anchor_id": anchors.get(str(statement.get("anchor_key", "")))}
                    for statement in statements
                ],
            }
        )
    return documents, truncated


def _existing_objects(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    """What is already recorded, so a re-run converges instead of duplicating.

    References are listed separately from the objects because they are unique per
    project: proposing one that is taken fails the whole change set at merge, and
    the agent has no other way to know which are free.
    """
    requirements = list(
        db.execute(
            select(Requirement)
            .where(Requirement.project_id == project_id, Requirement.deleted_at.is_(None))
            .order_by(Requirement.reference)
            .limit(MAX_EXISTING_OBJECTS)
        ).scalars()
    )
    assumptions = list(
        db.execute(
            select(Assumption)
            .where(Assumption.project_id == project_id, Assumption.deleted_at.is_(None))
            .order_by(Assumption.reference)
            .limit(MAX_EXISTING_OBJECTS)
        ).scalars()
    )
    constraints = list(
        db.execute(
            select(DesignConstraint)
            .where(DesignConstraint.project_id == project_id, DesignConstraint.deleted_at.is_(None))
            .order_by(DesignConstraint.reference)
            .limit(MAX_EXISTING_OBJECTS)
        ).scalars()
    )
    # References only: the criteria themselves add little the requirement above
    # does not already say, but the reference namespace is unique per project and
    # a collision fails the whole change set at merge.
    criterion_references = list(
        db.execute(
            select(AcceptanceCriterion.reference)
            .where(
                AcceptanceCriterion.project_id == project_id,
                AcceptanceCriterion.deleted_at.is_(None),
            )
            .order_by(AcceptanceCriterion.reference)
            .limit(MAX_EXISTING_OBJECTS)
        ).scalars()
    )
    open_clarifications = list(
        db.execute(
            select(Clarification)
            .where(
                Clarification.project_id == project_id,
                Clarification.deleted_at.is_(None),
                Clarification.status == ClarificationStatus.OPEN,
            )
            .order_by(Clarification.created_at)
            .limit(MAX_EXISTING_OBJECTS)
        ).scalars()
    )

    return {
        "requirements": [
            {
                "reference": item.reference,
                "title": item.title,
                "statement": item.statement,
                "kind": item.kind.value,
                "status": item.status.value,
                "is_inferred": item.is_inferred,
                "confidence": item.confidence,
            }
            for item in requirements
        ],
        "assumptions": [
            {
                "reference": item.reference,
                "statement": item.statement,
                "impact_if_wrong": item.impact_if_wrong,
                "is_confirmed": item.is_confirmed,
            }
            for item in assumptions
        ],
        "constraints": [
            {
                "reference": item.reference,
                "statement": item.statement,
                "is_hard": item.is_hard,
            }
            for item in constraints
        ],
        "open_clarifications": [
            {"question": item.question, "ambiguity": item.ambiguity} for item in open_clarifications
        ],
        "references_in_use": {
            ObjectType.REQUIREMENT.value: [item.reference for item in requirements],
            ObjectType.ACCEPTANCE_CRITERION.value: criterion_references,
            ObjectType.ASSUMPTION.value: [item.reference for item in assumptions],
            ObjectType.CONSTRAINT.value: [item.reference for item in constraints],
        },
    }
