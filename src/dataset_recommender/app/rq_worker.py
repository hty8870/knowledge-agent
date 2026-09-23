# -*- coding: utf-8 -*-
"""RQ worker 进程入口 —— 消费 web 进程投入的 corpus-sync 任务。

运行方式（部署侧独立进程 / 容器，与 web 进程分开）::

    BIODATA_REDIS_URL=redis://127.0.0.1:6379/0 \\
        python -m dataset_recommender.app.rq_worker

环境变量
--------
- ``BIODATA_REDIS_URL``：Redis 连接串（缺省 ``redis://127.0.0.1:6379/0``）。与 web 进程
  共用同一配置真源（``redis_store``），worker 侧不重复解析。
- ``BIODATA_JOB_BACKEND=rq``：由 **web 进程**读取，决定任务是否投到队列；worker 只认队列，
  该变量设不设都不影响它消费（但两侧都必须连得上同一个 Redis）。

队列名与状态 key 由 ``rq_jobs`` 定义（单一真源），本文件不重复字面量。worker 侧执行体是
``rq_jobs.corpus_sync_entry``：跑完 sync + 向量重建后写 Redis 状态 hash 并置跨进程失效旗标，
由 web 进程读取状态时消费——原因见 ``rq_jobs`` 模块 docstring（worker 进程清不掉 web 进程的
内存缓存）。连接走 ``redis_store.get_rq_client()``（bytes 模式：RQ job 数据是 zlib 二进制，
decode 模式连接读不了）。
"""
from __future__ import annotations

import os

from . import redis_store


def main() -> None:
    """阻塞式消费队列。

    惰性 import rq：最小安装（默认 requirements.txt）没有 rq 包，本模块被误 import 时
    不该炸掉调用方；真正缺包在启动 worker 时如实报错（ImportError 直接冒泡，由运维在
    启动日志里看到）。

    进程模型：`rq.Worker` 依赖 `os.fork`（仅 POSIX）；没有 fork 的平台（如 Windows 开发机）
    自动回落到 `rq.SimpleWorker`（同进程顺序执行，语义一致，仅失去崩溃隔离）——本机演示
    够用，生产 Linux 仍走 fork Worker。
    """
    import rq

    from . import rq_jobs

    connection = redis_store.get_rq_client()
    queue = rq.Queue(rq_jobs._QUEUE_NAME, connection=connection)
    worker_cls = rq.Worker if hasattr(os, "fork") else rq.SimpleWorker
    worker_cls([queue], connection=connection).work()


if __name__ == "__main__":
    main()
