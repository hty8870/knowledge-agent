# -*- coding: utf-8 -*-
"""锚定的 secret 值模式 + 扫描/脱敏助手（单一真源，交付复核、质量门 report 脱敏、
MCP 调用留痕值级脱敏与 desktop_launcher 日志脱敏四处共用）。

2026-08-10：自 `scripts/secret_patterns.py` 提升为 src 公共模块——
mcp_server 等 src 侧消费方不该反向依赖 scripts/（分层方向：scripts → src，永不反转）。
`scripts/secret_patterns.py` 保留兼容壳重导出，既有引用点零改动。

设计约束（三方对抗评审共识）：
1. **只用强锚定的服务商/云凭据前缀，绝不用裸熵 / 通用 hex 正则。** 本仓库交付集本身就是
   md5 / SHA-256 / 大小 元数据目录（release-manifest 满是 hex 摘要）；任何"高熵串/像密钥的 hex"
   正则会在交付物自己的核心数据上**持续误报**，几次假阳后这道门就没人信了。
2. **`sk-` 的 body 只含 alnum（不含连字符）**，因为内部任务编号命名规约是 `<tool>-<task-slug>-<id>`，
   "ta**sk-**slug-…" 含 `sk-`；alnum-only body 让 "slug" 后的连字符断开匹配，避开这个高频误报。
   这些前缀（sk-/ghp_/AKIA/AIza/xox*-/eyJ）都不会出现在 md5/sha256/task-slug 里，近零误报。
3. **只报 pattern_id + file:line，绝不回显命中的实际子串**——否则把真 secret 写进 CI/stderr 日志
   就是二次泄漏。redact() 也只把命中值替换成 [REDACTED:<id>]，不保留原值。

这些模式定义字符串本身**不自匹配**（前缀后紧跟 `[`，不是 alnum/base64），故本文件被交付扫描时不会自报。

边界：本模块只做**强锚定**值模式。desktop_launcher 的日志脱敏以本表为第一遍，
其上再叠更宽的日志形态网（key=value / Bearer / Basic / URL userinfo）——那边的
误报代价只是日志里多一个 `<redacted>`，与交付扫描的「假阳几次门就没人信」不同量级。
"""
from __future__ import annotations

import re

# (pattern_id, compiled_regex)
SECRET_VALUE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # 现代带连字符前缀的 LLM key（Anthropic sk-ant-…、OpenAI sk-proj-/sk-svcacct-/sk-admin-…）：
    # 前缀 sk-ant-/sk-proj-/… 足够独特（绝不出现在 task-slug 里），故 body 允许连字符/下划线（base64url）。
    # 本项目以 Claude/LLM 为核心，这些恰是最可能被误粘的 key，必须单列——legacy 纯 alnum 模式抓不到它们。
    ("scoped-llm-key", re.compile(r"sk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_-]{20,}")),
    # legacy 纯 alnum OpenAI key（body 只含 alnum，避免 ta+sk-slug 命名规约误报）
    ("openai-secret-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"gh[oprsu]_[A-Za-z0-9]{36}")),
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}")),
    # 智谱 key 是 `<32位小写hex>.<16位alnum>` 两段式：点号+定长分段是锚。裸 md5 没有点号第二段、
    # `md5.json` 第二段只有 4 位够不到 16、sha256 无点号，都不会误报。
    ("zhipu-api-key", re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9]{16}")),
    # HuggingFace token：`hf_` 前缀 + 34 位 alnum（定长，与 github-token 同风格）。
    # `hf_tooshort` 长度不够、`hf_model_download` 的下划线在 alnum-only body 处断开，均不误报。
    ("huggingface-token", re.compile(r"hf_[A-Za-z0-9]{34}")),
    # PEM 私钥头：`PRIVATE KEY` 字面是锚（RSA/EC/OPENSSH 等私钥头都拦）；
    # PUBLIC KEY / CERTIFICATE 头不含该字面，天然不匹配。
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
]


def find_secret_patterns(text: str) -> set[str]:
    """返回文本命中的 pattern_id 集合（**不含**命中的实际值）。"""
    hits: set[str] = set()
    for pattern_id, rx in SECRET_VALUE_PATTERNS:
        if rx.search(text):
            hits.add(pattern_id)
    return hits


def redact(text: str | None) -> str | None:
    """把命中的 secret 值替换成 [REDACTED:<pattern_id>]；None/空原样返回。"""
    if not text:
        return text
    for pattern_id, rx in SECRET_VALUE_PATTERNS:
        text = rx.sub(f"[REDACTED:{pattern_id}]", text)
    return text
