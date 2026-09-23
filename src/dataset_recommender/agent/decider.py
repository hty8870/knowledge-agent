# -*- coding: utf-8 -*-
"""Jev 决策器（生产推理半）：**Qwen3-1.7B + LoRA 真·RLCD 校准蒸馏**（2026-09-20
真·小 Jev 批转正；前代 Qwen2.5-0.5B 硬 CE 见 git 历史）。单前向并行选项打分——
答案位 logits 只对选项 token 集合做 softmax，一次前向出类型化决策 + 校准概率
（与训练侧 train_qwen_decider.py 同一形状；RLCD = 教师 20 票频率分布 KL 蒸馏，
校准内生，规格温度≈1.2-1.3 接近无需后校正）。

定位：L0（l0_router，字符 n-gram 线性模型）的**语义增强上级**——同一打分契约
（score(task, text) → {label, confidence, probs}），任务覆盖 binary 写闸 /
prelim 检索闸（route3 自本批退役：生产 route3 通道本就 prefer L0，decider.route3
从无出场机会的死代码——单通道原则；`score("route3")` 现回 None 自动落 L0）。
调用方经 `score_with_fallback` 走降级链：decider → L0 → None（LLM 共识兜底）。

纪律：
- 可选依赖（torch/transformers/peft 与 requirements-embeddings 同一 envelope）；
  缺依赖/缺工件/无 GPU（env 未强制 CPU）一律 fail-open 回 None，绝不成为新单点。
- 基础模型走 modelscope/HF 缓存（与 embedding 模型同一 fetch 模式），
  适配器与 decider_spec.json 随包（router_models/decider/）。
- chat 模板：Qwen3 混合思考模板必须 `enable_thinking=False`（否则答案位落在
  think 块内，判的是思考内容分布）；老模板静默忽略该 kwarg。
- env：`BIODATA_DECIDER` = auto（默认，可用即用）| off（跳过 decider 直落 L0）
  | only（只用 decider，缺席则不回落 L0——评测用）；
  `BIODATA_DECIDER_DEVICE` = cuda（默认）| cpu（强制 CPU——慢，仅供无卡环境验证）。
- GGUF 量化面（服务器 CPU 部署选项，未投产）：Q4_K_M 是唯一可用档（Q3 起悬崖式
  崩坏，见 研究/真小Jev-RLCD蒸馏批-2026-09-19.md §5.4 margin 定律）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import l0_router as _l0

_SPEC_PATH = Path(__file__).resolve().parent / "router_models" / "decider" / "decider_spec.json"
_BASE_DEFAULT = "Qwen/Qwen3-1.7B"

_lock = threading.Lock()
_cache: dict[str, Any] = {"state": None, "model": None, "tok": None, "spec": None}


def _env() -> str:
    return os.environ.get("BIODATA_DECIDER", "").strip().lower() or "auto"


def _device_ok() -> bool:
    forced = os.environ.get("BIODATA_DECIDER_DEVICE", "").strip().lower()
    if forced == "cpu":
        return True
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _load() -> tuple[Any, Any, dict] | None:
    """懒加载（tokenizer + 基底 + LoRA 合并 + spec）；任何一步失败 → None（fail-open）。"""
    with _lock:
        if _cache["state"] is not None:
            return _cache["state"] and (_cache["model"], _cache["tok"], _cache["spec"]) or None
        bundle = None
        try:
            if not _device_ok():
                raise RuntimeError("no usable device")
            import torch  # noqa: F401
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import PeftModel
            spec = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
            if str(spec.get("format") or "") != "decider-router-v1":
                raise RuntimeError("spec format mismatch")
            try:
                from modelscope import snapshot_download
                base_path = snapshot_download(str(spec.get("base_model") or _BASE_DEFAULT))
            except Exception:
                base_path = str(spec.get("base_model") or _BASE_DEFAULT)
            tok = AutoTokenizer.from_pretrained(base_path)
            tok.pad_token = tok.eos_token
            device = "cpu" if os.environ.get(
                "BIODATA_DECIDER_DEVICE", "").strip().lower() == "cpu" else "cuda"
            base = AutoModelForCausalLM.from_pretrained(
                base_path, dtype=torch.bfloat16).to(device)
            model = PeftModel.from_pretrained(base, _SPEC_PATH.parent / "adapter").to(device)
            model.eval()
            bundle = (model, tok, spec)
        except Exception:
            bundle = None
        if bundle is not None:
            _cache.update({"model": bundle[0], "tok": bundle[1], "spec": bundle[2]})
        _cache["state"] = bundle is not None
        return bundle


def reset_cache() -> None:
    """测试专用：清空模型缓存。"""
    with _lock:
        _cache.update({"state": None, "model": None, "tok": None, "spec": None})


def available() -> bool:
    return _load() is not None


def score(task: str, text: str) -> dict[str, Any] | None:
    """Jev 形状打分：task ∈ {"binary","route3","prelim"}；不可用/异常 → None。"""
    bundle = _load()
    if bundle is None:
        return None
    model, tok, spec = bundle
    tspec = (spec.get("tasks") or {}).get(task)
    if not tspec:
        return None
    try:
        import torch
        msgs = [{"role": "system", "content": spec["system"]},
                {"role": "user", "content": tspec["prompt"].format(utt=str(text or ""))}]
        # enable_thinking=False：Qwen3 混合思考模板必须显式关思考，否则答案位落在
        # think 块内（判的是思考内容分布）；老模板（Qwen2.5）静默忽略该 kwarg，零影响。
        ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True,
                                      return_tensors="pt", enable_thinking=False
                                      ).to(model.device)
        with torch.no_grad(), torch.autocast(model.device.type, dtype=torch.bfloat16,
                                             enabled=model.device.type == "cuda"):
            logits = model(input_ids=ids).logits[0, -1, :].float()
        options = list(tspec["options"])
        ids_opt = [int(tspec["option_token_ids"][o]) for o in options]
        temp = max(float(tspec["temperature"]), 1e-3)
        probs = torch.softmax(logits[ids_opt] / temp, dim=-1).cpu().tolist()
        best = max(range(len(probs)), key=lambda k: probs[k])
        return {"label": options[best], "confidence": probs[best],
                "probs": {o: probs[k] for k, o in enumerate(options)},
                "impl": "decider"}
    except Exception:
        return None


_L0_NAME = {"binary": _l0.ROUTE_BINARY, "route3": _l0.ROUTE_THREE_CLASS,
            "prelim": _l0.PRELIM_GATE}


def operating_tau(task: str, env_var: str, key: str, default: float) -> float:
    """工作点阈值（decider spec 内嵌）：env 显式覆盖 > spec operating_points > default。"""
    raw = os.environ.get(env_var, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            return default
    bundle = _load()
    if bundle is not None:
        try:
            return float(bundle[2]["operating_points"][task][key])
        except (KeyError, TypeError, ValueError):
            pass
    return default


def score_with_fallback(task: str, text: str,
                        prefer: tuple[str, ...] = ("decider", "l0")
                        ) -> dict[str, Any] | None:
    """降级链唯一出口：按 prefer 顺序取第一个可用判决（auto 模式 decider 缺席
    自动落 L0）；`impl` 字段如实标注判决实现。`BIODATA_DECIDER=off` 时 decider
    跳过（无论 prefer）；=only 时只许 decider（缺席 → None）。"""
    mode = _env()
    for impl in prefer:
        if impl == "decider":
            if mode == "off":
                continue
            hit = score(task, text)
            if hit is not None:
                return hit
            if mode == "only":
                return None
        else:
            if mode == "only":
                continue
            hit = _l0.score(_L0_NAME[task], text)
            if hit is not None:
                hit = dict(hit)
                hit["impl"] = "l0"
                return hit
    return None


def tau_with_fallback(task: str, env_var: str, key: str, impl: str | None,
                      default: float) -> float:
    """按实际判决实现取 τ：impl=decider → spec 工作点；impl=l0 → L0 工件工作点。"""
    if os.environ.get(env_var, "").strip():
        return operating_tau(task, env_var, key, default)
    if impl == "decider":
        tau = operating_tau(task, "", key, float("nan"))
        if tau == tau:  # not nan
            return tau
        return default
    return _l0.operating_tau(_L0_NAME[task], env_var, key, default)
