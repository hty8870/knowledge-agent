# -*- coding: utf-8 -*-
"""交付打包 + 交付安全复核。

做两件事：
1. **剔除**：按可选的仓库根 `.deliveryignore`（gitignore 风格附加排除清单，文件缺失时为空）
   + 硬排除（.git/.venv/__pycache__/秘密/`.userdata`）+ **git 对账**收集交付文件集——
   只有被 git 跟踪的文件才可能进包，交付包与干净 clone 对齐。
2. **复核**：对**留在包里**的文本文件做敏感词扫描（本机个人绝对路径等）与 secret 值扫描；
   命中即视为泄漏，拒绝打包并逐条报告（`file:line token=…`，不回显整行）。

用法：
  <python> scripts/make_delivery.py --check            # 只复核：有泄漏→退出 1，干净→0（CI/打包前门）
  <python> scripts/make_delivery.py --list             # 打印将纳入/排除的文件集（人工核对，不打包）
  <python> scripts/make_delivery.py --out dist/biodata-delivery.zip   # 复核通过后打包 ZIP

设计：纯标准库、确定性、跨平台。函数可被 tests/test_delivery_safety.py 直接 import 做持续护栏。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 锚定 secret 值模式的单一真源（与 quality_gate.py 的 report 脱敏共用同一批模式，防"什么算 secret"漂移）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_patterns import SECRET_VALUE_PATTERNS  # noqa: E402

# 交付包**绝不能出现**的敏感词。命中即拒绝打包。
# 分两层，避免把任何个人身份信息留在源码里：
# - **通用层**（源码持有）：本机 `Users\<用户名>` 绝对路径形态。从环境推导当前用户名而非
#   硬编码——任何贡献者在本机跑门禁都能查自己路径形态的泄漏，源码里不落任何人的用户名；
#   教程里的 C:\Users\<用户名> 模板不算泄漏、刻意不匹配。需要时再扩。
def _current_user_name() -> "str | None":
    """当前登录用户名（跨平台）：先看常见环境变量，再兜底 getpass。取不到返回 None。"""
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var)
        if value:
            return value
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return None


def _user_home_path_tokens() -> tuple[str, ...]:
    r"""当前用户的 `Users\<name>` 风格路径检测词：反斜杠 / 正斜杠 / 小写正斜杠形，去重保序。"""
    name = _current_user_name()
    if not name:
        return ()
    return tuple(dict.fromkeys((
        f"Users\\{name}",
        f"Users/{name}",
        f"Users/{name.lower()}",
    )))


GENERIC_FORBIDDEN_TOKENS: tuple[str, ...] = _user_home_path_tokens()
# 历史迭代代号清单已退役：批次/波次标注（日期+机制）是仓库注释的正常形态，门禁聚焦
# 个人路径形态（上方）与 secret 值锚定模式（扫描层），个人化 token 走 .delivery-tokens.local。
# - **个人层**（本机加载）：真名 / 邮箱前缀 / 内部称谓等个人化 token 一律不进源码，由仓库根
#   `.delivery-tokens.local` 提供（gitignored、与 .env 同级处理；每行一个 token，`#` 起始为注释，
#   空行忽略）。文件缺失时跳过并打印提示——交付安全检查不因此失效（通用层仍在），干净 clone
#   也能直接跑门禁；扫描覆盖只在持有该文件的机器上包含个人化 token，这是外置的代价与共识。
LOCAL_TOKENS_FILE = REPO_ROOT / ".delivery-tokens.local"

# 仓库一级目录整理：启动脚本群迁入 launchers/、requirements 群迁入
# requirements/、mcp_server.py 迁入 src/dataset_recommender/app/、测试说明.txt 迁入 docs/。
# **对外交付布局不变**——交付 zip / 提交包内这些文件仍按历史根目录相对路径呈现
# （scripts/build_release.py 的 SOURCE_PATH_OVERRIDES 是本表的反向，覆盖同一批文件）。
LEGACY_DELIVERY_PATHS = {
    "launchers/打开前端.bat": "打开前端.bat",
    "launchers/打开前端.command": "打开前端.command",
    "launchers/打开前端.sh": "打开前端.sh",
    "launchers/创建桌面快捷方式.bat": "创建桌面快捷方式.bat",
    "launchers/start-web.bat": "start-web.bat",
    "requirements/requirements.txt": "requirements.txt",
    "requirements/requirements-analytics.txt": "requirements-analytics.txt",
    "requirements/requirements-analytics.lock": "requirements-analytics.lock",
    "requirements/requirements-ci.txt": "requirements-ci.txt",
    "requirements/requirements-ci.lock": "requirements-ci.lock",
    "requirements/requirements-embeddings.txt": "requirements-embeddings.txt",
    "requirements/requirements-langchain.txt": "requirements-langchain.txt",
    "requirements/requirements-loadsmoke.txt": "requirements-loadsmoke.txt",
    "requirements/requirements-webview.txt": "requirements-webview.txt",
    "src/dataset_recommender/app/mcp_server.py": "mcp_server.py",
    "docs/测试说明.txt": "测试说明.txt",
}


def delivery_arcname(relative: str) -> str:
    """仓库相对路径 → 交付包内（历史布局）相对路径（未迁移文件恒等）。"""
    return LEGACY_DELIVERY_PATHS.get(relative, relative)


def load_local_forbidden_tokens(path: Path = LOCAL_TOKENS_FILE) -> tuple[str, ...]:
    """读本地个人化敏感 token 清单；文件缺失时打印一句提示并返回空元组（门禁照常运行）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"[交付安全] 提示：未找到 {path.name}，跳过个人化敏感词加载；通用 token 扫描不受影响。",
              file=sys.stderr)
        return ()
    tokens: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens.append(line)
    return tuple(tokens)


FORBIDDEN_TOKENS: tuple[str, ...] = GENERIC_FORBIDDEN_TOKENS + load_local_forbidden_tokens()

# 无论附加排除清单是否列出、也无论 git 是否可用，都硬排除的目录/文件名（秘密与缓存绝不外发）。
# `.userdata` 在此不是"再列一遍"：它装的是账户名 + scrypt salt/pwd_hash，是本仓库**唯一**在磁盘上的
# 用户凭据。git 对账（见 gitignored_paths）已经能挡住它，但凭据不该只有一道门——git 不可用时那道门会
# fail-closed 报错，而这道门在任何情况下都成立。
_HARD_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".userdata",
}
# 真正的 env 文件一律硬排除；但 `.env.example` / `.env.zhipu.example` 是**模板**（只有占位符），
# README 与 MCP 教程都指名让用户复制它们来建自己的 `.env`——把模板也挡掉，交付包里那条指引就断了。
# 模板仍会被后面的 secret 值扫描覆盖：万一有人把真 key 粘进模板，打包照样会被拒。
_HARD_SKIP_FILE_RE = re.compile(r"^\.env(\..+)?$")
_ENV_TEMPLATE_RE = re.compile(r"^\.env(\..+)?\.example$")

# 精确行号豁免机制保留为 fail-closed 逃生口，但默认必须为空。需要验证遮蔽行为的测试假 key
# 一律在运行时由不命中扫描器的片段拼接，源码不再保存完整 secret 形状；这样测试既能覆盖真实
# 值级遮蔽，又不需要让交付门为测试夹具开洞。若未来确需新增豁免，必须逐行审计并同步专项契约。
SECRET_SCAN_ALLOWLIST: "frozenset[tuple[str, int]]" = frozenset()

_GIT_TIMEOUT_S = 120


class GitUnavailable(RuntimeError):
    """无法用 git 核验交付集与 .gitignore 是否对账。

    这是**安全门**，故 fail-closed：核验不了就不许打包，而不是静默按"没有被忽略的文件"放行。
    本脚本只在仓库工作树里运行，git 缺失属异常环境、应当报出来。
    """

# 只扫这些扩展名的文本文件（模型权重/图片/压缩包等二进制不扫、也很少进交付）。
# `.mjs`（ES module 脚本）与 `.example`（示例配置）同属文本，泄漏面一并覆盖。
_TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css", ".md", ".markdown", ".txt",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".ps1", ".sh", ".bat",
    ".csv", ".tsv", ".xml", ".rst", ".example",
    # 安装器/打包脚本与依赖锁定文件同为纯文本，泄漏面一并覆盖
    ".iss", ".isl", ".command", ".spec", ".lock", ".in", ".manifest",
}

# 无后缀或「整名即文件名」的文本文件（dotfile 的 Path.suffix 是空串，只能按整名匹配）。
_TEXT_FILENAMES = {
    "Dockerfile", "LICENSE", ".gitignore", ".gitattributes", ".dockerignore",
}


def _is_scannable_text(path: Path) -> bool:
    """该路径是否属于应被文本扫描覆盖的文件（后缀或整名任一命中）。"""
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in _TEXT_FILENAMES


def _read_scannable_text(path: Path) -> "str | None":
    """读出文本内容：先 UTF-8，解码失败再按 GBK strict 重试（Windows 侧安装器脚本常见 GBK）。

    两种编码都失败或文件不可读时返回 None——调用方必须把它当「没扫到」处理，
    不许静默当干净（见 unscannable_text_files 的 fail-closed 语义）。
    含 NUL 字节的文件（典型：UTF-16 文本）也按 None 处理——GBK 能把 UTF-16LE 解成
    夹 \\x00 的串（0x00 在 GBK 合法），secret 正则被 NUL 隔断会整体失配，
    宁可 fail-closed 报 unscannable，也不放行扫描失效的假阴性。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    text: "str | None"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = None
    if text is None:
        return None
    # 保持旧 read_text() 的 universal-newlines 语义（\r\n / \r 一律归一为 \n），行号口径不漂移。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _pattern_to_regex(pattern: str) -> "re.Pattern[str] | None":
    """把一条 gitignore 风格 glob 编译成匹配 POSIX 相对路径的正则。支持 `dir/`、`**`、`*`、`?`、前导 `/`。"""
    p = pattern.strip()
    if not p or p.startswith("#"):
        return None
    anchored = "/" in p.strip("/")            # 含内部斜杠 → 锚定仓库根；否则匹配任意层级的同名段
    is_dir = p.endswith("/")
    p = p.lstrip("/").rstrip("/")
    body = p.replace("**", "\x00")
    out: list[str] = []
    for ch in body:
        if ch == "\x00":
            out.append(".*")
        elif ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    rx = "^" + "".join(out) if anchored else "(^|/)" + "".join(out)
    rx += "(/|$)" if is_dir else "$"
    return re.compile(rx)


def load_ignore_patterns(root: Path = REPO_ROOT) -> list["re.Pattern[str]"]:
    """读仓库根 .deliveryignore → 编译后的正则列表。文件缺失返回空（此时只靠硬排除）。"""
    f = root / ".deliveryignore"
    if not f.exists():
        return []
    out: list[re.Pattern[str]] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        rx = _pattern_to_regex(line)
        if rx is not None:
            out.append(rx)
    return out


def _is_ignored(rel_posix: str, patterns: list["re.Pattern[str]"]) -> bool:
    return any(rx.search(rel_posix) for rx in patterns)


def tracked_paths(root: Path = REPO_ROOT) -> set[str]:
    """仓库当前**被 git 跟踪**的全部文件（相对 POSIX 路径）。

    **为什么交付集要与「已跟踪」取交集**：`gitignored_paths` 只挡「git 明确忽略」的文件，
    挡不住**未跟踪且未被忽略**的那一类——个人文件、临时导出、误放进工作树的草稿；
    `.pdf` 等后缀不在 `_TEXT_SUFFIXES` 里，两个扫描器都不会看它们。

    正确语义在脚本开头就写着：**交付包应当与干净 clone 一致**。干净 clone 里只有被跟踪的文件，
    所以这里直接按「已跟踪」取交集——对未来任何新增的未跟踪文件自动成立，不需要有人记得补 glob。

    与 `gitignored_paths` 同样 fail-closed：git 不可用 → `GitUnavailable`，不放行。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitUnavailable(f"无法执行 git ls-files：{exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitUnavailable(f"git ls-files 退出码 {proc.returncode}：{detail}")
    return {p for p in proc.stdout.decode("utf-8").split("\0") if p}


def gitignored_paths(rel_posix_paths: "list[str]", root: Path = REPO_ROOT) -> set[str]:
    """在给定相对 POSIX 路径中，挑出被 git 忽略（= git 拒绝跟踪）的那些。

    **为什么问 git 而不是自己解析 `.gitignore`**（本文件明明有个 `_pattern_to_regex`）：
    1. **git 看 index**。gitignore 规则对**已跟踪**文件无效，`check-ignore` 默认据此不报它们。自己拿
       `_pattern_to_regex` 去匹配 `.gitignore`，会把碰巧撞上某条规则的已跟踪文件一起错杀出交付包。
    2. gitignore 的真实语义（`!` 反选、目录短路、多级 `.gitignore`、`core.excludesFile`、全局 excludes）
       远比这里那个"够用就好"的 glob 编译器复杂。这里要的是 **git 自己的答案**，不是一个近似。

    **为什么用 `-z`**：输入输出都以 NUL 分隔。不能用换行分隔——Windows 上 subprocess 的 text 模式会把
    `\\n` 翻成 `\\r\\n`，git 会把尾随的 `\\r` 当作文件名的一部分（并因此给整个路径加引号转义）；`-z`
    同时绕开 `core.quotepath` 对非 ASCII 路径的八进制转义（本仓库路径含中文）。

    退出码语义：0=有命中，1=无命中（都是正常），其它=真出错 → GitUnavailable（fail-closed）。
    """
    if not rel_posix_paths:
        return set()
    payload = "\0".join(rel_posix_paths).encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=str(root), input=payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # git 缺失 / 超时 / 无法启动
        raise GitUnavailable(f"无法执行 git check-ignore：{exc}") from exc
    if proc.returncode not in (0, 1):
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitUnavailable(f"git check-ignore 退出码 {proc.returncode}：{detail}")
    return {p for p in proc.stdout.decode("utf-8").split("\0") if p}


def collect_delivery_files(root: Path = REPO_ROOT) -> list[Path]:
    """收集对外交付文件（相对仓库根）。返回排序后的绝对路径列表。

    三道剔除，任一命中即出局：
    1. 硬排除（`_HARD_SKIP_DIRS` / `.env*`）——不依赖任何外部工具，凭据与缓存的兜底；
    2. **git 对账**——只保留**被 git 跟踪**的文件（`tracked_paths`）。这一步同时覆盖了「被忽略」和
       「未跟踪但没被忽略」两类：干净 clone 里两者都不存在。语义上，交付包应当与**干净 clone** 一致；
    3. 可选 `.deliveryignore`——维护者自加的附加排除清单（文件缺失时该层为空）。
    """
    patterns = load_ignore_patterns(root)
    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(seg in _HARD_SKIP_DIRS for seg in rel.parts):
            continue
        if _HARD_SKIP_FILE_RE.match(path.name) and not _ENV_TEMPLATE_RE.match(path.name):
            continue
        if _is_ignored(rel.as_posix(), patterns):
            continue
        candidates.append(path)
    tracked = tracked_paths(root)
    return [p for p in candidates if p.relative_to(root).as_posix() in tracked]


def scan_forbidden(files: list[Path], root: Path = REPO_ROOT) -> list[dict]:
    """对交付文件集里的文本文件扫敏感词，返回违规列表 [{path, line, token}]（不含整行内容，避免二次泄漏）。"""
    # 本文件是词表的法定存放点（检测词字面量必然命中自身），自豁免；
    # 真凭据层（scan_secret_values）不豁免本文件。root 不在仓库内（单测临时目录）时跳过自豁免。
    try:
        self_rel: str | None = Path(__file__).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        self_rel = None
    violations: list[dict] = []
    for path in files:
        if not _is_scannable_text(path):
            continue
        if self_rel is not None and path.relative_to(root).as_posix() == self_rel:
            continue
        text = _read_scannable_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in FORBIDDEN_TOKENS:
                if token in line:
                    violations.append({
                        "path": path.relative_to(root).as_posix(),
                        "line": lineno,
                        "token": token,
                    })
    return violations


# 空标签冒号机械门：注释/docstring 行首（注释符后）直接出现全角冒号。
_ORPHAN_COLON_RE = re.compile(r"^\s*(?:#+\s*|//+\s*|/\*+\s*|\*\s*|<!--\s*)?：")


def scan_orphan_colons(files: list[Path], root: Path = REPO_ROOT) -> list[dict]:
    """扫交付文件集里行首（注释符后）直接出现全角冒号的空标签残骸，返回 [{path, line}]。

    这是删除行首标签后留下的机械残骸（曾与评测用例 note 的孤儿冒号同族），不是正常
    标点风格：正常中文书写不会把全角冒号放在行首，误报率近零，故做常驻机械门。
    本文件自豁免（词表与正则字面量必然命中自身）。"""
    try:
        self_rel: str | None = Path(__file__).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        self_rel = None
    hits: list[dict] = []
    for path in files:
        if not _is_scannable_text(path):
            continue
        if self_rel is not None and path.relative_to(root).as_posix() == self_rel:
            continue
        text = _read_scannable_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ORPHAN_COLON_RE.match(line):
                hits.append({"path": path.relative_to(root).as_posix(), "line": lineno})
    return hits


def scan_secret_values(
    files: list[Path], root: Path = REPO_ROOT,
    allowlist: "set[tuple[str, int]] | frozenset[tuple[str, int]] | None" = None,
) -> list[dict]:
    """扫交付文件集里的文本文件是否含**锚定的 secret 值模式**（真 key 被误粘进 .md/.json/.py 等）。
    现有 scan_forbidden 只抓内部专名、抓不到 secret 值；`.env` 虽被硬排除，但一个真 key 粘进某个
    示例/配置/注释文本，此前交付门一个字都拦不住。返回 [{path, line, pattern}]——**只含 pattern_id，
    绝不含命中的实际值**（避免把真 secret 写进日志=二次泄漏）。

    `allowlist` 是已审计的「(相对路径, 行号)」豁免集（默认 `SECRET_SCAN_ALLOWLIST`）：只跳过
    **精确**命中的行，其余任何命中照常上报——门禁不掉，测试夹具白名单化也不静默。
    """
    allowlist = SECRET_SCAN_ALLOWLIST if allowlist is None else allowlist
    violations: list[dict] = []
    for path in files:
        if not _is_scannable_text(path):
            continue
        text = _read_scannable_text(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if (rel, lineno) in allowlist:
                continue
            for pattern_id, rx in SECRET_VALUE_PATTERNS:
                if rx.search(line):
                    violations.append({
                        "path": rel,
                        "line": lineno,
                        "pattern": pattern_id,
                    })
    return violations


_CONTACT_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9._%+\-])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}(?![A-Z0-9._%+\-])"
)


def scan_metadata_contacts(files: list[Path], root: Path = REPO_ROOT) -> list[dict]:
    """Reject contact email values embedded in distributable metadata snapshots.

    Only database JSON snapshots are in scope.  Locations are returned without
    the matched address so CI logs cannot become a second disclosure channel.
    """
    violations: list[dict] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if not rel.startswith("database/") or path.suffix.lower() != ".json":
            continue
        text = _read_scannable_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _CONTACT_EMAIL_RE.search(line):
                violations.append({"path": rel, "line": lineno})
    return violations


def count_allowlisted_secret_hits(
    files: list[Path], root: Path = REPO_ROOT,
    allowlist: "set[tuple[str, int]] | frozenset[tuple[str, int]] | None" = None,
) -> int:
    """统计被 `SECRET_SCAN_ALLOWLIST` 豁免的 (文件, 行) 命中数，供 `--check` 透明度展示。

    只作审计可见性，**不参与门禁**（门禁看 `scan_secret_values` 的返回值——白名单行已被跳过，
    不会被误报）。单独实现避免侵入 `scan_secret_values` 的返回形状。
    """
    allowlist = SECRET_SCAN_ALLOWLIST if allowlist is None else allowlist
    count = 0
    for path in files:
        if not _is_scannable_text(path):
            continue
        text = _read_scannable_text(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if (rel, lineno) not in allowlist:
                continue
            if any(rx.search(line) for _, rx in SECRET_VALUE_PATTERNS):
                count += 1
    return count


def unscannable_text_files(files: list[Path], root: Path = REPO_ROOT) -> list[str]:
    """交付集里应被文本扫描覆盖、但 UTF-8 与 GBK 都解码失败（或不可读）的文件。

    这类文件会被各扫描器跳过，等于藏 key 的文件报「0 命中」——故 main() 把它计入
    门禁判定（fail-closed），有即拒绝打包，杜绝静默放行。"""
    out: list[str] = []
    for path in files:
        if not _is_scannable_text(path):
            continue
        if _read_scannable_text(path) is None:
            out.append(path.relative_to(root).as_posix())
    return out


def excluded_internal(root: Path = REPO_ROOT) -> list[str]:
    """被 .deliveryignore 显式挡下的真实存在文件（供人工核对"排除了哪些内部件"，杜绝静默漏掉/多剔）。"""
    patterns = load_ignore_patterns(root)
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(seg in _HARD_SKIP_DIRS for seg in rel.parts):
            continue
        if _is_ignored(rel.as_posix(), patterns):
            out.append(rel.as_posix())
    return out


def excluded_by_git(root: Path = REPO_ROOT) -> "dict[str, list[str]]":
    """被 git 对账挡下、否则会进交付包的文件（供 `--list` 人工核对这道门到底剔了什么）。

    只统计"过了硬排除和 .deliveryignore、但 git 不跟踪"的那批——即这道门的**净增量**贡献，
    并按两类分开报：
    - `ignored`：`.gitignore` 明确忽略（缓存、权重、账户数据…）；
    - `untracked`：没被忽略、但也没被 git 跟踪（个人文件、临时导出、别人的草稿）。
      这一类最危险——它不会出现在任何 denylist 里，只有「与干净 clone 对齐」才挡得住。
    """
    patterns = load_ignore_patterns(root)
    candidates: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(seg in _HARD_SKIP_DIRS for seg in rel.parts):
            continue
        if _HARD_SKIP_FILE_RE.match(path.name) and not _ENV_TEMPLATE_RE.match(path.name):
            continue
        if _is_ignored(rel.as_posix(), patterns):
            continue
        candidates.append(rel.as_posix())
    tracked = tracked_paths(root)
    dropped = [c for c in candidates if c not in tracked]
    ignored = gitignored_paths(dropped, root)
    return {
        "ignored": sorted(ignored),
        "untracked": sorted(c for c in dropped if c not in ignored),
    }


def build_zip(out_path: Path, files: list[Path], root: Path = REPO_ROOT) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=delivery_arcname(path.relative_to(root).as_posix()))
    return len(files)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="交付打包 + 交付安全复核")
    ap.add_argument("--check", action="store_true", help="只复核（敏感词扫描），不打包；有泄漏退出 1")
    ap.add_argument("--list", action="store_true", help="打印纳入/排除的文件集，不打包")
    ap.add_argument("--out", help="打包目标 ZIP 路径（复核通过后才写）")
    ap.add_argument("--force", action="store_true", help="有泄漏时仍打包（不建议；仅调试）")
    args = ap.parse_args(argv)

    try:
        files = collect_delivery_files()
    except GitUnavailable as exc:
        print(f"\n[交付安全] 无法核验交付集与 .gitignore 是否对账：{exc}", file=sys.stderr)
        print("这道门挡的是「git 拒绝跟踪的文件混进交付包」（历史上漏过账户密码哈希与 2.27 GB 模型权重）。"
              "核验不了就不放行——请在仓库工作树内、用可用的 git 重跑。", file=sys.stderr)
        return 1

    violations = scan_forbidden(files)
    secret_hits = scan_secret_values(files)
    contact_hits = scan_metadata_contacts(files)
    colon_hits = scan_orphan_colons(files)
    allowlisted_hits = count_allowlisted_secret_hits(files)
    unscannable = unscannable_text_files(files)

    if args.list:
        excluded = excluded_internal()
        git_excluded = excluded_by_git()
        print(f"纳入交付：{len(files)} 个文件")
        print(f"排除内部件（.deliveryignore）：{len(excluded)} 个")
        for rel in excluded:
            print(f"  - {rel}")
        print(f"排除 git 忽略件：{len(git_excluded['ignored'])} 个")
        for rel in git_excluded["ignored"]:
            print(f"  - {rel}")
        print(f"排除未跟踪件（不在 git 里，干净 clone 中不存在）：{len(git_excluded['untracked'])} 个")
        for rel in git_excluded["untracked"]:
            print(f"  - {rel}")

    if unscannable:
        print(f"\n[交付安全] 发现 {len(unscannable)} 个应被文本扫描覆盖、但 UTF-8/GBK 均解码失败的文件"
              "（扫描覆盖不到＝可能藏着 secret，fail-closed 不放行；修复编码或将其移出交付集）：",
              file=sys.stderr)
        for rel in unscannable:
            print(f"  {rel}", file=sys.stderr)

    if violations:
        print(f"\n[交付安全] 发现 {len(violations)} 处内部专名/路径泄漏（交付文件集内，必须清除或加入 .deliveryignore）：",
              file=sys.stderr)
        for v in violations:
            print(f"  {v['path']}:{v['line']}  token={v['token']}", file=sys.stderr)
    if secret_hits:
        print(f"\n[交付安全] 发现 {len(secret_hits)} 处疑似 secret 值（锚定模式命中；只报模式名+位置，不回显值）：",
              file=sys.stderr)
        for h in secret_hits:
            print(f"  {h['path']}:{h['line']}  pattern={h['pattern']}", file=sys.stderr)
    if contact_hits:
        print(f"\n[交付安全] 发现 {len(contact_hits)} 处元数据联系邮箱（只报位置，不回显地址）：",
              file=sys.stderr)
        for h in contact_hits:
            print(f"  {h['path']}:{h['line']}", file=sys.stderr)
    if colon_hits:
        print(f"\n[交付安全] 发现 {len(colon_hits)} 处行首全角冒号残骸（删标签遗留的机械残骸，必须清除）：",
              file=sys.stderr)
        for h in colon_hits:
            print(f"  {h['path']}:{h['line']}", file=sys.stderr)

    if allowlisted_hits:
        # 已审计的脱敏测试夹具豁免：只作审计可见性，不参与门禁。
        print(f"\n[交付安全] {allowlisted_hits} 处已审计的脱敏测试夹具命中已按白名单豁免"
              "（make_delivery.SECRET_SCAN_ALLOWLIST，精确到 文件:行）：", file=sys.stderr)

    if violations or secret_hits or contact_hits or colon_hits or unscannable:
        if not args.force:
            print("\n拒绝打包。修掉上述泄漏，或把该文件加入 .deliveryignore 后重试。", file=sys.stderr)
            return 1
    else:
        # 与「已跟踪」取交是**双向**的：它挡住了个人文件混进包，但也会让**刚写的新模块**
        # 悄悄留在包外——客户拿到的包里，端点 import 的模块根本不存在。
        # 这一类只有在 --check 阶段 fail-closed 才拦得住：上一版这里恒打印「复核通过」。
        untracked = excluded_by_git()["untracked"]
        if untracked:
            print(f"[交付安全] 复核未通过：有 {len(untracked)} 个文件不在 git 里，"
                  "客户拿到的包中不会有它们。新模块请先 git add：", file=sys.stderr)
            for rel in untracked[:40]:
                print(f"  - {rel}", file=sys.stderr)
            if len(untracked) > 40:
                print(f"  …（共 {len(untracked)} 个）", file=sys.stderr)
            return 1
        total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
        print(f"[交付安全] 复核通过：交付文件集 {len(files)} 个（{total_mb:.1f} MiB），"
              f"0 内部专名 / 0 secret 值 / 0 元数据联系邮箱 / 0 冒号残骸 / 0 不可解码文本命中；"
              f"文件集已与 git 已跟踪集取交，"
              f"且工作树中没有游离在 git 之外的文件。")

    if args.out:
        n = build_zip(Path(args.out), files)
        print(f"已打包 {n} 个文件 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
