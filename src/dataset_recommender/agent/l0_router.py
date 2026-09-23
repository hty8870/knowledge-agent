# -*- coding: utf-8 -*-
"""L0 微型分流器：纯 Python 推理（零新增依赖，微秒级，CPU 本地）。

训练侧在 `research/route_eval/train_l0_router.py`（sklearn TF-IDF(char 2-4gram) +
LogisticRegression + 温度缩放校准）；本模块是它的生产推理半——模型经
`research/route_eval/export_l0_production.py` 导出为 JSON（导出时跑 sklearn
数值一致性 parity，证据随导出报告落盘），本模块加载后逐位复现
sklearn 的 decision_function（count×idf → L2 归一 → 点积 + intercept →
softmax(logits/温度)）。

模型工件（与本文件同目录 `router_models/`）：
- `l0_route_three_class.json`：三分类（search/action/general），三分类分流级联用
- `l0_route_binary.json`：二元写闸（write/read），二元分流级联用
- `l0_prelim_gate.json`：初步检索执行闸（retrieve/skip），turn 入口 flight 闸用

纪律：
- 一律 fail-open——工件缺失/损坏/格式不符/任何异常都返回 None，由调用方回落
  现状行为（LLM 共识 / 起跑 flight），本模块绝不成为新的单点。
- 开关与阈值每次调用现读 env（一次 os.getenv 成本可忽略，与 webapp 同口径），
  模型本体按名单懒加载缓存。
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any

_MODEL_DIR = Path(__file__).resolve().parent / "router_models"
_FORMAT = "l0-router-v1"

ROUTE_THREE_CLASS = "l0_route_three_class"
ROUTE_BINARY = "l0_route_binary"
PRELIM_GATE = "l0_prelim_gate"

_cache: dict[str, "_L0Model | None"] = {}
_cache_lock = threading.Lock()


class _L0Model:
    """一份导出工件的内存形态：vocab/idf/coef/intercept/温度/工作点。"""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.classes: list[str] = [str(c) for c in spec["classes"]]
        self.temperature: float = float(spec["temperature"])
        self.vocab: dict[str, int] = {str(k): int(v) for k, v in spec["vocab"].items()}
        self.idf: list[float] = [float(x) for x in spec["idf"]]
        self.coef: list[list[float]] = [[float(x) for x in row] for row in spec["coef"]]
        self.intercept: list[float] = [float(x) for x in spec["intercept"]]
        self.operating_point: dict[str, Any] = dict(spec.get("operating_point") or {})
        # 二元 LogReg 只有一行 coef（正类 = classes[1]），logits 补成 (-z, z) 两列——
        # 与 train_l0_router.py 的校准口径逐位一致。
        self.binary: bool = len(self.coef) == 1

    def probs(self, text: str) -> list[float]:
        """softmax(decision_function/温度)，与 sklearn 管线逐位一致。"""
        t = str(text or "").lower()
        counts: dict[int, int] = {}
        for n in (2, 3, 4):
            for i in range(len(t) - n + 1):
                j = self.vocab.get(t[i:i + n])
                if j is not None:
                    counts[j] = counts.get(j, 0) + 1
        norm = math.sqrt(sum((c * self.idf[j]) ** 2 for j, c in counts.items()))
        xs: dict[int, float] = ({j: c * self.idf[j] / norm for j, c in counts.items()}
                                if norm > 0 else {})
        temp = max(self.temperature, 1e-3)
        if self.binary:
            z = self.intercept[0] + sum(self.coef[0][j] * x for j, x in xs.items())
            logits = [-z / temp, z / temp]
        else:
            logits = [(self.intercept[k] + sum(self.coef[k][j] * x for j, x in xs.items()))
                      / temp for k in range(len(self.classes))]
        hi = max(logits)
        exps = [math.exp(v - hi) for v in logits]
        total = sum(exps)
        return [v / total for v in exps]


def _load(name: str) -> _L0Model | None:
    """懒加载 + 缓存；任何异常 → None（fail-open，warn-once 由调用方决定，本层安静）。"""
    with _cache_lock:
        if name in _cache:
            return _cache[name]
        model: _L0Model | None = None
        try:
            spec = json.loads((_MODEL_DIR / f"{name}.json").read_text(encoding="utf-8"))
            if str(spec.get("format") or "") == _FORMAT:
                model = _L0Model(spec)
        except Exception:
            model = None
        _cache[name] = model
        return model


def reset_cache() -> None:
    """测试专用：清空模型缓存（工件被替换后重载）。"""
    with _cache_lock:
        _cache.clear()


def available(name: str) -> bool:
    return _load(name) is not None


def score(name: str, text: str) -> dict[str, Any] | None:
    """打分主入口：返回 {label, confidence, probs, operating_point}；不可用 → None。"""
    model = _load(name)
    if model is None:
        return None
    try:
        probs = model.probs(text)
        best = max(range(len(probs)), key=lambda k: probs[k])
        return {"label": model.classes[best],
                "confidence": probs[best],
                "probs": {c: probs[k] for k, c in enumerate(model.classes)},
                "operating_point": dict(model.operating_point)}
    except Exception:
        return None


def operating_tau(name: str, env_var: str, key: str, default: float) -> float:
    """工作点阈值解析：env 显式覆盖 > 工件内嵌工作点 > default。非法值静默回 default。"""
    raw = os.environ.get(env_var, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            return default
    model = _load(name)
    if model is not None:
        try:
            return float(model.operating_point[key])
        except (KeyError, TypeError, ValueError):
            pass
    return default
