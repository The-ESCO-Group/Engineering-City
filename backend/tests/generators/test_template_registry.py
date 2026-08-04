"""Tests for the versioned control-pattern template registry.

Two things are being protected here.

The first is the shipped template set: every file in ``templates/`` must load,
with zero load errors, using only the published enum vocabulary. A template that
stops parsing should fail the build rather than disappear quietly at runtime and
leave a generator with no pattern to expand.

The second is the loader's two promises — that a checksum is derived from the
content and therefore cannot be stale, and that one bad file is recorded rather
than raised. Both are tested against temporary trees, so exercising the failure
paths never requires shipping a broken template.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.enums import (
    BaseDataType,
    ControllerFamily,
    LogicElementKind,
    RestrictedDomain,
    RoutineLanguage,
    TagRole,
    TemplateApprovalLevel,
    TemplateCategory,
)
from app.generators.plc.template_registry import (
    PARAMETER_TYPES,
    RungElement,
    RungSpec,
    Template,
    TemplateRegistry,
    compute_checksum,
    default_template_root,
    get_registry,
    load_templates,
    template_for,
)

#: A mnemonic as it may appear in a template: plain upper case, nothing else.
#: Deliberately not imported from ``instructions.py`` — that module is owned
#: elsewhere and this assertion must hold whether or not it exists.
MNEMONIC = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Every template the brief requires, by id. Listed explicitly so that deleting
#: one is a test failure rather than a smaller registry nobody notices.
EXPECTED_IDS = frozenset(
    {
        "esco.controller.compactlogix.machine",
        "esco.controller.controllogix.process",
        "esco.motor.standard",
        "esco.vfd.standard",
        "esco.valve.standard",
        "esco.analog.input.scaling",
        "esco.permissive.interlock",
        "esco.fault.reset",
        "esco.mode.selection",
        "esco.sequence.state_machine",
        "esco.communication.loss",
        "esco.diagnostic.controller",
    }
)

#: Patterns where a mistake would be dangerous. Each must demand a senior
#: reviewer; the whole point of the level is that it cannot be forgotten.
DANGEROUS_IDS = frozenset(
    {
        "esco.fault.reset",
        "esco.permissive.interlock",
        "esco.communication.loss",
        "esco.sequence.state_machine",
    }
)


@pytest.fixture(scope="module")
def registry() -> TemplateRegistry:
    """The shipped template set, loaded once."""
    return load_templates()


@pytest.fixture
def template_body() -> dict[str, object]:
    """A minimal, valid template body that tests can mutate."""
    return {
        "id": "esco.test.pattern",
        "version": "1.0.0",
        "category": TemplateCategory.MOTOR_CONTROL.value,
        "display_name": "Test pattern",
        "description": "A minimal template used by the registry tests.",
        "supported_families": [ControllerFamily.COMPACTLOGIX.value],
        "approval_level": TemplateApprovalLevel.STANDARD.value,
        "restricted_domains": [],
        "parameters": [
            {
                "name": "device_tag",
                "type": "identifier",
                "required": True,
                "description": "Device tag stem",
            },
            {
                "name": "has_run_feedback",
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Run feedback is wired",
            },
        ],
        "required_tags": [
            {
                "role": TagRole.COMMAND.value,
                "suffix": "_Cmd_Start",
                "data_type": BaseDataType.BOOL.value,
                "description": "Operator start request",
            }
        ],
        "generated_objects": {
            "routines": [
                {
                    "name": "R_Devices",
                    "language": RoutineLanguage.LADDER.value,
                    "description": "Device control blocks",
                }
            ],
            "rungs": [
                {
                    "routine": "R_Devices",
                    "title": "{device_tag} run request",
                    "comment": "Seal-in with the permissive on the start branch.",
                    "elements": [
                        {
                            "branch": [
                                [{"instruction": "XIC", "operand": "{device_tag}_Cmd_Start"}],
                                [{"instruction": "XIC", "operand": "{device_tag}_Cmd_Run"}],
                            ]
                        },
                        {
                            "instruction": "OTE",
                            "operand": "{device_tag}_Cmd_Run",
                            "kind": LogicElementKind.OUTPUT.value,
                        },
                    ],
                }
            ],
        },
        "engineering_notes": "Why this pattern is arranged the way it is.",
        "restrictions": ["Does nothing useful; it exists for the tests."],
        "tests": [
            {
                "name": "It starts",
                "given": "A stopped device",
                "when": "Start is pulsed",
                "then": "The run command seals in.",
            }
        ],
    }


def write_template(root: Path, name: str, body: object) -> Path:
    path = root / name
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped template set
# ---------------------------------------------------------------------------
def test_every_shipped_template_loads_with_no_errors(registry: TemplateRegistry) -> None:
    assert registry.load_errors == [], "shipped templates failed to load: " + "; ".join(
        f"{error.path}: {error.message}" for error in registry.load_errors
    )
    assert len(registry) > 0


def test_the_expected_templates_are_all_present(registry: TemplateRegistry) -> None:
    assert set(registry.ids) == EXPECTED_IDS


def test_the_template_root_is_found_from_the_source_tree() -> None:
    root = default_template_root()
    assert root.is_dir()
    assert (root / "README.md").is_file()
    assert (root / "patterns").is_dir()
    assert (root / "rockwell" / "controller").is_dir()


def test_qualified_id_is_id_at_version(registry: TemplateRegistry) -> None:
    motor = registry.get("esco.motor.standard")
    assert motor is not None
    assert motor.qualified_id() == f"esco.motor.standard@{motor.version}"


def test_versions_are_semantic(registry: TemplateRegistry) -> None:
    for template in registry.all():
        assert re.match(r"^\d+\.\d+\.\d+$", template.version), template.source


def test_supported_families_are_real_controller_family_members(
    registry: TemplateRegistry,
) -> None:
    valid = set(ControllerFamily)
    for template in registry.all():
        assert template.supported_families, template.source
        for family in template.supported_families:
            assert family in valid, f"{template.source}: {family!r}"


def test_every_category_is_a_real_template_category(registry: TemplateRegistry) -> None:
    valid = set(TemplateCategory)
    for template in registry.all():
        assert template.category in valid, f"{template.source}: {template.category!r}"


def test_every_tag_role_and_data_type_is_published_vocabulary(
    registry: TemplateRegistry,
) -> None:
    roles = set(TagRole)
    data_types = set(BaseDataType)
    for template in registry.all():
        for tag in template.required_tags:
            assert tag.role in roles, f"{template.source}: {tag.suffix}"
            assert tag.data_type in data_types, f"{template.source}: {tag.suffix}"
            assert tag.description, f"{template.source}: {tag.suffix} has no description"


def test_every_approval_level_is_published_vocabulary(registry: TemplateRegistry) -> None:
    valid = set(TemplateApprovalLevel)
    for template in registry.all():
        assert template.approval_level in valid, template.source


def test_restricted_domains_force_qualified_review(registry: TemplateRegistry) -> None:
    valid = set(RestrictedDomain)
    for template in registry.all():
        for domain in template.restricted_domains:
            assert domain in valid, template.source
        if template.restricted_domains:
            assert template.approval_level is TemplateApprovalLevel.QUALIFIED_REVIEW_REQUIRED


def test_dangerous_patterns_require_a_senior_reviewer(registry: TemplateRegistry) -> None:
    for template_id in DANGEROUS_IDS:
        template = registry.get(template_id)
        assert template is not None, template_id
        assert template.approval_level is TemplateApprovalLevel.SENIOR_REVIEW_REQUIRED
        assert template.needs_senior_review


def test_every_template_states_what_it_does_not_do(registry: TemplateRegistry) -> None:
    for template in registry.all():
        assert template.restrictions, f"{template.source} states no restrictions"
        for restriction in template.restrictions:
            assert restriction.strip(), template.source


def test_every_template_carries_notes_and_tests(registry: TemplateRegistry) -> None:
    for template in registry.all():
        assert template.engineering_notes.strip(), template.source
        assert template.tests, f"{template.source} declares no tests"
        for case in template.tests:
            assert case.then.strip(), f"{template.source}: test {case.name!r} asserts nothing"


def test_every_parameter_type_is_a_known_kind(registry: TemplateRegistry) -> None:
    for template in registry.all():
        for parameter in template.parameters:
            assert parameter.type in PARAMETER_TYPES, f"{template.source}: {parameter.name}"
            assert parameter.description, f"{template.source}: {parameter.name}"
            if parameter.required:
                assert parameter.default is None, (
                    f"{template.source}: required parameter {parameter.name!r} has a default"
                )


# ---------------------------------------------------------------------------
# Rung structure
# ---------------------------------------------------------------------------
def test_every_instruction_mnemonic_is_a_plain_upper_case_identifier(
    registry: TemplateRegistry,
) -> None:
    seen = 0
    for template in registry.all():
        for element in template.generated_objects.iter_instructions():
            assert MNEMONIC.match(element.instruction), (
                f"{template.source}: {element.instruction!r} is not a plain mnemonic"
            )
            seen += 1
    assert seen > 0, "no instructions were examined; the walk is not doing anything"


def test_every_rung_element_carries_operands_and_a_kind(registry: TemplateRegistry) -> None:
    for template in registry.all():
        for rung in template.generated_objects.rungs:
            assert rung.comment.strip(), f"{template.source}: rung {rung.title!r} has no comment"
            for element in rung.iter_instructions():
                assert element.operands, f"{template.source}: {element.instruction} has no operands"
                assert element.kind in {
                    LogicElementKind.CONDITION,
                    LogicElementKind.OUTPUT,
                }, f"{template.source}: {element.instruction} has kind {element.kind!r}"


def test_branches_are_nested_and_have_at_least_two_legs(registry: TemplateRegistry) -> None:
    branches = 0
    for template in registry.all():
        for rung in template.generated_objects.rungs:
            for element in rung.elements:
                if not element.is_branch:
                    continue
                branches += 1
                assert len(element.branch) >= 2, f"{template.source}: rung {rung.title!r}"
                for leg in element.branch:
                    assert leg, f"{template.source}: empty branch leg in {rung.title!r}"
    assert branches > 0, "no shipped template uses a branch; the nested form is untested"


def test_a_branch_walks_every_leg() -> None:
    element = RungElement.from_mapping(
        {
            "branch": [
                [{"instruction": "XIC", "operand": "A"}],
                [
                    {"instruction": "XIC", "operand": "B"},
                    {
                        "branch": [
                            [{"instruction": "XIC", "operand": "C"}],
                            [{"instruction": "XIO", "operand": "D"}],
                        ]
                    },
                ],
            ]
        },
        "test",
    )
    assert element.is_branch
    operands = [item.operands[0] for item in element.iter_instructions()]
    assert operands == ["A", "B", "C", "D"]


def test_a_single_leg_branch_is_refused(tmp_path: Path, template_body: dict[str, object]) -> None:
    objects = template_body["generated_objects"]
    assert isinstance(objects, dict)
    objects["rungs"][0]["elements"][0] = {  # type: ignore[index]
        "branch": [[{"instruction": "XIC", "operand": "A"}]]
    }
    write_template(tmp_path, "single_leg.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert len(registry.load_errors) == 1
    assert "at least two parallel legs" in registry.load_errors[0].message


def test_branch_markers_are_refused(tmp_path: Path, template_body: dict[str, object]) -> None:
    objects = template_body["generated_objects"]
    assert isinstance(objects, dict)
    objects["rungs"][0]["elements"][0] = {  # type: ignore[index]
        "instruction": "XIC",
        "operand": "A",
        "kind": LogicElementKind.BRANCH_START.value,
    }
    write_template(tmp_path, "markers.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "branch structure is expressed with 'branch'" in registry.load_errors[0].message


def test_a_lower_case_mnemonic_is_refused(tmp_path: Path, template_body: dict[str, object]) -> None:
    objects = template_body["generated_objects"]
    assert isinstance(objects, dict)
    objects["rungs"][0]["elements"][1]["instruction"] = "Ote"  # type: ignore[index]
    write_template(tmp_path, "lower.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "not a plain upper-case mnemonic" in registry.load_errors[0].message


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------
def test_checksums_are_stable_across_two_loads() -> None:
    first = {template.qualified_id(): template.checksum for template in load_templates().all()}
    second = {template.qualified_id(): template.checksum for template in load_templates().all()}
    assert first == second
    assert all(len(digest) == 64 for digest in first.values())


def test_a_checksum_changes_when_the_body_changes(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    write_template(tmp_path, "pattern.yaml", template_body)
    before = load_templates(tmp_path).get("esco.test.pattern")
    assert before is not None

    reloaded = load_templates(tmp_path).get("esco.test.pattern")
    assert reloaded is not None
    assert reloaded.checksum == before.checksum, "the same bytes must give the same digest"

    template_body["description"] = "The same template, with one word changed."
    write_template(tmp_path, "pattern.yaml", template_body)
    after = load_templates(tmp_path).get("esco.test.pattern")

    assert after is not None
    assert after.version == before.version
    assert after.checksum != before.checksum, (
        "an edited template kept its checksum; the digest is not derived from the content"
    )


def test_a_checksum_survives_reformatting(tmp_path: Path, template_body: dict[str, object]) -> None:
    """Key order and layout are not content, so they must not move the digest."""
    write_template(tmp_path, "pattern.yaml", template_body)
    before = load_templates(tmp_path).get("esco.test.pattern")
    assert before is not None

    reordered = dict(reversed(list(template_body.items())))
    (tmp_path / "pattern.yaml").write_text(
        yaml.safe_dump(reordered, sort_keys=False, default_flow_style=False, width=40),
        encoding="utf-8",
    )
    after = load_templates(tmp_path).get("esco.test.pattern")

    assert after is not None
    assert after.checksum == before.checksum


def test_a_stored_checksum_is_ignored(tmp_path: Path, template_body: dict[str, object]) -> None:
    """A checksum written into the file is not read back as the checksum.

    It is treated as body content like anything else, so a human-supplied digest
    can never be mistaken for a verified one.
    """
    template_body["checksum"] = "0" * 64
    write_template(tmp_path, "pattern.yaml", template_body)

    template = load_templates(tmp_path).get("esco.test.pattern")

    assert template is not None
    assert template.checksum != "0" * 64


def test_compute_checksum_is_order_independent() -> None:
    assert compute_checksum({"a": 1, "b": [1, 2]}) == compute_checksum({"b": [1, 2], "a": 1})
    assert compute_checksum({"a": 1}) != compute_checksum({"a": 2})


def test_list_order_does_change_the_checksum() -> None:
    """Rung order is behaviour, so a reordered list is a different template."""
    assert compute_checksum({"rungs": [1, 2]}) != compute_checksum({"rungs": [2, 1]})


# ---------------------------------------------------------------------------
# Defensive loading
# ---------------------------------------------------------------------------
def test_malformed_yaml_is_recorded_and_does_not_raise(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        "id: esco.broken\nversion: [1.0.0\n  bad: indent:\n", encoding="utf-8"
    )

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert len(registry.load_errors) == 1
    error = registry.load_errors[0]
    assert error.path == "broken.yaml"
    assert error.message


def test_one_bad_file_does_not_stop_the_others(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    write_template(tmp_path, "good.yaml", template_body)
    (tmp_path / "bad.yaml").write_text("::: not yaml :::\n- [", encoding="utf-8")

    registry = load_templates(tmp_path)

    assert registry.get("esco.test.pattern") is not None
    assert [error.path for error in registry.load_errors] == ["bad.yaml"]


def test_an_empty_file_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("", encoding="utf-8")

    registry = load_templates(tmp_path)

    assert len(registry.load_errors) == 1
    assert "empty" in registry.load_errors[0].message


def test_an_unknown_enum_value_is_a_load_error_naming_the_alternatives(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    template_body["category"] = "motor_control_v2"
    write_template(tmp_path, "pattern.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    message = registry.load_errors[0].message
    assert "TemplateCategory" in message
    assert TemplateCategory.MOTOR_CONTROL.value in message


def test_an_unknown_controller_family_is_a_load_error(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    template_body["supported_families"] = ["compactlogix", "micrologix"]
    write_template(tmp_path, "pattern.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "ControllerFamily" in registry.load_errors[0].message


def test_a_template_with_no_restrictions_is_refused(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    template_body["restrictions"] = []
    write_template(tmp_path, "pattern.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "what this template does not do" in registry.load_errors[0].message


def test_a_rung_targeting_an_unknown_routine_is_refused(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    objects = template_body["generated_objects"]
    assert isinstance(objects, dict)
    objects["rungs"][0]["routine"] = "R_Nowhere"  # type: ignore[index]
    write_template(tmp_path, "pattern.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "which this template does not create" in registry.load_errors[0].message


def test_a_required_parameter_with_a_default_is_refused(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    parameters = template_body["parameters"]
    assert isinstance(parameters, list)
    parameters[0]["default"] = "MTR_101"  # type: ignore[index]
    write_template(tmp_path, "pattern.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert "must not carry a default" in registry.load_errors[0].message


def test_a_duplicate_id_and_version_is_recorded(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    write_template(tmp_path, "a_first.yaml", template_body)
    write_template(tmp_path, "b_second.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 1
    assert "duplicate template" in registry.load_errors[0].message


def test_a_missing_template_root_is_recorded_not_raised(tmp_path: Path) -> None:
    registry = load_templates(tmp_path / "nowhere")

    assert len(registry) == 0
    assert "does not exist" in registry.load_errors[0].message


def test_a_directory_with_no_templates_is_simply_empty(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a template", encoding="utf-8")

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert registry.load_errors == []


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------
@pytest.fixture
def motor(registry: TemplateRegistry) -> Template:
    template = registry.get("esco.motor.standard")
    assert template is not None
    return template


def test_valid_parameters_produce_no_problems(motor: Template) -> None:
    assert motor.validate_parameters({"device_tag": "MTR_101"}) == []
    assert (
        motor.validate_parameters(
            {"device_tag": "MTR_101", "has_run_feedback": False, "fail_to_start_ms": 3000}
        )
        == []
    )


def test_a_missing_required_parameter_is_caught(motor: Template) -> None:
    problems = motor.validate_parameters({"has_run_feedback": True})
    assert problems == ["missing required parameter 'device_tag'"]


def test_an_unknown_parameter_is_caught(motor: Template) -> None:
    problems = motor.validate_parameters({"device_tag": "MTR_101", "has_runn_feedback": True})
    assert problems == ["unknown parameter 'has_runn_feedback'"]


def test_both_problems_are_reported_together(motor: Template) -> None:
    problems = motor.validate_parameters({"colour": "red"})
    assert problems == [
        "missing required parameter 'device_tag'",
        "unknown parameter 'colour'",
    ]


def test_a_wrong_type_is_caught(motor: Template) -> None:
    problems = motor.validate_parameters({"device_tag": "MTR_101", "has_run_feedback": "yes"})
    assert problems == ["parameter 'has_run_feedback' expects a boolean"]


def test_an_invalid_identifier_is_caught(motor: Template) -> None:
    problems = motor.validate_parameters({"device_tag": "101-MTR"})
    assert problems == ["parameter 'device_tag' must be a Logix identifier, got '101-MTR'"]


def test_a_boolean_is_not_accepted_as_an_integer(motor: Template) -> None:
    problems = motor.validate_parameters({"device_tag": "MTR_101", "fail_to_start_ms": True})
    assert problems == ["parameter 'fail_to_start_ms' expects an integer"]


def test_an_enum_parameter_rejects_a_value_outside_its_choices(
    registry: TemplateRegistry,
) -> None:
    comms = registry.get("esco.communication.loss")
    assert comms is not None
    problems = comms.validate_parameters({"node_tag": "RIO_1", "fail_state": "shut_down"})
    assert problems == [
        "parameter 'fail_state' must be one of: de_energize, energize, hold_last_state, go_to_value"
    ]
    assert comms.validate_parameters({"node_tag": "RIO_1", "fail_state": "de_energize"}) == []


def test_the_fail_state_has_no_default(registry: TemplateRegistry) -> None:
    """An undefined fail state is a documented gap, not something to guess."""
    comms = registry.get("esco.communication.loss")
    assert comms is not None
    fail_state = comms.parameter("fail_state")
    assert fail_state is not None
    assert fail_state.required
    assert fail_state.default is None
    assert comms.validate_parameters({"node_tag": "RIO_1"}) == [
        "missing required parameter 'fail_state'"
    ]


# ---------------------------------------------------------------------------
# Conditional tags and rungs
# ---------------------------------------------------------------------------
def test_run_feedback_tags_and_rungs_drop_out_when_it_is_absent(motor: Template) -> None:
    with_feedback = {"device_tag": "MTR_101", "has_run_feedback": True}
    without = {"device_tag": "MTR_101", "has_run_feedback": False}

    assert len(motor.required_tags_for(without)) < len(motor.required_tags_for(with_feedback))
    assert not any(tag.suffix == "_In_Run_Fbk" for tag in motor.required_tags_for(without))
    assert any(tag.suffix == "_In_Run_Fbk" for tag in motor.required_tags_for(with_feedback))

    titles = {rung.title for rung in motor.rungs_for(without)}
    assert "{device_tag} running status from command" in titles
    assert "{device_tag} running status from feedback" not in titles


def test_a_gate_falls_back_to_the_parameter_default(motor: Template) -> None:
    """has_run_feedback defaults to true, so an unstated value keeps the tags."""
    assert any(
        tag.suffix == "_In_Run_Fbk" for tag in motor.required_tags_for({"device_tag": "MTR_101"})
    )


def test_exactly_one_running_status_rung_applies_either_way(motor: Template) -> None:
    for value in (True, False):
        titles = [rung.title for rung in motor.rungs_for({"has_run_feedback": value})]
        running = [title for title in titles if "running status" in title]
        assert len(running) == 1, value


# ---------------------------------------------------------------------------
# Fault reset — the restriction that matters
# ---------------------------------------------------------------------------
def test_fault_reset_states_that_it_will_not_clear_an_active_condition(
    registry: TemplateRegistry,
) -> None:
    """The classic dangerous mistake, stated where a reviewer will read it.

    A reset that clears a fault whose condition is still true returns a machine
    to service with the original problem present and no indication of it. The
    template exists partly to make that hard, so the promise is asserted here
    rather than left to a reader noticing it.
    """
    fault_reset = registry.get("esco.fault.reset")
    assert fault_reset is not None

    stated = [
        restriction
        for restriction in fault_reset.restrictions
        if "reset does not clear an active condition" in restriction.lower()
    ]
    assert stated, (
        "esco.fault.reset must state plainly that the reset does not clear an "
        f"active condition. Restrictions were: {fault_reset.restrictions}"
    )


def test_fault_reset_puts_the_condition_check_in_series_with_the_unlatch(
    registry: TemplateRegistry,
) -> None:
    """The restriction is only worth anything if the logic implements it."""
    fault_reset = registry.get("esco.fault.reset")
    assert fault_reset is not None

    reset_rungs = [
        rung
        for rung in fault_reset.generated_objects.rungs
        if any(
            element.instruction == "OTU" and element.operands[0].endswith("_Sts_Faulted")
            for element in rung.iter_instructions()
        )
    ]
    assert len(reset_rungs) == 1, "expected exactly one rung that unlatches the fault"

    rung = reset_rungs[0]
    examined_if_open = {
        element.operands[0] for element in rung.iter_instructions() if element.instruction == "XIO"
    }
    assert "{device_tag}_Fault_Condition" in examined_if_open, (
        "the unlatch rung must examine the live fault condition if-open, or a reset "
        "will clear a fault whose cause is still present"
    )
    assert any(element.instruction == "ONS" for element in rung.iter_instructions()), (
        "the reset must be edge-triggered so a held reset cannot clear repeated faults"
    )


# ---------------------------------------------------------------------------
# The ladder itself
#
# Everything above this line protects the file format. These protect the
# CONTENT: the scan-order and latch defects that make a control pattern wrong
# rather than unreadable. A template is reviewed once and then replicated into
# every project that uses it, so a defect here is not one project's bug — and
# "the file parsed" says nothing about whether the logic works.
# ---------------------------------------------------------------------------
#: ``"{device_tag}_Tmr_Travel.DN"`` -> ``("device_tag", "_Tmr_Travel")``. The
#: member falls outside ``\w+``, which is what makes the captured suffix
#: directly comparable with a declared tag suffix.
PLACEHOLDER_TAG = re.compile(r"\{(\w+)\}(\w+)")

#: Bits latched in one template and unlatched in another (or deliberately never
#: unlatched), each with the reason. Anything else that is latched must be
#: unlatched by the same template, or it is a latch nothing turns off.
LATCHED_ELSEWHERE = {
    "_Sts_Faulted": "esco.fault.reset owns the unlatch; both templates document the pair",
    "_Sts_Conn_Established": "one-way for the life of the controller session, by design",
}

#: The equipment patterns that seal in a run/open command.
DEVICE_IDS = ("esco.motor.standard", "esco.vfd.standard", "esco.valve.standard")

CONTROLLER_LAYOUT_IDS = (
    "esco.controller.compactlogix.machine",
    "esco.controller.controllogix.process",
)


def gating_combinations(template: Template) -> list[dict[str, object]]:
    """Every set of parameter values that changes which tags and rungs apply.

    A template with no gates yields one empty combination, so callers can walk
    the result unconditionally.
    """
    gates = sorted(
        {tag.when_parameter for tag in template.required_tags if tag.when_parameter}
        | {rung.when_parameter for rung in template.generated_objects.rungs if rung.when_parameter}
    )
    options: list[list[tuple[str, object]]] = []
    for name in gates:
        parameter = template.parameter(name)
        assert parameter is not None, f"{template.source}: gate on undeclared {name!r}"
        if parameter.type == "boolean":
            values: list[object] = [True, False]
        elif parameter.type == "enum":
            values = list(parameter.choices)
        else:  # pragma: no cover - no shipped template gates on another type
            values = [parameter.default]
        options.append([(name, value) for value in values])
    return [dict(combination) for combination in itertools.product(*options)]


def rung_titled(template: Template, fragment: str) -> RungSpec:
    """The one rung whose title contains ``fragment``."""
    matches = [rung for rung in template.generated_objects.rungs if fragment in rung.title]
    assert len(matches) == 1, (
        f"{template.source}: {fragment!r} matched {len(matches)} rungs, expected exactly one"
    )
    return matches[0]


def written_with(rung: RungSpec, mnemonic: str) -> set[str]:
    """Operands this rung writes with ``mnemonic`` (OTE / OTL / OTU)."""
    return {
        element.operands[0]
        for element in rung.iter_instructions()
        if element.instruction == mnemonic
    }


def examined_if_open(rung: RungSpec) -> set[str]:
    return {
        element.operands[0] for element in rung.iter_instructions() if element.instruction == "XIO"
    }


def loaded(registry: TemplateRegistry, template_id: str) -> Template:
    template = registry.get(template_id)
    assert template is not None, f"{template_id} is not in the shipped template set"
    return template


def test_no_template_writes_one_bit_from_two_output_energises(
    registry: TemplateRegistry,
) -> None:
    """The duplicate-destination defect, checked per parameter combination.

    An OTE writes its operand every scan in both directions, so two of them on
    one tag means the later rung always wins and the earlier one might as well
    not be there. It is invisible when reading a single rung, which is exactly
    why it survives review. Rungs gated onto mutually exclusive parameter values
    are not duplicates, so the walk is per combination rather than over the file.
    """
    for template in registry.all():
        for values in gating_combinations(template):
            seen: dict[str, str] = {}
            for rung in template.rungs_for(values):
                for operand in written_with(rung, "OTE"):
                    first = seen.get(operand)
                    assert first is None, (
                        f"{template.source} with {values}: {operand} is energised by both "
                        f"{first!r} and {rung.title!r}; the second rung wins every scan"
                    )
                    seen[operand] = rung.title


def test_every_rung_operand_names_a_tag_the_template_would_create(
    registry: TemplateRegistry,
) -> None:
    """A rung may not reference a tag the same parameters do not produce.

    ``required_tags_for`` and ``rungs_for`` are gated separately, so a rung and
    the tag it reads can disagree — and the expansion then emits an L5X carrying
    an operand nothing declares. This walks every combination of every gate.
    """
    for template in registry.all():
        stems = {
            parameter.name for parameter in template.parameters if parameter.type == "identifier"
        }
        for values in gating_combinations(template):
            declared = {tag.suffix for tag in template.required_tags_for(values)}
            for rung in template.rungs_for(values):
                for element in rung.iter_instructions():
                    for operand in element.operands:
                        for stem, suffix in PLACEHOLDER_TAG.findall(operand):
                            if stem not in stems:
                                continue
                            assert suffix in declared, (
                                f"{template.source} with {values}: rung {rung.title!r} "
                                f"references {{{stem}}}{suffix}, which is not in the tag "
                                "list those parameters produce"
                            )


def test_every_latch_has_an_unlatch(registry: TemplateRegistry) -> None:
    """A latched bit with no reachable OTU is a bit nothing turns off.

    That is not a style point: it is how a tripped interlock becomes
    unrecoverable without a power cycle. The two bits that are cleared by a
    different template are named, with the reason, so adding a third is a
    deliberate act rather than an oversight.
    """
    for template in registry.all():
        latched = {
            element.operands[0]
            for element in template.generated_objects.iter_instructions()
            if element.instruction == "OTL"
        }
        unlatched = {
            element.operands[0]
            for element in template.generated_objects.iter_instructions()
            if element.instruction == "OTU"
        }
        for operand in sorted(latched - unlatched):
            reason = next(
                (why for suffix, why in LATCHED_ELSEWHERE.items() if operand.endswith(suffix)),
                None,
            )
            assert reason is not None, (
                f"{template.source}: {operand} is latched and never unlatched here. "
                "Either unlatch it in this template or add it to LATCHED_ELSEWHERE "
                "with the template that does."
            )


def test_the_sequence_start_latches_the_running_flag(registry: TemplateRegistry) -> None:
    """The start rung is true for one scan, so the flag has to be latched.

    Energising it wrote the flag back to false on the following scan: the step
    timer never ran, the advance rung was blocked by its own leading contact, no
    step ever timed out, and the machine sat on step 1 with no alarm.
    """
    sequence = loaded(registry, "esco.sequence.state_machine")
    running = "{machine_tag}_Sts_Seq_Running"

    start = rung_titled(sequence, "start the sequence")
    assert running in written_with(start, "OTL"), (
        "the start rung must latch _Sts_Seq_Running; its own rung condition is spent "
        "after one scan, so an OTE writes the flag back to false"
    )
    energised = {
        rung.title
        for rung in sequence.generated_objects.rungs
        if running in written_with(rung, "OTE")
    }
    assert not energised, f"_Sts_Seq_Running is energised by {energised}"
    unlatched = {
        rung.title
        for rung in sequence.generated_objects.rungs
        if running in written_with(rung, "OTU")
    }
    assert unlatched, "nothing clears _Sts_Seq_Running, so the sequence can never return to idle"


def test_the_sequence_timeout_alarm_outlives_the_abort_it_triggers(
    registry: TemplateRegistry,
) -> None:
    """The timeout faults the machine, the fault aborts, the abort resets the timer.

    An energised alarm driven from that timer therefore cleared within two scans
    of appearing, leaving a stopped machine with nothing saying why.
    """
    sequence = loaded(registry, "esco.sequence.state_machine")
    alarm = "{machine_tag}_Alm_Seq_Step_Timeout"

    timeout = rung_titled(sequence, "step timeout")
    assert alarm in written_with(timeout, "OTL")

    clearing = [
        rung for rung in sequence.generated_objects.rungs if alarm in written_with(rung, "OTU")
    ]
    assert len(clearing) == 1
    assert "{machine_tag}_Sts_Faulted" in examined_if_open(clearing[0]), (
        "the timeout alarm must be cleared with the fault it belongs to, not on its own"
    )


@pytest.mark.parametrize(
    ("template_id", "title_fragment", "command_suffix"),
    [
        ("esco.motor.standard", "run request", "_Cmd_Start"),
        ("esco.vfd.standard", "run request", "_Cmd_Start"),
        ("esco.valve.standard", "open request", "_Cmd_Open"),
    ],
)
def test_a_device_takes_its_start_as_a_rising_edge(
    registry: TemplateRegistry,
    template_id: str,
    title_fragment: str,
    command_suffix: str,
) -> None:
    """Three-wire control, and the one-shot is what makes it three-wire.

    Without it the start branch is a standing request: a sequence step or an HMI
    bit left true re-seals the equipment by itself the moment the stop, the
    interlock trip or the latched fault that dropped it goes away — including on
    the scan a fault reset is accepted, with somebody at the machine. The
    one-shot must also sit AHEAD of the permissive, or a held request starts the
    equipment unattended at the moment the permissive returns.
    """
    template = loaded(registry, template_id)
    rung = rung_titled(template, title_fragment)

    start_leg: list[tuple[str, str]] | None = None
    for element in rung.elements:
        if not element.is_branch:
            continue
        for leg in element.branch:
            pairs = [(item.instruction, item.operands[0]) for item in leg]
            if any(operand.endswith(command_suffix) for _, operand in pairs):
                start_leg = pairs
    assert start_leg is not None, f"{template.source}: no start branch found"

    mnemonics = [mnemonic for mnemonic, _ in start_leg]
    assert "ONS" in mnemonics, (
        f"{template.source}: the start branch has no one-shot, so a held "
        f"{command_suffix} restarts the equipment on its own"
    )
    command = next(
        index for index, (_, operand) in enumerate(start_leg) if operand.endswith(command_suffix)
    )
    one_shot = mnemonics.index("ONS")
    permissive = next(
        index for index, (_, operand) in enumerate(start_leg) if operand.endswith("_Permissive")
    )
    assert command < one_shot < permissive, (
        f"{template.source}: the one-shot must follow the command and precede the "
        "permissive, or a start held through a false permissive fires when it returns"
    )


@pytest.mark.parametrize("template_id", [*DEVICE_IDS, "esco.sequence.state_machine"])
def test_a_latched_fault_always_leaves_an_alarm_bit_set(
    registry: TemplateRegistry, template_id: str
) -> None:
    """Latching a fault removes the condition that was driving its own alarm.

    The fault drops the run command, the run command was enabling the timer, the
    timer was driving the alarm — so an energised alarm cleared one scan after it
    appeared and the equipment sat faulted with no stated cause. Every alarm
    raised alongside a fault latch must be latched too, and cleared with it.
    """
    template = loaded(registry, template_id)

    latched_alarms: set[str] = set()
    for rung in template.generated_objects.rungs:
        if not any(operand.endswith("_Sts_Faulted") for operand in written_with(rung, "OTL")):
            continue
        for element in rung.iter_instructions():
            if element.kind is not LogicElementKind.OUTPUT or "_Alm_" not in element.operands[0]:
                continue
            assert element.instruction == "OTL", (
                f"{template.source}: rung {rung.title!r} latches the fault but "
                f"{element.instruction}s {element.operands[0]}, which then clears itself"
            )
            latched_alarms.add(element.operands[0])
    assert latched_alarms, f"{template.source}: no alarm accompanies the fault latch"

    cleared_with_the_fault: set[str] = set()
    for rung in template.generated_objects.rungs:
        if not any(operand.endswith("_Sts_Faulted") for operand in examined_if_open(rung)):
            continue
        cleared_with_the_fault |= written_with(rung, "OTU")
    assert latched_alarms <= cleared_with_the_fault, (
        f"{template.source}: {sorted(latched_alarms - cleared_with_the_fault)} stay latched "
        "after the fault is reset"
    )


def test_the_valve_cannot_report_the_wrong_end_of_the_stroke(
    registry: TemplateRegistry,
) -> None:
    """Fail-to-open must not become fail-to-close one scan later.

    The two rungs are told apart by the sealed open command, and latching the
    fault removes it, so without a guard the surviving alarm was the opposite of
    what happened.
    """
    valve = loaded(registry, "esco.valve.standard")

    for fragment in ("fail to open", "fail to close"):
        rung = rung_titled(valve, fragment)
        assert "{device_tag}_Sts_Faulted" in examined_if_open(rung), (
            f"the {fragment!r} rung must be blocked once a fault is latched, or the "
            "other direction's rung becomes true and overwrites the diagnosis"
        )


def test_the_valve_takes_both_limit_switches_or_neither(registry: TemplateRegistry) -> None:
    """One limit switch is not a supported configuration, and says so.

    "Travelling" is defined as being at neither limit, so with one switch the
    valve was declared travelling for the whole time it rested at the position
    nothing was watching — and the travel timer faulted it a few seconds after
    every stroke to that position.
    """
    valve = loaded(registry, "esco.valve.standard")

    names = {parameter.name for parameter in valve.parameters}
    assert "has_position_feedback" in names
    assert not {"has_open_feedback", "has_closed_feedback"} & names, (
        "the two independent feedback parameters generated rungs against tags the "
        "tag list did not contain, and are replaced by one"
    )
    stated = [
        restriction
        for restriction in valve.restrictions
        if "one limit switch" in restriction.lower()
    ]
    assert stated, f"the unsupported configuration is not stated: {valve.restrictions}"


def test_the_vfd_flags_a_clamp_in_either_direction(registry: TemplateRegistry) -> None:
    """One destination, both directions.

    With an OTE on each clamp rung the later one won, so a request clamped HIGH
    was never flagged: the HMI showed 75 Hz, the drive ran at 60, and the bit
    that exists to say so stayed off.
    """
    vfd = loaded(registry, "esco.vfd.standard")
    clamped = "{device_tag}_Sts_SP_Clamped"

    writers = [rung for rung in vfd.generated_objects.rungs if clamped in written_with(rung, "OTE")]
    assert len(writers) == 1, f"{clamped} is written by {[rung.title for rung in writers]}"

    comparisons = {
        element.instruction
        for element in writers[0].iter_instructions()
        if element.kind is LogicElementKind.CONDITION
    }
    assert {"GRT", "LES"} <= comparisons, (
        "the single clamp flag must test both ends of the range, or one direction "
        "is silently unflagged"
    )


def test_the_interlock_trip_latch_is_reset_by_this_template(
    registry: TemplateRegistry,
) -> None:
    """The trip latch had no unlatch anywhere in the repository.

    The restriction said "use esco.fault.reset", but that pattern unlatches
    _Sts_Faulted, which this one never sets — so a tripped interlock could not be
    cleared for the life of the controller session.
    """
    gate = loaded(registry, "esco.permissive.interlock")
    tripped = "{device_tag}_Sts_Ilk_Tripped"

    resets = [rung for rung in gate.generated_objects.rungs if tripped in written_with(rung, "OTU")]
    assert len(resets) == 1, "expected exactly one rung that clears the interlock trip"
    rung = resets[0]

    mnemonics = [element.instruction for element in rung.iter_instructions()]
    operands = [element.operands[0] for element in rung.iter_instructions()]
    assert "ONS" in mnemonics, "the reset must be edge-triggered, like the fault reset"
    assert any(operand.endswith("_Cmd_Reset") for operand in operands), (
        "the reset must be driven by the operator command, not by the sources recovering"
    )
    assert any(
        element.instruction == "XIC" and element.operands[0].endswith("_Ilk_Sources_OK")
        for element in rung.iter_instructions()
    ), "the reset must be refused while the interlock sources are still bad"
    assert any(
        element.instruction == "MOV" and element.operands[-1].endswith("_Ilk_First_Out")
        for element in rung.iter_instructions()
    ), "the first-out record must be zeroed with the latch, or 'zero means no trip' is false"


def test_the_first_out_capture_is_locked_while_a_trip_is_latched(
    registry: TemplateRegistry,
) -> None:
    """A one-shot re-arms whenever its rung goes false, and recovery does that.

    So a source that tripped, cleared itself and was followed by a consequential
    trip overwrote the recorded cause with the consequence — with no reset in
    between. The capture is gated on the trip, and must be scanned before the
    latch or it could never capture anything at all.
    """
    gate = loaded(registry, "esco.permissive.interlock")
    tripped = "{device_tag}_Sts_Ilk_Tripped"

    capture = rung_titled(gate, "first out capture")
    assert tripped in examined_if_open(capture), (
        "the capture must be blocked while a trip is latched, or a later trip "
        "overwrites the first-out record"
    )

    order = [rung.title for rung in gate.generated_objects.rungs]
    latch = rung_titled(gate, "interlock trip latch")
    assert order.index(capture.title) < order.index(latch.title), (
        "the capture is gated on the trip not being latched, so it has to be scanned "
        "before the rung that latches it"
    )


@pytest.mark.parametrize(
    ("template_id", "timer_suffix"),
    [
        ("esco.fault.reset", "_Tmr_Reset_Blocked"),
        ("esco.mode.selection", "_Tmr_Mode_Blocked"),
    ],
)
def test_a_held_refusal_indication_outlives_the_button(
    registry: TemplateRegistry, template_id: str, timer_suffix: str
) -> None:
    """A TON cannot hold anything past its enabling condition.

    Both refusals are enabled by a momentary command — one of which the block
    itself clears on the scan it reads it — so a TON's .TT lasted a single scan
    and the operator, on an HMI polling every half second, saw nothing at all.
    A TOF holds .DN for its preset AFTER the rung goes false, which is what
    "held for the configured time" always claimed.
    """
    template = loaded(registry, template_id)

    timers = [
        element
        for element in template.generated_objects.iter_instructions()
        if element.operands and timer_suffix in element.operands[0]
    ]
    driving = [element for element in timers if element.kind is LogicElementKind.OUTPUT]
    assert len(driving) == 1
    assert driving[0].instruction == "TOF", (
        f"{template.source}: {timer_suffix} is a {driving[0].instruction}; a held "
        "indication driven from a momentary command needs a TOF"
    )

    reading = [element for element in timers if element.kind is LogicElementKind.CONDITION]
    assert reading, "nothing reads the hold timer"
    for element in reading:
        assert element.operands[0].endswith(".DN"), (
            f"{template.source}: the indication reads {element.operands[0]}; .TT on a TOF "
            "is true only while it is timing out, which inverts the meaning"
        )


def test_the_analog_defaults_call_no_part_of_the_calibrated_range_invalid(
    registry: TemplateRegistry,
) -> None:
    """The four range numbers have one relationship and the defaults must hold it.

    Shipping raw_over_range below raw_max declared the top of the transmitter's
    own range out of range: a genuinely full tank read invalid, _Sts_Valid
    dropped, and every interlock gated on it dropped with it.
    """
    analog = loaded(registry, "esco.analog.input.scaling")
    defaults = {
        name: analog.parameter(name).default  # type: ignore[union-attr]
        for name in ("raw_under_range", "raw_min", "raw_max", "raw_over_range")
    }
    assert all(value is not None for value in defaults.values())
    assert (
        defaults["raw_under_range"]
        <= defaults["raw_min"]
        < defaults["raw_max"]
        <= (defaults["raw_over_range"])
    ), f"the shipped defaults contradict each other: {defaults}"

    stated = [
        restriction for restriction in analog.restrictions if "raw_under_range" in restriction
    ]
    assert stated, "the unchecked parameter relationship is not stated in the restrictions"


def test_the_machine_layout_reads_safety_device_status_as_an_interlock(
    registry: TemplateRegistry,
) -> None:
    """A permissive gates starting. It cannot stop a conveyor that is running.

    The layout used to describe E-stop and guard status as being "for use as a
    machine permissive", which directed every project into the one
    classification guaranteed not to stop machinery — and the program was called
    Safety_Monitor, which invited exactly the reliance its restriction disclaims.
    """
    layout = loaded(registry, "esco.controller.compactlogix.machine")

    programs = {program.name for program in layout.generated_objects.programs}
    assert "Safety_Monitor" not in programs, (
        "a standard program that stops nothing must not be named as though it monitors safety"
    )
    assert "Safety_Device_Status" in programs

    for name in ("R_EStop_Status", "R_Guard_Status"):
        routine = next(item for item in layout.generated_objects.routines if item.name == name)
        assert "interlock" in routine.description.lower(), routine.description
        assert "_Ilk_Sources_OK" in routine.description, routine.description

    stated = [
        restriction
        for restriction in layout.restrictions
        if "never a permissive" in restriction.lower()
    ]
    assert stated, (
        "the layout must state that a condition which has to stop running equipment "
        f"is never a permissive. Restrictions were: {layout.restrictions}"
    )


def test_every_pattern_targets_a_routine_some_layout_creates(
    registry: TemplateRegistry,
) -> None:
    """A pattern whose routine no layout creates has nowhere to expand into."""
    created: set[str] = set()
    for layout_id in CONTROLLER_LAYOUT_IDS:
        created |= {
            routine.name for routine in loaded(registry, layout_id).generated_objects.routines
        }

    for template in registry.all():
        if template.category is TemplateCategory.CONTROLLER_PROJECT:
            continue
        for routine in template.generated_objects.routines:
            assert routine.name in created, (
                f"{template.source}: no controller layout creates {routine.name!r}"
            )


def test_both_layouts_create_the_routines_the_interlock_gate_needs(
    registry: TemplateRegistry,
) -> None:
    """The gate is where safety-device status becomes something that stops equipment.

    Telling a CompactLogix project to consume guard status as an interlock is
    only actionable if that layout has an interlock routine to consume it in.
    """
    gate = loaded(registry, "esco.permissive.interlock")
    needed = {rung.routine for rung in gate.generated_objects.rungs}

    for layout_id in CONTROLLER_LAYOUT_IDS:
        layout = loaded(registry, layout_id)
        created = {routine.name for routine in layout.generated_objects.routines}
        assert needed <= created, f"{layout.source} is missing {sorted(needed - created)}"


def test_the_layouts_declare_no_task_parameter_they_cannot_apply(
    registry: TemplateRegistry,
) -> None:
    """A parameter the template cannot apply is worse than no parameter.

    The task period and watchdog are literals in these layouts and the loader
    parses them as integers, so a placeholder would not even load. Offering them
    as parameters meant an engineer could state 20 ms, have it validated and
    recorded against the requirement as a reviewed decision, and be handed a
    controller running at 100 ms.
    """
    for layout_id in CONTROLLER_LAYOUT_IDS:
        layout = loaded(registry, layout_id)
        assert layout.generated_objects.tasks
        for parameter in layout.parameters:
            assert not re.search(r"watchdog|rate_ms|period", parameter.name), (
                f"{layout.source}: parameter {parameter.name!r} claims to set a task "
                "property this template hard-codes"
            )
        stated = [
            restriction for restriction in layout.restrictions if "watchdog" in restriction.lower()
        ]
        assert stated, f"{layout.source} does not say what it fixes the watchdog at"


def test_a_node_that_has_never_communicated_is_stale(registry: TemplateRegistry) -> None:
    """Driving the stale bit from the fault timer alone published zeros as data.

    For the whole of connection_timeout_ms after a power-up the timer has not
    reached its preset, so the bit read false — and an input image that has never
    been received reads, for anything wired normally-closed, as good news.
    """
    comms = loaded(registry, "esco.communication.loss")
    stale = "{node_tag}_Sts_Data_Stale"

    writers = [rung for rung in comms.generated_objects.rungs if stale in written_with(rung, "OTE")]
    assert len(writers) == 1
    assert "{node_tag}_Sts_Conn_Established" in examined_if_open(writers[0]), (
        "the stale bit must be true until the node has been heard from at least once"
    )


def test_the_energize_fail_state_is_released_on_recovery(registry: TemplateRegistry) -> None:
    """An OTL nothing unlatches left a node's outputs on after a network blip.

    The release has to be an edge: this routine is scanned last, so an unlatch
    written every scan would override whatever normally owns those outputs.
    """
    comms = loaded(registry, "esco.communication.loss")
    values = {"node_tag": "RIO_PANEL2", "fail_state": "energize"}
    rungs = comms.rungs_for(values)

    latching = [rung for rung in rungs if "{node_tag}_Out_All" in written_with(rung, "OTL")]
    releasing = [rung for rung in rungs if "{node_tag}_Out_All" in written_with(rung, "OTU")]
    assert latching, "the energize fail state does not energise anything"
    assert releasing, "nothing releases the energize fail state when the connection returns"
    assert any(element.instruction == "ONS" for element in releasing[0].iter_instructions()), (
        "the release must be a one-shot, or it fights normal control every scan"
    )

    for rung in comms.rungs_for({"node_tag": "RIO_PANEL2", "fail_state": "de_energize"}):
        assert "{node_tag}_Out_All" not in written_with(rung, "OTL"), (
            "the de-energize configuration must not latch outputs on"
        )


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------
def test_supports_answers_per_family(registry: TemplateRegistry) -> None:
    motor = registry.get("esco.motor.standard")
    assert motor is not None
    assert motor.supports(ControllerFamily.COMPACTLOGIX)
    assert not motor.supports(ControllerFamily.PLC5)


def test_by_category_returns_the_matching_templates(registry: TemplateRegistry) -> None:
    motors = registry.by_category(TemplateCategory.MOTOR_CONTROL)
    assert [template.id for template in motors] == ["esco.motor.standard"]

    layouts = registry.by_category(TemplateCategory.CONTROLLER_PROJECT)
    assert len(layouts) == 2


def test_for_family_filters_on_supported_families(registry: TemplateRegistry) -> None:
    compact = {template.id for template in registry.for_family(ControllerFamily.COMPACTLOGIX)}
    control = {template.id for template in registry.for_family(ControllerFamily.CONTROLLOGIX)}

    assert "esco.controller.compactlogix.machine" in compact
    assert "esco.controller.compactlogix.machine" not in control
    assert "esco.controller.controllogix.process" in control
    assert registry.for_family(ControllerFamily.PLC5) == []


def test_get_with_an_unknown_id_or_version_returns_none(registry: TemplateRegistry) -> None:
    assert registry.get("esco.nothing.here") is None
    assert registry.get("esco.motor.standard", "9.9.9") is None


def test_get_without_a_version_returns_the_newest(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    write_template(tmp_path, "v1.yaml", template_body)
    template_body["version"] = "1.10.0"
    write_template(tmp_path, "v1_10.yaml", template_body)
    template_body["version"] = "1.9.0"
    write_template(tmp_path, "v1_9.yaml", template_body)

    loaded = load_templates(tmp_path)

    assert loaded.load_errors == []
    assert loaded.versions("esco.test.pattern") == ["1.0.0", "1.9.0", "1.10.0"]
    newest = loaded.get("esco.test.pattern")
    assert newest is not None
    assert newest.version == "1.10.0", "versions must sort numerically, not as strings"
    pinned = loaded.get("esco.test.pattern", "1.0.0")
    assert pinned is not None
    assert pinned.version == "1.0.0"


def test_all_is_deterministically_ordered(registry: TemplateRegistry) -> None:
    ordering = [template.qualified_id() for template in registry.all()]
    assert ordering == [template.qualified_id() for template in load_templates().all()]
    assert ordering == sorted(ordering)


def test_get_registry_is_cached_and_template_for_reads_it() -> None:
    assert get_registry() is get_registry()
    motor = template_for("esco.motor.standard")
    assert motor is not None
    assert motor.id == "esco.motor.standard"
    assert template_for("esco.motor.standard", "9.9.9") is None
    assert template_for("esco.nothing.here") is None


def test_capability_rows_report_the_templates_and_any_errors(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("- [", encoding="utf-8")

    rows = load_templates(tmp_path).capability_rows()

    assert rows == [
        {
            "name": "template:bad.yaml",
            "available": False,
            "version": "",
            "detail": rows[0]["detail"],
        }
    ]
    assert rows[0]["detail"]


def test_the_source_path_is_relative_to_the_template_root(registry: TemplateRegistry) -> None:
    """Load errors and loaded templates must be reported in the same terms."""
    for template in registry.all():
        assert not Path(template.source).is_absolute()
        assert (default_template_root() / template.source).is_file()


# ---------------------------------------------------------------------------
# Canonicalisation: what the digest may and may not be moved by
# ---------------------------------------------------------------------------
def test_a_checksum_survives_a_change_the_loader_normalises_away(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """The digest answers "does this expand to the same thing", not "same bytes".

    Both edits here are invisible to the loaded template — ``_require_str``
    strips, and a key the schema does not name is discarded — so both expand to
    byte-identical logic. Moving the digest for either would report every
    approved expansion pinned to this template as drifted and force a re-review
    of control logic that did not change.
    """
    write_template(tmp_path, "pattern.yaml", template_body)
    before = load_templates(tmp_path).get("esco.test.pattern")
    assert before is not None

    template_body["display_name"] = "Test pattern "
    template_body["reviewed_by"] = "Jane"
    write_template(tmp_path, "pattern.yaml", template_body)
    after = load_templates(tmp_path).get("esco.test.pattern")

    assert after is not None
    assert after.display_name == before.display_name
    assert after.checksum == before.checksum


def test_the_same_pattern_under_two_filenames_has_one_checksum(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """The digest is over the pattern, not over where it was read from.

    ``settings.template_root`` is configurable, so the same approved pattern is
    read from the shipped tree on one machine and a customer root on another.
    """
    write_template(tmp_path, "one.yaml", template_body)
    first = load_templates(tmp_path).get("esco.test.pattern")

    (tmp_path / "one.yaml").unlink()
    nested = tmp_path / "vendor"
    nested.mkdir()
    write_template(nested, "two.yml", template_body)
    second = load_templates(tmp_path).get("esco.test.pattern")

    assert first is not None
    assert second is not None
    assert first.source != second.source
    assert first.checksum == second.checksum


def test_an_int_and_a_float_spelling_of_a_number_default_hash_alike(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """``default: 100`` and ``default: 100.0`` describe the same range."""
    parameters = template_body["parameters"]
    assert isinstance(parameters, list)
    parameters.append(
        {
            "name": "eu_max",
            "type": "number",
            "required": False,
            "default": 100,
            "description": "Engineering value at full scale",
        }
    )
    write_template(tmp_path, "pattern.yaml", template_body)
    as_int = load_templates(tmp_path).get("esco.test.pattern")

    parameters[-1]["default"] = 100.0
    write_template(tmp_path, "pattern.yaml", template_body)
    as_float = load_templates(tmp_path).get("esco.test.pattern")

    assert as_int is not None
    assert as_float is not None
    assert as_int.checksum == as_float.checksum

    declared = as_int.parameter("eu_max")
    assert declared is not None
    assert declared.default == 100.0
    assert isinstance(declared.default, float)


def test_a_value_that_cannot_be_canonically_serialised_is_a_load_error(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """A ``!!set`` default is refused rather than stringified.

    ``str()`` of a set renders in iteration order, which CPython randomises per
    process, so accepting one would mean the digest of an unedited file changed
    every time a worker restarted. The file is reported, by name, with the fix.
    """
    parameters = template_body["parameters"]
    assert isinstance(parameters, list)
    parameters.append(
        {
            "name": "keywords",
            "type": "string",
            "required": False,
            "default": {"motor", "starter", "vfd", "contactor", "overload"},
            "description": "Search keywords",
        }
    )
    (tmp_path / "pattern.yaml").write_text(
        yaml.safe_dump(template_body, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    registry = load_templates(tmp_path)

    assert len(registry) == 0
    assert len(registry.load_errors) == 1
    message = registry.load_errors[0].message
    assert "set" in message
    assert "canonical" in message


def test_a_date_and_the_string_that_spells_it_do_not_hash_alike() -> None:
    """The other direction of the same hole: distinct values must stay distinct.

    ``default=str`` made ``datetime.date(2024, 1, 1)`` and ``"2024-01-01"``
    serialise identically, so a real content change could leave the digest
    unchanged — which is the one thing a checksum is not allowed to do.
    """
    from datetime import date

    with pytest.raises(ValueError, match="canonical"):
        compute_checksum({"approved_on": date(2024, 1, 1)})

    assert compute_checksum({"approved_on": "2024-01-01"})


def test_compute_checksum_refuses_a_non_string_key() -> None:
    """JSON has only string keys, so ``1`` and ``"1"`` would collide."""
    with pytest.raises(ValueError, match="not a string"):
        compute_checksum({"limits": {1: "low"}})


# ---------------------------------------------------------------------------
# Discovery: the same tree must load the same way on Windows and on Linux
# ---------------------------------------------------------------------------
def test_an_upper_case_suffix_is_discovered(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """``rglob("*.yaml")`` matches ``.YAML`` on Windows and not on Linux.

    A template saved by a Windows editor as ``Motor_Standard.YAML`` would load on
    a developer's machine and never be *visited* in the Linux container — not a
    load error, which the registry reports carefully, but a pattern that is
    simply absent.
    """
    write_template(tmp_path, "Pattern.YAML", template_body)

    registry = load_templates(tmp_path)

    assert registry.load_errors == []
    assert registry.get("esco.test.pattern") is not None


def test_a_file_that_merely_starts_with_a_yaml_suffix_is_not_a_template(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """Matching is on the suffix, not on the name containing one."""
    write_template(tmp_path, "notes.yaml.bak", template_body)

    assert load_templates(tmp_path).load_errors == []
    assert len(load_templates(tmp_path)) == 0


def test_discovery_order_does_not_depend_on_the_platform_case_rules(
    tmp_path: Path, template_body: dict[str, object]
) -> None:
    """Two files, one template id: which one is reported as the duplicate is fixed.

    Comparing ``Path`` objects is case-folded on Windows and case-sensitive
    everywhere else, so sorting on them put ``a.yaml`` before ``B.yaml`` on one
    platform and after it on the other. The first file visited wins the id and
    the second becomes the load error, so the order decided which filename an
    engineer was sent to. ``B.yaml`` sorts first under POSIX rules, on every
    platform.
    """
    write_template(tmp_path, "B.yaml", template_body)
    write_template(tmp_path, "a.yaml", template_body)

    registry = load_templates(tmp_path)

    assert len(registry) == 1
    assert [error.path for error in registry.load_errors] == ["a.yaml"]


@pytest.mark.parametrize(
    ("field_path", "mutate"),
    [
        ("rung operand", lambda body: _set_operand(body, "{device_tag}_Cmd_Jog")),
        ("rung gate", lambda body: _gate_first_rung(body)),
        ("required tag suffix", lambda body: _set_suffix(body, "_Cmd_Go")),
        ("parameter default", lambda body: _add_parameter_default(body)),
        ("restriction", lambda body: body["restrictions"].append("Does not do X.")),  # type: ignore[union-attr]
        ("engineering notes", lambda body: body.__setitem__("engineering_notes", "Reworded.")),
        (
            "approval level",
            lambda body: body.__setitem__("approval_level", "senior_review_required"),
        ),
    ],
)
def test_every_part_of_the_loaded_template_moves_the_digest(
    tmp_path: Path,
    template_body: dict[str, object],
    field_path: str,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    """Hashing the loaded template must not have narrowed what counts as a change.

    Moving the digest off the raw YAML body onto the constructed template is only
    safe if everything that changes what the template *expands to* is still in
    it. Each case here changes the generated logic, the interface it needs, or
    the approval it demands.
    """
    write_template(tmp_path, "pattern.yaml", template_body)
    before = load_templates(tmp_path).get("esco.test.pattern")
    assert before is not None

    mutate(template_body)
    write_template(tmp_path, "pattern.yaml", template_body)
    registry = load_templates(tmp_path)
    after = registry.get("esco.test.pattern")

    assert registry.load_errors == [], registry.load_errors
    assert after is not None
    assert after.checksum != before.checksum, f"{field_path} did not move the digest"


def _rungs(body: dict[str, object]) -> list[dict[str, Any]]:
    objects = body["generated_objects"]
    assert isinstance(objects, dict)
    rungs = objects["rungs"]
    assert isinstance(rungs, list)
    return rungs


def _set_operand(body: dict[str, object], operand: str) -> None:
    _rungs(body)[0]["elements"][-1]["operand"] = operand


def _gate_first_rung(body: dict[str, object]) -> None:
    _rungs(body)[0]["when_parameter"] = "has_run_feedback"
    _rungs(body)[0]["when_value"] = False


def _set_suffix(body: dict[str, object], suffix: str) -> None:
    tags = body["required_tags"]
    assert isinstance(tags, list)
    tags[0]["suffix"] = suffix


def _add_parameter_default(body: dict[str, object]) -> None:
    parameters = body["parameters"]
    assert isinstance(parameters, list)
    parameters[1]["default"] = False
