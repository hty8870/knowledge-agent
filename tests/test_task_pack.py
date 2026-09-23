# -*- coding: utf-8 -*-
"""一句话任务包：四件套口径一致 + 下载分档诚实。

这一组测试盯的不是「函数跑通了」，而是几条**只要坏了就会给出一份没法用的投稿材料**的性质：
四份产物覆盖的数据集必须完全相同、分档判据不能把「有页面地址」当成「能下载」、
「只取主文件」这句话必须每处都在。
"""
from pathlib import Path

import pytest

from dataset_recommender.corpus import download_plan as DP
from dataset_recommender.corpus import download_script as DS
from dataset_recommender.corpus import corpus
from dataset_recommender.content import item_view, task_pack

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def records():
    return corpus.load_full_corpus(ROOT / "database" / "base", ROOT)


@pytest.fixture(scope="module")
def items(records):
    # 显式取策展产物 10x-Visium.json 的前 5 条，而非 records[:5]——2026-08-17 重基线后
    # base 多了 10x-synced.json（文件序在其前），records[:5] 会变成无文件清单的 sync 记录，
    # 本组「声明带真数字」钉的输入语义（有主文件、有被排除文件）就空了。
    visium = [r for r in records if r.source_file == "10x-Visium.json"]
    return [item_view.build_item(r, include_introduction=True) for r in visium[:5]]


@pytest.fixture(scope="module")
def pack(items, records):
    return task_pack.build_task_pack(
        query="人类肺组织单细胞数据", items=items, records=records,
        retrieval_params={"query": "人类肺组织单细胞数据", "auto_parse_sources": True},
        honesty={"coverage_caveats": [{"dim": "tissue", "label": "组织", "count": 37}],
                 "unused_query_terms": []},
        today="2026-07-22",
    )


# ----------------------------------------------------------------- 分档

def test_tier_is_decided_by_link_difference_not_by_link_presence():
    """判据必须是「直链与页面地址不同」，不是「有没有直链」。

    展示层的取值链带 `or record.url` 兜底，于是每一条的 download_url 都非空——
    按「有没有」判，全库都会被说成可下载。这条断言把那个坑钉死。
    """
    same = {"dataset_uid": "x", "url": "https://example.org/ds/1",
            "download_url": "https://example.org/ds/1", "filesize": "", "source": "ArrayExpress"}
    assert DP.classify_download_tier(same, files=[])["tier"] == DP.TIER_PAGE

    different = dict(same, download_url="https://files.example.org/ds/1.h5ad", filesize="1234")
    assert DP.classify_download_tier(different, files=[])["tier"] == DP.TIER_SIZE

    unsized = dict(same, download_url="https://files.example.org/ds/1", filesize="")
    assert DP.classify_download_tier(unsized, files=[])["tier"] == DP.TIER_DIRECT


def test_excluded_file_count_is_summed_per_dataset_not_subtracted_in_bulk():
    """「另有 N 个文件没有列入」整包相减会被没有文件级清单的数据集抵掉。

    来源没给清单、只给了带大小的直链时，这一条 `n_files_total=0` 而选中 1 个；
    整包 `sum(total) - sum(selected)` 每混进一条这样的数据集就少报一个被排除的文件。
    """
    plan_items = [
        {"tier": DP.TIER_CHECKSUM, "n_files_total": 10, "n_files_selected": 1},   # 排除 9
        {"tier": DP.TIER_SIZE, "n_files_total": 0, "n_files_selected": 1},        # 排除 0（不是 -1）
    ]
    totals = DP.plan_totals(plan_items, rows=[])
    assert totals["n_files_excluded"] == 9
    assert totals["n_files_total_in_source"] - totals["n_files_selected"] != 9, "这条测的就是两种算法不等价"


def test_excluded_bytes_are_not_attributed_to_the_fastq_count():
    """体积那个数字曾经紧跟在「其中 FASTQ M 个」后面，中文读起来就是「这 M 个约 X」；
    但 X 是**全部**被排除文件的合计。数字没错，位置放错了就等于虚报。"""
    plan = {"estimate": {"n_datasets_selected": 1, "n_files_selected": 1, "n_files_excluded": 9,
                         "n_fastq_files_excluded": 2, "bytes_excluded_lower_bound": 10 * 1024 ** 3}}
    text = DS.primary_only_sentence(plan)
    fastq_at = text.index("FASTQ 等 2 个")
    bytes_at = text.index("这些没有列入的文件合计约")
    assert fastq_at < bytes_at, "体积必须另起一句，主语写死成「这些没有列入的文件」"
    assert "个），这些没有列入的文件合计约" in text


def test_md5_coverage_uses_all_not_any():
    """100 个文件里 1 个带 md5，绝不能整体说成「可核对校验和」。"""
    item = {"dataset_uid": "x", "url": "https://p", "download_url": "https://d",
            "filesize": "", "source": "10x Genomics"}
    files = [{"download_url": f"https://d/{i}", "filename": f"{i}.bin", "bytes": 10,
              "md5sum": "abc" if i == 0 else ""} for i in range(5)]
    verdict = DP.classify_download_tier(item, files=files, scope=DP.SCOPE_ALL)
    assert verdict["tier"] == DP.TIER_SIZE
    assert "1" in verdict["evidence"]

    all_md5 = [dict(f, md5sum="abc") for f in files]
    assert DP.classify_download_tier(item, files=all_md5, scope=DP.SCOPE_ALL)["tier"] == DP.TIER_CHECKSUM


def test_missing_item_keys_fail_closed_instead_of_silently_downgrading():
    """不同入口喂不同形状会让同一批数据集落到不同的档，所以缺键必须报错。"""
    with pytest.raises(DP.DownloadPlanError) as excinfo:
        DP.classify_download_tier({"dataset_uid": "x", "url": "https://p"}, files=[])
    assert excinfo.value.code == "bad_item_shape"


def test_safe_name_neutralises_characters_that_break_on_windows():
    """10x 真有一批文件名带冒号。NTFS 上直接写会变成备用数据流——文件在、大小 0、md5 永远不符。"""
    assert ":" not in DP.safe_name("jurkat:293t_50:50_matrix.h5")
    assert DP.safe_name("CON.txt").upper().startswith("CON_")
    taken = set()
    first = DP.safe_name("a.h5", taken)
    second = DP.safe_name("a.h5", taken)
    assert first != second, "同目录重名必须自动避让"


# ----------------------------------------------------------------- 口径一致

def test_all_four_artifacts_cover_exactly_the_same_datasets(pack):
    """结果清单 / 下载计划 / FAIR / 引文覆盖的数据集必须逐一相等。

    这是本模块存在的全部理由：各自都没说错、合起来自相矛盾的材料是没法投稿的。
    """
    items = {it["dataset_uid"] for it in pack["items"]}
    planned = {it["dataset_uid"] for it in pack["plan"]["items"]}
    reported = {r["dataset_uid"] for r in pack["fair_reports"]}
    cited = {r["dataset_uid"] for r in pack["citation"]["pack"]["table"]}
    assert items == planned == reported == cited
    assert len(items) == 5


def test_scope_mismatch_is_caught_instead_of_shipped(items, records):
    """反重言式：人为让一份产物少一条，必须报错而不是发出去。"""
    pack = task_pack.build_task_pack(query="q", items=items, records=records, today="2026-07-22")
    pack["fair_reports"] = pack["fair_reports"][:-1]
    with pytest.raises(task_pack.TaskPackError) as excinfo:
        task_pack._assert_single_scope(pack)
    assert excinfo.value.code == "internal_scope_mismatch"


def test_one_snapshot_one_date_across_the_whole_pack(pack):
    assert pack["retrieval"]["date"] == "2026-07-22"
    assert pack["citation"]["pack"]["retrieval"]["date"] == pack["retrieval"]["date"]
    assert pack["citation"]["pack"]["retrieval"]["snapshot_id"] == pack["retrieval"]["snapshot_id"]
    assert pack["retrieval"]["content_digest"], "内容指纹必须算出来"


# ----------------------------------------------------------------- 主文件声明

def test_primary_only_sentence_appears_in_every_user_facing_artifact(pack):
    """搜「含 FASTQ 的数据」却拿到 0 个 FASTQ、脚本还报「全部通过」——
    这是这份产物最严重的失效形态。声明必须每处都在。"""
    sentence_head = "本包为每个数据集只列出 1 个代表性主文件"
    files = {f["path"]: f["text"] for f in task_pack.render_files(pack)}
    for name in ("00-START-HERE.txt", "README.md", "todo.md", "download.sh", "download.ps1"):
        assert sentence_head in files[name], f"{name} 里没有主文件声明"


def test_primary_only_sentence_carries_real_numbers(pack):
    text = DS.primary_only_sentence(pack["plan"])
    est = pack["plan"]["estimate"]
    assert str(est["n_datasets_selected"]) in text
    assert str(est["n_files_selected"]) in text
    assert "本次" in text and "另有" in text


# ----------------------------------------------------------------- 包内文件

def test_pack_contains_exactly_the_declared_files(pack):
    files = task_pack.render_files(pack)
    assert tuple(f["path"] for f in files) == task_pack.PACK_FILES
    for entry in files:
        assert entry["text"].strip(), f"{entry['path']} 是空的"


def test_file_list_groups_rows_by_dataset(pack):
    """dl2 可读性：file-list.md 按数据集分组，每组醒目标题 = 数据集编号 + 标题，文件列在其下。

    用户投诉「分不清哪些数据是哪个数据集的」——旧版是一张五列大表，归属藏在第一列里。
    新版组标题直读归属；组内文件行只含 文件/大小/能核对到什么 三列。
    """
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["file-list.md"]
    planned = {it["dataset_uid"]: it for it in pack["plan"]["items"]}
    for it in pack["items"]:
        if planned.get(it["dataset_uid"], {}).get("rows_planned", 0) == 0:
            continue   # 只有页面地址的数据集没有文件行，不要求出现在分组标题里
        assert f"## {it['dataset_uid']} · {it['dataset_name']}" in text, \
            f"file-list.md 缺数据集分组标题：{it['dataset_uid']}"
    # 组标题之后紧跟三列表格；「| 文件 | 大小 | 能核对到什么 |」必须出现
    assert "| 文件 | 大小 | 能核对到什么 |" in text
    # 分组顺序 = 行首次出现顺序：同一数据集的行必须连续（一组只出现一次标题）
    headers = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert len(headers) == len(set(headers)), "分组标题重复，说明同一数据集的文件行没有连续排在一起"
    for h in headers:
        assert " · " in h and "（来源：" in h, f"分组标题格式不符：{h}"


def test_manifest_keeps_dataset_group_headers_but_stays_script_parseable(pack):
    """dl2 可读性：manifest.tsv 组前插 `# ===== 编号 · 标题 =====` 注释分组行。

    两个脚本都 `grep -v '^#'` 跳过注释，因此分组行不得影响脚本解析：
    去掉 # 行后的首行仍是 dataset_uid 表头、数据行列数不变、逐文件行紧跟在组注释后。
    """
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["manifest.tsv"]
    data = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert data[0].split("\t")[0] == "dataset_uid"
    assert all(len(ln.split("\t")) == len(data[0].split("\t")) for ln in data[1:])
    planned = {it["dataset_uid"]: it for it in pack["plan"]["items"]}
    for it in pack["items"]:
        if planned.get(it["dataset_uid"], {}).get("rows_planned", 0) == 0:
            continue
        assert f"# ===== {it['dataset_uid']} · {it['dataset_name']} =====" in text, \
            f"manifest.tsv 缺数据集分组行：{it['dataset_uid']}"
    # 分组注释行数量 = 有文件行的数据集数量
    group_headers = [ln for ln in text.splitlines() if ln.startswith("# ===== ")]
    expected = sum(1 for it in pack["items"] if planned.get(it["dataset_uid"], {}).get("rows_planned", 0) > 0)
    assert len(group_headers) == expected and expected > 0


def test_start_here_explains_no_data_files_and_the_three_steps(pack):
    """dl2 可读性：00-START-HERE.txt 改成小白三步——
    ①包里没有数据文件（是清单+脚本）；②想直接下数据用界面「下载勾选的数据集文件」；
    ③自己跑脚本：每行属于哪个数据集怎么看、怎么跑、校验什么意思。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["00-START-HERE.txt"]
    assert "没有任何数据文件" in text and "是设计如此" in text          # ①
    assert "下载勾选的数据集文件" in text                                   # ②
    assert "manifest.tsv 每行的第一列就是数据集编号" in text              # ③ 归属怎么看
    assert "download.ps1" in text and "download.sh" in text               # ③ 怎么跑（双脚本）
    assert "校验是什么意思" in text and "md5" in text and "unverified" in text  # ③ 校验语义
    assert "真实数据共约" in text, "体积数字必须写成「真实数据共约 X」，不许再写成会被读成 zip 体积的「约 X 下载量」"


def test_windows_script_gets_a_bom_and_crlf(pack):
    """PowerShell 5.1 读无 BOM 的 UTF-8 会把中文变成一屏乱码。"""
    files = {f["path"]: f for f in task_pack.render_files(pack)}
    assert files["download.ps1"]["bom"] is True
    assert files["download.ps1"]["newline"] == "\r\n"
    assert files["download.sh"]["bom"] is False
    assert files["download.sh"]["newline"] == "\n"


def test_zip_encodes_bom_and_executable_bit(pack):
    import io
    import zipfile

    blob = task_pack.files_to_zip_bytes(task_pack.render_files(pack))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert tuple(archive.namelist()) == task_pack.PACK_FILES
        assert archive.read("download.ps1").startswith(b"\xef\xbb\xbf")
        assert not archive.read("download.sh").startswith(b"\xef\xbb\xbf")
        mode = archive.getinfo("download.sh").external_attr >> 16
        assert mode & 0o111, "download.sh 要可执行，否则用户还得先 chmod"


# ----------------------------------------------------------------- 脚本安全

@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_never_execute_arbitrary_text(pack, name):
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    for forbidden in ("eval ", "Invoke-Expression", "| sh", "| bash", "iex ", "$env:LLM", ".env"):
        assert forbidden not in text, f"{name} 里出现了 {forbidden}"


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_default_to_dry_run(pack, name):
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "以上只是计划，没有下载任何文件" in text
    assert ("--go" in text) or ("-Go" in text) or ("$Go" in text)


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_actually_verify_what_they_downloaded(pack, name):
    """四档里「只能核对文件大小」那一档，脚本必须**真的**去比字节数。

    对抗评审实测：两个脚本下完只在「有 md5 且本机有 md5sum」时核验，其余一律写 ok——
    包里三处却都写着「脚本只能比对字节数」。承诺了一件产物根本没做的事，是本项目最严重的一类缺陷。
    """
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    if name == "download.sh":
        assert 'wc -c < "$target"' in text, "sh 下完必须读实际字节数"
        assert 'size_mismatch' in text and 'size_ok' in text
        # macOS 没有 md5sum，只认它就等于在 macOS 上静默跳过校验
        assert "md5 -q" in text and "openssl md5" in text, "md5 工具必须有 macOS/通用回退"
        assert "no_md5_tool" in text, "本机核不动时要如实记，不许记成 ok"
    else:
        assert "(Get-Item -LiteralPath $target).Length" in text, "ps1 下完必须读实际字节数"
        assert "size_mismatch" in text and "size_ok" in text
    assert "unverified" in text, "既没校验和也没大小的，报告里必须是 unverified 而不是 ok"


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_pin_redirects_to_https(pack, name):
    """curl -fL 会跟着跳转走，包括降级到明文 http；脚本头部却承诺只放行 https。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "--proto '=https'" in text and "--proto-redir '=https'" in text


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_do_not_claim_files_were_saved_when_none_were(pack, name):
    """一个文件都没落地还打印「文件已保存到 …」，是在报告一件没发生的事。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "没有任何文件成功保存。" in text
    assert "核对通过" in text, "收尾必须把统计量真的打出来（以前算了从不输出）"


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_survive_a_pack_with_no_download_rows(pack, name):
    """全是「只有页面地址」的一批 → 清单里零行下载命令。这是正常情况，不是清单坏了。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "这一包里没有可以用脚本下载的文件。" in text


def test_sh_does_not_lose_its_counters_to_a_subshell(pack):
    """`grep … | while` 把循环放进子 shell，循环里累加的计数出了循环全部归零。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["download.sh"]
    assert 'done < "$ROWFILE"' in text, "循环必须从文件重定向，不能挂在管道右边"
    assert '| while IFS' not in text


def test_sh_keeps_logs_inside_the_pack_and_only_truncates_on_go(pack):
    """`--out=` 指到包外时，日志路径若跟着 OUT 走，`: > $REPORT` 会清空包外的用户文件；
    而且只看计划的那一次也不该抹掉上一次真下载的记录。"""
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["download.sh"]
    assert 'LOGS="$HERE/logs"' in text, "日志锚在脚本目录，不跟 --out= 走"
    assert '[ "$GO" -eq 1 ] && : > "$REPORT"' in text, "只有真要下载时才清空报告"


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_refuse_hosts_outside_the_declared_allowlist(pack, name):
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "allowed_hosts" in text
    assert "白名单" in text


@pytest.mark.parametrize("name", ["download.sh", "download.ps1"])
def test_scripts_explain_the_in_archive_mistake(pack, name):
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}[name]
    assert "压缩包" in text and "manifest.tsv" in text


def test_manifest_declares_hosts_and_md5_semantics(pack):
    text = {f["path"]: f["text"] for f in task_pack.render_files(pack)}["manifest.tsv"]
    assert "# allowed_hosts:" in text
    assert "本工具没有重新算过" in text, "md5 是来源声明值，这句必须在"
    header = [line for line in text.splitlines() if not line.startswith("#")][0]
    assert header.split("\t")[0] == "dataset_uid"


# ----------------------------------------------------------------- 待办诚实

def test_todo_starts_with_the_worst_news(pack):
    assert "只列出 1 个代表性主文件" in pack["todo"][0]


def test_todo_surfaces_coverage_gap_in_plain_words(pack):
    joined = "\n".join(pack["todo"])
    assert "还有 37 条数据可能也符合你的要求" in joined
    assert "也纳入" in joined


def test_todo_states_the_unpublished_data_boundary(pack):
    assert any("不接收你未发表的实验信息" in t for t in pack["todo"])


# ----------------------------------------------------------------- token

def test_plan_token_locks_the_candidate_pool_not_the_selection(items, records):
    """少勾几条是完全正常的操作，绝不能因此撞不一致——否则用户每勾一下都要重新预览。"""
    kwargs = dict(records=records, retrieval_params={"query": "q", "auto_parse_sources": False},
                  today="2026-07-22")
    full = task_pack.build_task_pack(query="q", items=items, **kwargs)
    spec_all = task_pack.plan_spec(
        candidate_uids=[it["dataset_uid"] for it in items], plan=full["plan"],
        snapshot=full["retrieval"] | {"snapshot_id": full["retrieval"]["snapshot_id"],
                                      "content_digest": full["retrieval"]["content_digest"]},
        retrieval_date="2026-07-22", scope="primary",
        retrieval_params={"query": "q", "auto_parse_sources": False}, honesty={})
    assert "selected_uids" not in spec_all, "勾选绝不能进 token 的原像"


def test_plan_token_changes_when_the_retrieval_conditions_change(items, records):
    base = task_pack.build_task_pack(query="q", items=items, records=records,
                                     retrieval_params={"query": "q", "lenient_dims": []},
                                     today="2026-07-22")
    other = task_pack.build_task_pack(query="q", items=items, records=records,
                                      retrieval_params={"query": "q", "lenient_dims": ["tissue"]},
                                      today="2026-07-22")
    assert base["plan_token"] != other["plan_token"]


def test_content_digest_catches_in_place_field_updates(records):
    """来源补录字段不会改变编号集合——只靠编号指纹的一致性检查在这种更新面前完全空转。"""
    from dataset_recommender.corpus.corpus import corpus_snapshot

    before = corpus_snapshot(records, with_content=True)
    mutated = list(records)
    original = mutated[0].raw.get("tissue")
    try:
        mutated[0].raw["tissue"] = "一个来源刚补录的组织"
        after = corpus_snapshot(mutated, with_content=True)
        assert after["snapshot_id"] == before["snapshot_id"], "编号集合确实没变"
        assert after["content_digest"] != before["content_digest"], "内容指纹必须变"
    finally:
        mutated[0].raw["tissue"] = original


def test_snapshot_without_content_is_byte_identical_to_before(records):
    from dataset_recommender.corpus.corpus import corpus_snapshot

    plain = corpus_snapshot(records)
    assert set(plain) == {"snapshot_id", "n_records", "sources"}


# ----------------------------------------------------------------- 入参

@pytest.mark.parametrize("raw,code", [
    ([], "empty_input"),
    ("not-a-list", "bad_param"),
    ([1, 2], "bad_param"),
    ([f"uid-{i}" for i in range(task_pack.MAX_PACK_ITEMS + 1)], "too_many"),
])
def test_bad_uid_input_raises_typed_errors(raw, code):
    with pytest.raises(task_pack.TaskPackError) as excinfo:
        task_pack.sanitize_uids_for_pack(raw)
    assert excinfo.value.code == code


def test_limit_only_accepts_the_declared_choices():
    assert task_pack.sanitize_limit(None) == task_pack.DEFAULT_PREVIEW_ITEMS
    for value in task_pack.ALLOWED_LIMITS:
        assert task_pack.sanitize_limit(value) == value
    with pytest.raises(task_pack.TaskPackError):
        task_pack.sanitize_limit(11)


def test_page_only_datasets_still_appear_in_the_pack():
    """下不了的也要留一行，否则四份产物的数据集集合就对不齐，也会让用户以为它被漏掉了。"""
    item = {"dataset_uid": "ae:E-MTAB-1", "dataset_name": "只有页面地址的数据集",
            "url": "https://www.ebi.ac.uk/x", "download_url": "https://www.ebi.ac.uk/x",
            "filesize": "", "source": "ArrayExpress"}
    plan = DP.build_plan([item])
    assert len(plan["items"]) == 1
    assert plan["items"][0]["rows_planned"] == 0
    assert plan["manual"] and plan["manual"][0]["dataset_uid"] == "ae:E-MTAB-1"
    assert plan["rows"] == []
