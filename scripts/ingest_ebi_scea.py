# -*- coding: utf-8 -*-
"""把 EBI Single Cell Expression Atlas（EMBL-EBI 单细胞表达图谱）的实验拉取为一份**离线快照**，
归一成本项目的记录 schema，写入 `database/external/ebi_scea.json`（外部平台库，opt-in）。

为什么加 EBI SCEA（差异化价值）：
- **多物种、跨机构**：SCEA 收录人/鼠/斑马鱼/果蝇/拟南芥等多物种单细胞实验，与偏人的 HCA、CELLxGENE
  互补；在浏览页并列为独立一库。
- **物种归一**：SCEA 存学名（Homo sapiens…）→ 复用 CELLxGENE 的 `map_species` 映射成通用名，否则 species
  约束会静默滤掉它们。

tissue/disease 从哪来（修复，见下「历史教训」）：
- 汇总接口（/json/experiments）确实只给**因子名**（experimentalFactors: 如「organism part」「disease」）、
  不给取值——这一点原始判断没错。但**取值在另一个端点上**：每个实验的 experiment-design 文件
  （`/gxa/sc/experiment/{acc}/download?fileType=experiment-design`）逐样本给出
  `Sample Characteristic[organism part]` / `[disease]` 及其**本体 URI**（UBERON / PATO / EFO）。
  实测 384/384 条实验全部可得 → tissue/disease 不该留空。

诚实的字段局限（写清楚，避免误导）：
- **design 文件是逐细胞行**，最大可达数百 MB（E-ANND-1 人类肺图谱 = 453MB / 584884 assays）。
  故大文件采用「表头 + 跨全文多点 Range 取样」（端点实测 `Accept-Ranges: bytes`），**不整读**。
  取样意味着**可能漏掉只出现在未采样区间的取值** → 每条记录的 `metadata_provenance.complete`
  如实标记是否整读；`sampled_bytes/total_bytes` 给出采样覆盖率。**不得据此声称取值已穷尽。**
- 取值是 SCEA **策展声明值**（带本体 URI），**不是**从论文推断的猜测 → provenance.origin = "declared"。
- tissue 只取 `organism part`，**不并入 `sampling site`**：二者语义不同（E-CURD-10 = organism part `kidney`
  / sampling site `neoplasm`），并入会把「肿瘤」写成组织，制造比缺失更糟的假事实。
- 多值拼接复用 `cxg._clean_join`（去重 + 超 8 项截断成「等N项」），与 CELLxGENE/HCA/ArrayExpress 三库
  既有约定一致（那三库分别有 595/131/221 条多值 tissue）；截断只影响展示与靠后标签的匹配。
  **但 cap=8 只对 complete=True 的记录成立**（截断的是完整策展集，与三库同理）；complete=False 的
  记录取值来自 0.1%~2.5% 的 Range 抽样——这些值是已经付了网络代价取回来的已知值，再截断丢掉的
  不是「本来不知道」而是「已知却扔」（实测 tissue 截断 3 条，如 E-ANND-1 抽样拿到
  12 个肺区取值只写 8 个）。故 complete=False 时不截断，保留全部已取回取值
  （scea-sampled-cap8-truncation 修正在案）。
- has_raw_data=False：SCEA 在本资源托管的是处理后表达矩阵；原始 FASTQ 在 ENA/ArrayExpress（accession 可溯源），
  据实标 False → 「要 FASTQ」查询正确排除。

历史教训（别再犯）：本文件原先写「汇总接口不给取值 → **无法**填 tissue/disease」并据此留空 384 条。
前半句属实，后半句是把「**这个**接口没有」当成了「拿不到」，没去追查其它端点。代价：搜「人类肺组织」时
`E-ANND-1`（Human Lung Cell Atlas，源库明写 organism part = "lower lobe of left lung"）被硬过滤**无声排除**。
诚实地记录一个局限，和把局限当既成事实接受，是两回事。

设计约束（与 ingest_cellxgene.py 一致）：不动 `database/base/`；纯标准库；复用 cxg 助手保持单一真源。
冻结隔离：本快照写 `database/external/`，官方 767 评测走 base-only（`corpus.load_normalized_corpus(sources=None)`
只装 base，已读码确认）→ 本文件产物**结构上**不影响冻结基准。
数据许可：EMBL-EBI SCEA 数据开放；两个端点均为公开接口，仅抓元数据 + 门户直链。

用法：
    py -3 scripts/ingest_ebi_scea.py
    py -3 scripts/ingest_ebi_scea.py --limit 50
    py -3 scripts/ingest_ebi_scea.py --no-enrich   # 跳过 design 取样（快，但 tissue/disease 留空）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ingest_cellxgene as cxg  # 复用 map_species / platform_hint（单一真源）

API_URL = "https://www.ebi.ac.uk/gxa/sc/json/experiments"
SOURCE_LABEL = "EBI Single Cell Expression Atlas"
EXP_TMPL = "https://www.ebi.ac.uk/gxa/sc/experiments/{acc}"
OUT_PATH = REPO_ROOT / "database" / "external" / "ebi_scea.json"


def _iso_date(load_date: str) -> str:
    """SCEA loadDate 形如 'DD-MM-YYYY' → ISO 'YYYY-MM-DD'（供日期过滤 + 新鲜度排序）。"""
    parts = str(load_date or "").strip().split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, y = parts
        if len(y) == 4:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return ""


# ---------------------------------------------------------------- design 取样
# 逐样本取值只在 experiment-design 端点上（汇总接口只有因子名）。文件逐细胞行、可达数百 MB，
# 但端点实测支持 Range（`Accept-Ranges: bytes`）→ 小文件整读、大文件多点取样，成本有界。
DESIGN_TMPL = ("https://www.ebi.ac.uk/gxa/sc/experiment/{acc}/download"
               "?fileType=experiment-design&accessKey=")
CHUNK = 65536                       # 单次取样窗口
# 整读阈值 > 取样总量（6×64KB + 表头 ≈ 448KB）时，整读反而**请求更少且 complete=True**：
# 实测 E-MTAB-5061 = 2.18MB，若阈值取 2MB 会退化成「7 个请求 / 覆盖 20% / complete=False」，
# 而整读只需 1 个请求、覆盖 100%。故阈值抬到 4MB —— 用带宽换「取值穷尽」，诚实度优先。
FULL_READ_LIMIT = 4 * 1024 * 1024
# 取样点按文件比例分布（确定性：同一文件大小 → 同一组偏移 → 可复现）。
# 均匀分布而非只读头部：design 常按样本聚簇排列，只读前 N 字节会被排序偏差骗
# （E-ANND-1 首行只给「左肺下叶」，而该图谱覆盖多个肺区）。
SAMPLE_POINTS = (0.15, 0.30, 0.45, 0.60, 0.75, 0.90)
TISSUE_COL = "Sample Characteristic[organism part]"
DISEASE_COL = "Sample Characteristic[disease]"


# 对 EMBL-EBI 公开端点的礼貌约束：一次全量 ingest ≈ 1758 个请求 / 约 270MB（384 表头 + 186 整读
# + 198×6 取样点）。这是**别人免费提供的学术服务**，不是我们的机器。
REQUEST_GAP_S = 0.12        # 请求间最小间隔 → 峰值 <10 req/s
_last_request_at = 0.0


def _http_get(url: str, byte_range: "tuple[int, int] | None" = None, tries: int = 3):
    """GET（可选 Range）。返回 (status, headers, body)。**调用方必须校验 status**——
    Range 是建议性的，收到 200 意味着 body 是全文不是切片（见 fetch_design_facets）。

    重试纪律：
    - 只对**瞬时**网络错重试（EBI 偶发 TLS 握手中断 WinError 10053）——别把瞬时错当成
      「端点不可用」而静默留空字段，那正是本文件要修的老毛病。
    - **4xx 不重试**（404/400 是确定性答案，重试只是白烧 3 倍时间）。
    - **429/503 用长退避**：原实现对任何异常都按 1.5/3/4.5s 重试，遇上限流那是**放大**不是退避。
    """
    global _last_request_at
    import time
    last: Exception | None = None
    for i in range(tries):
        gap = REQUEST_GAP_S - (time.monotonic() - _last_request_at)
        if gap > 0:
            time.sleep(gap)
        try:
            headers = {"User-Agent": "biodata-agent-ingest/1.0 (+metadata snapshot; contact via repo)"}
            if byte_range is not None:
                headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = resp.read()
                _last_request_at = time.monotonic()
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as exc:
            _last_request_at = time.monotonic()
            last = exc
            if exc.code in (429, 503):
                time.sleep(min(60.0, 5.0 * (2 ** i)))   # 5/10/20s：真退避
                continue
            raise                                        # 其余 4xx/5xx 是确定性答案，别重试
        except Exception as exc:  # noqa: BLE001  瞬时网络/TLS 错
            _last_request_at = time.monotonic()
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def _total_from(headers: dict, body_len: int) -> int:
    cr = headers.get("Content-Range") or ""
    if "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    cl = headers.get("Content-Length")
    if cl and str(cl).isdigit() and headers.get("Content-Range") is None:
        return int(cl)
    return body_len


def _complete_rows(blob: bytes, *, is_first: bool) -> list[str]:
    """把 Range 片段切成**完整**数据行。
    非首片：丢弃开头被切断的残行。任何片：丢弃结尾可能残缺的行。首片的第 0 行是表头，由调用方取走。"""
    text = blob.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not is_first and lines:
        lines = lines[1:]
    if lines:
        lines = lines[:-1]
    return [ln for ln in lines if ln.strip()]


def _parse_tsv_line(line: str) -> list[str]:
    import csv
    for row in csv.reader([line], delimiter="\t", quotechar='"'):
        return row
    return []


def fetch_design_facets(acc: str) -> tuple[list[str], list[str], dict]:
    """取该实验的 organism part / disease **去重取值集合** + provenance。

    诚实性契约：`complete=False` 时取值集合**可能不全**（只覆盖 sampled_bytes/total_bytes）；
    调用方与前端不得据此声称已穷尽。tissue 只取 organism part，绝不并入 sampling site
    （语义不同，并入会把「肿瘤」写成组织＝比缺失更糟的假事实）。
    """
    url = DESIGN_TMPL.format(acc=acc)
    status, headers, body = _http_get(url, byte_range=(0, CHUNK - 1))
    total = _total_from(headers, len(body))
    sampled = len(body)
    # Range 是**建议性**的（RFC 9110: "A server MAY ignore the Range header field"），而本端点是
    # 动态生成响应、不是静态文件 —— 实测 `Accept-Ranges: bytes` 只证明当下行为、不是不变量。
    # 若服务端忽略 Range 改回 200，body 就是**全文**而非切片：继续按切片语义走会把 453MB 逐个取样点
    # 各拉一遍（7×≈3.1GB + decode 成 58 万 str，极可能 MemoryError），且 provenance 变成
    # sampled_bytes(33.6MB) > total_bytes(4.8MB)＝700% 覆盖率这种自证荒谬的数字。
    # 既然已经拿到全文，正解是**短路成整读**：取值反而穷尽（complete=True），且不再多发 6 个请求。
    range_ignored = status == 200 and len(body) >= total > 0

    text = body.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    first_nl = text.find("\n")
    if first_nl < 0:
        return [], [], {"origin": "declared", "complete": False, "error": "no_header",
                        "total_bytes": total, "sampled_bytes": sampled, "rows_parsed": 0}
    cols = _parse_tsv_line(text[:first_nl])
    try:
        i_tissue = cols.index(TISSUE_COL)
    except ValueError:
        i_tissue = -1
    try:
        i_disease = cols.index(DISEASE_COL)
    except ValueError:
        i_disease = -1
    if i_tissue < 0 and i_disease < 0:
        # 该实验根本没标注这两维 → 如实留空，不猜。
        # `complete` 保持「是否整读」的字面语义（这里没整读 → False），但**结论本身是确定的**：
        # 表头 100% 落在首片内（否则上面 first_nl<0 已早退），列不存在与文件多大无关。
        # 故另加 `determinate`——别把 complete 翻成 True（既有守卫断言 complete=True → sampled>=total，
        # 会当场炸；且与文件头「complete=是否整读」的定义冲突）。
        # 消费面注意：这两维字段为空 → `retriever._dim_field_present` 在「非空」那步就返回 False，
        # 本标记对诚实层零影响，纯为运维可读性（否则日志显示「只看了 6%」会误导成「可能藏在没采到的 94% 里」）。
        return [], [], {"origin": "declared", "endpoint": "experiment-design",
                        "complete": total <= len(body), "determinate": True,
                        "total_bytes": total, "sampled_bytes": sampled, "rows_parsed": 0,
                        "note": "no organism part / disease column"}

    tis: list[str] = []
    dis: list[str] = []
    rows = 0

    def eat(blob: bytes, *, is_first: bool) -> None:
        nonlocal rows
        for line in _complete_rows(blob, is_first=is_first)[1 if is_first else 0:]:
            vals = _parse_tsv_line(line)
            rows += 1
            for idx, sink in ((i_tissue, tis), (i_disease, dis)):
                if 0 <= idx < len(vals):
                    v = vals[idx].strip()
                    # NA 占位（"not applicable" 等）绝不能当成取值：SCEA 真的用它填 organism part
                    # （E-MTAB-4617 → `spleen, not applicable, heart, testes`）。复用共享过滤器，
                    # 与 ArrayExpress 同一真源，别自己判「非空即有效」。
                    if cxg._is_informative(v) and v.lower() not in {s.lower() for s in sink}:
                        sink.append(v)

    if range_ignored:
        # 服务端忽略了 Range、已经把全文给了 → 直接当整读用，别再拉 6 遍
        eat(body + b"\n", is_first=True)
        complete = True
    elif total <= FULL_READ_LIMIT:
        # 小文件整读 → 取值集合**穷尽**，complete=True
        status, headers, whole = _http_get(url)
        sampled = len(whole)
        eat(whole + b"\n", is_first=True)   # 补尾 \n：整读时末行是完整的，别被 _complete_rows 丢掉
        complete = True
    else:
        eat(body, is_first=True)
        for frac in SAMPLE_POINTS:
            start = int(total * frac)
            end = min(start + CHUNK - 1, total - 1)
            if start >= total:
                continue
            try:
                s2, _h, chunk = _http_get(url, byte_range=(start, end))
            except Exception:  # noqa: BLE001
                continue        # 单点取样失败不该毁掉整条记录；覆盖率如实反映在 sampled_bytes
            if s2 != 206:
                # 中途退化成 200：这一片是全文不是切片 → 按整读处理并**停止**后续取样（否则 N× 重拉）
                sampled = len(chunk)
                tis.clear(); dis.clear(); rows = 0
                eat(chunk + b"\n", is_first=True)
                complete = True
                break
            sampled += len(chunk)
            eat(chunk, is_first=False)
        else:
            complete = False

    # 不变量：采样量绝不可能超过文件本身。超了＝上面某条切片语义假设被打破，宁可炸也不发布假覆盖率。
    if sampled > total > 0:
        raise RuntimeError(
            "sampled_bytes(%d) > total_bytes(%d) —— Range 语义被破坏，拒绝产出假覆盖率" % (sampled, total)
        )

    prov = {
        "origin": "declared",            # SCEA 策展声明值（带本体），非 LLM 推断
        "endpoint": "experiment-design",
        "total_bytes": total,
        "sampled_bytes": sampled,
        "complete": complete,
        "rows_parsed": rows,
    }
    return tis, dis, prov


def to_record(exp: dict, *, enrich: bool = True) -> dict | None:
    acc = str(exp.get("experimentAccession") or "").strip()
    title = str(exp.get("experimentDescription") or "").strip()
    if not acc or not title:
        return None

    species = cxg.map_species([str(exp.get("species") or "").strip()])
    techs = [str(t).strip() for t in (exp.get("technologyType") or []) if str(t).strip()]
    factors = [str(f).strip() for f in (exp.get("experimentalFactors") or []) if str(f).strip()]
    n = exp.get("numberOfAssays")
    exp_type = str(exp.get("experimentType") or "").strip()

    # 逐样本取值只在 design 端点上（见文件头）。--no-enrich 时留空并如实标 origin=skipped。
    tissue = disease = ""
    prov: dict = {"origin": "skipped", "complete": False}
    if enrich:
        try:
            tis, dis, prov = fetch_design_facets(acc)
            # complete=True：取值来自整读（完整策展集）→ 维持四库共用 cap=8 约定逐位不变。
            # complete=False：取值来自 Range 抽样（最差 0.1% 覆盖率）——已付网络代价取回的
            # 已知值不该再被 cap=8 丢掉 → 用大上限保留全部取值（其余三库与共享
            # `_clean_join` 默认 cap=8 不动）。
            cap = 8 if prov.get("complete") is True else 1_000_000
            tissue = cxg._clean_join(tis, cap=cap)
            disease = cxg._clean_join(dis, cap=cap)
        except Exception as exc:  # noqa: BLE001
            # 取样失败 → 字段留空 + provenance 如实记错误。绝不因一条失败中断整库，
            # 也绝不把「没取到」伪装成「源库没有」（那正是本文件修复的老毛病）。
            print(f"  [warn] {acc}: design 取样失败 {type(exc).__name__}: {exc}")
            prov = {"origin": "declared", "complete": False, "error": type(exc).__name__}

    desc_bits = [b for b in (acc, exp_type, ("因子: " + ", ".join(factors)) if factors else "") if b]
    page = EXP_TMPL.format(acc=acc)
    return {
        "dataset_name": title,
        "species": species,
        "tissue": tissue,                                # ← SCEA 声明值，取自 design 端点
        "disease": disease,
        "metadata_provenance": prov,                     # 诚实层：complete=False 时取值可能不全
        "chemistry": ", ".join(dict.fromkeys(techs)),    # technologyType（如 10xv2, 10xv3）
        "platform": cxg.platform_hint(techs),            # 10x* → Chromium；smart-seq 等保留自身名
        "count": str(int(n)) if isinstance(n, (int, float)) and n else "",
        "unit": "Cells",
        "has_raw_data": False,                           # 处理后矩阵；原始 FASTQ 在 ENA/ArrayExpress
        "url": page,
        "download_url": page,
        "filesize": 0,
        "published_date": _iso_date(exp.get("loadDate")),
        "description": " · ".join(desc_bits),
        "source": SOURCE_LABEL,
        "collection_doi": "",
        # 前缀 ebi: → 绝不与 10x by_uid 直链表键碰撞。
        "dataset_uid": f"ebi:{acc}",
    }


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "biodata-agent-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（0=全量）")
    ap.add_argument("--no-enrich", action="store_true",
                    help="跳过 design 端点取样（快；但 tissue/disease 留空、provenance 标 skipped）")
    args = ap.parse_args()
    enrich = not args.no_enrich

    print(f"fetching {API_URL} ...")
    try:
        payload = fetch(API_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"FETCH_FAILED: {type(exc).__name__}: {exc}")
        return 2
    exps = payload.get("experiments") if isinstance(payload, dict) else None
    if not isinstance(exps, list):
        print("unexpected payload (no experiments list)")
        return 2
    print(f"got {len(exps)} experiments"
          + ("" if enrich else "  (--no-enrich: 不取 tissue/disease)"))

    records: list[dict] = []
    seen: set[str] = set()
    todo = [e for e in exps if isinstance(e, dict)]
    for i, exp in enumerate(todo, 1):
        rec = to_record(exp, enrich=enrich)
        if rec is None:
            continue
        if rec["dataset_uid"] in seen:
            continue
        seen.add(rec["dataset_uid"])
        records.append(rec)
        if enrich:  # design 取样是网络密集的长流程 → 给出进度，避免看起来卡死
            p = rec.get("metadata_provenance") or {}
            print(f"  [{i}/{len(todo)}] {rec['dataset_uid']:<18} "
                  f"tissue={(rec['tissue'] or '-')[:38]:<38} "
                  f"{'full' if p.get('complete') else 'sampled'}")
        if args.limit and len(records) >= args.limit:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload_out = {
        "source": SOURCE_LABEL,
        "source_url": API_URL,
        "note": ("离线快照·外部平台库（opt-in）。不并入官方评测语料。"
                 "tissue/disease 取自各实验的 experiment-design 端点（SCEA 策展声明值，带本体）；"
                 "design 文件逐细胞、可达数百 MB，故大文件按 Range 多点取样 → "
                 "每条 metadata_provenance.complete=False 时取值可能不全，不得据此声称已穷尽。"),
        "record_count": len(records),
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} records -> {OUT_PATH}")

    specs: dict[str, int] = {}
    for r in records:
        for s in str(r["species"]).split(","):
            s = s.strip()
            if s:
                specs[s] = specs.get(s, 0) + 1
    print("species:", ", ".join(f"{k}={v}" for k, v in sorted(specs.items(), key=lambda kv: -kv[1])[:12]))

    # 覆盖率与诚实度自报（本次修复的核心指标：修复前 tissue 恒为 0/384）
    n = len(records) or 1
    t_ok = sum(1 for r in records if r["tissue"])
    d_ok = sum(1 for r in records if r["disease"])
    provs = [(r.get("metadata_provenance") or {}) for r in records]
    full = sum(1 for p in provs if p.get("complete"))
    err = sum(1 for p in provs if p.get("error"))
    # 「表头已证明没有这两列」的记录结论是确定的，别混进「可能不全」（否则运维读到
    # 「只看了 6%」会误以为取值藏在没采到的 94% 里）。
    det = sum(1 for p in provs if p.get("determinate") and not p.get("complete"))
    print(f"tissue 有值: {t_ok}/{len(records)} ({100.0*t_ok/n:.1f}%)")
    print(f"disease 有值: {d_ok}/{len(records)} ({100.0*d_ok/n:.1f}%)")
    print(f"整读(取值穷尽): {full}/{len(records)}；"
          f"取样(可能不全): {len(records)-full-err-det}；"
          f"源库无该列(结论确定): {det}；取样失败: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
