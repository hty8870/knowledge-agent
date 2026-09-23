# -*- coding: utf-8 -*-
"""活台账只读层：把「逐文件检查向量 + 最近核验时间」喂给 MCP / webapp（运行时不联网、优雅降级）。

与 `downloads.py` 同层（Data/IO）、同合同：数据缺失/损坏 → 一律返回空/None（永不崩、永不阻塞调用方）。
读的是 `data/inspection/current.json`（由 `scripts/seed_inspection_ledger.py` 播种、`scripts/patrol_links.py`
重测后重建的物化视图）。**不改也不依赖 downloads.py**；调用方拿到 dataset_uid 后来查逐文件状态。

对外语义（v0 契约，见 seed 脚本头 / 设计文档 §5.5）：
  reachable ok|dead|unknown · size match|mismatch|unknown ·
  integrity unknown|verified|mismatch（unknown=md5 未重算；后两档由 provision 回写实测落盘）·
  load unknown|loaded|failed（unknown=未真加载；后两档由 load_smoke 抽样真下载真加载落盘）
  problem := reachable==dead 或 size==mismatch 或 integrity==mismatch 或 load==failed
  （unknown 不算 problem）

用 `BIODATA_INSPECTION` 环境变量可覆盖 current.json 位置（供测试）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import fs_utils

_DEFAULT = str(Path(__file__).resolve().parents[1] / "data" / "inspection" / "current.json")
_DATA_PATH = os.environ.get("BIODATA_INSPECTION", _DEFAULT)


def norm(u: "str | None") -> str:
    """与 seed / patrol / 阶段二同口径的 URL 归一（join 键）。"""
    if not u or "://" not in u:
        return u or ""
    scheme, rest = u.split("://", 1)
    for sep in ("?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    rest = re.sub(r"/{2,}", "/", rest)
    rest = rest.replace("\\u002F", "/").replace("\\/", "/")
    return f"{scheme}://{rest}"


def _ledger_shape(data: object) -> dict:
    """形状闸：必须 {by_uid: dict}，缺形状 = 文件损坏（骨架见 fs_utils.make_sidecar_loader）。"""
    if not isinstance(data, dict) or not isinstance(data.get("by_uid"), dict):
        raise ValueError("台账形状不符（缺 by_uid dict，文件损坏）")
    return data


def _data_path() -> str:
    """台账路径：每次加载现读环境变量，未设置回落 import 期快照 `_DATA_PATH`（可 monkeypatch）。"""
    return os.environ.get("BIODATA_INSPECTION") or _DATA_PATH


_load = fs_utils.make_sidecar_loader(
    data_path=_data_path,
    shape_gate=_ledger_shape,
    missing={},
)


def is_available() -> bool:
    """活台账是否成功加载（供诊断/自检/测试）。"""
    cur = _load()
    return bool(cur.get("by_uid"))


def snapshot_info() -> "dict | None":
    """全局快照元信息：schema / snapshot_id / snapshot_date / source / totals；未就绪返回 None。"""
    cur = _load()
    if not cur.get("by_uid"):
        return None
    return {
        "schema": cur.get("schema"),
        "snapshot_id": cur.get("snapshot_id"),
        "snapshot_date": cur.get("snapshot_date"),
        "source": cur.get("source"),
        "totals": cur.get("totals", {}),
    }


def _derive(url: str, v: dict) -> dict:
    """把紧凑存储行 {r,h,s,srv,v[,i][,l]} 派生成对外逐文件状态（problem/reason 在此单点计算）。

    additive（2026-08-01）：`i` 是 provision 回写落盘的 integrity 实测档
    （verified|mismatch；无此键 = 未实测 = unknown）。旧向量没有 `i`，行为逐位不变。
    additive（2026-08-01，load_smoke）：`l` 是抽样冒烟落盘的 load 实测档
    （loaded|failed；无此键 = 未真加载 = unknown），failed 计入 problem 并派生 reason。
    """
    reach = v.get("r", "unknown")
    size = v.get("s", "unknown")
    http = v.get("h")
    srv = v.get("srv")
    last = v.get("v")
    integrity = v.get("i") or "unknown"
    load = v.get("l") or "unknown"
    problem = (reach == "dead") or (size == "mismatch") or (integrity == "mismatch") or (load == "failed")
    reason = None
    if reach == "dead":
        reason = f"链接失效（HTTP {http}）；最近核验 {last}。"
    elif size == "mismatch":
        reason = (f"服务器报告的文件大小（{srv} 字节）与清单记录不一致，内容可能已变更或被替换；"
                  f"最近核验 {last}。")
    elif integrity == "mismatch":
        reason = (f"实际下载并重算的 md5 与来源声明不一致，内容可能已变更或被替换；"
                  f"最近核验 {last}。")
    elif load == "failed":
        reason = (f"抽样冒烟真实加载失败（下载核对通过但 scanpy 读入报错），文件可能损坏或格式不符；"
                  f"最近核验 {last}。")
    return {
        "reachable": reach,
        "http": http,
        "size": size,
        "server_bytes": srv,
        "integrity": integrity,   # unknown=未实测（md5 为 10x 声明值）；verified/mismatch=provision 实下实算
        "load": load,             # unknown=未真加载；loaded/failed=load_smoke 抽样真加载实测
        "problem": problem,
        "problem_reason": reason,
        "last_verified": last,
    }


def status_for(uid: "str | None", url: "str | None") -> "dict | None":
    """查某数据集某文件（按 dataset_uid + 文件直链）的派生状态；无记录返回 None。"""
    if not uid or not url:
        return None
    rec = _load().get("by_uid", {}).get(uid)
    if not rec:
        return None
    v = (rec.get("f") or {}).get(norm(url))
    return _derive(norm(url), v) if v else None


def file_status(uid: "str | None") -> "dict[str, dict]":
    """某数据集全部文件的派生状态：{norm_url: status}。无记录返回空 dict。

    **测试支撑接口，生产侧无调用点**（webapp / mcp_server 都按单个直链走 `status_for`）。
    保留理由：`tests/test_inspection.py` 用它**独立**逐文件派生问题数，交叉核验台账的 `np` 字段与
    `dataset_summary` 的 `n_problem` —— 即它钉的是「摘要的计数 == 逐文件重算的计数」这条不变量。
    2026-07-17 全盘审计把它报成死代码（零生产调用点，属实），但删掉＝丢掉该不变量、换一个整洁度，
    是坏交易 → 明确保留并在此标注，避免下一轮再被当垃圾清掉。
    """
    if not uid:
        return {}
    rec = _load().get("by_uid", {}).get(uid)
    if not rec:
        return {}
    return {u: _derive(u, v) for u, v in (rec.get("f") or {}).items()}


def dataset_summary(uid: "str | None") -> "dict | None":
    """某数据集的 provisioning 摘要：文件数 / 问题数 / 是否全绿 / 最近核验；无记录返回 None。"""
    if not uid:
        return None
    rec = _load().get("by_uid", {}).get(uid)
    if not rec:
        return None
    nf = rec.get("nf", 0)
    npb = rec.get("np", 0)
    return {
        "n_files": nf,
        "n_problem": npb,
        "provisioning_ok": npb == 0,
        "problem_rate": round(npb / nf, 4) if nf else None,
        "last_verified": rec.get("lv"),
        "snapshot_id": _load().get("snapshot_id"),
        "snapshot_date": _load().get("snapshot_date"),
    }
