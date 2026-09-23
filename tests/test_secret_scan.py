# -*- coding: utf-8 -*-
"""锚定 secret 值扫描的正/负样本 + 交付集成 + 「绝不回显命中值」契约。

负样本（防误报）是本测的重点：交付集本身满是 md5/sha256，命名规约里满是 task-slug——
这些**绝不能**命中，否则误报淹没后整道门被禁用/无视。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import secret_patterns as SP  # noqa: E402
import make_delivery as MD  # noqa: E402

# 逼真形状（非真实凭据，仅供模式匹配测试）。全部用拼接构造：使**源码里不出现可被扫描命中的完整字面**，
# 否则本测试文件自身进交付集时会被 make_delivery 的 secret 扫描（真阳性）拦下。
FAKE_OPENAI = "sk-" + "A" * 40                            # legacy 纯 alnum
FAKE_ANTHROPIC = "sk-ant-" + "api03-" + "A" * 60          # 现代带连字符前缀（实现 项目最可能粘的）
FAKE_OPENAI_PROJ = "sk-proj-" + "T3BlbkFJ" + "-" + "x" * 40
FAKE_GHP = "ghp_" + "b" * 36
FAKE_GHR = "ghr_" + "c" * 36                              # GitHub refresh token（曾漏）
FAKE_AKIA = "AKIA" + "IOSFODNN7EXAMPLE"                   # AKIA + 16
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
FAKE_ZHIPU = "0123456789abcdef" * 2 + "." + "Zz9" + "y" * 13   # 32 位小写 hex + "." + 16 位 alnum
FAKE_HF = "hf_" + "Abc123" * 5 + "xy12"                        # hf_ + 34 位 alnum
FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def test_anchored_patterns_match_real_secret_shapes() -> None:
    assert "openai-secret-key" in SP.find_secret_patterns(f"LLM_API_KEY={FAKE_OPENAI}")
    assert "scoped-llm-key" in SP.find_secret_patterns(f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC}")
    assert "scoped-llm-key" in SP.find_secret_patterns(f"OPENAI_API_KEY={FAKE_OPENAI_PROJ}")
    assert "github-token" in SP.find_secret_patterns(f"token: {FAKE_GHP}")
    assert "github-token" in SP.find_secret_patterns(f"GITHUB_TOKEN={FAKE_GHR}")
    assert "aws-access-key-id" in SP.find_secret_patterns(f"id={FAKE_AKIA}")
    assert "jwt" in SP.find_secret_patterns(f"Authorization: Bearer {FAKE_JWT}")


def test_new_patterns_match_zhipu_hf_pem() -> None:
    """zhipu / huggingface / PEM 私钥三个新模式的正样本钉（拼接构造，源码不落完整字面）。"""
    assert "zhipu-api-key" in SP.find_secret_patterns(f"ZHIPU_API_KEY={FAKE_ZHIPU}")
    assert "huggingface-token" in SP.find_secret_patterns(f"token: {FAKE_HF}")
    assert "pem-private-key" in SP.find_secret_patterns(FAKE_PEM + "\nMII...\n-----END "
                                                       + "RSA PRIVATE KEY" + "-----")


def test_no_false_positive_on_repo_native_shapes() -> None:
    """红队点名的高频误报源：task-slug 命名规约、md5、sha256 —— 必须全部 0 命中。"""
    for benign in (
        "claude-eng-hardening-2-e2h9",                # 内部 short-id 命名
        "<tool>-<task-slug>-<short-id>",              # 模板里的字面规约
        "task-slug-negation-constraints-a1b2c3d4e5",  # 含 sk- 子串但 body 有连字符断开
        "d41d8cd98f00b204e9800998ecf8427e",           # md5
        "60f3ebd51da42c9c5a799619b75d0236e5a25aa974fcafc058e7d034428b22fe",  # sha256
        "https://open.bigmodel.cn/api/paas/v4/",      # 正常 URL
        "your_api_key_here",                          # 模板占位
        # 2026-08 新模式的良性探针：短 hf_ 标识、公钥/证书 PEM 头、md5 摘要带扩展名
        "hf_" + "tooshort",
        "hf_model_download",
        "-----BEGIN PUBLIC KEY-----",
        "-----BEGIN OPENSSH PUBLIC KEY-----",
        "-----BEGIN CERTIFICATE-----",
        "d41d8cd98f00b204e9800998ecf8427e" + "." + "json",
    ):
        assert not SP.find_secret_patterns(benign), f"锚定模式误命中良性串：{benign!r}"


def test_redact_removes_value_but_keeps_pattern_id() -> None:
    red = SP.redact(f"key={FAKE_OPENAI} tail")
    assert FAKE_OPENAI not in red, "redact 未移除 secret 值"
    assert "[REDACTED:openai-secret-key]" in red
    assert SP.redact(None) is None and SP.redact("") == ""


def test_scan_secret_values_flags_pasted_key_and_never_echoes_value(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text(f"示例：LLM_API_KEY={FAKE_OPENAI}\n干净的一行\n", encoding="utf-8")
    hits = MD.scan_secret_values([f], root=tmp_path)
    assert hits, "误粘进 .md 的 key 未被交付扫描抓到"
    assert hits[0]["pattern"] == "openai-secret-key" and hits[0]["line"] == 1
    # 契约：命中记录里绝不含真实值（只有 path/line/pattern 三个键）
    for h in hits:
        assert set(h) == {"path", "line", "pattern"}
        assert FAKE_OPENAI not in str(h)


def test_scan_secret_values_clean_on_benign_file(tmp_path: Path) -> None:
    f = tmp_path / "manifest.json"
    f.write_text('{"md5":"d41d8cd98f00b204e9800998ecf8427e","slug":"task-slug-x-a1b2c3d4e5"}\n', encoding="utf-8")
    assert MD.scan_secret_values([f], root=tmp_path) == []


def test_precommit_check_secret_scan_and_py_compile(tmp_path: Path) -> None:
    import precommit_check as PC  # noqa: E402

    leak = tmp_path / "cfg.md"
    leak.write_text(f"LLM_API_KEY={FAKE_OPENAI}\n", encoding="utf-8")
    hits = PC._scan_secrets([leak])
    assert hits and hits[0][2] == "openai-secret-key"

    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    bad = tmp_path / "broken.py"
    bad.write_text("def oops(:\n    pass\n", encoding="utf-8")
    assert PC._compile_py([good]) == []
    assert PC._compile_py([bad]), "broken.py 语法错误应被 pre-commit 编译检查抓到"
