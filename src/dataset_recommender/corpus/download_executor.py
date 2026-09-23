# -*- coding: utf-8 -*-
"""下载执行器：真正把计划里的文件拉下来、边下边算 md5、逐文件给出诚实分级。

与 `download_script.py` 的分工：那个模块**只生成脚本文本**（用户拿去自己跑），本模块在本机
直接执行同一份 `download_plan.build_plan()` 计划。两边的安全口径刻意保持一致：

- 只放行 https，且主机必须在 `plan["allowed_hosts"]` 里（download_script 把同一份名单写进
  manifest.tsv 头部、由生成的脚本逐行核对——白名单的单一真源是 build_plan 的输出行本身，
  download_script.py 并没有另一份私有常量，故这里直接复用计划值，无需抽取共享）。
- **SSRF / 磁盘耗尽防线**：生产下载**不再使用裸
  urllib.request.urlopen 的默认跟随重定向**，改为 `_open_stream_safe` 手动逐跳重校验：
  每一跳（含首次）都重新执行 scheme 仅 https + 主机精确匹配白名单 + 端口仅 443 +
  IP 解析闸（全部 A/AAAA 拒绝回环/私网/链路本地/保留/组播/未指定/非全球单播，含云元数据
  169.254.169.254），重定向上限 3 跳；连接固定到已校验的 IP（SNI/证书校验/Host 头仍用
  原始主机名），防 DNS rebinding。流式循环带 `hard_max_bytes`（声明大小×1.05 与全局
  单文件上限 1 TiB 取小；无声明大小按全局上限；Content-Length 超限一个字都不读），
  超限立即中止并删除 `.part`，不重试（确定答案）。
- 巡检旗标（dead / size_mismatch）的文件**默认跳过**，显式 `include_flagged=True` 才下，
  与生成脚本的 `--include-flagged` 行为一致，记 `skipped_flagged`。
- 先写 `.part`，下完原子改名；核对不通过（md5/大小不符）改名 `.corrupt` 留证据，
  不覆盖也不删除——与生成脚本的处置完全相同。

依赖说明：巡逻脚本 `scripts/patrol_links.py` 刻意只用 stdlib urllib（「不引入 requests，
保持主线依赖精简」），本模块沿用同一选择——纯标准库，零新增依赖。`opener` 仍是唯一的
网络接缝，测试注入它即可全程禁网；生产默认改为带白名单的 `_policy_opener`（内部走
`_open_stream_safe`），`_open_stream` 保留为无白名单形态供 load_smoke 等既有调用方使用
（https+端口+IP 闸+逐跳重校验依旧无条件生效）。

逐文件分级（与 download_script 生成的 runner 的 verdict 同词，互不发明）：
  ok / size_ok / md5_mismatch / size_mismatch / unverified / unreachable / rejected / skipped(flagged)

本模块**只写用户指定的目标目录**（fail-closed 校验），绝不碰 `database/base/`、顶层
`research/`（研究流水线归档）与在仓数据`src/dataset_recommender/data/`。台账回写在
`scripts/record_provision_results.py`，不在这里。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlsplit

from ..app.runtime_paths import get_app_paths

from . import download_plan as DP
from . import downloads, provenance

# ---- 逐文件状态词表（与 download_script 生成脚本的 verdict 对齐）----
STATUS_OK = "ok"                            # md5 与来源声明一致
STATUS_SIZE_OK = "size_ok"                  # 无 md5，字节数与来源声明一致
STATUS_MD5_MISMATCH = "md5_mismatch"        # md5 对不上（已改名 .corrupt 留证据）
STATUS_SIZE_MISMATCH = "size_mismatch"      # 字节数对不上（已改名 .corrupt 留证据）
STATUS_UNVERIFIED = "unverified"            # 下完了，但来源既没给 md5 也没给大小，核不动
STATUS_UNREACHABLE = "unreachable"          # 重试耗尽仍没拿到完整文件
STATUS_REJECTED = "rejected"                # 不是 https / 主机不在计划白名单（未发起请求）
STATUS_SKIPPED_FLAGGED = "skipped_flagged"  # 巡检旗标文件，默认跳过
STATUSES = (STATUS_OK, STATUS_SIZE_OK, STATUS_MD5_MISMATCH, STATUS_SIZE_MISMATCH,
            STATUS_UNVERIFIED, STATUS_UNREACHABLE, STATUS_REJECTED, STATUS_SKIPPED_FLAGGED)

DEFAULT_TIMEOUT = 60          # 单次请求超时（秒）；patrol 用 45，下载整文件放宽到 60
DEFAULT_MAX_ATTEMPTS = 3      # 总尝试次数上限（含首次），即最多重试 2 次
DEFAULT_BACKOFF = 1.0         # 指数退避基数（秒）：第 n 次重试前睡 backoff * 2**(n-1)
MAX_WORKERS = 4               # 小并发上限，别把上游敲太狠
_CHUNK = 1024 * 1024          # 1 MiB 流式块
#: 可重试的 HTTP 状态码（与 corpus_net._RETRYABLE_HTTP 同口径）：429 限流 /
#: 503 临时不可用是「稍后可能好」而非确定答案，走指数退避；其余 4xx 仍是确定答案不重试。
RETRYABLE_HTTP = frozenset({429, 503})

# ---- SSRF / 磁盘耗尽防线----
MAX_REDIRECTS = 3              # 重定向跳数上限：每一跳都重校验，超限即拒绝
HARD_SIZE_FACTOR = 1.05        # 声明大小的硬上限系数（与 download_manager.DISK_HEADROOM 同口径）
GLOBAL_FILE_CAP = 1024 ** 4    # 全局单文件硬上限（1 TiB）：台账实测最大真实文件 ≈858.6 GB，须在其上
_ALLOWED_PORTS = frozenset({443})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class ProvisionError(ValueError):
    """入参非法。带 `code` 供 CLI / MCP 映射成稳定错误码（与 DownloadPlanError 同构）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DownloadCancelled(BaseException):
    """下载被取消（`cancel_event` 在流式读块之间置位）。

    特意继承 `BaseException` 而不是 `Exception`：`download_one` 的 `except Exception`
    会把网络/写盘错按指数退避重试——取消不是错误，不该重试，也不该被当成网络失败吞掉。
    调用方（download_manager 的任务线程）catch 它做取消收尾；`.part` 由 finally 保留
    （见 `download_one` 的取消分支），语义是可续传。
    """


class DownloadPolicyError(Exception):
    """下载被安全策略拒绝：非 https / 端口不在白名单 / 主机不在白名单 /
    IP 解析闸 / 重定向超限。

    确定答案，不重试（与 4xx 同纪律）；`download_one` 收敛进 `STATUS_REJECTED` 并保留原因。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DownloadTooLarge(Exception):
    """流式读取超过单文件硬上限（含 Content-Length 早退）。带 limit 供报告引用。

    收敛进 `STATUS_UNREACHABLE`（语义=「没拿到完整文件」）+ 中文 error；不重试——
    服务器实际输出远超声明/上限是确定答案，重试只会再写一遍同样的字节。
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"超过单文件硬上限（{limit} 字节），已中止并清理 .part")
        self.limit = limit


@dataclass
class FileResult:
    """一个文件的执行结果。`status` 只取 STATUSES 里的词；证据字段能填则填、填不了 None。"""
    dataset_uid: str
    safe_uid: str
    filename: str
    safe_name: str
    url: str
    status: str
    http_status: "int | None" = None        # 服务器真回了 HTTP 才有（含 4xx/5xx）
    bytes_downloaded: "int | None" = None
    expected_bytes: "int | None" = None
    md5_expected: "str | None" = None
    md5_actual: "str | None" = None
    elapsed_s: float = 0.0
    attempts: int = 0
    error: "str | None" = None              # 最后一击的错误摘要（脱敏：只有异常类型+状态码）
    flag_kind: "str | None" = None          # 计划上的巡检旗标（dead/size_mismatch），原样带过
    saved_as: "str | None" = None           # 相对 out_dir 的落盘路径（.corrupt 也如实写）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProvisionReport:
    """一次执行的结构化报告：`--json` 机器可读 + `summary_zh()` 人类可读摘要，同一份数据。"""
    dataset_uids: list
    out_dir: str
    scope: str
    include_flagged: bool
    started_at: str
    finished_at: str = ""
    results: list = field(default_factory=list)   # list[FileResult]，按计划顺序

    def counts(self) -> dict:
        c = {s: 0 for s in STATUSES}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    def to_dict(self) -> dict:
        return {
            "schema": "biodata-provision/v0",
            "dataset_uids": list(self.dataset_uids),
            "out_dir": self.out_dir,
            "scope": self.scope,
            "include_flagged": self.include_flagged,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": self.counts(),
            "results": [r.to_dict() for r in self.results],
        }

    def summary_zh(self) -> str:
        c = self.counts()
        n = len(self.results)
        verified = c[STATUS_OK] + c[STATUS_SIZE_OK]
        lines = [
            f"本次执行：{len(self.dataset_uids)} 个数据集，计划 {n} 个文件。",
            f"  核对通过 {verified}（md5 一致 {c[STATUS_OK]}，仅大小一致 {c[STATUS_SIZE_OK]}）；"
            f"未能核对 {c[STATUS_UNVERIFIED]}。",
            f"  核对不通过 {c[STATUS_MD5_MISMATCH] + c[STATUS_SIZE_MISMATCH]}"
            f"（md5 不符 {c[STATUS_MD5_MISMATCH]}，大小不符 {c[STATUS_SIZE_MISMATCH]}，均已改名 .corrupt 留证据）；"
            f"不可达 {c[STATUS_UNREACHABLE]}。",
            f"  按巡检旗标跳过 {c[STATUS_SKIPPED_FLAGGED]}；按安全规则拒绝 {c[STATUS_REJECTED]}。",
        ]
        if verified + c[STATUS_UNVERIFIED] > 0:
            lines.append(f"文件已保存到：{self.out_dir}")
        else:
            lines.append("没有任何文件成功保存。")
        return "\n".join(lines)


# ---------------------------------------------------------------- 目标目录（fail-closed）

def _repo_root() -> Path:
    """源码项目根（source 模式真源）。frozen 下 `Path(__file__)` 会指向只读快照
    `sys._MEIPASS`，故写侧真实路径必须改经 `_protected_dirs` 的 runtime_paths 推导。"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _protected_dirs() -> "tuple[Path, ...]":
    """下载物绝不许落进的受保护区（fail-closed 名单）。

    - 单根（source / portable 同根）：整个 database/（base 冻结基准 + external 元数据库）
      + 顶层 research/（原 database 下 workstream 流水线整体上移，
      受保护区随之等价扩展）+ 在仓数据真源 src/dataset_recommender/data。
    - 双根分离（frozen / portable 异根）：`_repo_root()` 指向只读快照，真实写侧 data_root
      下的 external/trace/.userdata/run 等会漏出保护名单；改经 runtime_paths 的真实路径——
      写侧 data_root 整体（覆盖 user_external_dir/userdata_dir/trace_root 等）与读侧 shipped
      资源（resource_root/database、resource_root/research 覆盖 shipped_base_dir +
      shipped_external_dir + 流水线归档、在仓 data）都要护住。
    """
    paths = get_app_paths()
    if paths.resource_root == paths.data_root:
        root = paths.data_root
        return (
            root / "database",
            root / "research",
            root / "src" / "dataset_recommender" / "data",
        )
    return (
        paths.data_root,
        paths.resource_root / "database",
        paths.resource_root / "research",
        paths.resource_root / "src" / "dataset_recommender" / "data",
    )


def resolve_out_dir(path: "str | os.PathLike") -> Path:
    """目标目录必须显式、绝对、且不在受保护区内；不存在则创建。任何一条不满足直接报错。"""
    if not path or not str(path).strip():
        raise ProvisionError("bad_out_dir", "目标目录不能为空：必须显式给一个绝对路径。")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ProvisionError(
            "bad_out_dir",
            f"目标目录必须是绝对路径（收到 {path!r}）：相对路径会把文件下到调用者意想不到的地方。")
    resolved = p.resolve()
    for protected in _protected_dirs():
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        raise ProvisionError(
            "protected_out_dir",
            f"目标目录 {resolved} 在受保护区 {protected} 内：下载物不许落进仓库 database/、research/ 目录或在仓数据目录。")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# ---------------------------------------------------------------- 计划与白名单

def _item_for_uid(uid: str) -> dict:
    """executor 只服务本机文件清单覆盖到的数据集（10x 宇宙）；合成 build_plan 要求的最小 item。"""
    rec = downloads.get(uid)
    if not rec:
        raise ProvisionError(
            "unknown_uid",
            f"本机文件清单里没有 {uid!r}：executor 只下载清单覆盖到的数据集，"
            "查不到就不猜、不降级到页面链接。")
    return {
        "dataset_uid": uid,
        "url": rec.get("url") or "",
        "download_url": rec.get("primary_download_url") or rec.get("url") or "",
        "filesize": rec.get("primary_bytes"),
        "source": provenance.SOURCE_10X,
        "dataset_name": rec.get("primary_title") or uid,
    }


def url_policy_error(url: str, allowed_hosts: Sequence[str]) -> "str | None":
    """https + 计划白名单 + 端口三闸；返回 None 表示放行，否则返回中文原因（fail-closed）。

    端口只放 443（台账 15215 条直链实测全部 443/无端口）；带 userinfo 的 URL
    （`https://user@host/`）拒绝——它是钓鱼形态（视觉主机名 ≠ 连接目标），且重定向时
    可能被用来夹带凭据。非法端口/无法解析的 URL 一律按策略拒绝。
    """
    if not url.startswith("https://"):
        # A1-L6：不回显完整 URL（可能含 userinfo/查询串等敏感片段）——只给原因 + 主机名
        #（hostname 天然不含 userinfo 凭据）
        host = urlsplit(url).hostname or "<无法解析>"
        return f"不是 https：安全策略拒绝，未发起请求（主机 {host}）"
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"URL 无法解析：{url[:80]}"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL 没有主机名"
    if parts.username is not None or parts.password is not None:
        return "URL 不允许带用户名/密码（userinfo），拒绝"
    if host not in {h.lower() for h in allowed_hosts}:
        return f"主机 {host} 不在本次计划的白名单里"
    try:
        port = parts.port
    except ValueError:
        return f"URL 端口非法：{url[:80]}"
    if port is not None and port not in _ALLOWED_PORTS:
        return f"端口 {port} 不在白名单（仅允许 443）"
    return None


def build_rows(dataset_uids: Sequence[str], *, scope: str = DP.SCOPE_PRIMARY,
               only_files: "Sequence[str] | None" = None) -> "tuple[list[dict], list[str]]":
    """uid 列表 → build_plan 的输出行（带四档、旗标、safe_name）+ 白名单。

    `only_files` 可显式给一个文件子集（按 filename / safe_name / download_url 匹配）；
    给了却一个都匹配不上属于调用方笔误，fail-closed 报错而不是悄悄下个空包。
    """
    if scope not in DP.SCOPES:
        raise ProvisionError("bad_param", f"scope 只能是 {'/'.join(DP.SCOPES)}。")
    if not dataset_uids:
        raise ProvisionError("bad_param", "至少给一个 dataset_uid。")
    items = [_item_for_uid(uid) for uid in dataset_uids]
    plan = DP.build_plan(items, scope=scope)
    rows = list(plan.get("rows", []))
    if only_files:
        want = {w.strip() for w in only_files if w and w.strip()}
        rows = [r for r in rows
                if r["filename"] in want or r["safe_name"] in want or r["download_url"] in want]
        if not rows:
            raise ProvisionError(
                "bad_param",
                "显式文件子集一个都没匹配上（按 filename / safe_name / download_url 比对）："
                "宁可报错也不悄悄执行一个空计划。")
    allowed = plan.get("allowed_hosts") or []
    if rows and not allowed:
        raise ProvisionError(
            "no_allowed_hosts",
            "计划没有给出 allowed_hosts 声明：为安全起见不下载（与生成脚本同一条红线）。")
    return rows, allowed


# ---------------------------------------------------------------- 网络接缝（测试注入点）

def forbidden_ip_reason(ip: str) -> "str | None":
    """单个 IP 是否命中禁止网段；返回中文原因或 None（放行）。

    拒绝：未指定 / 回环 / 链路本地（含云元数据 169.254.169.254）/ 私网 / 组播 / 保留 /
    非全球单播。IPv4 映射的 IPv6（`::ffff:a.b.c.d`）按映射的 IPv4 判定。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"不是合法 IP（{ip}）"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_unspecified:
        return "未指定地址"
    if addr.is_loopback:
        return "回环地址"
    if addr.is_link_local:
        return "链路本地地址（含云元数据 169.254.x.x）"
    if addr.is_private:
        return "私网地址"
    if addr.is_multicast:
        return "组播地址"
    if addr.is_reserved:
        return "保留地址"
    if not addr.is_global:
        return "非全球单播地址"
    return None


def _resolve_host(host: str) -> "list[str]":
    """默认解析器：取 host 的全部 A/AAAA（去重），返回 IP 字符串列表。"""
    infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    seen: "set[str]" = set()
    addrs: "list[str]" = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.add(ip)
            addrs.append(ip)
    return addrs


def resolve_and_validate(host: str, resolver: "Callable | None" = None) -> "list[str]":
    """解析 host 的全部 A/AAAA 并逐一过 IP 闸；**任一**地址命中禁止网段 → fail-closed 拒绝。

    返回全部通过的地址；调用方**必须**用这里固定的地址建连（防 DNS rebinding：连接阶段
    不再重新解析，攻击者第二次查询换 IP 也影响不到连接目标）。
    """
    if not host:
        raise DownloadPolicyError("no_host", "URL 缺少主机名。")
    addrs = (resolver or _resolve_host)(host)
    if not addrs:
        raise DownloadPolicyError("dns_failed", f"主机 {host} 解析不到任何地址（DNS 失败或不存在）。")
    for ip in addrs:
        why = forbidden_ip_reason(ip)
        if why:
            raise DownloadPolicyError(
                "ip_blocked",
                f"主机 {host} 解析到被禁止的地址：{ip}（{why}）。"
                "服务端下载只允许公网地址；回环/私网/链路本地/保留/组播网段一律拒绝。")
    return addrs


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到已校验的固定 IP，但 SNI / 证书校验 / Host 头都用原始主机名（防 DNS rebinding）。"""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout,
                                             self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)


def _connect_pinned(url: str, timeout: int, *, ip: str,
                    context: "ssl.SSLContext | None" = None) -> http.client.HTTPResponse:
    """向固定 IP 发起一次 https GET；URL 的主机名用于 Host 头与 TLS SNI/证书校验。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or 443
    ctx = context or ssl.create_default_context()
    conn = _PinnedHTTPSConnection(host, port, pinned_ip=ip, timeout=timeout, context=ctx)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers={"User-Agent": _UA})
        return conn.getresponse()
    except BaseException:
        conn.close()
        raise


def _open_stream_safe(url: str, timeout: int, *, allowed_hosts: "Sequence[str] | None" = None,
                      resolver: "Callable | None" = None, connect: "Callable | None" = None,
                      max_hops: int = MAX_REDIRECTS) -> http.client.HTTPResponse:
    """带 SSRF 防线的流式 GET（生产路径唯一出口）。

    **不依赖 urllib 的默认重定向跟随**：重定向由本函数手动跟随，且**每一跳**（含首次）
    都重新执行——scheme 仅 https + 主机白名单 + 端口仅 443 + IP 解析闸（全部 A/AAAA 拒绝
    禁止网段）+ 固定到已校验 IP 建连；跳数上限 `max_hops`。`allowed_hosts=None` 时跳过
    主机白名单（`_open_stream` 形态，https/端口/IP/重定向闸依然生效）。
    `resolver/connect` 是测试注入点（禁真网）；返回带 .status/.getcode()/.read(n)/.headers
    的响应对象（http.client 约定）。
    """
    hops = 0
    current = url
    while True:
        hops += 1
        if hops > max_hops:
            raise DownloadPolicyError(
                "redirect_limit", f"重定向超过 {max_hops} 跳上限，拒绝继续（疑似重定向环或攻击）。")
        if allowed_hosts is not None:
            why = url_policy_error(current, allowed_hosts)
            if why:
                raise DownloadPolicyError("url_blocked", why)
        try:
            host = (urlsplit(current).hostname or "").lower()
        except ValueError:
            raise DownloadPolicyError("url_blocked", f"URL 无法解析：{current[:80]}") from None
        addrs = resolve_and_validate(host, resolver=resolver)
        do_connect = connect or _connect_pinned
        resp = do_connect(current, timeout, ip=addrs[0])
        status = getattr(resp, "status", None) or resp.getcode()
        if status in _REDIRECT_STATUSES:
            location = resp.getheader("Location")
            resp.close()
            if not location:
                raise urllib.error.HTTPError(current, status, "redirect without Location", None, None)
            current = urljoin(current, location)
            continue
        return resp


def _open_stream(url: str, timeout: int):
    """无主机白名单的安全流式 GET（向后兼容接缝：`scripts/load_smoke.py` 等默认用它）。

    只下 10x 台账直链（受信数据文件）的调用方不需要计划白名单；https + 端口 + IP 闸 +
    重定向逐跳重校验依然无条件生效——比旧版裸 urllib.urlopen 严格。测试 monkeypatch 它
    即可全程禁网；download_one / provision 的**生产默认**是带白名单的 `_policy_opener`。
    """
    return _open_stream_safe(url, timeout, allowed_hosts=None)


def _policy_opener(allowed_hosts: Sequence[str]) -> Callable:
    """生产默认 opener：带计划白名单的逐跳重校验（download_one/provision 未注入时使用）。"""

    def open_(url: str, timeout: int):
        return _open_stream_safe(url, timeout, allowed_hosts=allowed_hosts)
    return open_


def _err_summary(exc: BaseException) -> str:
    """错误摘要只留异常类型与状态码，不把 URL 里的查询串/内部堆栈写进报告。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError({exc.code})"
    return type(exc).__name__


# ---------------------------------------------------------------- 单文件执行

def _hard_max_bytes(row: dict) -> int:
    """单文件硬上限 = min(声明大小×HARD_SIZE_FACTOR, GLOBAL_FILE_CAP)；无声明大小 → 全局上限。

    声明大小是攻击者可控的，不能只信它——×1.05 系数兜住「实际输出略多于声明」的
    正常情况（与磁盘预检同系数），全局上限兜住「无声明大小 / 声明被造假得很小」的无限流。
    """
    declared = row.get("bytes")
    try:
        declared = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared = None
    if declared and declared > 0:
        return min(max(1, int(declared * HARD_SIZE_FACTOR)), GLOBAL_FILE_CAP)
    return GLOBAL_FILE_CAP


def _verify(target: Path, md5_actual: str, n_bytes: int,
            md5_expected: "str | None", expected_bytes: "int | None") -> str:
    """下完回头核对：有 md5 比 md5，没 md5 比字节数，都没有就如实 unverified。"""
    if md5_expected:
        return STATUS_OK if md5_actual.lower() == md5_expected.lower() else STATUS_MD5_MISMATCH
    if expected_bytes:
        return STATUS_SIZE_OK if n_bytes == int(expected_bytes) else STATUS_SIZE_MISMATCH
    return STATUS_UNVERIFIED


def download_one(row: dict, out_root: Path, allowed_hosts: Sequence[str], *,
                 timeout: int = DEFAULT_TIMEOUT, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff: float = DEFAULT_BACKOFF, include_flagged: bool = False,
                 opener: "Callable | None" = None, sleep: Callable = time.sleep,
                 subdir: "str | None" = None,
                 cancel_event: "threading.Event | None" = None,
                 progress_cb: "Callable[[int], None] | None" = None) -> FileResult:
    """执行计划里的一行：旗标跳过 → 白名单闸 → 流式下载+边算 md5 → 核对 → 原子落盘。

    `opener` 为 None 时使用生产默认 `_policy_opener(allowed_hosts)`（每一跳
    https+白名单+端口+IP 解析闸、限 3 跳、防 DNS rebinding；硬字节上限见 `_hard_max_bytes`，
    超限中止并清理 `.part`）。注入 opener 只影响「网络怎么拿响应」，不影响其余防线。
    `subdir` 非 None 时覆盖落盘目录：文件写到 `out_root/<subdir>/<safe_name>` 而不是
    `out_root/<safe_uid>/<safe_name>`（download_manager 用它实现「一个数据集一个子文件夹」）；
    None 保持原行为。`cancel_event` 在每读一块前检查，置位即抛 `DownloadCancelled` 并**保留
    `.part`**（可续传语义）；`progress_cb` 每写完一块收到本块字节数（调用方自管累计）。
    """
    base = FileResult(
        dataset_uid=row["dataset_uid"], safe_uid=row["safe_uid"],
        filename=row["filename"], safe_name=row["safe_name"], url=row["download_url"],
        status=STATUS_UNREACHABLE, expected_bytes=row.get("bytes"),
        md5_expected=row.get("md5sum"), flag_kind=row.get("flag_kind"))

    if row.get("flag_kind") and not include_flagged:
        base.status = STATUS_SKIPPED_FLAGGED
        return base
    why = url_policy_error(row["download_url"], allowed_hosts)
    if why:
        base.status = STATUS_REJECTED
        base.error = why
        return base
    if opener is None:
        opener = _policy_opener(allowed_hosts)

    target_dir = out_root / (subdir if subdir is not None else row["safe_uid"])
    target = target_dir / row["safe_name"]
    part = target.with_name(target.name + ".part")
    start = time.monotonic()
    attempts = 0
    last_exc: "BaseException | None" = None

    while attempts < max(1, max_attempts):
        attempts += 1
        md5 = hashlib.md5()  # noqa: S324 —— 核对来源声明值，不是安全用途
        n_bytes = 0
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with opener(row["download_url"], timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                base.http_status = status
                if status not in (200, 206):
                    raise urllib.error.HTTPError(row["download_url"], status, "unexpected status", None, None)
                hard_limit = _hard_max_bytes(row)
                # Content-Length 早退：服务器声明发送量已超硬上限 → 一个字都不读就拒绝
                resp_headers = getattr(resp, "headers", None)
                if resp_headers is not None and hasattr(resp_headers, "get"):
                    try:
                        clen = int(resp_headers.get("Content-Length"))
                    except (TypeError, ValueError):
                        clen = None
                    if clen is not None and clen > hard_limit:
                        raise _DownloadTooLarge(hard_limit)
                with part.open("wb") as fh:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        md5.update(chunk)
                        n_bytes += len(chunk)
                        if progress_cb is not None:
                            progress_cb(len(chunk))
                        if n_bytes > hard_limit:
                            raise _DownloadTooLarge(hard_limit)
            base.bytes_downloaded = n_bytes
            base.md5_actual = md5.hexdigest()
            verdict = _verify(target, base.md5_actual, n_bytes,
                              base.md5_expected, base.expected_bytes)
            if verdict in (STATUS_MD5_MISMATCH, STATUS_SIZE_MISMATCH):
                corrupt = target.with_name(target.name + ".corrupt")
                os.replace(part, corrupt)          # 留证据，不覆盖正主也不删除
                base.saved_as = f"{target_dir.name}/{corrupt.name}"
            else:
                os.replace(part, target)           # .part → 正名，原子改名
                base.saved_as = f"{target_dir.name}/{target.name}"
            base.status = verdict
            base.attempts = attempts
            base.elapsed_s = round(time.monotonic() - start, 3)
            return base
        except _DownloadTooLarge as e:
            # 超硬上限是确定答案：不重试（重试只会再写一遍同样多的字节）；http_status 置 None
            # 表示「主动中止」不是 HTTP 结论；.part 由 finally 清理。
            base.http_status = None
            base.status = STATUS_UNREACHABLE
            base.error = str(e)
            last_exc = e
            break
        except DownloadPolicyError as e:
            # 策略拒绝（重定向到非白名单/禁 IP/跳数超限等）是确定答案：不重试。
            base.status = STATUS_REJECTED
            base.error = str(e)
            last_exc = e
            break
        except urllib.error.HTTPError as e:
            # 429/503（限流/临时不可用）按指数退避重试（与 corpus_net/corpus_curation 同一
            # 网络纪律）；其余 4xx/5xx 是服务器给的确定答案，重试无意义（与 patrol 口径一致）。
            base.http_status = e.code
            last_exc = e
            if e.code in RETRYABLE_HTTP and attempts < max_attempts:
                sleep(backoff * (2 ** (attempts - 1)))
                continue
            break
        except Exception as e:  # 网络错/超时/写盘错：不结论，按指数退避重试
            last_exc = e
            if attempts < max_attempts:
                sleep(backoff * (2 ** (attempts - 1)))
        finally:
            # 任何没走到原子改名的路径都必须清掉 .part，不留半成品（已改名的 replace 后不存在，无害）。
            # 例外只有一条：取消（DownloadCancelled 经这里逃逸）——保留 .part，语义是可续传。
            try:
                if (part.exists() and base.status == STATUS_UNREACHABLE
                        and not (cancel_event is not None and cancel_event.is_set())):
                    part.unlink()
            except OSError:
                pass

    base.attempts = attempts
    base.elapsed_s = round(time.monotonic() - start, 3)
    if not base.error:
        base.error = _err_summary(last_exc) if last_exc else "unknown"
    return base


# ---------------------------------------------------------------- 一次执行

def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def provision(dataset_uids: Sequence[str], out_dir: "str | os.PathLike", *,
              scope: str = DP.SCOPE_PRIMARY, only_files: "Sequence[str] | None" = None,
              include_flagged: bool = False, workers: int = 1,
              timeout: int = DEFAULT_TIMEOUT, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
              backoff: float = DEFAULT_BACKOFF,
              opener: "Callable | None" = None, sleep: Callable = time.sleep) -> ProvisionReport:
    """把一批数据集的计划真正执行掉，返回结构化报告。默认顺序下载；workers 上限 4。

    `opener` 默认 None → 每行用带白名单的 `_policy_opener`（安全防线）；
    注入 opener 只替换「网络怎么拿响应」，其余防线（硬字节上限/重试纪律）不变。
    """
    out_root = resolve_out_dir(out_dir)
    rows, allowed = build_rows(dataset_uids, scope=scope, only_files=only_files)
    workers = max(1, min(int(workers or 1), MAX_WORKERS))

    report = ProvisionReport(
        dataset_uids=list(dataset_uids), out_dir=str(out_root), scope=scope,
        include_flagged=include_flagged, started_at=_utc_now())

    kwargs = dict(timeout=timeout, max_attempts=max_attempts, backoff=backoff,
                  include_flagged=include_flagged, opener=opener, sleep=sleep)
    if workers == 1 or len(rows) <= 1:
        results = [download_one(row, out_root, allowed, **kwargs) for row in rows]
    else:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda r: download_one(r, out_root, allowed, **kwargs), rows))
    report.results = results
    report.finished_at = _utc_now()
    return report


# ---------------------------------------------------------------- CLI

def main(argv: "Sequence[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="下载执行器：按 build_plan 计划真实下载并逐文件核对（md5/大小）。")
    ap.add_argument("uids", nargs="+", help="dataset_uid（一个或多个）")
    ap.add_argument("--out", required=True, help="目标目录（必须绝对路径；不存在则创建）")
    ap.add_argument("--scope", default=DP.SCOPE_PRIMARY, choices=DP.SCOPES,
                    help="primary=每数据集 1 个主文件（默认）；all=全部文件")
    ap.add_argument("--files", default="", help="只下这些文件（逗号分隔，按文件名/URL 匹配）")
    ap.add_argument("--include-flagged", action="store_true",
                    help="连同巡检旗标（dead/size_mismatch）文件一起下（默认跳过）")
    ap.add_argument("--workers", type=int, default=1, help=f"小并发（1-{MAX_WORKERS}，默认 1）")
    ap.add_argument("--json", action="store_true", help="只输出机器可读 JSON 报告")
    ap.add_argument("--report-json", default="",
                    help="把 JSON 报告写到这个文件（供 scripts/record_provision_results.py 回写台账）")
    args = ap.parse_args(argv)

    only = [w for w in args.files.split(",") if w.strip()] or None
    try:
        report = provision(args.uids, args.out, scope=args.scope, only_files=only,
                           include_flagged=args.include_flagged, workers=args.workers)
    except ProvisionError as e:
        print(f"[executor] {e.code}: {e}", file=sys.stderr)
        return 2

    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=1)
    if args.report_json:
        Path(args.report_json).write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        for r in report.results:
            print(f"  {r.status:16s} {r.safe_uid}/{r.safe_name}  {r.error or ''}")
        print(report.summary_zh())
    bad = report.counts()
    return 1 if (bad[STATUS_MD5_MISMATCH] + bad[STATUS_SIZE_MISMATCH]
                 + bad[STATUS_UNREACHABLE] + bad[STATUS_REJECTED]) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
