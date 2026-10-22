from copy import deepcopy


def example_definition():
    return deepcopy({
        "title": "校园活动与学习体验调查", "description": "教学演示问卷：展示年龄分支、选填、零值、多选与匿名。",
        "allowAnonymous": True, "entryQuestionId": "q_age", "questions": [
            {"id": "q_age", "kind": "number", "title": "你的年龄", "required": True,
             "config": {"min": 0, "max": 120, "integerOnly": True}, "defaultTarget": "q_adult",
             "rules": [{"id": "r_minor", "priority": 10, "condition": {"operator": "lt", "value": 18}, "target": "q_minor"}]},
            {"id": "q_minor", "kind": "text", "title": "你正在参加什么学习活动", "required": False,
             "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "q_hobby", "rules": []},
            {"id": "q_adult", "kind": "single", "title": "你目前的身份", "required": True,
             "config": {"options": [{"id": "student", "label": "学生"}, {"id": "worker", "label": "工作人士"}]},
             "defaultTarget": "q_hobby", "rules": []},
            {"id": "q_hobby", "kind": "multi", "title": "你喜欢的活动（选1—2项）", "required": True,
             "config": {"options": [{"id": "read", "label": "阅读"}, {"id": "sport", "label": "运动"},
                                    {"id": "music", "label": "音乐"}], "minSelected": 1, "maxSelected": 2},
             "defaultTarget": "q_score", "rules": []},
            {"id": "q_score", "kind": "number", "title": "对活动的评分（0—10，可不填）", "required": False,
             "config": {"min": 0, "max": 10, "integerOnly": False}, "defaultTarget": "q_note", "rules": []},
            {"id": "q_note", "kind": "text", "title": "其他建议", "required": False,
             "config": {"minLength": 1, "maxLength": 100}, "defaultTarget": "END", "rules": []},
        ],
    })


ROWS = [
    ("sample_01", True, {"q_age": 17, "q_minor": "学生", "q_hobby": ["read", "sport"], "q_score": 0}),
    ("sample_02", False, {"q_age": 18, "q_adult": "student", "q_hobby": ["read"], "q_score": 8, "q_note": "课程很好"}),
    ("sample_03", True, {"q_age": 20, "q_adult": "worker", "q_hobby": ["sport", "music"], "q_score": 6}),
    ("sample_04", False, {"q_age": 16, "q_hobby": ["music"], "q_note": "希望增加活动"}),
    ("sample_01", True, {"q_age": 18, "q_adult": "student", "q_hobby": ["read", "music"], "q_score": 10}),
    ("sample_05", False, {"q_age": 12, "q_minor": "参加兴趣班", "q_hobby": ["sport"], "q_score": 4}),
]
