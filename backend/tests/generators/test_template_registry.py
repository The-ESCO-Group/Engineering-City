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

import re
from pathlib import Path

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
