"""Static design checks use exact sets; no HTTP server or database is needed."""

import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.design_checks import analyze_definition
from app.domain import MAX_NUMBER, _matches, evaluate, validate_definition


def leaf(op, value):
    return {"operator": op, "value": value}


def rule(rid, condition, priority, target="END"):
    return {"id": rid, "priority": priority, "condition": condition, "target": target}


def question(qid="q_gate", kind="number", config=None, rules=None, target="END", required=False):
    if config is None:
        config = {"min": 0, "max": 100, "integerOnly": True}
    return {"id": qid, "kind": kind, "config": config, "rules": rules or [],
            "title": qid, "defaultTarget": target, "required": required}


def definition(rules=None, config=None, kind="number", rest=None, required=False):
    return {"title": "设计提示测试", "description": "", "allowAnonymous": True,
            "entryQuestionId": "q_gate", "questions": [question(kind=kind, config=config,
            rules=rules, required=required)] + (rest or [])}


class DesignChecksTests(unittest.TestCase):
    def checks(self, value):
        self.assertEqual(validate_definition(value), [], "test input must be a valid definition")
        return analyze_definition(value)

    def rule_checks(self, value):
        return [item for item in self.checks(value) if item["ruleId"] is not None]

    def test_clean_definition_has_no_advisories(self):
        self.assertEqual(self.checks(definition([rule("r_low", leaf("lt", 50), 10),
                                              rule("r_high", leaf("gte", 50), 20)])), [])

    def test_invalid_shapes_graph_and_protocol_limits_return_no_warnings(self):
        bad = [None, {}, [], {"questions": []}]
        duplicate_priority = definition([rule("r_a", leaf("lt", 10), 10), rule("r_b", leaf("lt", 10), 10)])
        bad.append(duplicate_priority)
        cycle = definition()
        cycle["questions"][0]["defaultTarget"] = "q_gate"
        bad.append(cycle)
        bad.append(definition([rule(f"r_{i}", leaf("lt", 10), i) for i in range(21)]))
        deep = leaf("lt", 10)
        for _ in range(4):
            deep = {"allOf": [deep]}
        bad.append(definition([rule("r_deep", deep, 1)]))
        bad.append(definition([rule("r_large", {"anyOf": [leaf("eq", 1)] * 100}, 1)]))
        for value in bad:
            with self.subTest(value=value):
                self.assertTrue(validate_definition(value))
                self.assertEqual(analyze_definition(value), [])

    def test_complete_graph_uses_all_defaults_and_condition_edges(self):
        value = definition([rule("r_branch", leaf("lt", 10), 10, "q_branch")], rest=[
            question("q_default", "text", {}, target="q_tail"),
            question("q_branch", "text", {}, target="q_tail"),
            question("q_tail", "text", {}), question("q_orphan", "text", {})])
        value["questions"][0]["defaultTarget"] = "q_default"
        checks = self.checks(value)
        self.assertEqual([(item["code"], item["questionId"], item["field"]) for item in checks],
                         [("unreachable_question", "q_orphan", "questions[4].title")])

    def test_structural_reachability_does_not_guess_branch_satisfiability(self):
        value = definition([rule("r_never", leaf("gt", 100), 1, "q_branch")],
                           rest=[question("q_branch", "text", {})])
        checks = self.checks(value)
        self.assertEqual([item["code"] for item in checks], ["unsatisfiable_rule"])

    def test_priority_not_array_order_and_original_field_indices(self):
        value = definition([rule("r_later", leaf("lt", 40), 100), rule("r_first", leaf("lt", 50), -10)])
        checks = self.rule_checks(value)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["code"], "shadowed_rule")
        self.assertEqual(checks[0]["ruleId"], "r_later")
        self.assertEqual(checks[0]["field"], "questions[0].rules[0].condition")
        self.assertEqual(checks[0]["relatedRuleIds"], ["r_first"])

    def test_duplicate_condition_different_or_same_targets_one_advisory(self):
        for target in ("END", "q_target"):
            with self.subTest(target=target):
                checks = self.rule_checks(definition([
                    rule("r_a", leaf("lt", 50), 10), rule("r_b", leaf("lt", 50), 20, target)],
                    rest=[question("q_target", "text", {})]))
                self.assertEqual([item["code"] for item in checks], ["duplicate_rule_condition"])
                self.assertEqual(checks[0]["relatedRuleIds"], ["r_a"])

    def test_equivalent_tree_child_key_and_numeric_order_normalized(self):
        first = {"allOf": [leaf("gte", 0), {"anyOf": [leaf("lt", 10), leaf("eq", 20)]}]}
        second = {"allOf": [{"anyOf": [{"value": 20.0, "operator": "eq"}, leaf("lt", 10.0)]}, leaf("gte", -0.0)]}
        checks = self.rule_checks(definition([rule("r_a", first, 1), rule("r_b", second, 2)]))
        self.assertEqual([item["code"] for item in checks], ["duplicate_rule_condition"])

    def test_between_object_key_and_signed_zero_normalized(self):
        checks = self.rule_checks(definition([
            rule("r_a", leaf("between", {"min": 0, "max": 10}), 1),
            rule("r_b", leaf("between", {"max": 10.0, "min": -0.0}), 2)]))
        self.assertEqual([item["code"] for item in checks], ["duplicate_rule_condition"])

    def test_multi_option_and_child_order_duplicate_but_no_speculative_overlap(self):
        config = {"options": [{"id": value, "label": value} for value in ("a", "b", "c")],
                  "minSelected": 1, "maxSelected": 2}
        first = {"allOf": [leaf("containsAny", ["b", "a"]), leaf("containsAll", ["a"])]}
        second = {"allOf": [leaf("containsAll", ["a"]), leaf("containsAny", ["a", "b"])]}
        checks = self.rule_checks(definition([
            rule("r_a", first, 1), rule("r_b", second, 2),
            rule("r_c", leaf("containsAny", ["a"]), 3),
            rule("r_d", leaf("setEquals", ["a", "b", "c"]), 4)], config=config, kind="multi"))
        self.assertEqual([item["code"] for item in checks], ["duplicate_rule_condition"])

    def test_single_choice_combination_is_exact_finite_set(self):
        config = {"options": [{"id": value, "label": value} for value in ("a", "b", "c")]}
        checks = self.rule_checks(definition([
            rule("r_ab", {"anyOf": [leaf("eqOption", "a"), leaf("eqOption", "b")]}, 1),
            rule("r_a", leaf("eqOption", "a"), 2),
            rule("r_bc", {"anyOf": [leaf("eqOption", "b"), leaf("eqOption", "c")]}, 3),
            rule("r_none", {"allOf": [leaf("eqOption", "a"), leaf("eqOption", "b")]}, 4)],
            config=config, kind="single"))
        self.assertEqual([(item["ruleId"], item["code"]) for item in checks],
                         [("r_a", "shadowed_rule"), ("r_bc", "overlapping_rules"),
                          ("r_none", "unsatisfiable_rule")])

    def test_union_of_earlier_rules_can_fully_shadow(self):
        checks = self.rule_checks(definition([
            rule("r_low", leaf("lte", 4), 1), rule("r_high", leaf("gte", 5), 2),
            rule("r_all", leaf("between", {"min": 0, "max": 100}), 3)]))
        self.assertEqual([item["code"] for item in checks], ["shadowed_rule"])
        self.assertEqual(checks[0]["relatedRuleIds"], ["r_low", "r_high"])

    def test_only_effective_earlier_rules_are_related(self):
        checks = self.rule_checks(definition([
            rule("r_first", leaf("lt", 50), 1), rule("r_hidden", leaf("lt", 40), 2),
            rule("r_overlap", leaf("gte", 30), 3)]))
        self.assertEqual(checks[-1]["code"], "overlapping_rules")
        self.assertEqual(checks[-1]["relatedRuleIds"], ["r_first"])

    def test_partial_overlap_same_target_is_still_useful_advice(self):
        checks = self.rule_checks(definition([
            rule("r_low", leaf("lte", 50), 1), rule("r_high", leaf("gte", 50), 2)]))
        self.assertEqual([item["code"] for item in checks], ["overlapping_rules"])

    def test_strict_inclusive_endpoint_table(self):
        cases = [("lt", "gte", None), ("lt", "gt", None),
                 ("lte", "gt", None), ("lte", "gte", "overlapping_rules")]
        for integer in (False, True):
            for first, second, expected in cases:
                with self.subTest(integer=integer, first=first, second=second):
                    checks = self.rule_checks(definition([
                        rule("r_a", leaf(first, 50), 1), rule("r_b", leaf(second, 50), 2)],
                        config={"min": 0, "max": 100, "integerOnly": integer}))
                    self.assertEqual([item["code"] for item in checks], [] if expected is None else [expected])

    def test_numeric_config_domain_clips_rules_and_proves_empty_sets(self):
        checks = self.rule_checks(definition([
            rule("r_before", leaf("lt", 2), 1), rule("r_cover", leaf("gte", 1), 2),
            rule("r_inside", leaf("between", {"min": 2, "max": 3}), 3),
            rule("r_after", leaf("gt", 3), 4)], config={"min": 2, "max": 3}))
        self.assertEqual([(item["ruleId"], item["code"]) for item in checks],
                         [("r_before", "unsatisfiable_rule"), ("r_inside", "shadowed_rule"),
                          ("r_after", "unsatisfiable_rule")])

    def test_integer_domain_rejects_decimal_equals_and_empty_between(self):
        checks = self.rule_checks(definition([
            rule("r_decimal", leaf("eq", 1.5), 1),
            rule("r_gap", leaf("between", {"min": 1.1, "max": 1.9}), 2),
            rule("r_lt", leaf("lt", 2.1), 3), rule("r_eq", leaf("eq", 2), 4)],
            config={"min": 1.1, "max": 3.9, "integerOnly": True}))
        self.assertEqual([item["code"] for item in checks],
                         ["unsatisfiable_rule", "unsatisfiable_rule", "shadowed_rule"])

    def test_decimal_answers_preserved_when_integer_only_false(self):
        checks = self.rule_checks(definition([
            rule("r_lt", leaf("lt", 2), 1), rule("r_gap", leaf("between", {"min": 1.1, "max": 1.9}), 2)],
            config={"min": 1, "max": 3, "integerOnly": False}))
        self.assertEqual([item["code"] for item in checks], ["shadowed_rule"])

    def test_float_adjacent_open_interval_is_empty_not_false_overlap(self):
        above_one = math.nextafter(1.0, math.inf)
        checks = self.rule_checks(definition([
            rule("r_gap", {"allOf": [leaf("gt", 1), leaf("lt", above_one)]}, 1),
            rule("r_one", leaf("lte", 1), 2), rule("r_next", leaf("gte", above_one), 3)],
            config={"min": 1, "max": above_one}))
        self.assertEqual([(item["ruleId"], item["code"]) for item in checks], [("r_gap", "unsatisfiable_rule")])

    def test_float_adjacent_sets_union_exactly_covers_domain(self):
        above_one = math.nextafter(1.0, math.inf)
        checks = self.rule_checks(definition([
            rule("r_one", leaf("eq", 1), 1), rule("r_next", leaf("eq", above_one), 2),
            rule("r_both", leaf("between", {"min": 1, "max": above_one}), 3)],
            config={"min": 1, "max": above_one}))
        self.assertEqual([item["code"] for item in checks], ["shadowed_rule"])
        self.assertEqual(checks[0]["relatedRuleIds"], ["r_one", "r_next"])

    def test_subnormal_zero_and_negative_float_boundaries(self):
        smallest = math.nextafter(0.0, math.inf)
        checks = self.rule_checks(definition([
            rule("r_negative", leaf("lt", 0), 1), rule("r_positive", leaf("gt", 0), 2),
            rule("r_zero", leaf("eq", -0.0), 3),
            rule("r_all", leaf("between", {"min": -smallest, "max": smallest}), 4)],
            config={"min": -smallest, "max": smallest}))
        self.assertEqual([item["code"] for item in checks], ["shadowed_rule"])
        self.assertEqual(checks[0]["relatedRuleIds"], ["r_negative", "r_positive", "r_zero"])

    def test_safe_maximum_and_minimum_and_unbounded_config(self):
        for value, outer, inner in ((MAX_NUMBER, "gt", "gte"), (-MAX_NUMBER, "lt", "lte")):
            for integer in (False, True):
                with self.subTest(value=value, integer=integer):
                    checks = self.rule_checks(definition([
                        rule("r_outside", leaf(outer, value), 1),
                        rule("r_end", leaf("eq", value), 2), rule("r_same", leaf(inner, value), 3)],
                        config={"integerOnly": integer}))
                    self.assertEqual([item["code"] for item in checks], ["unsatisfiable_rule", "shadowed_rule"])

    def test_optional_missing_default_remains_unchanged(self):
        value = definition([rule("r_all", leaf("gte", 0), 1, "q_answered")], rest=[
            question("q_default", "text", {}), question("q_answered", "text", {})])
        value["questions"][0]["defaultTarget"] = "q_default"
        self.assertEqual(self.checks(value), [])
        missing = evaluate(value, {}, "final")
        answered = evaluate(value, {"q_gate": 0}, "final")
        self.assertEqual(missing["path"], ["q_gate", "q_default"])
        self.assertEqual(answered["path"], ["q_gate", "q_answered"])
        self.assertEqual(missing["errors"], [])

    def test_near_safe_integer_maximum_adjacent_values_are_not_rounded_away(self):
        for integer in (False, True):
            with self.subTest(integer=integer):
                checks = self.rule_checks(definition([
                    rule("r_low", leaf("lt", MAX_NUMBER - 1), 1),
                    rule("r_middle", leaf("eq", float(MAX_NUMBER - 1)), 2),
                    rule("r_high", leaf("gt", MAX_NUMBER - 1), 3),
                    rule("r_all", leaf("gte", MAX_NUMBER - 2), 4)],
                    config={"min": MAX_NUMBER - 2, "max": MAX_NUMBER, "integerOnly": integer}))
                self.assertEqual([item["code"] for item in checks], ["shadowed_rule"])
                self.assertEqual(checks[0]["relatedRuleIds"], ["r_low", "r_middle", "r_high"])

    def test_analysis_does_not_mutate_definition_or_evaluation(self):
        value = definition([rule("r_b", leaf("lt", 40), 20), rule("r_a", leaf("lt", 50), 10)])
        before = copy.deepcopy(value)
        evaluated = evaluate(value, {"q_gate": 35})
        self.assertTrue(self.checks(value))
        self.assertEqual(value, before)
        self.assertEqual(evaluate(value, {"q_gate": 35}), evaluated)

    def test_exhaustive_small_integer_oracle_for_combinations(self):
        conditions = [leaf("lt", 2), leaf("lte", 2), leaf("gt", 2), leaf("gte", 2),
                      leaf("eq", 2), leaf("eq", 2.5), leaf("between", {"min": 1.2, "max": 3.2}),
                      {"anyOf": [leaf("eq", 0), leaf("gt", 3)]},
                      {"allOf": [leaf("gt", 0), leaf("lt", 3)]}]
        for first in conditions:
            for second in conditions:
                with self.subTest(first=first, second=second):
                    a = {value for value in range(5) if _matches(first, value)}
                    b = {value for value in range(5) if _matches(second, value)}
                    checks = self.rule_checks(definition([rule("r_a", first, 1), rule("r_b", second, 2)],
                                                        config={"min": 0, "max": 4, "integerOnly": True}))
                    actual = next((item["code"] for item in checks if item["ruleId"] == "r_b"), None)
                    expected = ("unsatisfiable_rule" if not b else "duplicate_rule_condition" if first == second
                                else "shadowed_rule" if b <= a else "overlapping_rules" if a & b else None)
                    self.assertEqual(actual, expected)

    def test_protocol_maximum_question_rule_and_condition_sizes_are_bounded(self):
        questions = [question(f"q_{qi}", rules=[rule(f"r_{qi}_{ri}", leaf("eq", 1), ri)
                                               for ri in range(20)]) for qi in range(100)]
        value = {"title": "协议上限", "description": "", "allowAnonymous": False,
                 "entryQuestionId": "q_0", "questions": questions}
        checks = self.checks(value)
        self.assertEqual(len(checks), 100 * 19 + 99)
        self.assertTrue(all(len(item.get("relatedRuleIds", [])) <= 19 for item in checks))
        self.assertTrue(all({"code", "message", "questionId", "ruleId", "field"} <= item.keys() for item in checks))
        self.assertTrue(all(isinstance(item["relatedRuleIds"], list) for item in checks))
        # All maxima combined exceed the separate 512 KiB wire limit. Exercise
        # maximum condition size independently using 20 valid 100-node trees.
        condition = {"anyOf": [leaf("eq", 1) for _ in range(99)]}
        tree_checks = self.rule_checks(definition([rule(f"r_tree_{ri}", condition, ri) for ri in range(20)]))
        self.assertEqual(len(tree_checks), 19)

    def test_exhaustive_small_floating_lattice_oracle(self):
        # These four adjacent binary64 values are the complete allowed domain,
        # rather than arbitrary probes standing in for a continuous interval.
        values = [1.0]
        for _ in range(3):
            values.append(math.nextafter(values[-1], math.inf))
        conditions = [leaf("lt", values[1]), leaf("lte", values[1]),
                      leaf("gt", values[1]), leaf("gte", values[1]), leaf("eq", values[1]),
                      leaf("between", {"min": values[0], "max": values[2]}),
                      {"allOf": [leaf("gt", values[1]), leaf("lt", values[2])]},
                      {"anyOf": [leaf("eq", values[0]), leaf("eq", values[-1])]}]
        for first in conditions:
            for second in conditions:
                with self.subTest(first=first, second=second):
                    a = {value for value in values if _matches(first, value)}
                    b = {value for value in values if _matches(second, value)}
                    checks = self.rule_checks(definition([rule("r_a", first, 1), rule("r_b", second, 2)],
                                                        config={"min": values[0], "max": values[-1]}))
                    actual = next((item["code"] for item in checks if item["ruleId"] == "r_b"), None)
                    expected = ("unsatisfiable_rule" if not b else "duplicate_rule_condition" if first == second
                                else "shadowed_rule" if b <= a else "overlapping_rules" if a & b else None)
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
