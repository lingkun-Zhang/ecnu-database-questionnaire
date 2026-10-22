"""V4 protocol tests; no HTTP server or database is required."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.domain import evaluate, validate_definition


def question(qid, kind, config, target="END", required=False, rules=None):
    return {"id": qid, "kind": kind, "title": qid, "required": required,
            "config": config, "defaultTarget": target, "rules": rules or []}


def rule(rid, op, value, target, priority=10):
    return {"id": rid, "priority": priority, "condition": {"operator": op, "value": value}, "target": target}


def definition(questions):
    return {"title": "测试问卷", "description": "", "allowAnonymous": True,
            "entryQuestionId": questions[0]["id"], "questions": questions}


def fixture_definition():
    return definition([
        question("q_age", "number", {"min": 0, "max": 120, "integerOnly": True}, "q_adult", True,
                 [rule("r_minor", "lt", 18, "q_minor")]),
        question("q_minor", "text", {"minLength": 1, "maxLength": 100}, "q_hobby"),
        question("q_adult", "single", {"options": [{"id": "student", "label": "学生"}, {"id": "worker", "label": "工作"}]}, "q_hobby", True),
        question("q_hobby", "multi", {"options": [{"id": v, "label": v} for v in ("read", "sport", "music")], "minSelected": 1, "maxSelected": 2}, "q_score", True),
        question("q_score", "number", {"min": 0, "max": 10, "integerOnly": False}, "q_note"),
        question("q_note", "text", {"minLength": 1, "maxLength": 100}),
    ])


FIXTURE_ANSWERS = [
    {"q_age": 17, "q_minor": "学生", "q_hobby": ["read", "sport"], "q_score": 0},
    {"q_age": 18, "q_adult": "student", "q_hobby": ["read"], "q_score": 8, "q_note": "课程很好"},
    {"q_age": 20, "q_adult": "worker", "q_hobby": ["sport", "music"], "q_score": 6, "q_note": ""},
    {"q_age": 16, "q_minor": "  ", "q_hobby": ["music"], "q_score": None, "q_note": "希望增加活动"},
    {"q_age": 18, "q_adult": "student", "q_hobby": ["read", "music"], "q_score": 10},
    {"q_age": 12, "q_minor": "参加兴趣班", "q_hobby": ["sport"], "q_score": 4},
]


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.d = fixture_definition()

    def codes(self, d=None):
        return {e["code"] for e in validate_definition(d or self.d)}

    def test_fixture_definition_valid(self):
        self.assertEqual(validate_definition(self.d), [])

    def test_errors_are_locatable(self):
        self.d["questions"][0]["rules"][0]["condition"]["questionId"] = "q_minor"
        errors = validate_definition(self.d)
        self.assertTrue(errors)
        self.assertTrue(all({"code", "message", "questionId", "ruleId", "field"} <= set(e) for e in errors))
        self.assertTrue(any(e["questionId"] == "q_age" and e["ruleId"] == "r_minor" and e["code"] == "unknown_field" for e in errors))

    def test_unknown_fields_at_all_levels(self):
        for path in ((), ("questions", 0), ("questions", 0, "config"), ("questions", 0, "rules", 0)):
            with self.subTest(path=path):
                d = fixture_definition()
                obj = d
                for part in path:
                    obj = obj[part]
                obj["unexpected"] = True
                self.assertIn("unknown_field", self.codes(d))

    def test_options_required_but_optional_config_defaults_allowed(self):
        del self.d["questions"][2]["config"]["options"]
        self.assertIn("required_field", self.codes())
        d = definition([question("q_number", "number", {})])
        self.assertEqual(validate_definition(d), [])
        self.assertEqual(evaluate(d, {"q_number": 2.5})["activeAnswers"], {"q_number": 2.5})
        d = definition([question("q_text", "text", {})])
        self.assertEqual(validate_definition(d), [])
        self.assertEqual(evaluate(d, {"q_text": " x "})["activeAnswers"], {"q_text": "x"})

    def test_empty_and_missing_entry(self):
        self.assertIn("question_count", self.codes({**self.d, "questions": []}))
        self.d["entryQuestionId"] = "not_present"
        self.assertIn("invalid_entry", self.codes())

    def test_illegal_and_duplicate_ids(self):
        for value in ("END", "bad.id", "$bad", "", True):
            with self.subTest(value=value):
                d = fixture_definition()
                d["questions"][0]["id"] = value
                self.assertIn("invalid_id", self.codes(d))
        self.d["questions"][1]["id"] = "q_age"
        self.assertIn("duplicate_question_id", self.codes())

    def test_duplicate_option_and_rule_id(self):
        self.d["questions"][2]["config"]["options"][1]["id"] = "student"
        self.assertIn("duplicate_option_id", self.codes())
        self.d = fixture_definition()
        self.d["questions"][0]["rules"].append(rule("r_minor", "lt", 10, "q_minor", 11))
        self.assertIn("duplicate_rule_id", self.codes())

    def test_duplicate_priority(self):
        self.d["questions"][0]["rules"].append(rule("r_second", "lt", 10, "q_minor"))
        self.assertIn("duplicate_priority", self.codes())

    def test_wrong_condition_type_and_cross_question_field(self):
        self.d["questions"][0]["rules"][0]["condition"] = {"operator": "eqOption", "value": "student"}
        self.assertIn("condition_kind", self.codes())
        self.d = fixture_definition()
        self.d["questions"][0]["rules"][0]["condition"]["questionId"] = "q_adult"
        self.assertIn("unknown_field", self.codes())

    def test_text_cannot_branch(self):
        self.d["questions"][1]["rules"] = [rule("r_text", "eq", 3, "q_hobby")]
        self.assertIn("condition_kind", self.codes())

    def test_condition_combination_shape_depth_and_size(self):
        for cond in ({"allOf": []}, {"allOf": [{"operator": "lt", "value": 18}], "anyOf": []}):
            with self.subTest(cond=cond):
                d = fixture_definition()
                d["questions"][0]["rules"][0]["condition"] = cond
                self.assertIn("invalid_condition", self.codes(d))
        cond = {"operator": "lt", "value": 18}
        for _ in range(4):
            cond = {"allOf": [cond]}
        self.d["questions"][0]["rules"][0]["condition"] = cond
        self.assertIn("condition_depth", self.codes())
        self.d["questions"][0]["rules"][0]["condition"] = {"anyOf": [{"operator": "lt", "value": i} for i in range(100)]}
        self.assertIn("condition_size", self.codes())

    def test_numeric_boundaries_and_types(self):
        for low, high in ((5, 4), (True, 4), (0, float("inf")), (0, 2**53), (0.1, 0.9)):
            with self.subTest(low=low, high=high):
                d = fixture_definition()
                d["questions"][0]["config"].update(min=low, max=high)
                self.assertTrue(validate_definition(d))

    def test_actual_bson_int64_roundtrip_remains_executable(self):
        try:
            from bson import BSON, Int64
        except ImportError:
            self.skipTest("Optional PyMongo dependency is needed for the BSON boundary regression")
        d = definition([question("q_phone", "number", {
            "min": 10_000_000_000, "max": 10_000_000_100, "integerOnly": True,
        }, required=True, rules=[rule("r_phone", "between", {
            "min": 10_000_000_000, "max": 10_000_000_100,
        }, "END", priority=2**40)])])
        restored = BSON.encode(d).decode()
        self.assertIsInstance(restored["questions"][0]["config"]["min"], Int64)
        self.assertIsInstance(restored["questions"][0]["rules"][0]["priority"], Int64)
        self.assertEqual(validate_definition(restored), [])
        result = evaluate(restored, {"q_phone": Int64(10_000_000_000)})
        self.assertEqual(result["errors"], [])
        self.assertIs(type(result["activeAnswers"]["q_phone"]), int)
        self.assertEqual(result["trace"][0]["matchedRuleId"], "r_phone")
        self.assertEqual(evaluate(restored, {"q_phone": True})["errors"][0]["code"], "invalid_number")

    def test_multi_constraints_and_comparison_sets(self):
        for low, high in ((3, 2), (-1, 2), (1, 4), (True, 2), (0, 0)):
            with self.subTest(low=low, high=high):
                d = fixture_definition()
                d["questions"][3]["config"].update(minSelected=low, maxSelected=high)
                self.assertTrue(validate_definition(d))
        for values in ([], ["read", "read"], ["other"], [{}]):
            with self.subTest(values=values):
                d = fixture_definition()
                d["questions"][3]["rules"] = [rule("r_hobby", "containsAll", values, "q_score")]
                self.assertIn("condition_option", self.codes(d))

    def test_invalid_target_and_default_cycle(self):
        self.d["questions"][0]["defaultTarget"] = "missing"
        self.assertIn("invalid_target", self.codes())
        self.d = fixture_definition()
        self.d["questions"][1]["defaultTarget"] = "q_age"
        self.assertIn("flow_cycle", self.codes())

    def test_bad_rule_target_has_rule_location(self):
        self.d["questions"][0]["rules"][0]["target"] = "q_missing"
        error = next(e for e in validate_definition(self.d) if e["code"] == "invalid_target")
        self.assertEqual(error["questionId"], "q_age")
        self.assertEqual(error["ruleId"], "r_minor")
        self.assertTrue(error["field"].endswith("rules[0].target"))

    def test_unreachable_cycle_is_still_rejected(self):
        self.d["questions"].append(question("q_unused", "text", {"minLength": None, "maxLength": None}, "q_unused"))
        self.assertIn("flow_cycle", self.codes())

    def test_display_order_can_differ_from_flow(self):
        self.d["questions"].reverse()
        self.assertEqual(validate_definition(self.d), [])
        self.assertEqual(evaluate(self.d, FIXTURE_ANSWERS[0])["path"][0], "q_age")

    def test_scale_limits(self):
        d = fixture_definition()
        d["questions"] = [question(f"q_{i}", "text", {"minLength": None, "maxLength": None}) for i in range(101)]
        self.assertIn("question_count", self.codes(d))
        d = fixture_definition()
        d["questions"][2]["config"]["options"] = [{"id": f"o_{i}", "label": "x"} for i in range(51)]
        self.assertIn("option_count", self.codes(d))
        d = fixture_definition()
        d["questions"][0]["rules"] = [rule(f"r_{i}", "lt", i, "q_minor", i) for i in range(21)]
        self.assertIn("rule_count", self.codes(d))

    def test_nullable_and_omitted_config_bounds(self):
        for kind, config, value in (("text", {}, "内容"), ("number", {"min": None, "max": None}, -2.5),
                                    ("multi", {"options": [{"id": "yes", "label": "是"}]}, ["yes"])):
            with self.subTest(kind=kind):
                d = definition([question("q_test", kind, config)])
                self.assertEqual(validate_definition(d), [])
                self.assertEqual(evaluate(d, {"q_test": value})["errors"], [])

    def test_invalid_definition_root_does_not_raise(self):
        for value in (None, [], "bad", {"questions": [{}]}):
            with self.subTest(value=value):
                self.assertTrue(validate_definition(value))
                self.assertTrue(evaluate(value, {})["errors"])


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.d = fixture_definition()

    def test_six_valid_fixtures_and_idempotence(self):
        for raw in FIXTURE_ANSWERS:
            with self.subTest(raw=raw):
                result = evaluate(self.d, raw)
                self.assertEqual(result["errors"], [])
                self.assertTrue(result["complete"])
                self.assertFalse(result["provisional"])
                self.assertEqual(len(result["path"]), len(set(result["path"])))
                self.assertTrue(set(result["activeAnswers"]) <= set(result["path"]))
                again = evaluate(self.d, result["activeAnswers"])
                self.assertEqual(result["path"], again["path"])
                self.assertEqual(result["activeAnswers"], again["activeAnswers"])

    def test_age_boundary_and_skipped_required(self):
        minor = evaluate(self.d, FIXTURE_ANSWERS[0])
        adult = evaluate(self.d, FIXTURE_ANSWERS[1])
        self.assertIn("q_minor", minor["path"])
        self.assertNotIn("q_adult", minor["path"])
        self.assertIn("q_adult", adult["path"])
        self.assertNotIn("q_minor", adult["path"])
        self.assertEqual(minor["trace"][0]["matchedRuleId"], "r_minor")

    def test_inactive_invalid_old_answers_are_ignored(self):
        raw = {**FIXTURE_ANSWERS[0], "q_adult": [False, "not-an-option"]}
        result = evaluate(self.d, raw)
        self.assertEqual(result["errors"], [])
        self.assertNotIn("q_adult", result["activeAnswers"])

    def test_interactive_missing_required_pauses(self):
        result = evaluate(self.d, {}, "interactive")
        self.assertEqual(result["path"], ["q_age"])
        self.assertEqual(result["blockedAt"], "q_age")
        self.assertFalse(result["complete"])
        self.assertFalse(result["provisional"])
        self.assertIsNone(result["trace"][0]["target"])

    def test_final_errors_take_provisional_default_route(self):
        result = evaluate(self.d, {})
        self.assertTrue(result["complete"])
        self.assertTrue(result["provisional"])
        self.assertIsNone(result["blockedAt"])
        self.assertEqual([e["questionId"] for e in result["errors"]], ["q_age", "q_adult", "q_hobby"])
        self.assertNotIn("q_minor", result["path"])

    def test_numeric_invalid_values(self):
        for value in (True, False, "18", [], {}, float("nan"), float("inf"), -float("inf"), 2**53, 18.5, -1, 121):
            with self.subTest(value=value):
                result = evaluate(self.d, {"q_age": value}, "interactive")
                self.assertEqual(result["blockedAt"], "q_age")
                self.assertNotIn("q_age", result["activeAnswers"])

    def test_integer_float_and_negative_zero_canonicalization(self):
        raw = {**FIXTURE_ANSWERS[1], "q_age": 18.0, "q_score": -0.0}
        result = evaluate(self.d, raw)
        self.assertEqual(result["errors"], [])
        self.assertIs(type(result["activeAnswers"]["q_age"]), int)
        self.assertIs(type(result["activeAnswers"]["q_score"]), int)
        self.assertEqual(result["activeAnswers"]["q_score"], 0)

    def test_non_integer_optional_number_is_preserved(self):
        result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_score": 2.5})
        self.assertEqual(result["activeAnswers"]["q_score"], 2.5)

    def test_multi_count_duplicates_and_strict_type(self):
        for value in (["read", "read"], ["read", "sport", "music"], ["invalid"], [True], "read"):
            with self.subTest(value=value):
                result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_hobby": value}, "interactive")
                self.assertEqual(result["blockedAt"], "q_hobby")
        result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_hobby": ["sport", "read"]})
        self.assertEqual(result["activeAnswers"]["q_hobby"], ["read", "sport"])

    def test_empty_multi_is_missing_not_answered(self):
        result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_hobby": []}, "interactive")
        self.assertEqual(result["errors"][0]["code"], "required")
        self.assertNotIn("q_hobby", result["activeAnswers"])

    def test_unicode_trim_and_codepoint_length(self):
        self.d["questions"][1]["config"] = {"minLength": 1, "maxLength": 1}
        result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_minor": " \u3000😀\n"})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["activeAnswers"]["q_minor"], "😀")
        result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_minor": "两个"}, "interactive")
        self.assertEqual(result["blockedAt"], "q_minor")

    def test_empty_optional_does_not_check_minimum(self):
        result = evaluate(self.d, FIXTURE_ANSWERS[3])
        self.assertEqual(result["errors"], [])
        self.assertIn("q_minor", result["path"])
        self.assertNotIn("q_minor", result["activeAnswers"])
        self.assertNotIn("q_score", result["activeAnswers"])

    def test_typed_empty_values_not_universally_missing(self):
        for qid, value in (("q_age", []), ("q_adult", "")):
            with self.subTest(qid=qid):
                result = evaluate(self.d, {**FIXTURE_ANSWERS[1], qid: value}, "interactive")
                self.assertEqual(result["blockedAt"], qid)
                self.assertNotEqual(result["errors"][0]["code"], "required")

    def test_priority_not_array_order(self):
        self.d["questions"][0]["rules"] = [rule("r_later", "lt", 30, "q_adult", 50), rule("r_first", "lt", 18, "q_minor", -5)]
        result = evaluate(self.d, FIXTURE_ANSWERS[0])
        self.assertEqual(result["trace"][0]["matchedRuleId"], "r_first")
        self.assertEqual(len(result["trace"][0]["ruleResults"]), 1)

    def test_current_question_combination_and_closed_interval(self):
        self.d["questions"][0]["rules"][0]["condition"] = {"allOf": [
            {"operator": "between", "value": {"min": 0, "max": 17}},
            {"anyOf": [{"operator": "eq", "value": 17}, {"operator": "lt", "value": 15}]}]}
        self.assertEqual(validate_definition(self.d), [])
        self.assertIn("q_minor", evaluate(self.d, FIXTURE_ANSWERS[0])["path"])
        self.assertIn("q_adult", evaluate(self.d, FIXTURE_ANSWERS[1])["path"])

    def test_multi_operators(self):
        for op, values, matches in (("containsAny", ["sport"], True), ("containsAll", ["read", "sport"], True), ("setEquals", ["sport", "read"], True), ("setEquals", ["read"], False)):
            with self.subTest(op=op, values=values):
                d = fixture_definition()
                d["questions"][3]["rules"] = [rule("r_hobby", op, values, "q_note")]
                result = evaluate(d, FIXTURE_ANSWERS[0])
                self.assertEqual("q_score" not in result["path"], matches)

    def test_unknown_question_and_invalid_root(self):
        result = evaluate(self.d, {"unknown": 3})
        self.assertEqual(result["errors"][0]["code"], "unknown_question")
        self.assertFalse(result["complete"])
        self.assertEqual(evaluate(self.d, [1])["errors"][0]["code"], "invalid_answers")
        self.assertEqual(evaluate(self.d, {}, "bypass")["errors"][0]["code"], "invalid_mode")

    def test_no_inputs_mutated(self):
        raw = {**FIXTURE_ANSWERS[0], "q_hobby": ["sport", "read"], "q_minor": " 学生 "}
        before_d, before_raw = copy.deepcopy(self.d), copy.deepcopy(raw)
        evaluate(self.d, raw)
        self.assertEqual(self.d, before_d)
        self.assertEqual(raw, before_raw)

    def test_all_optional_empty_submission_can_complete(self):
        d = definition([question("q_text", "text", {"minLength": 5, "maxLength": 10})])
        result = evaluate(d, {})
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(result["activeAnswers"], {})

    def test_global_text_cap_and_unpaired_surrogate(self):
        for text in ("x" * 10001, "\ud800"):
            with self.subTest(length=len(text)):
                result = evaluate(self.d, {**FIXTURE_ANSWERS[0], "q_minor": text}, "interactive")
                self.assertEqual(result["blockedAt"], "q_minor")

    def test_request_limits_count_utf8_not_ascii_escapes(self):
        questions = [question(f"q_{i}", "text", {}) for i in range(20)]
        d = definition(questions)
        raw = {q["id"]: "中" * 10_000 for q in questions}
        result = evaluate(d, raw)
        self.assertEqual(result["errors"], [])
        self.assertEqual(set(result["activeAnswers"]), {"q_0"})

    def test_answer_request_size_limit(self):
        result = evaluate(self.d, {"q_minor": "x" * (1024 * 1024)})
        self.assertEqual(result["errors"][0]["code"], "answers_too_large")

    def test_six_fixture_independent_statistics(self):
        results = [evaluate(self.d, raw) for raw in FIXTURE_ANSWERS]
        expected = {"q_age": (6, 6, 0, 0), "q_minor": (3, 2, 1, 3),
                    "q_adult": (3, 3, 0, 3), "q_hobby": (6, 6, 0, 0),
                    "q_score": (6, 5, 1, 0), "q_note": (6, 2, 4, 0)}
        for qid, counts in expected.items():
            reached = sum(qid in r["path"] for r in results)
            answered = sum(qid in r["activeAnswers"] for r in results)
            self.assertEqual((reached, answered, reached - answered, 6 - reached), counts)
            self.assertTrue(0 <= answered <= reached <= 6)
        scores = [r["activeAnswers"]["q_score"] for r in results if "q_score" in r["activeAnswers"]]
        self.assertEqual(len(scores), 5)
        self.assertEqual(sum(scores), 28)
        self.assertAlmostEqual(sum(scores) / len(scores), 5.6)
        hobbies = [r["activeAnswers"]["q_hobby"] for r in results]
        self.assertEqual(sum(map(len, hobbies)), 9)
        self.assertEqual({oid: sum(oid in values for values in hobbies) for oid in ("read", "sport", "music")}, {"read": 3, "sport": 3, "music": 3})


if __name__ == "__main__":
    unittest.main()
