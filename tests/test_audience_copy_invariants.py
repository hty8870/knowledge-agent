"""受众文案不变量门：产品面向数据集**复用者**，不声称研究新颖性。

红线：语料是公开数据集**库存**、不是**文献库**——「本目录内无匹配」推不出「这个研究没人做过 /
这是新颖方向」。这类新颖性断言若进产品文案，会被中文研究者当成新颖性判断、甚至写进标书，
正是本项目一直在杀的编造同型（把「我不知道」讲成「不存在」）。

这道门把「受众/新颖性不变量」从口头约定变成机械护栏：扫描**运行期 / 客户面**文案，
钉死一批**具体新颖性断言短语**不出现。

隔离与边界（为什么这样划扫描面）：
- **扫**：前端 HTML/JS、后端**面向用户输出**的模块与 MCP、LLM 提示词、面向客户的 README/PRODUCT/
  DEVELOPMENT/使用教程。
- **不扫**（这些地方**定义/讨论**这条规则本身，合法出现禁语，不是给用户看的运行期文案）：
  - `database/`——源数据集标题里带 "novel" 是**别人论文的真名字**，改它才是另一种编造；
  - `tests/`——本门自己要把禁语当夹具写进来；
  - 内部工程文档与开发笔记——讨论规则本身；
  - `PRODUCT.md` / `DEVELOPMENT.md`——**内部设计/规格文档**（本不变量的人读定义就写在 PRODUCT.md，
    必须能点名这些禁语），不是运行期或客户 onboarding 文案。
- 短语是**具体新颖性断言**（没人做过 / 首次研究 / 填补空白…），**不是**裸词「首次」——
  「首次加载 / 首次运行 / 回溯首个实体 / 第一个非空值」都是合法技术表述，绝不误伤。

复用者受众不变量的人读版见 `PRODUCT.md`（Anti-references + Users）。改禁语清单两处一起改。
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 具体新颖性断言短语。每一条都是「一个数据集目录无从判断」的主张。
# 中文：断言式，避免裸「首次/首个/第一个」这类合法技术词。
_BANNED_ZH = [
    r"新颖性?",                    # 新颖 / 新颖性
    r"没人做过",
    r"没有人做过",
    r"有没有人(做过|研究过|发表过|做)",
    r"尚无人(研究|做过|做|发表)",
    r"无人研究过",
    r"前所未有",
    r"填补[^，。；\s]{0,8}空白",
    r"研究空白",
    r"首次(研究|发现|报道|报导|揭示|证明|提出|实现)",
    r"首创",
    r"(世界|全球|国内|业界|领域)首个",
    r"独家首发",
]
# 英文：短语级或语义唯一，避免裸词歧义（IGNORECASE）。
_BANNED_EN = [
    r"\bnovel(ty)?\b",
    r"\bunprecedented\b",
    r"\bno one has (studied|done|researched|reported)\b",
    r"\bfirst (to|ever) (study|report|describe|characteri[sz]e|discover)\b",
    r"\bnever been (studied|done|reported|characteri[sz]ed)\b",
    r"\bworld[- ]?first\b",
]

_BANNED = [(re.compile(p), p) for p in _BANNED_ZH] + [
    (re.compile(p, re.IGNORECASE), p) for p in _BANNED_EN
]


def _copy_files() -> list[Path]:
    """运行期 / 客户面文案文件集合（存在才纳入）。"""
    files: list[Path] = []
    # 前端
    idx = ROOT / "web" / "static" / "index.html"
    if idx.exists():
        files.append(idx)
    files += sorted((ROOT / "web" / "static" / "js").glob("*/*.js"))
    css = ROOT / "web" / "static" / "css" / "app.css"
    if css.exists():
        files.append(css)
    # 后端面向用户输出 + MCP（对具体新颖性断言而言，扫全部 src 也安全）
    files += sorted((ROOT / "src" / "dataset_recommender").glob("*.py"))
    mcp = ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py"
    if mcp.exists():
        files.append(mcp)
    # 面向客户的文档（README 是安装/使用入口；PRODUCT.md/DEVELOPMENT.md 是内部规格、定义本规则，故排除）
    readme = ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    tut = ROOT / "使用教程"
    if tut.exists():
        files += sorted(tut.rglob("*.md"))
    return files


def test_no_novelty_overclaim_in_product_copy() -> None:
    hits: list[str] = []
    for path in _copy_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for rx, pat in _BANNED:
                m = rx.search(line)
                if m:
                    rel = path.relative_to(ROOT).as_posix()
                    hits.append(f"{rel}:{lineno}  「{m.group(0)}」(禁语 /{pat}/)  → {line.strip()[:80]}")
    assert not hits, (
        "产品文案里出现了**新颖性/独占性断言**——数据集目录无从支撑这类主张，"
        "会被用户当新颖性判断。改成供给侧措辞（『有哪些可复用数据集』『本目录内无匹配』），"
        "或若确属讨论规则本身请移出被扫文件集：\n  " + "\n  ".join(hits)
    )


def test_scan_set_is_non_trivial() -> None:
    """防御：扫描面不能因为路径解析失败而空掉，否则上面那条会空过。"""
    files = _copy_files()
    assert len(files) >= 15, f"受众文案扫描面异常小（{len(files)}），可能路径没解析对"
    assert any(p.name == "index.html" for p in files)
    assert any(p.suffix == ".js" for p in files)
