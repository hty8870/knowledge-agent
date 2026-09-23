"""domain：骨肉分离架构的骨架侧机制（领域包契约 + 注册表 + 描述符 + 目录驱动词表工具）。

架构文档见 `docs/agent/DOMAIN_PACKS.md`。骨架通用机制文件只许 import 本包，
不得 import `dataset_recommender.domains.*` 下的任何具体领域包（防火墙测试钉死）。
"""
from .descriptors import MATCH_EXACT, MATCH_SUBSTRING, DimensionDescriptor
from .pack import DomainBranding, DomainPack
from .registry import (
    DEFAULT_DOMAIN,
    ENV_DOMAIN,
    ENV_DOMAIN_DIR,
    DomainError,
    domain_base_source,
    domain_catalog,
    domain_dimensions,
    domain_labels_zh,
    get_domain,
    reset_domain_cache,
    set_domain,
)

__all__ = [
    "MATCH_EXACT",
    "MATCH_SUBSTRING",
    "DimensionDescriptor",
    "DomainBranding",
    "DomainPack",
    "DEFAULT_DOMAIN",
    "ENV_DOMAIN",
    "ENV_DOMAIN_DIR",
    "DomainError",
    "domain_base_source",
    "domain_catalog",
    "domain_dimensions",
    "domain_labels_zh",
    "get_domain",
    "reset_domain_cache",
    "set_domain",
]
