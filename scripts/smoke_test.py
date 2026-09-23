from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, RecommendParams  # noqa: E402


TABLE_HEADER = "| 数据集名称 | 物种 | 组织 | 疾病 | 技术方案 | 样本量 | 原始数据状态 | 下载链接 |"
TABLE_SEPARATOR = "|---|---|---|---|---|---|---|---|"
LINK_CELL_PATTERN = re.compile(r"^\[点击下载\]\((https?://[^)\s]+)\)$")


def fail(message: str) -> None:
    print(f"SMOKE TEST FAILED: {message}")
    raise SystemExit(1)


def run_cli(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(PROJECT_ROOT / "src" / "dataset_recommender" / "app" / "cli.py"), *args]
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if proc.returncode != 0:
        fail(f"CLI command failed: {' '.join(args)}\nSTDERR:\n{proc.stderr}")
    return proc


def extract_table_rows(markdown: str) -> list[list[str]]:
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    if len(table_lines) < 3:
        return []
    rows: list[list[str]] = []
    for line in table_lines[2:]:
        cols = [part.strip() for part in line.split("|")[1:-1]]
        if len(cols) == 8:
            rows.append(cols)
    return rows


def extract_answer_body(text: str) -> str:
    if text.startswith("Pipeline:") and "\n\n" in text:
        return text.split("\n\n", 1)[1]
    return text


def validate_markdown_table_integrity(markdown: str) -> tuple[bool, str]:
    if TABLE_HEADER not in markdown:
        return False, "missing table header"

    if len(re.findall(r"\[点击下载\]\(", markdown)) != len(re.findall(r"\[点击下载\]\(https?://[^)\s]+\)", markdown)):
        return False, "contains unclosed markdown link"
    if re.search(r"\[点击下载\]\([^\)]*$", markdown):
        return False, "contains trailing unclosed markdown link"

    lines = markdown.splitlines()
    header_index = -1
    for i, line in enumerate(lines):
        if line.strip() == TABLE_HEADER:
            header_index = i
            break
    if header_index < 0:
        return False, "cannot locate table header line"

    separator_index = -1
    for i in range(header_index + 1, len(lines)):
        if not lines[i].strip():
            continue
        separator_index = i
        break
    if separator_index < 0 or lines[separator_index].strip() != TABLE_SEPARATOR:
        return False, "missing table separator"

    row_count = 0
    for i in range(separator_index + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            if row_count > 0:
                break
            continue
        if not stripped.startswith("|"):
            if row_count > 0:
                break
            continue
        if not stripped.endswith("|"):
            return False, f"incomplete table row at line {i + 1}"
        cols = [part.strip() for part in stripped.split("|")[1:-1]]
        if len(cols) != 8:
            return False, f"invalid column count {len(cols)} at line {i + 1}"
        link_cell = cols[7]
        if not LINK_CELL_PATTERN.fullmatch(link_cell):
            return False, f"invalid markdown link cell at line {i + 1}: {link_cell}"
        row_count += 1

    if row_count == 0:
        return False, "no table rows"

    return True, ""


def is_mixed_value(value: str) -> bool:
    chunks = [part.strip() for part in re.split(r"[,;/|]+", value) if part.strip()]
    return len(chunks) > 1


def assert_sample_size_format(sample_size: str, label: str) -> None:
    value = sample_size.strip()
    if value in {"Cells", "Spots", "Nuclei"}:
        fail(f"{label} 样本量列不能只显示单位: {value}")

    if "Spots" in value and not re.match(r"^(未说明|\d[\d,]*)\s+Spots$", value):
        fail(f"{label} 样本量 Spots 格式错误: {value}")
    if "Cells" in value and not re.match(r"^(未说明|\d[\d,]*)\s+Cells$", value):
        fail(f"{label} 样本量 Cells 格式错误: {value}")
    if "Nuclei" in value and not re.match(r"^(未说明|\d[\d,]*)\s+Nuclei$", value):
        fail(f"{label} 样本量 Nuclei 格式错误: {value}")


def assert_rows_sample_size_format(rows: list[list[str]], label: str) -> None:
    for row in rows:
        assert_sample_size_format(row[5], label)


def assert_prompt_preview(workflow: DatasetRecommendationWorkflow) -> None:
    prompt = workflow.build_prompt_preview("请帮我推荐经典的人类乳腺癌数据", top_k=5)
    required_tokens = [
        "The 10x Curator",
        "User Query",
        "Retrieved Data",
        "Human",
        "Breast",
        "has_raw_data",
        "FASTQ",
        "Markdown",
        "Nuclei",
        "Translated Keywords",
    ]
    for token in required_tokens:
        if token not in prompt:
            fail(f"--print-prompt 缺少关键字段: {token}")
    if "抱歉，数据库中未检索到符合【" not in prompt:
        fail("--print-prompt 的 Fallback 部分必须包含 `符合【`")
    if "抱歉，数据库中未检索到符合条件的数据。" in prompt:
        fail("--print-prompt 的 Fallback 出现了无英文关键词的回归文案")
    if "Human Breast Breast Cancer Cancer" in prompt:
        fail("--print-prompt 出现重复英文关键词: Human Breast Breast Cancer Cancer")
    if "species=Human" not in prompt and "Human Breast Cancer" not in prompt:
        fail("--print-prompt 未显示清洗后的英文关键词（species=Human 或 Human Breast Cancer）")


def assert_fallback_keywords(workflow: DatasetRecommendationWorkflow) -> None:
    # 重构后：无结果走结构化诊断（设计 spec：说明缺失条件 + 给出放宽建议），不再是旧的模板前缀。
    q = "推荐有 FASTQ 的小鼠心脏空间转录组数据"
    result = workflow.run(q, top_k=5, use_llm=False)
    if TABLE_HEADER in result:
        fail("无结果场景应触发 fallback，但返回了表格")
    if "抱歉" not in result:
        fail("无结果 fallback 缺少『抱歉』前缀")
    # spec 硬要求：无结果时必须给出可放宽建议
    if not any(tok in result for tok in ("放宽", "去掉", "可尝试")):
        fail("无结果 fallback 缺少放宽建议（spec 要求：说明缺失条件 + 放宽建议）")


def assert_missing_key_pipeline_status() -> None:
    env = dict(os.environ)
    env["ENABLE_LLM"] = "true"
    env["MOCK_LLM"] = "false"
    env["OPENAI_API_KEY"] = ""  # 置空以稳健模拟“无 key”（避免子进程从 .env 重载）
    env["LLM_API_KEY"] = ""

    proc = run_cli(
        [
            "--query",
            "请帮我推荐经典的人类乳腺癌数据",
            "--show-pipeline",
            "--use-llm",
            "--provider",
            "openai-compatible",
        ],
        env=env,
    )
    output = proc.stdout
    required = [
        "LLM attempted:",
        "LLM succeeded:",
        "LLM response used:",
        "Fallback reason:",
        "missing",
    ]
    for token in required:
        if token not in output:
            fail(f"--use-llm 缺 key 时缺少状态字段: {token}")


def assert_healthcheck_runs() -> None:
    env = dict(os.environ)
    env["ENABLE_LLM"] = "true"
    env["MOCK_LLM"] = "false"
    env["OPENAI_API_KEY"] = ""  # 置空以稳健模拟“无 key”（避免子进程从 .env 重载）
    env["LLM_API_KEY"] = ""

    proc = run_cli(["--llm-healthcheck"], env=env)
    output = proc.stdout
    required = [
        "LLM healthcheck",
        "Connection:",
        "Error:",
    ]
    for token in required:
        if token not in output:
            fail(f"--llm-healthcheck 输出缺失: {token}")


def assert_healthcheck_use_llm_runs() -> None:
    env = dict(os.environ)
    env["MOCK_LLM"] = "false"
    env["OPENAI_API_KEY"] = ""  # 置空以稳健模拟“无 key”（避免子进程从 .env 重载）
    env["LLM_API_KEY"] = ""

    proc = run_cli(["--llm-healthcheck", "--use-llm"], env=env)
    output = proc.stdout
    if "LLM healthcheck" not in output:
        fail("--llm-healthcheck --use-llm 未输出 healthcheck 标题")
    if not any(status in output for status in ["Connection: skipped", "Connection: failed", "Connection: success"]):
        fail("--llm-healthcheck --use-llm 缺少合法 Connection 状态")
    if "Effective API key:" not in output:
        fail("--llm-healthcheck --use-llm 应输出 Effective API key 字段")


def assert_zhipu_env_templates() -> None:
    env_zhipu_example = PROJECT_ROOT / ".env.zhipu.example"
    if not env_zhipu_example.exists():
        fail(".env.zhipu.example 不存在")

    def _parse_env(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    values = _parse_env(env_zhipu_example)
    required = {
        "LLM_PROVIDER": "zhipuai",
        "LLM_API_KEY": None,
        "LLM_BASE_URL": None,
        "LLM_MODEL": None,
    }
    for key, expected in required.items():
        raw_value = (values.get(key) or "").strip()
        if not raw_value:
            fail(f"{env_zhipu_example.name} 的 {key} 不能为空")
        if expected is not None and raw_value != expected:
            fail(f"{env_zhipu_example.name} 的 {key} 应为 {expected}")

    template_text = env_zhipu_example.read_text(encoding="utf-8")
    for alias in ["ZAI_API_KEY", "ZHIPUAI_API_KEY", "ZHIPUAI_TOKEN"]:
        if f"{alias}=" not in template_text:
            fail(f"{env_zhipu_example.name} 缺少旧 Key 别名的兼容说明: {alias}")


def assert_zhipu_healthcheck() -> None:
    proc = run_cli(["--llm-healthcheck", "--provider", "zhipuai"])
    output = proc.stdout
    required = [
        "LLM healthcheck",
        "LLM_PROVIDER: zhipuai",
        "ZHIPUAI_BASE_URL:",
        "ZHIPUAI_MODEL:",
        "ZAI_API_KEY:",
        "ZHIPUAI_API_KEY:",
        "ZHIPUAI_TOKEN:",
        "Effective API key:",
        "Connection:",
    ]
    for token in required:
        if token not in output:
            fail(f"--llm-healthcheck --provider zhipuai 输出缺失: {token}")


def assert_zhipu_healthcheck_use_llm() -> None:
    proc = run_cli(["--llm-healthcheck", "--use-llm", "--provider", "zhipuai"])
    output = proc.stdout
    required = [
        "LLM_PROVIDER: zhipuai",
        "ZHIPUAI_BASE_URL:",
        "ZHIPUAI_MODEL:",
        "Effective API key:",
        "Connection:",
        "Error:",
    ]
    for token in required:
        if token not in output:
            fail(f"--llm-healthcheck --use-llm --provider zhipuai 输出缺失: {token}")
    if not any(status in output for status in ["Connection: skipped", "Connection: failed", "Connection: success"]):
        fail("--llm-healthcheck --use-llm --provider zhipuai 缺少合法 Connection 状态")


def assert_zhipu_pipeline_fallback_behavior() -> None:
    proc = run_cli(
        [
            "--query",
            "请帮我推荐经典的人类乳腺癌数据",
            "--show-pipeline",
            "--use-llm",
            "--provider",
            "zhipuai",
        ]
    )
    output = proc.stdout
    if "LLM provider: zhipuai" not in output:
        fail("--show-pipeline --use-llm --provider zhipuai 未显示 provider=zhipuai")
    if "LLM attempted:" not in output or "LLM succeeded:" not in output or "LLM response used:" not in output:
        fail("--show-pipeline 缺少 LLM 状态字段")
    if "LLM succeeded: true" in output and "LLM response used: true" not in output:
        fail("出现 LLM succeeded: true 但 LLM response used 不是 true")
    if "LLM response used: false" in output and "Fallback reason:" not in output:
        fail("LLM 未使用时应显示 Fallback reason")
    if "LLM response used: true" in output:
        answer = extract_answer_body(output)
        ok, reason = validate_markdown_table_integrity(answer)
        if not ok:
            fail(f"LLM response used=true 但输出表格不完整: {reason}")


def assert_network_diagnose_runs() -> None:
    proc = run_cli(["--network-diagnose", "--provider", "zhipuai"])
    output = proc.stdout
    required = [
        "Network diagnose",
        "Provider: zhipuai",
        "DNS:",
        "TCP 443:",
        "HTTPS:",
        "Proxy detected:",
        "API request:",
    ]
    for token in required:
        if token not in output:
            fail(f"--network-diagnose --provider zhipuai 输出缺失: {token}")
    if not any(status in output for status in ["API request: success", "API request: failed", "API request: skipped"]):
        fail("--network-diagnose --provider zhipuai 缺少合法 API request 状态")


def assert_mock_llm_chain(workflow: DatasetRecommendationWorkflow) -> None:
    query = "请帮我推荐经典的人类乳腺癌数据"
    meta = workflow.run_with_meta(
        RecommendParams(query=query, top_k=5, use_llm=True, mock_llm=True))
    rows = extract_table_rows(meta.answer)
    if not rows:
        fail("mock LLM 链路未返回有效 Markdown 表格")

    retrieved_names = {name.strip().lower() for name in meta.retrieved_dataset_names}
    if not retrieved_names:
        fail("mock LLM 缺少 retrieved candidates 调试信息")

    for row in rows:
        row_name = row[0].strip().lower()
        if row_name not in retrieved_names:
            fail(f"mock LLM 输出包含非 Retrieved Data 数据集: {row[0]}")

    proc = run_cli(
        ["--query", query, "--show-pipeline", "--mock-llm", "--use-llm"]
    )
    output = proc.stdout
    required = [
        "LLM mode: enabled",
        "LLM provider: mock",
        "Prompt: The 10x Curator",
        "LLM attempted: true",
        "LLM succeeded: true",
        "LLM response used: true",
        TABLE_HEADER,
    ]
    for token in required:
        if token not in output:
            fail(f"mock LLM CLI 输出缺少: {token}")
    answer = extract_answer_body(output)
    ok, reason = validate_markdown_table_integrity(answer)
    if not ok:
        fail(f"mock LLM 输出表格不完整: {reason}")


def assert_fastq_query_with_mock_llm() -> None:
    proc = run_cli(
        ["--query", "推荐有 FASTQ 的人类乳腺癌数据", "--show-pipeline", "--mock-llm", "--use-llm"]
    )
    output = proc.stdout
    if "LLM response used: true" not in output:
        fail("FASTQ 查询 mock LLM 模式应使用 LLM 输出")
    answer = extract_answer_body(output)
    ok, reason = validate_markdown_table_integrity(answer)
    if not ok:
        fail(f"FASTQ 查询 mock LLM 输出表格不完整: {reason}")
    if "❌ 无 FASTQ" in answer:
        fail("FASTQ 查询 mock LLM 输出包含 ❌ 无 FASTQ")


def assert_workflow_uses_llm_client() -> None:
    llm_client_path = PROJECT_ROOT / "src" / "dataset_recommender" / "llm" / "llm_client.py"
    workflow_path = PROJECT_ROOT / "src" / "dataset_recommender" / "app" / "workflow.py"
    if not llm_client_path.exists():
        fail("缺少统一 LLM API 文件: llm_client.py")

    workflow_text = workflow_path.read_text(encoding="utf-8")
    if "from ..llm.llm_client import" not in workflow_text:
        fail("workflow.py 未通过 llm_client.py 调用 LLM")
    if "call_llm(" not in workflow_text:
        fail("workflow.py 未调用 llm_client.call_llm")

    llm_client_text = llm_client_path.read_text(encoding="utf-8")
    required_defs = [
        "def call_llm",
        "def call_openai_compatible",
        "def call_mock_llm",
        "def call_zhipuai",
    ]
    for marker in required_defs:
        if marker not in llm_client_text:
            fail(f"llm_client.py 缺少关键函数: {marker}")
    if "if provider == \"zhipuai\":" not in llm_client_text or "call_zhipuai(" not in llm_client_text:
        fail("call_llm 未根据 provider=zhipuai 分发到 call_zhipuai")


def main() -> int:
    workflow = DatasetRecommendationWorkflow()

    q1 = "请帮我推荐经典的人类乳腺癌数据"
    q2 = "有没有小鼠脑组织空间转录组数据"
    q3 = "推荐有 FASTQ 的人类乳腺癌数据"

    result1 = workflow.run(q1, top_k=5, use_llm=False)
    result2 = workflow.run(q2, top_k=5, use_llm=False)
    result3 = workflow.run(q3, top_k=5, use_llm=False)

    if TABLE_HEADER not in result1:
        fail("查询1输出缺少 Markdown 表头")
    if "Human" not in result1 or "Breast" not in result1:
        fail("查询1输出必须同时包含 Human 和 Breast")
    rows1 = extract_table_rows(result1)
    if not rows1:
        fail("查询1未解析到有效表格行")
    ok, reason = validate_markdown_table_integrity(result1)
    if not ok:
        fail(f"查询1表格完整性校验失败: {reason}")
    first1 = rows1[0]
    first_name = first1[0].lower()
    first_tissue = first1[2]
    if is_mixed_value(first_tissue) and "breast" in first_tissue.lower():
        fail("查询1第一条结果是混合组织（含 Breast 但非纯 Breast）")
    preferred_first = (
        ("breast" in first_tissue.lower() and not is_mixed_value(first_tissue))
        or ("human breast cancer" in first_name)
        or ("breast cancer" in first_name)
    )
    if not preferred_first:
        fail("查询1第一条结果未体现 Breast/Breast Cancer 优先")
    assert_rows_sample_size_format(rows1, "查询1")

    if "Mouse" not in result2 or "Brain" not in result2:
        fail("查询2输出必须同时包含 Mouse 和 Brain")
    if "Visium" not in result2 and "Spatial" not in result2:
        fail("查询2输出必须包含 Visium 或 Spatial")
    if "Spots" not in result2:
        fail("查询2输出必须包含 Spots")
    rows2 = extract_table_rows(result2)
    if not rows2:
        fail("查询2未解析到有效表格行")
    ok, reason = validate_markdown_table_integrity(result2)
    if not ok:
        fail(f"查询2表格完整性校验失败: {reason}")
    assert_rows_sample_size_format(rows2, "查询2")

    if TABLE_HEADER in result3:
        if "❌ 无 FASTQ" in result3:
            fail("查询3要求 FASTQ，但结果中出现 ❌ 无 FASTQ")
        if "✅ 包含 FASTQ" not in result3:
            fail("查询3结果中未出现 ✅ 包含 FASTQ")
        if "Human" not in result3 or "Breast" not in result3:
            fail("查询3输出必须同时包含 Human 和 Breast")
        if "Nuclei" in result3 and "“Nuclei” 指单核测序或细胞核层面的计数单位" not in result3:
            fail("查询3出现 Nuclei 但缺少 Nuclei 解释")
        rows3 = extract_table_rows(result3)
        if not rows3:
            fail("查询3未解析到有效表格行")
        ok, reason = validate_markdown_table_integrity(result3)
        if not ok:
            fail(f"查询3表格完整性校验失败: {reason}")
        assert_rows_sample_size_format(rows3, "查询3")
    else:
        if "抱歉，数据库中未检索到符合" not in result3:
            fail("查询3非表格输出但未按 fallback 格式返回")

    assert_prompt_preview(workflow)
    assert_fallback_keywords(workflow)
    assert_missing_key_pipeline_status()
    assert_healthcheck_runs()
    assert_healthcheck_use_llm_runs()
    assert_zhipu_env_templates()
    assert_zhipu_healthcheck()
    assert_zhipu_healthcheck_use_llm()
    assert_network_diagnose_runs()
    assert_zhipu_pipeline_fallback_behavior()
    assert_mock_llm_chain(workflow)
    assert_fastq_query_with_mock_llm()
    assert_workflow_uses_llm_client()

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
