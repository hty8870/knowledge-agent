# -*- coding: utf-8 -*-
"""国内可达性启发：从下载 URL 的**托管方**推断中国大陆的可达性档 + 下载建议。确定性、离线、只读。

**诚实边界（自审 5 的直接结论）**：这**不是实测速度**。开发机单点测速≈噪声当信号，正是本项目在杀的
诚实病。这里只做**基于托管 host 的启发**（EBI/NCBI 在境外、国内直连通常慢；阿里云/华为云在境内、通常快；
Google Cloud 通常需代理），并**明说是按托管位置推断、非实测**。真实测量需要联网的 patrol 多点采样，
另做（`scripts/patrol_links.py` 已做 Range-GET，加多点计时是后续网络任务，不在此模块）。

不被检索/排序/评测路径调用。
"""
from __future__ import annotations

import re
from typing import Any

# tier → 中文档位标签
_TIER_LABEL = {
    "good": "国内直连通常较快",
    "variable": "跨境带宽波动",
    "slow": "国内直连通常较慢",
    "proxy": "国内通常需代理",
    "unknown": "托管方未识别",
}

# (host 子串, tier, 托管方, 建议)。顺序即优先级（先具体后宽泛）。
_HOSTS: list[tuple[str, str, str, str]] = [
    ("aliyuncs.com", "good", "阿里云 OSS", "境内托管，直连通常较快。"),
    ("myhuaweicloud.com", "good", "华为云 OBS", "境内托管，直连通常较快。"),
    ("myqcloud.com", "good", "腾讯云 COS", "境内托管，直连通常较快。"),
    ("obs.cn-", "good", "华为云 OBS", "境内托管，直连通常较快。"),
    ("storage.googleapis.com", "proxy", "Google Cloud Storage", "国内通常需要代理才能访问，请准备可用的网络环境。"),
    ("googleapis.com", "proxy", "Google Cloud", "国内通常需要代理才能访问。"),
    ("ebi.ac.uk", "slow", "EBI（欧洲）", "国内直连通常较慢，大文件建议用支持断点续传的下载器或就近镜像。"),
    ("ncbi.nlm.nih.gov", "slow", "NCBI（美国）", "国内直连通常较慢，GEO/SRA 大文件建议用 Aspera 或镜像。"),
    ("sra-download", "slow", "NCBI SRA", "国内直连通常较慢，建议 Aspera/prefetch 或镜像。"),
    ("ftp.ncbi", "slow", "NCBI FTP", "国内直连通常较慢，建议断点续传下载器。"),
    # 10x 官方下载域（cf.=CloudFront CDN）与通用 CloudFront——此前未识别，全库主力来源可达性徽章恒缺。
    ("cf.10xgenomics.com", "variable", "10x Genomics（CloudFront CDN）", "CDN 跨境分发，速度因运营商与时段波动；大文件建议用支持断点续传的下载器。"),
    ("cloudfront.net", "variable", "AWS CloudFront CDN", "CDN 跨境分发，速度因运营商与时段波动。"),
    ("data.humancellatlas.org", "variable", "HCA（AWS 承载）", "跨境带宽波动，建议错峰或用下载器。"),
    ("amazonaws.com", "variable", "AWS S3", "跨境带宽波动，建议错峰下载或用支持断点续传的下载器。"),
    ("s3.amazonaws", "variable", "AWS S3", "跨境带宽波动，建议错峰下载。"),
    ("cellxgene", "variable", "CELLxGENE（AWS 承载）", "跨境带宽波动，建议用支持断点续传的下载器。"),
]

_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/:?#]+)")


def _host_of(url: str) -> str:
    m = _HOST_RE.match((url or "").strip())
    return m.group(1).lower() if m else ""


def classify(url: str) -> dict[str, Any] | None:
    """下载 URL → 可达性启发。非 http(s) / 空 → None（不臆造）。

    返回 {host, tier, tier_label, hosting, advice, heuristic}；`heuristic=True` 恒真——
    提醒消费方这是**按托管位置推断、非实测速度**。
    """
    host = _host_of(url)
    if not host:
        return None
    for substr, tier, hosting, advice in _HOSTS:
        if substr in host:
            return {
                "host": host,
                "tier": tier,
                "tier_label": _TIER_LABEL[tier],
                "hosting": hosting,
                "advice": advice,
                "heuristic": True,
            }
    return {
        "host": host,
        "tier": "unknown",
        "tier_label": _TIER_LABEL["unknown"],
        "hosting": host,
        "advice": "托管方未识别，实际可达性请以真实下载为准。",
        "heuristic": True,
    }
