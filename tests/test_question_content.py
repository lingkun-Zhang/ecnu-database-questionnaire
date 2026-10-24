"""Independent question content: domain boundaries and canonical request meaning.

These tests need no database. Requiredness and routing belong to a questionnaire
usage, so a reusable question may legitimately allow an empty answer.
"""

from copy import deepcopy

import pytest

from app.question_content import canonical_question_content, validate_question_content


OPTIONS = [{"id": "yes", "label": " 是 "}, {"id": "no", "label": "否"}]


def content(kind="number", config=None):
    return {"kind": kind, "title": " 保留题干空格 ", "config": {} if config is None else deepcopy(config)}


@pytest.mark.parametrize("value", [
    content("single", {"options": OPTIONS}),
    content("multi", {"options": OPTIONS}),
    content("multi", {"options": OPTIONS, "minSelected": 0, "maxSelected": 0}),
    content("text"),
    content("text", {"minLength": 0, "maxLength": 0}),
    content("text", {"minLength": 1, "maxLength": 10000}),
    content("number"),
    content("number", {"min": -9007199254740991, "max": 9007199254740991}),
    content("number", {"min": 0.1, "max": 0.2, "integerOnly": False}),
    content("number", {"min": -0.1, "max": 0.1, "integerOnly": True}),
])
def test_valid_content_boundaries_and_input_not_mutated(value):
    before = deepcopy(value)
    assert validate_question_content(value) == []
    normalized = canonical_question_content(value)
    assert value == before
    assert set(normalized) == {"kind", "title", "config"}
    assert normalized["title"] == before["title"]
    assert validate_question_content(normalized) == []
    if "options" in before["config"]:
        assert normalized["config"]["options"] == before["config"]["options"]


INVALID_CONTENT = [
    pytest.param(None, "content", id="not-object"),
    pytest.param({}, "content.kind", id="missing-kind"),
    pytest.param({"kind": "text", "config": {}}, "content.title", id="missing-title"),
    pytest.param({"kind": "text", "title": "题干"}, "content.config", id="missing-config"),
    pytest.param(content("unknown"), "content.kind", id="unknown-kind"),
    pytest.param({**content(), "title": " "}, "content.title", id="blank-title"),
    pytest.param({**content(), "title": "题" * 501}, "content.title", id="long-title"),
    pytest.param({**content(), "title": "\ud800"}, "content.title", id="invalid-unicode"),
    pytest.param(content("single"), "content.config.options", id="missing-options"),
    pytest.param(content("single", {"options": []}), "content.config.options", id="empty-options"),
    pytest.param(content("single", {"options": [{"id": f"o{i}", "label": "选项"} for i in range(51)]}),
                 "content.config.options", id="too-many-options"),
    pytest.param(content("single", {"options": [OPTIONS[0], OPTIONS[0]]}),
                 "content.config.options[1].id", id="duplicate-option"),
    pytest.param(content("single", {"options": [{"id": "END", "label": "结束"}]}),
                 "content.config.options[0].id", id="reserved-option-id"),
    pytest.param(content("single", {"options": [{"id": "bad id", "label": "选项"}]}),
                 "content.config.options[0].id", id="invalid-option-id"),
    pytest.param(content("single", {"options": [{"id": "yes", "label": " "}]}),
                 "content.config.options[0].label", id="blank-option-label"),
    pytest.param(content("single", {"options": [{"id": "yes", "label": "字" * 201}]}),
                 "content.config.options[0].label", id="long-option-label"),
    pytest.param(content("single", {"options": [{**OPTIONS[0], "score": 1}]}),
                 "content.config.options[0].score", id="unknown-option-field"),
    pytest.param(content("single", {"options": OPTIONS, "minSelected": 1}),
                 "content.config.minSelected", id="config-for-other-kind"),
    pytest.param(content("multi", {"options": OPTIONS, "minSelected": True}),
                 "content.config.minSelected", id="bool-choice-bound"),
    pytest.param(content("multi", {"options": OPTIONS, "minSelected": 1.0}),
                 "content.config.minSelected", id="float-choice-bound"),
    pytest.param(content("multi", {"options": OPTIONS, "minSelected": -1}),
                 "content.config.minSelected", id="negative-choice-bound"),
    pytest.param(content("multi", {"options": OPTIONS, "maxSelected": 3}),
                 "content.config.maxSelected", id="choice-bound-exceeds-options"),
    pytest.param(content("multi", {"options": OPTIONS, "minSelected": 2, "maxSelected": 1}),
                 "content.config", id="reversed-choice-range"),
    pytest.param(content("text", {"minLength": True}), "content.config.minLength", id="bool-text-bound"),
    pytest.param(content("text", {"minLength": 1.0}), "content.config.minLength", id="float-text-bound"),
    pytest.param(content("text", {"minLength": -1}), "content.config.minLength", id="negative-text-bound"),
    pytest.param(content("text", {"maxLength": 10001}), "content.config.maxLength", id="large-text-bound"),
    pytest.param(content("text", {"minLength": 2, "maxLength": 1}), "content.config", id="reversed-text-range"),
    pytest.param(content("number", {"min": True}), "content.config.min", id="bool-number-bound"),
    pytest.param(content("number", {"max": 9007199254740992}), "content.config.max", id="unsafe-number"),
    pytest.param(content("number", {"min": float("inf")}), "content.config.min", id="infinite-number"),
    pytest.param(content("number", {"max": float("nan")}), "content.config.max", id="nan-number"),
    pytest.param(content("number", {"min": 2, "max": 1}), "content.config", id="reversed-number-range"),
    pytest.param(content("number", {"integerOnly": None}), "content.config.integerOnly", id="null-integer-flag"),
    pytest.param(content("number", {"integerOnly": 1}), "content.config.integerOnly", id="integer-integer-flag"),
    pytest.param(content("number", {"min": 0.1, "max": 0.2, "integerOnly": True}),
                 "content.config", id="no-integer-in-range"),
    pytest.param(content("number", {"unknown": 1}), "content.config.unknown", id="unknown-config"),
]


@pytest.mark.parametrize("value,field", INVALID_CONTENT)
def test_invalid_content_has_actionable_content_paths(value, field):
    errors = validate_question_content(value)
    assert errors
    assert field in {error["field"] for error in errors}
    assert all(error["field"].startswith("content") for error in errors)


@pytest.mark.parametrize("field,value", [("id", "q1"), ("required", True),
                                        ("rules", []), ("defaultTarget", "END")])
def test_questionnaire_usage_fields_cannot_be_stored_as_content(field, value):
    errors = validate_question_content({**content(), field: value})
    assert any(error["field"] == f"content.{field}" for error in errors)


@pytest.mark.parametrize("kind,implicit,explicit", [
    ("multi", {"options": OPTIONS}, {"options": OPTIONS, "minSelected": None, "maxSelected": None}),
    ("text", {}, {"minLength": None, "maxLength": None}),
    ("number", {"min": 1, "max": 2}, {"min": 1.0, "max": 2.0, "integerOnly": False}),
    ("number", {}, {"min": None, "max": None, "integerOnly": False}),
])
def test_optional_bounds_and_equal_numbers_have_one_canonical_meaning(kind, implicit, explicit):
    left, right = content(kind, implicit), content(kind, explicit)
    assert validate_question_content(left) == validate_question_content(right) == []
    assert canonical_question_content(left) == canonical_question_content(right)


def test_canonicalization_keeps_option_order_labels_and_title():
    source = content("single", {"options": OPTIONS})
    forward = canonical_question_content(source)
    backward = canonical_question_content(content("single", {"options": list(reversed(OPTIONS))}))
    assert forward != backward
    assert forward["title"] == " 保留题干空格 "
    assert forward["config"]["options"][0]["label"] == " 是 "
