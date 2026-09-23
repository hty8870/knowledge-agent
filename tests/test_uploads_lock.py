# -*- coding: utf-8 -*-
"""（验证）：uploads 摄取临界区的跨进程互斥门。

病形：`_INGEST_LOCK` 是 threading.Lock——只管内进程线程；Web / MCP / CLI 是**独立进程**，
两个进程可同时通过计数闸（双双越限）、落盘与记账交错。修复 = `ingest_critical_section`
（线程锁 + OS 文件锁，同线程可重入，超时如实 lock_busy）。

钉死：同进程重入无死锁；跨进程持锁期间另一进程摄取 → lock_busy 零写入；释锁后排队的
进程摄取成功（文件与流水账双双落齐）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import uploads  # noqa: E402

_PAYLOAD = json.dumps({"records": [{"dataset_uid": "c1", "dataset_name": "子进程数据集",
                                    "source": "测试源"}]}, ensure_ascii=False).encode("utf-8")

# 子进程脚本：独立进程直调 ingest_dataset（与父进程抢同一把 OS 锁）。打印结果码供父断言。
_CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from dataset_recommender.corpus import uploads
payload = b'{"records": [{"dataset_uid": "c1", "dataset_name": "child", "source": "T"}]}'
try:
    res = uploads.ingest_dataset(raw_bytes=payload,
                                 safe_name=uploads.new_upload_name("child.json"),
                                 project_root=Path(sys.argv[1]))
    print("OK", res.filename)
except uploads.UploadError as e:
    print("ERR", e.code)
"""


def _run_child(root: Path, timeout_s: str) -> "subprocess.CompletedProcess":
    env = dict(os.environ)
    env["BIODATA_INGEST_LOCK_TIMEOUT"] = timeout_s
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(root), str(ROOT / "src")],
        capture_output=True, text=True, encoding="utf-8", timeout=90, env=env,
    )


def test_critical_section_is_reentrant_same_process(tmp_path):
    """同线程重入：外层持双锁，内层 ingest_dataset 直进——无死锁、落盘正常。"""
    with uploads.ingest_critical_section(tmp_path):
        res = uploads.ingest_dataset(raw_bytes=_PAYLOAD,
                                     safe_name=uploads.new_upload_name("x.json"),
                                     project_root=tmp_path)
    assert res.record_count == 1
    assert (tmp_path / res.saved_to).is_file()


def test_cross_process_lock_busy_when_held(tmp_path):
    """父进程持锁期间，子进程摄取在短超时后如实 lock_busy（零写入）。"""
    with uploads.ingest_critical_section(tmp_path):
        child = _run_child(tmp_path, "2")
    assert "ERR lock_busy" in child.stdout, child.stdout + (child.stderr or "")
    ext = tmp_path / "database" / "external"
    assert not ext.exists() or not list(ext.glob("*.json")), "lock_busy 必须零写入"


def test_cross_process_serializes_and_landing_intact(tmp_path):
    """释锁后排队的子进程摄取成功：文件落盘、流水账成行（跨进程互斥 ≠ 互相破坏）。"""
    env = dict(os.environ)
    env["BIODATA_INGEST_LOCK_TIMEOUT"] = "30"
    with uploads.ingest_critical_section(tmp_path):
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(tmp_path), str(ROOT / "src")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        time.sleep(1.0)   # 让子进程先进入等锁重试循环
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0 and "OK" in out, (out or "") + (err or "")
    files = list((tmp_path / "database" / "external").glob("upload_*.json"))
    assert len(files) == 1
    journal = tmp_path / ".userdata" / "uploads_journal.jsonl"
    assert journal.is_file() and len(journal.read_text(encoding="utf-8").splitlines()) == 1
