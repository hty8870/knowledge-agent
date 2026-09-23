# BioData Agent 网页版镜像（灰度部署，2026-08-25）
#
# 构建上下文 = 仓库根（.dockerignore 在仓库根）：
#   docker build -f deploy/web/Dockerfile -t biodata-web:2.6.0-g<短sha> .
#
# 红线（2026-08-25 网页版部署定稿）：
#   - 不装本地语义重排模型：models/ 与 requirements/requirements-embeddings.txt 不进镜像、不设相关环境变量；
#   - 单 worker 单进程：UVICORN_WORKERS=1（run_web.py 内部亦硬编码 workers=1）；
#   - 服务端不代下大文件：护栏模式（BIODATA_REQUIRE_ACCOUNT=1）下 /api/download/* 一律 403
#     （公网护栏硬化，2026-08-26），下载走任务包由客户端直连原站，镜像不引入任何代理层。
# digest 钉死防基础镜像静默漂移；升级基础镜像时手动更新（当前值 2026-08-26 取自服务器 docker inspect）。
FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

# 启动契约（scripts/run_web.py）：PORT 环境变量（容器内固定 8510，宿主 80 由 compose 映射）；
# BIODATA_WEB_HOST 缺省 127.0.0.1，容器内必须 0.0.0.0 才能被端口映射触达。
# BIODATA_DATA_ROOT（→ /data 卷）与 BIODATA_TRUSTED_HOSTS（Host 守卫白名单）由 compose 注入。
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=8510 \
    BIODATA_WEB_HOST=0.0.0.0 \
    UVICORN_WORKERS=1

WORKDIR /app

# 依赖层（独立分层，改代码不重装）：按锁定文件精确安装——
# requirements-web.lock 由 python:3.12 容器内 `pip install -r requirements/requirements.txt
# langgraph==… langchain-openai==… mcp==…（版本均与 requirements/requirements-ci.lock 对齐）
# && pip freeze` 生成（流程见 deploy/web/README.md §3），与镜像基础环境逐位一致。
# mcp 自 2026-08-28 起入镜像：在线 MCP 形态下 webapp 顶层 import mcp_server 挂载 /mcp。
# PIP_INDEX_URL 为构建期可选 pip 源覆盖（默认 PyPI；上海服务器构建经
# `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 加速，运行态无影响）。
ARG PIP_INDEX_URL=""
COPY deploy/web/requirements-web.lock /tmp/requirements-web.lock
RUN if [ -n "$PIP_INDEX_URL" ]; then pip config set global.index-url "$PIP_INDEX_URL"; fi \
    && pip install --no-cache-dir -r /tmp/requirements-web.lock && rm -f /tmp/requirements-web.lock

# 应用层：语料与代码同版本烤入（回退 = 换 tag 重部署，镜像内数据天然一致）。
COPY src/ src/
COPY web/ web/
COPY database/ database/
COPY scripts/run_web.py scripts/run_web.py
# corpus-sync 批：运营同步后的向量重建子进程入口（job 第二阶段调用）。
COPY scripts/build_corpus_vectors.py scripts/build_corpus_vectors.py
# 运行时会读的随包静态资源（webapp.py RESOURCE_ROOT 消费点全量清单）：
# 上传规范 / agent 接入提示词（本地 + 在线模板） / biodata-dataset-discovery 技能目录。
# 刻意不烤整份 使用教程/（含组内材料，未发表内容不进镜像层）。
COPY "使用教程/数据集上传/数据集上传规范.md" "使用教程/数据集上传/数据集上传规范.md"
COPY "使用教程/MCP安装/agent接入提示词.md" "使用教程/MCP安装/agent接入提示词.md"
COPY "使用教程/MCP安装/在线接入提示词模板.md" "使用教程/MCP安装/在线接入提示词模板.md"
COPY .agents/skills/biodata-dataset-discovery/ .agents/skills/biodata-dataset-discovery/

# 非 root 运行；uid/gid 1000 与宿主 /data/biodata-web 属主对齐（README §4 初始化步骤）。
RUN groupadd --gid 1000 biodata \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin biodata
USER biodata

EXPOSE 8510

# 容器内健康自检走 loopback Host——webapp Host 守卫对 127.0.0.1 天然放行；
# 公网 Host 的放行由 BIODATA_TRUSTED_HOSTS 在 /opt/biodata-web/.env 显式给出。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8510/api/health', timeout=4).status == 200 else 1)"]

# 召回预热（~12s）只在本地语义模型可用时发生；本镜像无 models/、无 embeddings 依赖，
# warm_web_recall 返回 unavailable 秒过（BIODATA_SKIP_RECALL_WARM 刻意不设置，见任务书 §3）。
CMD ["python", "scripts/run_web.py"]
