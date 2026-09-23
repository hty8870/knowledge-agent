from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from dataset_recommender.llm.llm_client import diagnose_network, healthcheck, load_llm_config
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, RecommendParams
else:
    from ..llm.llm_client import diagnose_network, healthcheck, load_llm_config
    from .workflow import DatasetRecommendationWorkflow, RecommendParams


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="10x / 生物信息学测序数据集推荐 RAG Workflow")
    parser.add_argument("query", nargs="?", help="自然语言查询，例如：请帮我推荐经典的人类乳腺癌数据")
    parser.add_argument("--query", dest="query_opt", help="自然语言查询")
    parser.add_argument("--top-k", type=int, default=None, help="返回候选数量，默认使用配置值")
    parser.add_argument("--use-llm", action="store_true", help="强制启用 LLM 分支（需配置 API）")
    parser.add_argument("--no-llm", action="store_true", help="强制禁用 LLM，使用规则模式")
    parser.add_argument("--mock-llm", action="store_true", help="启用 mock LLM（不请求网络）")
    parser.add_argument("--show-pipeline", action="store_true", help="显示 workflow pipeline 与 LLM 状态")
    parser.add_argument("--print-prompt", action="store_true", help="仅打印构造后的 The 10x Curator Prompt")
    parser.add_argument("--llm-healthcheck", action="store_true", help="仅检查 LLM 配置与连接状态")
    parser.add_argument("--network-diagnose", action="store_true", help="网络分层诊断（DNS/TCP/HTTPS/API）")
    parser.add_argument("--provider", help="LLM provider: zhipuai / openai-compatible / mock")
    parser.add_argument(
        "--rerank", choices=["off", "llm"], default="off",
        help="可选 AI 重排（默认 off；与推荐说明润色 --use-llm 相互独立。只调整已通过条件筛选的候选顺序，不会放进违反条件的数据）",
    )
    parser.add_argument(
        "--rerank-top-n", type=int, default=None,
        help="启用重排时喂给 LLM 的候选池大小（默认 12；仅在 --rerank llm 下生效）",
    )
    parser.add_argument(
        "--recall", choices=["off", "dense", "cross_encoder", "vector"], default="off",
        help="可选语义重排（默认 off；只调整已通过条件筛选的候选顺序，后端不可用则保持原顺序）。"
             "cross_encoder=本地重排模型 bge-reranker-v2-m3（推荐）；dense=本地稠密嵌入；"
             "vector=语料级向量索引召回（本地索引或 API 向量文件，缺省 RRF 融合）。"
             "本地模型需先装 requirements-embeddings.txt + 跑 scripts/fetch_embedding_model.py",
    )
    parser.add_argument(
        "--recall-alpha", type=float, default=None,
        help="dense/vector 的 linear 融合：稠密分权重 ∈ [0,1]（默认 0.5 词面/稠密融合；1=纯稠密，0=纯词面）",
    )
    parser.add_argument(
        "--strategy", choices=["fixed", "auto"], default="fixed",
        help="排序策略（默认 fixed=按上面显式给的 --recall/--rerank 执行）。auto=按查询宽泛程度（通过筛选的候选条数）自动决定："
             "候选多时按可用性叠加语义重排（偏好 vector > cross_encoder），候选少时保持规则顺序（auto 会覆盖显式给的 --recall/--rerank）",
    )
    parser.add_argument("--output-file", help="将结果写入文件，例如 outputs/result.md")
    return parser


def _print_healthcheck(use_llm: bool | None, mock_llm: bool | None, provider: str | None) -> int:
    from dataset_recommender.app.runtime_paths import get_app_paths

    config = load_llm_config(
        project_root=get_app_paths().config_root,
        provider_override=provider,
    )
    if use_llm is not None:
        config.enable_llm = use_llm
    if provider is not None:
        config.provider = provider.strip().lower()
    if mock_llm is not None:
        config.mock_llm = mock_llm

    report = healthcheck(config)
    lines = ["LLM healthcheck"]
    ordered_keys = [
        "ENABLE_LLM",
        "MOCK_LLM",
        "LLM_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_API_KEY",
        "ZHIPUAI_BASE_URL",
        "ZHIPUAI_MODEL",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "ZHIPUAI_TOKEN",
        "Effective API key",
        "LLM_TIMEOUT",
        "Connection",
        "Error",
        "Hint",
    ]
    for key in ordered_keys:
        if key in report:
            lines.append(f"{key}: {report[key]}")
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


def _print_network_diagnose(use_llm: bool | None, mock_llm: bool | None, provider: str | None) -> int:
    from dataset_recommender.app.runtime_paths import get_app_paths

    config = load_llm_config(
        project_root=get_app_paths().config_root,
        provider_override=provider,
    )
    if use_llm is not None:
        config.enable_llm = use_llm
    if provider is not None:
        config.provider = provider.strip().lower()
    if mock_llm is not None:
        config.mock_llm = mock_llm

    report = diagnose_network(config)
    lines = ["Network diagnose"]
    ordered_keys = [
        "Provider",
        "Target host",
        "Target port",
        "Base URL",
        "Chat completions URL",
        "API key",
        "Model",
        "Timeout",
        "DNS",
        "TCP 443",
        "HTTPS",
        "Proxy detected",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "API request",
        "Error",
    ]
    for key in ordered_keys:
        if key in report:
            lines.append(f"{key}: {report[key]}")
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    use_llm: bool | None
    if args.use_llm and args.no_llm:
        parser.error("--use-llm 和 --no-llm 不能同时使用")
        return 2
    if args.use_llm:
        use_llm = True
    elif args.no_llm:
        use_llm = False
    else:
        use_llm = None

    mock_llm: bool | None = True if args.mock_llm else None
    if args.mock_llm and use_llm is None:
        use_llm = True

    provider = args.provider.strip().lower() if args.provider else None
    if args.mock_llm:
        provider = "mock"

    if args.llm_healthcheck:
        return _print_healthcheck(use_llm=use_llm, mock_llm=mock_llm, provider=provider)
    if args.network_diagnose:
        return _print_network_diagnose(use_llm=use_llm, mock_llm=mock_llm, provider=provider)

    query = args.query_opt or args.query
    if not query:
        parser.error('请提供查询文本，例如 --query "请帮我推荐经典的人类乳腺癌数据"')

    workflow = DatasetRecommendationWorkflow()

    if args.print_prompt:
        prompt = workflow.build_prompt_preview(query=query, top_k=args.top_k)
        payload = prompt + "\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        if args.output_file:
            output_path = Path(args.output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(prompt, encoding="utf-8")
        return 0

    # 策略 auto：偏好解析唯一真源（vector > cross_encoder），传「可执行」语义——本地可加载或 API 就绪均计可用。
    recall_available = None
    preferred_recall = "cross_encoder"
    if args.strategy == "auto":
        from dataset_recommender.retrieval.vector_recall import resolve_auto_recall_preference
        preferred_recall, recall_available = resolve_auto_recall_preference()

    workflow_result = workflow.run_with_meta(
        RecommendParams(
            query=query,
            top_k=args.top_k,
            use_llm=use_llm,
            mock_llm=mock_llm,
            provider=provider,
            rerank_backend=args.rerank,
            rerank_top_n=args.rerank_top_n,
            recall_backend=args.recall,
            recall_alpha=args.recall_alpha,
            strategy=args.strategy,
            recall_available=recall_available,
            preferred_recall=preferred_recall,
        )
    )
    answer = workflow_result.answer

    prefix = ""
    if args.show_pipeline:
        lines = [
            f"Pipeline: {workflow_result.pipeline}",
            f"LLM mode: {workflow_result.llm_mode}",
            f"LLM attempted: {'true' if workflow_result.llm_attempted else 'false'}",
            f"LLM succeeded: {'true' if workflow_result.llm_succeeded else 'false'}",
            f"LLM response used: {'true' if workflow_result.llm_response_used else 'false'}",
        ]
        if workflow_result.llm_provider:
            lines.append(f"LLM provider: {workflow_result.llm_provider}")
        if workflow_result.model:
            lines.append(f"Model: {workflow_result.model}")
        if workflow_result.prompt_name:
            lines.append(f"Prompt: {workflow_result.prompt_name}")
        if workflow_result.fallback:
            lines.append(f"Fallback: {workflow_result.fallback}")
        if workflow_result.fallback_reason:
            lines.append(f"Fallback reason: {workflow_result.fallback_reason}")
        if workflow_result.strategy:
            d = workflow_result.strategy
            lines.append(
                f"Strategy(auto): tier={d.get('tier')} → recall={d.get('recall_backend')} "
                f"rerank={d.get('rerank_backend')}（通过筛选的候选 {d.get('signals', {}).get('n_survivors')} 条）"
            )
        prefix = "\n".join(lines) + "\n\n"

    payload = f"{prefix}{answer}\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))

    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{prefix}{answer}", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
