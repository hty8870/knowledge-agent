"""领域注册表：当前活动 DomainPack 的解析与缓存（骨架访问领域数据的唯一入口）。

解析顺序：`BIODATA_DOMAIN_DIR`（外部领域包目录，含 pack.py）→ `BIODATA_DOMAIN`
（内置包 id）→ 默认 `"biodata"`。不设任何环境变量时解析为内置 biodata 包，
一切行为与旧版逐位一致。

外部装载（`BIODATA_DOMAIN_DIR`）是「肉不进骨架仓库」的正式通道：领域包可以是
磁盘上任意目录，内有 `pack.py`（暴露 `get_pack()`）即可，按文件位置装载。
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

from .pack import DomainPack

DEFAULT_DOMAIN = "biodata"
ENV_DOMAIN = "BIODATA_DOMAIN"
ENV_DOMAIN_DIR = "BIODATA_DOMAIN_DIR"

_BUILTIN_PACKS = {
    "biodata": "dataset_recommender.domains.biodata.pack",
}

_cache: DomainPack | None = None


class DomainError(RuntimeError):
    pass


def _load_external(domain_dir: str) -> DomainPack:
    root = Path(domain_dir).resolve()
    pack_py = root / "pack.py"
    if not pack_py.is_file():
        raise DomainError(f"外部领域包缺少 pack.py: {pack_py}")
    # 目录即包：外部包的 pack.py 允许包内相对导入（from . import x）。
    # 命名空间键入规范化绝对路径哈希——不同路径的同名包（reviewpack/A 与 reviewpack/B）
    # 各自独立；装载前清同命名空间旧子模块，重载不串旧内容。
    path_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    pkg_name = f"_biodata_external_domain_{root.name}_{path_key}"
    for stale in [k for k in list(sys.modules) if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[stale]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(root)]  # type: ignore[attr-defined]
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg
    module_name = f"{pkg_name}.pack"
    spec = importlib.util.spec_from_file_location(module_name, pack_py)
    if spec is None or spec.loader is None:
        raise DomainError(f"无法装载外部领域包: {pack_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    get_pack = getattr(module, "get_pack", None)
    if not callable(get_pack):
        raise DomainError(f"{pack_py} 未暴露 get_pack()")
    pack = get_pack()
    if not isinstance(pack, DomainPack):
        raise DomainError(f"{pack_py} 的 get_pack() 未返回 DomainPack")
    return pack


def _load_builtin(domain_id: str) -> DomainPack:
    target = _BUILTIN_PACKS.get(domain_id)
    if target is None:
        raise DomainError(
            f"未知内置领域: {domain_id!r}（可选 {sorted(_BUILTIN_PACKS)}；"
            f"外部包请用 {ENV_DOMAIN_DIR} 指向含 pack.py 的目录）"
        )
    module = importlib.import_module(target)
    pack = module.get_pack()
    if not isinstance(pack, DomainPack):
        raise DomainError(f"{target}.get_pack() 未返回 DomainPack")
    return pack


def get_domain() -> DomainPack:
    """当前活动领域包（惰性解析 + 进程级缓存）。"""
    global _cache
    if _cache is None:
        domain_dir = (os.environ.get(ENV_DOMAIN_DIR) or "").strip()
        if domain_dir:
            _cache = _load_external(domain_dir)
        else:
            domain_id = (os.environ.get(ENV_DOMAIN) or "").strip() or DEFAULT_DOMAIN
            _cache = _load_builtin(domain_id)
    return _cache


def set_domain(pack: DomainPack) -> None:
    """测试/评测显式注入领域包。"""
    global _cache
    if not isinstance(pack, DomainPack):
        raise TypeError("set_domain 只接受 DomainPack")
    _cache = pack


def reset_domain_cache() -> None:
    """清空缓存（下次 get_domain() 重新按环境解析）。测试隔离用。"""
    global _cache
    _cache = None


# ---- 骨架常用的窄口便捷访问器（避免每个消费方各写一遍 get_domain().xxx）----

def domain_dimensions() -> tuple[str, ...]:
    return get_domain().dimension_names()


def domain_catalog() -> dict:
    return get_domain().catalog


def domain_labels_zh() -> dict[str, str]:
    return get_domain().dim_labels_zh


def domain_base_source() -> str:
    return get_domain().base_source
