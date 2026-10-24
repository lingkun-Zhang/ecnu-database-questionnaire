"""Reusable question content; questionnaire-local routing stays in domain.py."""

from copy import deepcopy

from .domain import validate_definition


def validate_question_content(content):
    """Validate the original object before projecting any of its fields.

    Use the existing four-kind protocol with a neutral, optional question. The
    enclosing definition is only a validation adapter, never persisted.
    """
    if type(content) is not dict:
        return [{"code": "invalid_type", "message": "题目内容必须是对象",
                 "questionId": None, "ruleId": None, "field": "content"}]
    fields = {"kind", "title", "config"}
    errors = []
    for key in content.keys() - fields:
        errors.append({"code": "unknown_field", "message": "题目内容包含未定义的字段",
                       "questionId": None, "ruleId": None, "field": f"content.{key}"})
    for key in fields - content.keys():
        errors.append({"code": "required_field", "message": "题目内容缺少必要字段",
                       "questionId": None, "ruleId": None, "field": f"content.{key}"})
    if errors:
        return errors
    definition = {
        "title": "题目内容校验", "description": "", "allowAnonymous": False,
        "entryQuestionId": "question",
        "questions": [{"id": "question", **content, "required": False,
                       "defaultTarget": "END", "rules": []}],
    }
    for error in validate_definition(definition):
        field = error["field"] or "content"
        if field == "definition":
            field = "content"
        elif field.startswith("questions[0]"):
            field = "content" + field[len("questions[0]"):]
        errors.append({**error, "questionId": None, "ruleId": None, "field": field})
    return errors


def canonical_question_content(content):
    """Copy validated content and make equivalent optional config explicit.

    Preserve wording, option IDs and option order. Call validation first; this
    function must not turn invalid input into an accepted default.
    """
    result = deepcopy(content)
    config = result["config"]
    bounds = {"single": (), "multi": ("minSelected", "maxSelected"),
              "text": ("minLength", "maxLength"), "number": ("min", "max")}
    for key in bounds[result["kind"]]:
        config.setdefault(key, None)
    if result["kind"] == "number":
        config.setdefault("integerOnly", False)
        for key in ("min", "max"):
            value = config[key]
            if value is not None and float(value).is_integer():
                config[key] = int(value)
    return result
