"""course_kb 的领域数据（被 pack.py 以**包内相对导入**引用——外部装载器必须支持相对导入）。"""
from __future__ import annotations

CATALOG: dict[str, list[dict]] = {
    "course": [
        {"aliases": ["数据结构", "data structures", "data structure"],
         "targets": ["data structures", "数据结构"], "display": "数据结构"},
        {"aliases": ["机器学习", "machine learning", "ml"],
         "targets": ["machine learning", "机器学习"], "display": "机器学习"},
        {"aliases": ["操作系统", "operating systems", "os"],
         "targets": ["operating systems", "操作系统"], "display": "操作系统"},
        {"aliases": ["统计学习", "statistical learning"],
         "targets": ["statistical learning", "统计学习"], "display": "统计学习"},
    ],
    "instructor": [
        {"aliases": ["王老师", "wang"], "targets": ["王老师", "wang"], "display": "王老师"},
        {"aliases": ["李老师", "li"], "targets": ["李老师", "li"], "display": "李老师"},
        {"aliases": ["赵老师", "zhao"], "targets": ["赵老师", "zhao"], "display": "赵老师"},
    ],
    "semester": [
        {"aliases": ["2024秋", "2024秋季", "2024-fall"], "targets": ["2024-fall"], "display": "2024秋"},
        {"aliases": ["2025春", "2025春季", "2025-spring"], "targets": ["2025-spring"], "display": "2025春"},
    ],
}

RECORD_ROWS: tuple[tuple[str, dict, str], ...] = (
    ("数据结构（2024秋·王老师）", {"course": "数据结构", "instructor": "王老师", "semester": "2024-fall"},
     "线性表、树、图、排序与查找。"),
    ("数据结构（2025春·李老师）", {"course": "数据结构", "instructor": "李老师", "semester": "2025-spring"}, ""),
    ("机器学习（2024秋·王老师）", {"course": "机器学习", "instructor": "王老师", "semester": "2024-fall"},
     "回归、分类、聚类与正则化。"),
    ("操作系统（2024秋·李老师）", {"course": "操作系统", "instructor": "李老师", "semester": "2024-fall"},
     "进程、内存、文件系统。"),
    ("机器学习（2025春·赵老师）", {"course": "机器学习", "instructor": "赵老师", "semester": "2025-spring"},
     "机器学习 2025春学期。"),
    ("统计学习导论（2025春·赵老师）", {"course": "统计学习", "instructor": "赵老师", "semester": "2025-spring"},
     "统计学习理论基础。"),
)
