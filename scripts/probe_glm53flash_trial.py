"""验证 LLM provider 在工具调用链路关键参数下的真实行为。

key 从项目 .env.zhipu 读取（只读、绝不打印）。探 4 种组合：
  A. 裸对话（无 tools、无 thinking 参数）
  B. tools + tool_choice=required + thinking=disabled（现行行为，预期 400）
  C. tools + tool_choice=required，不发 thinking
  D. tools + tool_choice=auto，不发 thinking
只打印 HTTP 状态、finish_reason、是否产生 tool_calls、错误摘要（截断）。
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "glm-5.3-flash"


def load_key() -> str:
    # 优先进程环境（服务器容器内跑时直接读 BIODATA_EMBED_API_KEY）；
    # 本地跑时回落项目 .env.zhipu 的智谱 key。只读不打印。
    env_key = os.environ.get("BIODATA_EMBED_API_KEY", "").strip()
    if env_key:
        return env_key
    for line in (ROOT / ".env.zhipu").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() in ("ZHIPUAI_API_KEY", "ZAI_API_KEY", "LLM_API_KEY"):
            return v.strip()
    raise SystemExit("no zhipu key found")


def probe(tag: str, payload: dict) -> None:
    payload = {"model": MODEL, "max_tokens": 2000, **payload}
    req = urllib.request.Request(
        url=f"{BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "")[:80].replace("\n", " ")
        reasoning = (msg.get("reasoning_content") or "")
        usage = data.get("usage") or {}
        print(f"[{tag}] 200 finish={choice.get('finish_reason')} "
              f"tool_calls={len(tool_calls)} reasoning_chars={len(reasoning)} "
              f"completion_tokens={usage.get('completion_tokens')} content={content!r}")
        if tool_calls:
            fn = tool_calls[0].get("function") or {}
            print(f"      first_tool={fn.get('name')} args={(fn.get('arguments') or '')[:100]}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="ignore")[:300]
        print(f"[{tag}] HTTP {exc.code}: {detail}")
    except Exception as exc:
        print(f"[{tag}] ERROR {type(exc).__name__}: {exc}")


KEY = load_key()

MESSAGES = [
    {"role": "system", "content": "你是一位资深的生物信息学数据策展专家 The 10x Curator。"},
    {"role": "user", "content": "用一句话介绍 10x Visium 技术。"},
]

TOOLS = [{
    "type": "function",
    "function": {
        "name": "emit_filters",
        "description": "输出检索过滤条件",
        "parameters": {
            "type": "object",
            "properties": {"tissue": {"type": "string"}},
            "required": ["tissue"],
        },
    },
}]
TOOL_MESSAGES = [
    {"role": "user", "content": "我要找小鼠脑组织的空间转录组数据，调用工具给出过滤条件。"},
]

probe("A plain", {"messages": MESSAGES, "temperature": 0.2})
probe("B required+thinking_off", {
    "messages": TOOL_MESSAGES, "tools": TOOLS,
    "tool_choice": "required", "thinking": {"type": "disabled"}, "temperature": 0.2,
})
probe("C required", {
    "messages": TOOL_MESSAGES, "tools": TOOLS,
    "tool_choice": "required", "temperature": 0.2,
})
probe("D auto", {
    "messages": TOOL_MESSAGES, "tools": TOOLS,
    "tool_choice": "auto", "temperature": 0.2,
})
