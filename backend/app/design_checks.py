"""Conservative, non-blocking checks for a valid questionnaire definition.

No examples are sampled as evidence. Single-choice sets are finite and exact;
numeric sets use intervals over the evaluator's representable, safe numbers.
Multi-choice checks deliberately stop at equivalent condition-tree syntax.
The protocol's limits (100 questions, 20 rules, 100 condition nodes) also bound
the work and output of this analyzer. It never changes the supplied definition.
"""

from __future__ import annotations

import math

from .domain import MAX_NUMBER, _matches, validate_definition


def _warning(code, message, question_id, field, rule_id=None, related=None):
    result = {"code": code, "message": message, "questionId": question_id,
              "ruleId": rule_id, "field": field, "relatedRuleIds": related or []}
    return result


def _canonical(condition):
    """Normalize only identities whose semantics do not depend on order."""
    for group in ("allOf", "anyOf"):
        if group in condition:
            children = {_canonical(child) for child in condition[group]}
            return group, tuple(sorted(children, key=repr))
    op, value = condition["operator"], condition["value"]
    if op in ("containsAny", "containsAll", "setEquals"):
        return op, tuple(sorted(value))
    if op == "between":
        return op, float(value["min"] or 0).hex(), float(value["max"] or 0).hex()
    if op == "eqOption":
        return op, value
    # Every allowed integer is exactly representable as a float. This also
    # normalizes 1/1.0 and -0.0/0 without changing comparison semantics.
    return op, float(value or 0).hex()


class _Intervals:
    """Closed sets of admissible numbers, with exact open-end conversion.

    All accepted integers are within +/- (2**53 - 1), and are representable as
    binary64 floats. Thus non-integer-only answers have precisely the finite
    binary64 lattice in this safe range; nextafter preserves strict endpoints
    and detects gaps between adjacent floating-point values correctly.
    """

    def __init__(self, config):
        self.integer = config.get("integerOnly", False)
        low = config.get("min")
        high = config.get("max")
        low = -MAX_NUMBER if low is None else low
        high = MAX_NUMBER if high is None else high
        self.domain = [(math.ceil(low), math.floor(high))] if self.integer else [(float(low), float(high))]

    def next(self, value):
        return value + 1 if self.integer else math.nextafter(value, math.inf)

    def previous(self, value):
        return value - 1 if self.integer else math.nextafter(value, -math.inf)

    def union(self, intervals):
        merged = []
        for low, high in sorted(intervals):
            if low > high:
                continue
            if merged and low <= self.next(merged[-1][1]):
                merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
            else:
                merged.append((low, high))
        return merged

    def intersection(self, left, right):
        result, li, ri = [], 0, 0
        while li < len(left) and ri < len(right):
            low = max(left[li][0], right[ri][0])
            high = min(left[li][1], right[ri][1])
            if low <= high:
                result.append((low, high))
            if left[li][1] < right[ri][1]:
                li += 1
            else:
                ri += 1
        return self.union(result)

    def difference(self, left, right):
        """Subtract normalized intervals without inventing boundary values."""
        result = []
        for low, high in left:
            current = low
            for other_low, other_high in right:
                if other_high < current:
                    continue
                if other_low > high:
                    break
                if other_low > current:
                    result.append((current, self.previous(other_low)))
                if other_high >= high:
                    current = None
                    break
                current = self.next(other_high)
            if current is not None and current <= high:
                result.append((current, high))
        return self.union(result)

    def matching(self, condition):
        for group in ("allOf", "anyOf"):
            if group in condition:
                children = [self.matching(child) for child in condition[group]]
                if group == "anyOf":
                    return self.union([interval for child in children for interval in child])
                result = self.domain
                for child in children:
                    result = self.intersection(result, child)
                return result
        op, value = condition["operator"], condition["value"]
        if op == "between":
            low, high = value["min"], value["max"]
            interval = [(math.ceil(low), math.floor(high))] if self.integer else [(float(low), float(high))]
        elif op == "eq":
            interval = [] if self.integer and not float(value).is_integer() else [(value, value)]
        elif op in ("lt", "lte"):
            if self.integer:
                high = math.ceil(value) - 1 if op == "lt" else math.floor(value)
            else:
                high = math.nextafter(float(value), -math.inf) if op == "lt" else float(value)
            interval = [(-MAX_NUMBER, high)]
        else:
            if self.integer:
                low = math.floor(value) + 1 if op == "gt" else math.ceil(value)
            else:
                low = math.nextafter(float(value), math.inf) if op == "gt" else float(value)
            interval = [(low, MAX_NUMBER)]
        return self.intersection(self.union(interval), self.domain)


def _reachable(definition):
    questions = {question["id"]: question for question in definition["questions"]}
    reached, pending = set(), [definition["entryQuestionId"]]
    while pending:
        qid = pending.pop()
        if qid == "END" or qid in reached:
            continue
        reached.add(qid)
        question = questions[qid]
        pending.append(question["defaultTarget"])
        pending.extend(rule["target"] for rule in question["rules"])
    return reached


def analyze_definition(definition) -> list[dict]:
    """Return proven design advisories, never blocking validation errors.

    Reachability uses the complete *structural* graph, including default edges
    and every conditional edge. Coverage only concerns answered, valid values:
    optional missing answers still follow the evaluator's default edge. A
    later rule gets at most one pair/coverage warning, in the order duplicate,
    complete shadowing, partial overlap. Empty numeric/single sets instead get
    one unsatisfiable warning. Related rule IDs follow priority order.
    """
    if validate_definition(definition):
        return []
    warnings = []
    reached = _reachable(definition)
    for qi, question in enumerate(definition["questions"]):
        qid, kind = question["id"], question["kind"]
        if qid not in reached:
            warnings.append(_warning(
                "unreachable_question", "从入口沿默认去向和条件去向均无法到达此题，请检查是否为遗留题目。",
                qid, f"questions[{qi}].title"))
        numeric = _Intervals(question["config"]) if kind == "number" else None
        seen, previous_effective = {}, []
        covered = [] if numeric else set()
        ordered_rules = sorted(enumerate(question["rules"]), key=lambda item: item[1]["priority"])
        for ri, rule in ordered_rules:
            rid = rule["id"]
            field = f"questions[{qi}].rules[{ri}].condition"
            canonical = _canonical(rule["condition"])
            matching = None
            if numeric:
                matching = numeric.matching(rule["condition"])
            elif kind == "single":
                matching = {option["id"] for option in question["config"]["options"]
                            if _matches(rule["condition"], option["id"])}
            if matching is not None and not matching:
                warnings.append(_warning(
                    "unsatisfiable_rule", "在本题允许的有效答案范围内，此条件不可能成立。",
                    qid, field, rid))
                seen.setdefault(canonical, rid)
                continue
            if canonical in seen:
                warnings.append(_warning(
                    "duplicate_rule_condition", "条件与较高优先级规则重复；按首次命中规则执行，请检查是否需要合并。",
                    qid, field, rid, [seen[canonical]]))
            elif matching is not None:
                if numeric:
                    remaining = numeric.difference(matching, covered)
                    related = [other_id for other_id, values in previous_effective
                               if numeric.intersection(matching, values)]
                else:
                    remaining = matching - covered
                    related = [other_id for other_id, values in previous_effective if matching & values]
                if related:
                    code = "shadowed_rule" if not remaining else "overlapping_rules"
                    message = ("条件的全部有效答案已被较高优先级规则覆盖，此规则不会被命中。"
                               if not remaining else
                               "条件与较高优先级规则部分重叠；重叠答案采用先匹配规则，请核对优先级和去向。")
                    warnings.append(_warning(code, message, qid, field, rid, related))
            seen.setdefault(canonical, rid)
            if matching is not None:
                if numeric:
                    effective = numeric.difference(matching, covered)
                    covered = numeric.union(covered + matching)
                else:
                    effective = matching - covered
                    covered = covered | matching
                if effective:
                    previous_effective.append((rid, effective))
    return warnings
