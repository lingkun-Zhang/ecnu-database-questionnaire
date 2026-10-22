"""The V4 questionnaire protocol and deterministic, database-free evaluator.

Only JSON-shaped built-in values are accepted.  Display order never determines
execution order; all routing uses the persisted entry and explicit targets.
"""

from __future__ import annotations

import json
import math
import re

MAX_QUESTIONS = 100
MAX_OPTIONS = 50
MAX_RULES = 20
MAX_CONDITION_DEPTH = 4
MAX_CONDITION_NODES = 100
MAX_TEXT_LENGTH = 10_000
MAX_DEFINITION_BYTES = 512 * 1024
MAX_ANSWER_BYTES = 1024 * 1024
MAX_NUMBER = 9_007_199_254_740_991
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MISSING = object()
_KINDS = {"single", "multi", "text", "number"}
_NUMBER_OPS = {"lt", "lte", "gt", "gte", "eq", "between"}
_MULTI_OPS = {"containsAny", "containsAll", "setEquals"}


def _error(code, message, question_id=None, rule_id=None, field=None):
    return {"code": code, "message": message, "questionId": question_id,
            "ruleId": rule_id, "field": field}


def _number(value):
    # BSON long values decode to an int subclass.  Accept numeric subclasses
    # without importing a database driver, but never accept bool as a number.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= MAX_NUMBER
            and (isinstance(value, int) or math.isfinite(value)))


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_id(value):
    return type(value) is str and value != "END" and bool(_ID.fullmatch(value))


def _unicode(value):
    return type(value) is str and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _json_size(value):
    # Non-finite values are rejected at their active numeric field.  Allowing
    # their representation here prevents an inactive cached value from affecting
    # routing.  HTTP parsing still rejects non-JSON requests independently.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="surrogatepass"))


def validate_definition(definition: dict) -> list[dict]:
    """Return blocking errors only; never mutate the supplied definition.

    IDs are unique across questions and across rules in a questionnaire. Option
    IDs are unique within their owning question, so common IDs such as ``yes``
    may be used by different questions. Missing optional config bounds mean
    null; omitted integerOnly means false. Other protocol fields are explicit.
    """
    errors = []

    def add(code, message, qid=None, rid=None, field=None):
        errors.append(_error(code, message, qid, rid, field))

    def shape(obj, fields, field, qid=None, rid=None, required=None):
        if type(obj) is not dict:
            add("invalid_type", "必须是对象", qid, rid, field)
            return False
        for key in obj:
            if key not in fields:
                add("unknown_field", "存在协议未定义的字段", qid, rid, f"{field}.{key}")
        for key in fields if required is None else required:
            if key not in obj:
                add("required_field", "缺少必要字段", qid, rid, f"{field}.{key}")
        return True

    def text(value, field, limit, qid=None, rid=None, empty=False):
        if not _unicode(value):
            add("invalid_type", "必须是有效 Unicode 字符串", qid, rid, field)
        elif len(value) > limit or (not empty and not value.strip()):
            add("text_limit", "文字为空或超出长度上限", qid, rid, field)

    if type(definition) is not dict:
        return [_error("invalid_definition", "问卷定义必须是对象", field="definition")]
    try:
        if _json_size(definition) > MAX_DEFINITION_BYTES:
            return [_error("definition_too_large", "问卷定义超过 512 KiB", field="definition")]
    except (TypeError, ValueError, RecursionError, OverflowError):
        return [_error("invalid_definition", "问卷定义必须是有限 JSON 结构", field="definition")]
    shape(definition, {"title", "description", "allowAnonymous", "entryQuestionId", "questions"}, "definition")
    text(definition.get("title"), "title", 200)
    text(definition.get("description"), "description", 5000, empty=True)
    if type(definition.get("allowAnonymous")) is not bool:
        add("invalid_type", "allowAnonymous 必须是布尔值", field="allowAnonymous")
    questions = definition.get("questions")
    if type(questions) is not list or not 1 <= len(questions) <= MAX_QUESTIONS:
        add("question_count", "问卷必须包含 1—100 道题", field="questions")
        return errors

    question_ids = set()
    rule_ids = set()
    graph = {}

    def condition(cond, kind, option_ids, qid, rid, field, depth, counter):
        counter[0] += 1
        if counter[0] > MAX_CONDITION_NODES:
            if counter[0] == MAX_CONDITION_NODES + 1:
                add("condition_size", "每条条件树最多 100 个节点", qid, rid, field)
            return
        if depth > MAX_CONDITION_DEPTH:
            add("condition_depth", "条件树深度不能超过 4", qid, rid, field)
            return
        if type(cond) is not dict:
            add("invalid_condition", "条件必须是对象", qid, rid, field)
            return
        groups = [key for key in ("allOf", "anyOf") if key in cond]
        if groups:
            key = groups[0]
            if set(cond) != {key}:
                add("invalid_condition", "组合条件只能采用一种语法且不得有额外字段", qid, rid, field)
            children = cond[key]
            if type(children) is not list or not children:
                add("invalid_condition", "组合条件至少包含一个子条件", qid, rid, field)
                return
            if len(children) > MAX_CONDITION_NODES:
                add("condition_size", "每条条件树最多 100 个节点", qid, rid, field)
                return
            for i, child in enumerate(children):
                condition(child, kind, option_ids, qid, rid, f"{field}.{key}[{i}]", depth + 1, counter)
            return
        shape(cond, {"operator", "value"}, field, qid, rid)
        op, value = cond.get("operator"), cond.get("value")
        allowed = {"eqOption"} if kind == "single" else _MULTI_OPS if kind == "multi" else _NUMBER_OPS if kind == "number" else set()
        if type(op) is not str or op not in allowed:
            add("condition_kind", "运算符不适用于当前题型", qid, rid, f"{field}.operator")
            return
        if kind == "single":
            if type(value) is not str or value not in option_ids:
                add("condition_option", "条件引用的选项不属于当前题", qid, rid, f"{field}.value")
        elif kind == "multi":
            if (type(value) is not list or not value or any(type(v) is not str for v in value)
                    or len(value) != len(set(value)) or not set(value) <= option_ids):
                add("condition_option", "比较集合必须非空、无重复且属于当前题", qid, rid, f"{field}.value")
        elif op == "between":
            if shape(value, {"min", "max"}, f"{field}.value", qid, rid):
                low, high = value.get("min"), value.get("max")
                if not _number(low) or not _number(high) or low > high:
                    add("condition_range", "条件区间必须是合法的闭区间", qid, rid, f"{field}.value")
        elif not _number(value):
            add("invalid_number", "比较值必须是有限安全数字", qid, rid, f"{field}.value")

    for index, question in enumerate(questions):
        base = f"questions[{index}]"
        if not shape(question, {"id", "kind", "title", "required", "config", "defaultTarget", "rules"}, base):
            continue
        qid, kind = question.get("id"), question.get("kind")
        if not _valid_id(qid):
            add("invalid_id", "题目 ID 必须是 1—64 位合法稳定 ID，且不能是 END", field=f"{base}.id")
            qid = None
        elif qid in question_ids:
            add("duplicate_question_id", "题目 ID 重复", qid, field=f"{base}.id")
        else:
            question_ids.add(qid)
        if type(kind) is not str or kind not in _KINDS:
            add("invalid_kind", "未知题型", qid, field=f"{base}.kind")
            kind = None
        text(question.get("title"), f"{base}.title", 500, qid)
        if type(question.get("required")) is not bool:
            add("invalid_type", "required 必须是布尔值", qid, field=f"{base}.required")
        config = question.get("config")
        config_fields = {"single": {"options"}, "multi": {"options", "minSelected", "maxSelected"},
                         "text": {"minLength", "maxLength"}, "number": {"min", "max", "integerOnly"}}
        option_ids = set()
        config_ok = shape(config, config_fields.get(kind, set()), f"{base}.config", qid,
                          required={"options"} if kind in ("single", "multi") else set())
        if config_ok and kind in ("single", "multi"):
            options = config.get("options")
            if type(options) is not list or not 1 <= len(options) <= MAX_OPTIONS:
                add("option_count", "选择题必须包含 1—50 个选项", qid, field=f"{base}.config.options")
                options = []
            for oi, option in enumerate(options):
                field = f"{base}.config.options[{oi}]"
                if not shape(option, {"id", "label"}, field, qid):
                    continue
                oid = option.get("id")
                if not _valid_id(oid):
                    add("invalid_id", "选项 ID 非法", qid, field=f"{field}.id")
                elif oid in option_ids:
                    add("duplicate_option_id", "当前题内选项 ID 重复", qid, field=f"{field}.id")
                else:
                    option_ids.add(oid)
                text(option.get("label"), f"{field}.label", 200, qid)
            if kind == "multi":
                low, high = config.get("minSelected"), config.get("maxSelected")
                for name, value in (("minSelected", low), ("maxSelected", high)):
                    if value is not None and (not _integer(value) or not 0 <= value <= len(options)):
                        add("choice_count", "选择数量必须是选项数量范围内的非负整数或 null", qid, field=f"{base}.config.{name}")
                if _integer(low) and _integer(high) and low > high:
                    add("bound_order", "最少选择数不能大于最多选择数", qid, field=f"{base}.config")
                if question.get("required") is True and high == 0:
                    add("impossible_constraint", "必答多选题至少应允许选择一项", qid, field=f"{base}.config.maxSelected")
        elif config_ok and kind == "text":
            low, high = config.get("minLength"), config.get("maxLength")
            for name, value in (("minLength", low), ("maxLength", high)):
                if value is not None and (not _integer(value) or not 0 <= value <= MAX_TEXT_LENGTH):
                    add("text_limit", "文本长度边界须为 0—10000 整数或 null", qid, field=f"{base}.config.{name}")
            if _integer(low) and _integer(high) and low > high:
                add("bound_order", "最短文本长度不能大于最长长度", qid, field=f"{base}.config")
            if question.get("required") is True and high == 0:
                add("impossible_constraint", "必答文本题应允许非空答案", qid, field=f"{base}.config.maxLength")
        elif config_ok and kind == "number":
            low, high = config.get("min"), config.get("max")
            for name, value in (("min", low), ("max", high)):
                if value is not None and not _number(value):
                    add("invalid_number", "数字边界必须是有限安全数字或 null", qid, field=f"{base}.config.{name}")
            if type(config.get("integerOnly", False)) is not bool:
                add("invalid_type", "integerOnly 必须是布尔值", qid, field=f"{base}.config.integerOnly")
            if _number(low) and _number(high):
                if low > high:
                    add("bound_order", "数字下界不能大于上界", qid, field=f"{base}.config")
                elif config.get("integerOnly") is True and math.ceil(low) > math.floor(high):
                    add("impossible_constraint", "该数值范围不包含任何整数", qid, field=f"{base}.config")
        edges = [(question.get("defaultTarget"), None, f"{base}.defaultTarget")]
        rules = question.get("rules")
        if type(rules) is not list or len(rules) > MAX_RULES:
            add("rule_count", "rules 必须是最多 20 条规则的数组", qid, field=f"{base}.rules")
            rules = []
        priorities = set()
        for ri, rule in enumerate(rules):
            field = f"{base}.rules[{ri}]"
            if not shape(rule, {"id", "priority", "condition", "target"}, field, qid):
                continue
            rid = rule.get("id")
            if not _valid_id(rid):
                add("invalid_id", "规则 ID 非法", qid, field=f"{field}.id")
                rid = None
            elif rid in rule_ids:
                add("duplicate_rule_id", "整卷规则 ID 重复", qid, rid, f"{field}.id")
            else:
                rule_ids.add(rid)
            priority = rule.get("priority")
            if not _integer(priority) or abs(priority) > MAX_NUMBER:
                add("invalid_priority", "优先级必须是安全整数", qid, rid, f"{field}.priority")
            elif priority in priorities:
                add("duplicate_priority", "同题规则优先级不能重复", qid, rid, f"{field}.priority")
            else:
                priorities.add(priority)
            condition(rule.get("condition"), kind, option_ids, qid, rid, f"{field}.condition", 1, [0])
            edges.append((rule.get("target"), rid, f"{field}.target"))
        if qid is not None:
            graph[qid] = edges

    entry = definition.get("entryQuestionId")
    if type(entry) is not str or entry not in question_ids:
        add("invalid_entry", "入口必须指向一道卷内有效题目", field="entryQuestionId")
    for qid, edges in graph.items():
        for target, rid, field in edges:
            if type(target) is not str or (target != "END" and target not in question_ids):
                add("invalid_target", "默认或条件目标无效", qid, rid, field)
    colors = {}

    def visit(qid):
        colors[qid] = 1
        for target, rid, field in graph[qid]:
            if type(target) is not str or target not in graph:
                continue
            if colors.get(target) == 1:
                add("flow_cycle", "默认边和条件边组成的完整图存在循环", qid, rid, field)
            elif colors.get(target, 0) == 0:
                visit(target)
        colors[qid] = 2

    for qid in graph:
        if colors.get(qid, 0) == 0:
            visit(qid)
    return errors


def _normalize(question, raw):
    """Return (status, canonical value, error code/message or None)."""
    kind, config = question["kind"], question["config"]
    if raw is _MISSING or raw is None:
        return "missing", None, None
    if kind == "single":
        if type(raw) is not str or raw not in {o["id"] for o in config["options"]}:
            return "invalid", None, ("invalid_option", "必须选择当前题的一个合法选项")
        value = raw
    elif kind == "multi":
        if type(raw) is not list or any(type(v) is not str for v in raw):
            return "invalid", None, ("invalid_type", "多选答案必须是选项 ID 数组")
        if len(raw) != len(set(raw)):
            return "invalid", None, ("duplicate_option", "多选答案不能包含重复选项")
        if not set(raw) <= {o["id"] for o in config["options"]}:
            return "invalid", None, ("invalid_option", "多选答案包含不属于当前题的选项")
        if not raw:
            return "missing", None, None
        low, high = config.get("minSelected"), config.get("maxSelected")
        if (low is not None and len(raw) < low) or (high is not None and len(raw) > high):
            return "invalid", None, ("choice_count", "所选数量不符合题目要求")
        value = sorted(raw)
    elif kind == "text":
        if not _unicode(raw):
            return "invalid", None, ("invalid_type", "文本答案必须是有效 Unicode 字符串")
        value = raw.strip()
        if not value:
            return "missing", None, None
        low, high = config.get("minLength"), config.get("maxLength")
        if len(value) > MAX_TEXT_LENGTH or (low is not None and len(value) < low) or (high is not None and len(value) > high):
            return "invalid", None, ("text_length", "文本长度不符合题目要求")
    else:
        if not _number(raw):
            return "invalid", None, ("invalid_number", "答案必须是有限安全数字，不能是字符串或布尔值")
        if config.get("integerOnly", False) and isinstance(raw, float) and not raw.is_integer():
            return "invalid", None, ("integer_required", "该题要求整数")
        if (config.get("min") is not None and raw < config["min"]) or (config.get("max") is not None and raw > config["max"]):
            return "invalid", None, ("number_range", "数字超出允许范围")
        value = int(raw) if isinstance(raw, int) or raw.is_integer() else raw
    return "answered", value, None


def _matches(condition, answer):
    if "allOf" in condition:
        return all(_matches(child, answer) for child in condition["allOf"])
    if "anyOf" in condition:
        return any(_matches(child, answer) for child in condition["anyOf"])
    op, value = condition["operator"], condition["value"]
    if op in ("eqOption", "eq"):
        return answer == value
    if op in _MULTI_OPS:
        actual, wanted = set(answer), set(value)
        return bool(actual & wanted) if op == "containsAny" else wanted <= actual if op == "containsAll" else actual == wanted
    if op == "between":
        return value["min"] <= answer <= value["max"]
    if op == "lt":
        return answer < value
    if op == "lte":
        return answer <= value
    if op == "gt":
        return answer > value
    return answer >= value


def evaluate(definition: dict, rawAnswers: dict, mode: str = "final") -> dict:
    """Evaluate one snapshot; only reached questions read their raw values.

    ``complete`` means END was reached, not that submission is valid. A final
    submission requires complete=True, errors=[], and provisional=False.
    Interactive blocking returns only the determined prefix and its current
    error. Final errors use default edges for a provisional diagnostic path.
    """
    result = {"path": [], "activeAnswers": {}, "trace": [], "errors": [],
              "blockedAt": None, "complete": False, "provisional": False}
    if mode not in ("interactive", "final"):
        result["errors"] = [_error("invalid_mode", "mode 必须是 interactive 或 final", field="mode")]
        return result
    definition_errors = validate_definition(definition)
    if definition_errors:
        result["errors"] = definition_errors
        result["provisional"] = mode == "final"
        return result
    if type(rawAnswers) is not dict:
        result["errors"] = [_error("invalid_answers", "答案必须是题目 ID 到值的对象", field="answers")]
    else:
        try:
            if _json_size(rawAnswers) > MAX_ANSWER_BYTES:
                result["errors"].append(_error("answers_too_large", "答案超过 1 MiB", field="answers"))
        except (TypeError, ValueError, RecursionError, OverflowError):
            result["errors"].append(_error("invalid_answers", "答案必须是 JSON 结构", field="answers"))
    if result["errors"]:
        result["provisional"] = mode == "final"
        return result
    questions = {q["id"]: q for q in definition["questions"]}
    for qid in rawAnswers:
        if type(qid) is not str or qid not in questions:
            result["errors"].append(_error("unknown_question", "答案包含未知题目 ID", field=f"answers.{qid}"))
    if result["errors"]:
        result["provisional"] = mode == "final"
        return result
    current = definition["entryQuestionId"]
    visited = set()
    while current != "END":
        if current in visited or len(visited) >= MAX_QUESTIONS or current not in questions:
            result["errors"].append(_error("flow_execution", "执行路径异常或超过步数上限", current, field="path"))
            result["provisional"] = True
            return result
        visited.add(current)
        result["path"].append(current)
        question = questions[current]
        status, value, failure = _normalize(question, rawAnswers.get(current, _MISSING))
        if status == "missing" and question["required"]:
            failure = ("required", "请回答此必答题")
        step = {"questionId": current, "status": status, "answer": value,
                "ruleResults": [], "matchedRuleId": None, "target": None,
                "reason": "", "provisional": result["provisional"]}
        if failure:
            result["errors"].append(_error(failure[0], failure[1], current, field=f"answers.{current}"))
            if mode == "interactive":
                result["blockedAt"] = current
                step["reason"] = failure[1] + "；后续路径尚未确定"
                result["trace"].append(step)
                return result
            result["provisional"] = True
            step["provisional"] = True
            step["reason"] = failure[1] + "；仅沿默认边收集错误，不可提交"
        elif status == "answered":
            result["activeAnswers"][current] = value
            for rule in sorted(question["rules"], key=lambda rule: rule["priority"]):
                matched = _matches(rule["condition"], value)
                step["ruleResults"].append({"ruleId": rule["id"], "priority": rule["priority"],
                                            "matched": matched,
                                            "reason": "条件成立" if matched else "条件不成立"})
                if matched:
                    step["matchedRuleId"] = rule["id"]
                    step["target"] = rule["target"]
                    step["reason"] = f"按优先级命中规则 {rule['id']}"
                    break
            if step["target"] is None:
                step["reason"] = "没有条件命中，采用默认去向"
        else:
            step["reason"] = "选填题未答，不执行条件，采用默认去向"
        if step["target"] is None:
            step["target"] = question["defaultTarget"]
        result["trace"].append(step)
        current = step["target"]
    result["complete"] = True
    return result
