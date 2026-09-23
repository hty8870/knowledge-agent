# -*- coding: utf-8 -*-
"""一次性把「向量召回」用的本地模型下到项目内 models/ 目录，之后运行时**不再联网**。

两类模型（都存到 models/ 下的固定子目录，vector_recall 从那里离线加载）：
- **cross_encoder（默认，推荐）**：bge-reranker-v2-m3 → models/cross_encoders/bge-reranker-v2-m3
  语义重排器（MIT 许可、短中文查询表现好）。
- **dense（可选）**：多语稠密嵌入 MiniLM → models/embeddings/<name>（dense/vector 后端的嵌入器，
  也是 vector 语料索引的嵌入器）。

下载实现唯一真源是 model_worker.download_model（ModelScope 优先 → HuggingFace 回退、
下载后完整性校验），与安装版隔离运行时同一条通道；本脚本只是其 CLI 包装（source/portable
形态的手工下载入口；frozen 由新启动器接管模型安装，本脚本不服务 frozen）。
主项目 requirements 刻意零重依赖；本脚本与模型只在启用向量召回时需要。未下载时向量召回
自动回退为规则顺序，不报错、不影响结果正确性。

用法：
  pip install -r requirements/requirements-embeddings.txt
  py scripts/fetch_embedding_model.py                    # 下 cross-encoder（默认）
  py scripts/fetch_embedding_model.py --cross-encoder    # 同上（显式）
  py scripts/fetch_embedding_model.py --dense            # 下稠密嵌入（备选后端）
  py scripts/fetch_embedding_model.py --all              # 两者都下
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# sys.path 锚定真实源码位置（本文件上两级 = 仓库根）；模型落盘目录由
# vector_recall.default_*_dir 经 runtime_paths.model_root 解析（source/portable = 项目根/models，
# 历史逐字节一致；frozen 由新启动器接管模型安装，本脚本不服务 frozen）。
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.retrieval.vector_recall import (  # noqa: E402
    DEFAULT_CROSS_ENCODER_MODEL, DEFAULT_EMBEDDING_MODEL,
    default_cross_encoder_dir, default_model_dir,
)
from dataset_recommender.retrieval.model_worker import (  # noqa: E402
    EMBED_MODEL_ID, MODEL_ID, download_model,
)


def _fetch(model_id: str, target: Path) -> bool:
    # 国产站直连绕过慢代理（仅本脚本的手工下载场景；安装器隔离运行时不管代理）
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    print(f"[fetch] 下载 {model_id} → {target} …（首次几十~几百 MB）")
    ok = download_model(target, model_id=model_id)
    print(f"[fetch] {'完成' if ok else '失败'}：{target}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="下载向量召回用的本地模型到 models/")
    ap.add_argument("--cross-encoder", action="store_true", help="下 cross-encoder 重排器（默认）")
    ap.add_argument("--dense", action="store_true", help="下稠密嵌入（dense/vector 后端）")
    ap.add_argument("--all", action="store_true", help="两者都下")
    args = ap.parse_args()

    do_cross = args.cross_encoder or args.all or not (args.dense or args.cross_encoder or args.all)
    do_dense = args.dense or args.all

    ok = True
    if do_cross:
        ok &= _fetch(MODEL_ID, default_cross_encoder_dir(DEFAULT_CROSS_ENCODER_MODEL))
    if do_dense:
        ok &= _fetch(EMBED_MODEL_ID, default_model_dir(DEFAULT_EMBEDDING_MODEL))

    if ok and do_dense:
        # 安装期建索引（2026-09-14 冷启动治理）：嵌入模型刚下好就把全语料向量索引
        # 一并构建——「首查建索引」（全语料编码，持 _INDEX_LOCK 单飞）不再落在第一条
        # 用户搜索的请求路径上。失败不掀翻下载流程（请求内惰性构建仍是兜底）。
        try:
            from dataset_recommender.retrieval import vector_index
            print("[fetch] 构建语料向量索引（全语料编码，一次性）…")
            built = vector_index.ensure_index()
            print(f"[fetch] 语料向量索引{'已就绪' if built is not None else '未能构建（首查时将惰性重试）'}。")
        except Exception as exc:  # noqa: BLE001
            print(f"[fetch] 语料向量索引构建跳过（{type(exc).__name__}）——首查时将惰性构建。")

    if ok:
        print("[fetch] 就绪。CLI 加 --recall cross_encoder（或 dense / vector），或在 Web 设置选择对应语义排序后端。")
        print("[fetch] 验证：py scripts/evaluate_recall.py")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
