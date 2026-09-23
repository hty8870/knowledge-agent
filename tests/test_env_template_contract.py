# -*- coding: utf-8 -*-
"""契约测试：钉死「使 provider-key 残留缺口不可达」的配置前提。

背景（webapp.py:463-467 自陈的已知残留）：仅当①服务器用**通用名** `LLM_API_KEY`（非 provider 专名）
②**未固定** `LLM_BASE_URL` ③请求切到异 provider 且不带 key —— 这一非常规配置下，通用 key 才可能被
送往该 provider 的官方默认端点。验证结论是「现网两套 .env 模板均用 provider 专名 key 或固定 base_url，
故不可达」。但那句「不可达」此前**没有任何门守住**：谁哪天把模板改成通用 key + 不固定 base_url，就静默
捅开这个安全边界，而 webapp 热路径的运行期分支没变、没有测试会红。

本测把「使缺口不可达」的前提显性化为门（红队+务实组共识：不动 webapp 热路径的 masking 分支，避免为
一个当前不可达的低危缺口给每请求都过的安全代码引入回归；改成钉死前提的契约测试）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _active_assignments(text: str) -> dict[str, str]:
    """解析未注释的 KEY=value 行（# 开头或空行忽略；剥离 `export ` 前缀——python-dotenv 会认它，
    否则 `export LLM_API_KEY=x` 会被当成键名 `export LLM_API_KEY`、让真正开着缺口的模板骗过校验）。
    只看生效配置，不看注释里的示例。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        out[key] = value.strip()
    return out


def test_env_templates_keep_provider_key_gap_unreachable() -> None:
    templates = sorted(ROOT.glob(".env*.example"))
    assert templates, "未发现任何 .env*.example 模板（应至少有 .env.example / .env.zhipu.example）"
    names = {t.name for t in templates}
    assert {".env.example", ".env.zhipu.example"} <= names, f"缺少已知模板：现有 {sorted(names)}"

    for tpl in templates:
        active = _active_assignments(tpl.read_text(encoding="utf-8"))
        generic_key = active.get("LLM_API_KEY", "").strip()
        if not generic_key:
            continue  # 未启用通用 key → 缺口前提不成立，跳过
        base_url = active.get("LLM_BASE_URL", "").strip().lower()
        fixed_base_url = base_url.startswith(("http://", "https://"))
        # 唯一真正关上缺口的前提是**固定 LLM_BASE_URL**。不接受「另设一个 provider 专名 key」作为替代：
        # load_llm_config 里通用 LLM_API_KEY 优先级最高（llm_client 先取它），切异 provider 且不带 key/base_url
        # 时用的仍是通用 key、仍会流向该 provider 官方默认端点——专名 key 挡不住它（验证证伪）。
        assert fixed_base_url, (
            f"{tpl.name}: 用通用 LLM_API_KEY 却未固定 LLM_BASE_URL —— 会捅开 webapp.py:463-467 记录的 "
            "provider-key 残留缺口（切异 provider 不带 key 时通用 key 可能被送往该 provider 官方默认端点）。"
            "请固定 LLM_BASE_URL（另设 provider 专名 key 不足以关上此缺口，因通用 LLM_API_KEY 优先级最高）。"
        )
