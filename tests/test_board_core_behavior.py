# -*- coding: utf-8 -*-
"""条件板纯核的真行为门 + 前后端同一份规则的跨语言对拍。

为什么要它：`web_smoke_test.py` 只对前端 JS 做静态字符串检查，`node --check` 只验语法，
两门都测不出「四分区归并对不对」「撤销栈的游标会不会越界」。

更要紧的是第二条：条件板的行在**两处**实现——浏览器要在每次检索后立刻画出来，不可能为了几行字
再往返一次服务端，所以 `board_core.js` 与 `board.board_view` 是同一份规则的两份代码。
本仓库已经被「两份手抄必然漂移」坑过好几次（交付集与忽略清单、item_view 的两份投影）。
这里用同样的入参逐字段对拍，让漂移当场亮红。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from dataset_recommender.app import board

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "js" / "board_core_spec.mjs"
CORE = ROOT / "web" / "static" / "js" / "panel" / "board_core.js"
#: board_core.js 已是 ES Module：对拍脚本在临时 .mjs 里经 file:// URL import 真文件。
BOARD_CORE_URI = CORE.as_uri()


def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_node(script: str, payload: object) -> object:
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门（full 质量门的语法检查环节必有 node）。")
    # ESM 脚本（import 真 board_core.js）落临时 .mjs 再跑；stdin 喂入参、stdout 收 JSON——
    # 门的语义不变：仍是在 node 里加载真 board_core.js、跑真函数。
    fd, script_path = tempfile.mkstemp(suffix=".mjs", prefix="biodata_board_gate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        proc = subprocess.run(
            [node, script_path],
            cwd=str(ROOT),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_board_core_behavior_spec_passes_under_node():
    assert SPEC.is_file(), f"缺少行为规格文件：{SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门。")
    proc = subprocess.run(
        [node, str(SPEC)], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"条件板纯核行为规格失败：\n{combined}"
    assert "BOARD_CORE_SPEC_OK" in (proc.stdout or ""), f"未见成功标记：\n{combined}"


#: 覆盖：正向 / 负向 / 原始数据 / 时间 / 分面 / 放宽 / 忽略 / 有无覆盖缺口
PARITY_CASES = [
    {
        "active": [
            {"filter_id": "include:species", "polarity": "include", "dim": "species", "label": "物种", "values": ["Human"]},
            {"filter_id": "include:tissue", "polarity": "include", "dim": "tissue", "label": "组织", "values": ["Lung"]},
        ],
        "facets": [{"dim": "species", "value": "homo sapiens", "display": "Homo sapiens"}],
        "lenient": ["disease"],
        "suppressed": ["include:platform"],
        "coverage": ["tissue"],
    },
    {
        "active": [
            {"filter_id": "exclude:disease", "polarity": "exclude", "dim": "disease", "label": "排除·疾病", "values": ["Tumor"]},
            {"filter_id": "raw:required", "polarity": "include", "dim": "has_raw_data", "label": "原始数据", "values": ["需要 FASTQ"]},
            {"filter_id": "date:range", "polarity": "include", "dim": "date", "label": "发表时间", "values": ["≥ 2020-01-01"]},
        ],
        "facets": [],
        "lenient": [],
        "suppressed": ["exclude:disease", "raw:required", "date:range"],
        "coverage": [],
    },
    {"active": [], "facets": [], "lenient": [], "suppressed": [], "coverage": []},
    # 软偏好（「优先 X」）。加这一组是因为前一轮**只改了 JS 一侧**：`board_core.js` 有了
    # prefer / prefer_off 两区，`board.board_view` 一个字没动，而上面的用例里一条 prefer 都没有，
    # 于是这道对拍门全绿地放过了漂移。四种 prefer 编号都要覆盖——`prefer:raw` / `prefer:date`
    # 的 dim 段与硬条件重名，最容易被还原成硬条件的中文名。
    {
        "active": [
            {"filter_id": "include:tissue", "polarity": "include", "dim": "tissue", "label": "组织", "values": ["Lung"]},
            {"filter_id": "prefer:species", "polarity": "prefer", "dim": "species", "label": "优先·物种", "values": ["Human"]},
            {"filter_id": "prefer:raw", "polarity": "prefer", "dim": "has_raw_data", "label": "优先·原始数据", "values": ["有 FASTQ"]},
            {"filter_id": "prefer:source", "polarity": "prefer", "dim": "source", "label": "优先·数据来源", "values": ["10x Genomics"]},
            {"filter_id": "prefer:date", "polarity": "prefer", "dim": "date", "label": "优先·发表时间", "values": ["2024-01-01 ~ 2024-12-31"]},
        ],
        "facets": [],
        "lenient": [],
        "suppressed": ["prefer:platform", "prefer:raw", "prefer:date", "prefer:source", "include:species"],
        "coverage": [],
    },
]

_PARITY_SCRIPT = (
    f'import * as core from "{BOARD_CORE_URI}";\n'
    'import { readFileSync } from "node:fs";\n'
    "const cases = JSON.parse(readFileSync(0, \"utf-8\"));\n"
    "const out = cases.map((c) =>\n"
    "    core.cbRowsFrom(c.active, c.facets, c.lenient, c.suppressed, c.coverage));\n"
    "process.stdout.write(JSON.stringify(out));\n"
)


def test_frontend_and_backend_project_the_same_rows():
    """同样的入参，浏览器画出来的行必须与服务端算出来的行逐字段一致。"""
    js_rows = _run_node(_PARITY_SCRIPT, PARITY_CASES)
    for index, case in enumerate(PARITY_CASES):
        py_rows = board.board_view(
            case["active"], case["facets"], case["lenient"], case["suppressed"], case["coverage"]
        )["rows"]
        assert js_rows[index] == py_rows, (
            f"第 {index + 1} 组入参上前后端产出的行不一致：\n"
            f"  浏览器：{json.dumps(js_rows[index], ensure_ascii=False)}\n"
            f"  服务端：{json.dumps(py_rows, ensure_ascii=False)}"
        )


def test_dimension_labels_are_identical_on_both_sides():
    """条件名的中文标签也必须一致——同一个条件在两处显示成不同的名字最难自查。"""
    js_labels = _run_node(
        f'import * as c from "{BOARD_CORE_URI}";\n'
        'import { readFileSync } from "node:fs";\n'
        "const ids = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        "process.stdout.write(JSON.stringify(ids.map(c.cbLabelForFilterId)));\n",
        ["include:species", "include:tissue", "include:disease", "include:platform",
         "include:assay", "include:modality", "exclude:species", "exclude:disease",
         "raw:required", "raw:forbidden", "date:range"],
    )
    py_labels = [board._label_for_filter_id(fid) for fid in
                 ["include:species", "include:tissue", "include:disease", "include:platform",
                  "include:assay", "include:modality", "exclude:species", "exclude:disease",
                  "raw:required", "raw:forbidden", "date:range"]]
    assert js_labels == py_labels, f"条件名两边不一致：浏览器 {js_labels} vs 服务端 {py_labels}"


def test_parity_check_can_actually_fail():
    """反重言式：把入参故意改一处，两边就应该给出不同的行——证明上面那条比较有鉴别力。"""
    case = PARITY_CASES[0]
    py_rows = board.board_view(case["active"], case["facets"], case["lenient"], case["suppressed"], [])["rows"]
    py_rows_with_gap = board.board_view(
        case["active"], case["facets"], case["lenient"], case["suppressed"], ["tissue"]
    )["rows"]
    assert py_rows != py_rows_with_gap, "有无覆盖缺口必须影响行内容，否则「可放宽」这个标记是死的"
