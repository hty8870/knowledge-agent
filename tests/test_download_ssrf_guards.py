# -*- coding: utf-8 -*-
"""服务端下载 SSRF / 磁盘耗尽防线测试。

与 test_download_executor / test_download_manager 同一纪律：**全程禁网**。SSRF 防线分两层：

- 策略层（纯函数，直接喂输入）：`url_policy_error`（scheme/主机/端口/userinfo）、
  `forbidden_ip_reason`（禁止网段）、`resolve_and_validate`（全部 A/AAAA 任一违规即拒）。
- 传输层（`_open_stream_safe`）：注入 `resolver` / `connect` 假件驱动，不触真实 DNS/socket；
  断言每一跳重校验、跳数上限、重定向目标复核、IP 固定（防 rebinding）。
- 流式硬上限（`download_one` 集成）：超声明大小×1.05 / 全局上限 / Content-Length 早退，
  `.part` 清理、不重试；策略拒绝收敛进 rejected、不重试。

覆盖（对照安全审查修复建议）：
重定向到非白名单 / loopback / 私网、DNS rebinding 形态、无 Content-Length、超声明大小中止
与 .part 清理、无限流截断、取消清理（取消保留 .part 的既有钉在 test_download_executor.py，
这里补「取消 + 硬上限不互相干扰」与 manager 默认 opener 端到端）。
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_manager as DM  # noqa: E402
from dataset_recommender.corpus import download_plan as DP  # noqa: E402


# ---------------------------------------------------------------- 假 HTTP / 假解析

class FakeResp:
    """模拟 http.client 响应：.status/.getcode()/.read(n)/.headers/.getheader()，上下文管理。"""

    def __init__(self, data: bytes, status: int = 200, headers: "dict | None" = None,
                 location: "str | None" = None):
        self._data, self.status, self._pos = data, status, 0
        self.headers = headers or {}
        self._location = location
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status

    def getheader(self, name, default=None):
        if name == "Location":
            return self._location
        return self.headers.get(name, default)

    def close(self):
        self.closed = True

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:] if n is None or n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _row(content: bytes, *, url: str = "https://cf.10xgenomics.com/x/f.h5", bytes_: "int | None" = None):
    """与 test_download_executor 同构的最小计划行；bytes_ 可显式覆盖声明大小。"""
    declared = len(content) if bytes_ is None else bytes_
    return {
        "dataset_uid": "10x:A", "safe_uid": "10x_A", "source": "10x Genomics",
        "tier": DP.TIER_CHECKSUM, "filename": "f.h5", "filename_derived": False,
        "safe_name": "f.h5", "download_url": url, "netloc": "cf.10xgenomics.com",
        "bytes": declared, "md5sum": _md5(content),
        "verify": "md5", "category": "", "pipeline": "", "flag_kind": None,
        "flag_reason_zh": "", "last_verified": "",
    }


ALLOWED = ["cf.10xgenomics.com", "cdn.10xgenomics.com"]


def _connect_fake(by_url: "dict[str, FakeResp] | None" = None):
    """`_open_stream_safe` 的 connect 接缝假件（签名 (url, timeout, *, ip)），记录调用 (url, ip)。"""
    calls = []

    def connect(url, timeout, *, ip, context=None):
        calls.append((url, ip))
        if by_url is not None:
            return by_url[url]
        return FakeResp(b"")  # 无响应表时仅用于探调用
    connect.calls = calls
    return connect


def _opener_fake(by_url: "dict[str, FakeResp] | None" = None):
    """`download_one` 的 opener 接缝假件（签名 (url, timeout)），记录调用。"""
    calls = []

    def opener(url, timeout):
        calls.append(url)
        if by_url is not None:
            return by_url[url]
        return FakeResp(b"")
    opener.calls = calls
    return opener


def _resolver_fake(addrs: "list[str] | None" = None, *,
                   per_host: "dict[str, list[str]] | None" = None):
    """解析假件：per_host 按主机分发；缺省用 addrs。记录调用次数。"""
    calls = []

    def resolve(host):
        calls.append(host)
        if per_host is not None:
            return per_host[host]
        return list(addrs or [])
    resolve.calls = calls
    return resolve


# ================================================================ A. url_policy_error（每跳闸纯函数面）

def test_url_policy_rejects_http_and_ws():
    assert DE.url_policy_error("http://cf.10xgenomics.com/x", ALLOWED)
    assert DE.url_policy_error("ws://cf.10xgenomics.com/x", ALLOWED)


def test_url_policy_rejects_bad_port():
    assert "端口" in DE.url_policy_error("https://cf.10xgenomics.com:8443/x", ALLOWED)
    assert "端口" in DE.url_policy_error("https://cf.10xgenomics.com:80/x", ALLOWED)


def test_url_policy_allows_default_and_443():
    assert DE.url_policy_error("https://cf.10xgenomics.com/x", ALLOWED) is None
    assert DE.url_policy_error("https://cf.10xgenomics.com:443/x", ALLOWED) is None


def test_url_policy_rejects_invalid_port():
    assert DE.url_policy_error("https://cf.10xgenomics.com:abc/x", ALLOWED)
    assert DE.url_policy_error("https://cf.10xgenomics.com:99999/x", ALLOWED)


def test_url_policy_rejects_userinfo_phishing():
    """`https://user@allowed-host/` 是钓鱼形态：视觉主机名合法但可夹带凭据/迷惑，一律拒绝。"""
    assert "userinfo" in DE.url_policy_error("https://user@cf.10xgenomics.com/x", ALLOWED)
    assert "userinfo" in DE.url_policy_error("https://user:pass@cf.10xgenomics.com/x", ALLOWED)


def test_url_policy_rejects_missing_host():
    assert DE.url_policy_error("https:///path-only", ALLOWED)


def test_url_policy_rejects_suffix_lookalike_host():
    """主机精确匹配：`cf.10xgenomics.com.evil.com` 不是 `cf.10xgenomics.com`，不能借前缀混入。"""
    assert DE.url_policy_error("https://cf.10xgenomics.com.evil.com/x", ALLOWED)


# ================================================================ B. IP 解析闸（纯函数面）

@pytest.mark.parametrize("ip", [
    "0.0.0.0",            # 未指定
    "127.0.0.1", "127.8.8.8",   # 回环
    "10.0.0.1",           # 私网 A
    "172.16.0.1", "172.31.255.255",  # 私网 B
    "192.168.1.1",        # 私网 C
    "169.254.169.254", "169.254.0.1",  # 链路本地 / 云元数据
    "100.64.0.1",         # CGNAT 保留
    "224.0.0.1",          # 组播
    "240.0.0.1",          # 保留
    "198.18.0.1",         # 基准测试保留段
    "192.0.2.1",          # 文档示例（非全球单播）
    "::1",                # 回环 v6
    "::",                 # 未指定 v6
    "fe80::1",            # 链路本地 v6
    "ff02::1",            # 组播 v6
    "::ffff:127.0.0.1",   # IPv4 映射 v6 → 按映射 v4 判定回环
    "::ffff:192.168.1.1", # IPv4 映射 v6 → 私网
])
def test_forbidden_ip_ranges_rejected(ip):
    assert DE.forbidden_ip_reason(ip) is not None, f"{ip} 应被禁止"


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "104.16.132.229", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert DE.forbidden_ip_reason(ip) is None, f"{ip} 应放行"


def test_forbidden_ip_garbage():
    assert "不是合法 IP" in DE.forbidden_ip_reason("not-an-ip")


def test_resolve_any_forbidden_fails_closed():
    """解析出的地址**只要有一个**命中禁止网段 → 整体拒绝（fail-closed：攻击者可控制 DNS，
    私公混解析时不能赌「恰好连到公网那个」）。"""
    resolver = _resolver_fake(["8.8.8.8", "192.168.0.1"])
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE.resolve_and_validate("cf.10xgenomics.com", resolver=resolver)
    assert e.value.code == "ip_blocked" and "192.168.0.1" in str(e.value)


def test_resolve_all_public_returns_all_addrs():
    resolver = _resolver_fake(["8.8.8.8", "1.1.1.1"])
    assert DE.resolve_and_validate("cf.10xgenomics.com", resolver=resolver) == ["8.8.8.8", "1.1.1.1"]


def test_resolve_empty_raises_dns_failed():
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE.resolve_and_validate("cf.10xgenomics.com", resolver=_resolver_fake([]))
    assert e.value.code == "dns_failed"


def test_resolve_no_host_raises():
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE.resolve_and_validate("", resolver=_resolver_fake(["8.8.8.8"]))
    assert e.value.code == "no_host"


# ================================================================ C. _open_stream_safe（每跳重校验传输层）

def test_safe_open_single_hop_returns_response_and_pins_ip():
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({"https://cf.10xgenomics.com/x": FakeResp(b"DATA", 200)})
    resp = DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                                allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert resp.read() == b"DATA"
    assert connect.calls == [("https://cf.10xgenomics.com/x", "8.8.8.8")]
    assert resolver.calls == ["cf.10xgenomics.com"], "每跳只解析一次（连接用固定 IP，不二次解析）"


def test_safe_open_follows_whitelisted_redirect_per_hop_revalidation():
    """同一白名单内的合法重定向：每一跳都重校验（connect 每次都被调用、拿到各自的固定 IP）。"""
    resolver = _resolver_fake(per_host={
        "cf.10xgenomics.com": ["8.8.8.8"],
        "cdn.10xgenomics.com": ["1.1.1.1"],
    })
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x": FakeResp(b"", 302, location="https://cdn.10xgenomics.com/y"),
        "https://cdn.10xgenomics.com/y": FakeResp(b"FINAL", 200),
    })
    resp = DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                                allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert resp.read() == b"FINAL"
    assert connect.calls == [
        ("https://cf.10xgenomics.com/x", "8.8.8.8"),
        ("https://cdn.10xgenomics.com/y", "1.1.1.1"),
    ]
    assert resolver.calls == ["cf.10xgenomics.com", "cdn.10xgenomics.com"]


def test_safe_open_redirect_relative_location():
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({
        "https://cf.10xgenomics.com/a/b": FakeResp(b"", 302, location="c"),
        "https://cf.10xgenomics.com/a/c": FakeResp(b"OK", 200),
    })
    resp = DE._open_stream_safe("https://cf.10xgenomics.com/a/b", 5,
                                allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert resp.read() == b"OK"


def test_safe_open_redirect_to_unknown_host_blocked():
    resolver = _resolver_fake(per_host={
        "cf.10xgenomics.com": ["8.8.8.8"],
        "evil.example.com": ["6.6.6.6"],
    })
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x": FakeResp(b"", 302, location="https://evil.example.com/x"),
        "https://evil.example.com/x": FakeResp(b"EVIL", 200),
    })
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert e.value.code == "url_blocked" and "evil.example.com" in str(e.value)
    assert [u for u, _ in connect.calls] == ["https://cf.10xgenomics.com/x"], "第二跳不应发起连接"


def test_safe_open_redirect_to_http_blocked():
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x": FakeResp(b"", 302, location="http://cf.10xgenomics.com/x"),
    })
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert e.value.code == "url_blocked" and "不是 https" in str(e.value)


def test_safe_open_redirect_to_loopback_ip_host_blocked():
    """重定向目标主机在白名单内，但解析到回环/私网 → IP 闸拦截（DNS rebinding 形态：解析层喂坏 IP）。"""
    resolver = _resolver_fake(per_host={
        "cf.10xgenomics.com": ["8.8.8.8"],
        "cdn.10xgenomics.com": ["127.0.0.1"],   # 白名单主机被污染成回环
    })
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x": FakeResp(b"", 302, location="https://cdn.10xgenomics.com/y"),
    })
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert e.value.code == "ip_blocked" and "127.0.0.1" in str(e.value)


def test_safe_open_redirect_loop_over_three_hops_blocked():
    """重定向环：3 跳后仍重定向 → redirect_limit；connect 只发起 3 次（第 4 跳在连接前被拒）。"""
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x": FakeResp(b"", 302, location="https://cf.10xgenomics.com/x"),
    })
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert e.value.code == "redirect_limit"
    assert len(connect.calls) == 3


def test_safe_open_redirect_without_location():
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({"https://cf.10xgenomics.com/x": FakeResp(b"", 302, location=None)})
    with pytest.raises(urllib.error.HTTPError):
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=ALLOWED, resolver=resolver, connect=connect)


def test_safe_open_dns_rebinding_pinned_no_second_resolution():
    """防 rebinding 的关键不变量：每跳只解析一次，连接拿到的是「已校验的固定 IP」——
    攻击者在连接阶段二次查询换成私网 IP 也影响不到连接目标（connect 收到的 ip 就是解析结果）。"""
    resolver = _resolver_fake(["8.8.8.8"])
    connect = _connect_fake({"https://cf.10xgenomics.com/x": FakeResp(b"OK", 200)})
    DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                         allowed_hosts=ALLOWED, resolver=resolver, connect=connect)
    assert len(resolver.calls) == 1
    assert connect.calls[0][1] == "8.8.8.8", "必须用解析并校验过的固定 IP 建连"


def test_safe_open_without_whitelist_still_enforces_ip_gate():
    """`_open_stream` 形态（allowed_hosts=None）跳过主机白名单，但 https/IP 闸依然生效。"""
    resolver = _resolver_fake(["127.0.0.1"])
    connect = _connect_fake()
    with pytest.raises(DE.DownloadPolicyError) as e:
        DE._open_stream_safe("https://cf.10xgenomics.com/x", 5,
                             allowed_hosts=None, resolver=resolver, connect=connect)
    assert e.value.code == "ip_blocked"


# ================================================================ D. download_one 集成（硬上限 / 策略拒绝）

def test_too_large_aborts_cleans_part_no_retry(tmp_path):
    """声明 100 字节、服务器实发 1 MiB → 硬上限（100×1.05）立即中止；.part 清理；不重试。"""
    sleeps = []
    row = _row(b"x" * (1024 * 1024), bytes_=100)
    r = DE.download_one(row, tmp_path, ALLOWED, opener=_opener_fake(
        {"https://cf.10xgenomics.com/x/f.h5": FakeResp(b"x" * (1024 * 1024), 200)}),
        sleep=sleeps.append, max_attempts=3)
    assert r.status == DE.STATUS_UNREACHABLE
    assert "硬上限" in (r.error or "")
    assert r.http_status is None, "主动中止不是 HTTP 结论"
    assert r.attempts == 1 and sleeps == [], "超限是确定答案，不能触发退避重试"
    assert not (tmp_path / "10x_A" / "f.h5.part").exists(), ".part 必须被清理"
    assert not (tmp_path / "10x_A" / "f.h5").exists()


def test_no_declared_size_capped_by_global(tmp_path, monkeypatch):
    """无声明大小（verify=none 形态）→ 按全局单文件上限（monkeypatch 调小验证闸本身）。"""
    monkeypatch.setattr(DE, "GLOBAL_FILE_CAP", 2048)
    row = _row(b"z" * (10 * 1024), bytes_=None)
    r = DE.download_one(row, tmp_path, ALLOWED, opener=_opener_fake(
        {"https://cf.10xgenomics.com/x/f.h5": FakeResp(b"z" * (10 * 1024), 200)}),
        sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE
    assert "硬上限" in (r.error or "")
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()


def test_content_length_over_limit_early_abort(tmp_path):
    """服务器 Content-Length 声明已超硬上限 → 一个字都不读、不落任何字节即中止。"""
    row = _row(b"", bytes_=10)
    opener = _opener_fake({
        "https://cf.10xgenomics.com/x/f.h5": FakeResp(b"x" * 5, 200, headers={"Content-Length": "99999999"})})
    r = DE.download_one(row, tmp_path, ALLOWED, opener=opener, sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE and "硬上限" in (r.error or "")
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()


def test_no_content_length_still_streams_then_caps(tmp_path, monkeypatch):
    """无 Content-Length 头（攻击者不限流输出）→ 流式循环按硬上限截断。"""
    monkeypatch.setattr(DE, "GLOBAL_FILE_CAP", 4096)
    row = _row(b"y" * 8192, bytes_=None)  # 无声明大小 + 无 CL → 全局上限兜底
    r = DE.download_one(row, tmp_path, ALLOWED, opener=_opener_fake(
        {"https://cf.10xgenomics.com/x/f.h5": FakeResp(b"y" * 8192, 200)}),
        sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE and "硬上限" in (r.error or "")
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()


def test_policy_error_in_opener_maps_rejected_no_retry(tmp_path):
    """opener 抛 DownloadPolicyError（重定向到禁 IP 等）→ rejected + 原因保留，不重试。"""
    sleeps = []

    def opener(url, timeout):
        raise DE.DownloadPolicyError("ip_blocked", "主机 x 解析到被禁止的地址：127.0.0.1")

    r = DE.download_one(_row(b"ok"), tmp_path, ALLOWED, opener=opener,
                        sleep=sleeps.append, max_attempts=3)
    assert r.status == DE.STATUS_REJECTED
    assert "127.0.0.1" in (r.error or "")
    assert r.attempts == 1 and sleeps == []


def test_too_large_does_not_clobber_cancel_semantics(tmp_path):
    """硬上限与取消互不干扰：先触发硬上限 → unreachable + 清理；取消保留 .part 的既有钉
    在 test_download_executor.py（两分支的 status 边界不互串）。"""
    row = _row(b"k" * 4096, bytes_=10)
    r = DE.download_one(row, tmp_path, ALLOWED, opener=_opener_fake(
        {"https://cf.10xgenomics.com/x/f.h5": FakeResp(b"k" * 4096, 200)}),
        sleep=lambda s: None, cancel_event=threading.Event())
    assert r.status == DE.STATUS_UNREACHABLE
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()


def test_download_one_follows_whitelisted_redirect_end_to_end(tmp_path):
    """download_one 全流程走带白名单的策略 opener：白名单内重定向 → 下载成功并 md5 核验。"""
    content = b"final-content-bytes" * 10
    resolver = _resolver_fake(per_host={
        "cf.10xgenomics.com": ["8.8.8.8"],
        "cdn.10xgenomics.com": ["1.1.1.1"],
    })
    connect = _connect_fake({
        "https://cf.10xgenomics.com/x/f.h5": FakeResp(b"", 302,
                                                      location="https://cdn.10xgenomics.com/f.h5"),
        "https://cdn.10xgenomics.com/f.h5": FakeResp(content, 200),
    })

    def opener(url, timeout):
        return DE._open_stream_safe(url, timeout, allowed_hosts=ALLOWED,
                                    resolver=resolver, connect=connect)

    row = _row(content)
    r = DE.download_one(row, tmp_path, ALLOWED, opener=opener, sleep=lambda s: None)
    assert r.status == DE.STATUS_OK
    assert (tmp_path / "10x_A" / "f.h5").read_bytes() == content
    assert r.md5_actual == _md5(content)


# ================================================================ E. manager 级（默认 opener + 上传代下开关）

def _wait_job_terminal(job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = DM.get_status(job_id)
        if st and st["state"] in ("done", "error", "cancelled") and st["finished_at"]:
            return st
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 在 {timeout}s 内未到达终态")


def test_manager_default_opener_is_policy_opener(tmp_path, monkeypatch):
    """start_job 不注入 opener 时走生产 `_policy_opener`（每跳+IP 闸）——用假的
    `_resolve_host` / `_connect_pinned` 驱动，证明默认管线已接入 SSRF 防线。"""
    content = b"A" * 3000
    monkeypatch.setattr(DE, "_resolve_host", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(DE, "_connect_pinned",
                        lambda url, timeout, *, ip, context=None: FakeResp(content, 200))
    rec = {"dataset_uid": "cxg:g", "url": "https://cellxgene.cziscience.com/e/g",
           "download_url": "https://datasets.cellxgene.cziscience.com/g.h5ad",
           "filesize": len(content), "source": "CELLxGENE Discover", "dataset_name": "G"}
    job = DM.start_job(["cxg:g"], records={rec["dataset_uid"]: rec}, out_dir=str(tmp_path))
    st = _wait_job_terminal(job["job_id"])
    assert st["state"] == "done"
    assert st["files"][0]["status"] == "size_ok"


def test_manager_default_opener_rejects_private_ip(tmp_path, monkeypatch):
    """manager 默认管线（不注入 opener）在 IP 闸前 fail-closed：解析到私网 → 文件 error。"""
    content = b"B" * 3000
    monkeypatch.setattr(DE, "_resolve_host", lambda host: ["192.168.0.5"])
    monkeypatch.setattr(DE, "_connect_pinned",
                        lambda url, timeout, *, ip, context=None: FakeResp(content, 200))
    rec = {"dataset_uid": "cxg:h", "url": "https://cellxgene.cziscience.com/e/h",
           "download_url": "https://datasets.cellxgene.cziscience.com/h.h5ad",
           "filesize": len(content), "source": "CELLxGENE Discover", "dataset_name": "H"}
    job = DM.start_job(["cxg:h"], records={rec["dataset_uid"]: rec}, out_dir=str(tmp_path))
    st = _wait_job_terminal(job["job_id"])
    assert st["state"] == "done"          # job 整体照常结束
    assert st["files"][0]["status"] == "error"
    assert "禁止的地址" in st["files"][0]["error"]
    assert not list(tmp_path.rglob("*.part")), "被拒文件不得残留 .part"


def test_manager_block_user_uploaded_switch_on(monkeypatch):
    monkeypatch.setenv("BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED", "1")
    up = {"dataset_uid": "up:1", "url": "https://example.org/ds",
          "download_url": "https://example.org/f.h5ad", "filesize": 123456,
          "source": "用户上传", "dataset_name": "U"}
    cxg = {"dataset_uid": "cxg:1", "url": "https://cellxgene.cziscience.com/e/1",
           "download_url": "https://datasets.cellxgene.cziscience.com/1.h5ad",
           "filesize": 123456, "source": "CELLxGENE Discover", "dataset_name": "C"}
    plan = DM.build_download_plan(["up:1", "cxg:1"],
                                  records={up["dataset_uid"]: up, cxg["dataset_uid"]: cxg})
    assert {u["dataset_uid"] for u in plan["unsupported"]} == {"up:1"}
    assert {it["dataset_uid"] for it in plan["items"]} == {"cxg:1"}
    assert "BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED" in plan["unsupported"][0]["reason"]


def test_manager_block_user_uploaded_switch_off_by_default(monkeypatch):
    monkeypatch.delenv("BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED", raising=False)
    up = {"dataset_uid": "up:1", "url": "https://example.org/ds",
          "download_url": "https://example.org/f.h5ad", "filesize": 123456,
          "source": "用户上传", "dataset_name": "U"}
    plan = DM.build_download_plan(["up:1"], records={up["dataset_uid"]: up})
    assert not plan["unsupported"]
    assert [it["dataset_uid"] for it in plan["items"]] == ["up:1"]


def test_manager_finalize_too_large_message(tmp_path):
    """超硬上限在 manager 文件状态里给专门文案，不套「重试后仍未能下载完整文件」。"""
    sleeps = []

    def opener(url, timeout):
        raise DE._DownloadTooLarge(105)

    row = _row(b"x" * 200, bytes_=100)
    r = DE.download_one(row, tmp_path, ALLOWED, opener=opener, sleep=sleeps.append)
    job = {"dir": str(tmp_path), "_subdirs": {"10x:A": "10x_A__X"},
           "_plan_items": [], "unsupported": [], "state": "done", "started_at": "",
           "finished_at": "", "cancel_requested": False, "_cancel_event": threading.Event(),
           "_manifest_path": str(tmp_path / "manifest.tsv"), "_readme_path": str(tmp_path / "README.txt"),
           "_manifest_error": False}
    entry = {"dataset_uid": "10x:A", "dataset_title": "X", "filename": "f.h5", "url": row["download_url"],
             "bytes": 100, "done_bytes": 0, "status": "downloading", "error": "", "saved_as": "",
             "md5_actual": "", "http_status": None}
    DM._finalize_file(job, entry, r, row)
    assert entry["status"] == "error"
    assert "硬上限" in entry["error"] and "重试" not in entry["error"]
