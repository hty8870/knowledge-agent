# BioData Agent 网页版部署手册（2026-08-25）

> 范围：把仓库以**版本化 Docker 镜像**部署到 Internet-facing 主机。应用端口只发布到
> 宿主 loopback，公网入口必须先经过 TLS 反向代理；无域名/证书时不得承载真实账户、查询、
> 会话或 BYOK 凭据。回退 = 换 tag 重新部署，分钟级。
>
> **过渡状态（2026-09-09 用户拍板）**：域名 ICP 备案中，公网暂以 HTTP 直连 + 账户门运行，
> `PUBLIC_HEALTH_URL` 用 `http://<server-host>/api/health`；备案与证书落地后恢复上述 TLS 形态
> （恢复动作 = 装回反向代理 + 把 environment 变量切回 https，代码与脚本不变）。

## 1. 形态一览

| 项 | 值 |
|---|---|
| 公网入口 | `https://<your-domain>/`（80 只做 HTTPS 重定向） |
| 容器 | `biodata-web`（单容器、单进程单 worker、非 root uid 1000） |
| 端口 | 宿主 `127.0.0.1:8510` → 容器 `8510`；Nginx/Caddy 在 443 终止 TLS |
| 镜像 | `biodata-web:<WEB_API_VERSION>-g<短sha>`，如 `biodata-web:2.6.0-gae5156f`；本机保留最近 5 个 |
| 语料/前端 | `src/ web/ database/`（tracked 快照）**烤进镜像**——与代码同版本，回退天然一致 |
| 运行态数据 | 宿主卷 `/data/biodata-web` → 容器 `/data`（`BIODATA_DATA_ROOT=/data`，portable 双根分离）：账号 `.userdata/`、上传 `database/external/`、trace、日志、run；回退不动数据 |
| 密钥 | `/opt/biodata-web/.env`（root:600；以 `deploy/web/.env.example` 为结构合同） |
| 内存 | `mem_limit: 1536m`（服务器预算 ≤1.5G；2C4G 同机还有遥测栈） |
| 遥测 | 独立端点；新部署必须使用 HTTPS，client ingest token 仅作滥用过滤、不作身份认证 |

服务器路径边界（本任务的一切改动以此为止）：

- `/opt/biodata-web/`——部署物：`.env`、compose、root-owned `deploy.sh`、`RELEASES.log`
- `/data/biodata-web/`——应用运行态数据（宿主每日 03:00 自动备份 `/data`，见 §6）
- 受控运维系统——保存批准、digest、备份、回退和健康证据；不把真实记录回填 Git

## 2. 代码同步到服务器（仅首次自建/离线构建）

原则：**一个 tag 一份完整构建上下文，禁止手工散拷单文件**。上下文 = worktree 的 git tracked
快照（`.dockerignore` 在构建时进一步瘦身）。

在已经通过完整质量门的本机 worktree 执行；远程身份与地址均用占位符表示：

```bash
TAG=<WEB_API_VERSION>-g<短sha>
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-host> "sudo mkdir -p /opt/biodata-web/build && sudo chown <deploy-user>:<deploy-user> /opt/biodata-web/build"
git archive --format=tar.gz -o /tmp/biodata-ctx-$TAG.tar.gz HEAD
scp -i ~/.ssh/<ssh-key> /tmp/biodata-ctx-$TAG.tar.gz <deploy-user>@<server-host>:/tmp/
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-host> "mkdir -p /opt/biodata-web/build/$TAG && tar -xzf /tmp/biodata-ctx-$TAG.tar.gz -C /opt/biodata-web/build/$TAG && rm /tmp/biodata-ctx-$TAG.tar.gz"
```

注意：git archive 只含 tracked 内容——`database/trace/`、`database/external/upload_*.json`
等 gitignored 运行产物天然不进上下文（镜像也不需要它们）。

## 3. 生成依赖锁定文件（改依赖时才需要重跑）

`requirements/requirements.txt` 只有 5 个下界约束；镜像必须按**精确锁定**安装。网页版镜像额外装
「AI 执行」扩展链 `langgraph` + `langchain-openai`（2026-08-26 产品所有者拍板装入镜像；
版本与 requirements/requirements-ci.lock 对齐），本机形态不受影响（扩展在本地仍可选装）。
2026-08-28 起再补 `mcp`（在线 MCP 形态：webapp 顶层 import mcp_server 挂载 `/mcp`，
版本与 requirements/requirements-ci.lock 的 `mcp==1.28.1` 对齐）。
锁定文件在服务器上用与镜像完全相同的基础环境生成（本机 Windows Python 版本不同，不在此生成）：

```bash
# $CTX = /opt/biodata-web/build/$TAG（构建上下文已按 §2 同步）
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-host> "
  sudo docker run --rm -v $CTX:/ctx -w /ctx python:3.12-slim bash -lc \
    'pip install --no-cache-dir -q -r requirements/requirements.txt \
       langgraph==<ci锁版本> langchain-openai==<ci锁版本> mcp==<ci锁版本> && pip freeze' \
    | sudo tee $CTX/deploy/web/requirements-web.lock >/dev/null
"
scp -i ~/.ssh/<ssh-key> <deploy-user>@<server-host>:/opt/biodata-web/build/$TAG/deploy/web/requirements-web.lock deploy/web/requirements-web.lock
```

回填 worktree 后 `deploy/web/requirements-web.lock` 随分支 commit（Dockerfile COPY 它安装）。
若曾生成过锁定文件且依赖未变，直接复用 build 目录里的上一份即可。

## 4. 首次部署（one-time）

```bash
# 1) 服务器目录与数据卷（部署目录由 root 管；数据卷属主 = 容器 uid 1000）
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo mkdir -p /opt/biodata-web /data/biodata-web
  sudo chown root:root /opt/biodata-web
  sudo chown 1000:1000 /data/biodata-web
  sudo chmod 700 /data/biodata-web
"

# 2) 按 deploy/web/.env.example 另建 server-only .env；必须包含账户门、Secure cookie、
#    域名、邀请码和正整数配额。真实文件绝不从 Git 生成、绝不打印。
scp -i ~/.ssh/<ssh-key> /secure/local/biodata-web.env <admin-user>@<server-host>:/tmp/biodata-web.env.upload
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo install -m 600 -o root -g root /tmp/biodata-web.env.upload /opt/biodata-web/.env
  rm -f /tmp/biodata-web.env.upload
"

# 3) root-owned compose / deploy.sh 就位
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo install -m 644 -o root -g root /opt/biodata-web/build/$TAG/deploy/web/docker-compose.web.yml /opt/biodata-web/docker-compose.web.yml
  sudo install -m 755 -o root -g root /opt/biodata-web/build/$TAG/deploy/web/deploy.sh /opt/biodata-web/deploy.sh
"

# 4) 安装 TLS 反向代理模板并验证 nginx 配置/证书，再构建镜像
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo docker build -f /opt/biodata-web/build/$TAG/deploy/web/Dockerfile \
    -t biodata-web:$TAG /opt/biodata-web/build/$TAG
  sudo nginx -t
"

# 5) 部署：日常远程账号经 sudoers 白名单直接调 deploy.sh（镜像已在 step 4 本机构建）
ssh -i ~/.ssh/<deploy-key> deploy@<server-host> "sudo -n /opt/biodata-web/deploy.sh '$TAG'"
```

`BIODATA_TRUSTED_HOSTS` 填浏览器实际访问域名；compose 固定开启账户门与 Secure cookie，
并只发布宿主 loopback。`PUBLIC_HEALTH_URL` 用同一入口的 `/api/health`；目标形态为 HTTPS，
2026-09-09 起域名 ICP 备案过渡期允许 http（见 §5 末注）。

## 5. 日常发布与回退

发布新版 = ① 按 §2 同步构建上下文到服务器，构建并推送经过验证的镜像到 TCR（`docker build` +
`docker push`，标签规则见 §1）→ ② GitHub「Deploy web」workflow 手动触发（`workflow_dispatch`，
**唯一部署入口**，部署 TCR 中已存在的 tag；`AUTO_DEPLOY=false` 时不存在自动路径）。
workflow 经受限 deploy 账号在服务器上执行 `sudo docker pull` → `sudo docker tag` →
`sudo /opt/biodata-web/deploy.sh <new-tag>`（sudoers 白名单只放行这三条）；
deploy.sh 写 `IMAGE_TAG` → `up -d` → 等 healthy（≤120s）→ 容器内断言账户护栏 →
失败自动切回上一 tag → 成功记 `/opt/biodata-web/RELEASES.log` 并清理镜像只留最近 5 个。

> 2026-09-09 起域名 ICP 备案过渡：`PUBLIC_HEALTH_URL` 允许 `http://<server-host>/api/health`，
> 公网健康断言按 scheme 分支（https 钉死 TLS1.2+，http 明文直连并在日志给警告）。
> 备案与证书落地后，只需把 environment 变量 `PUBLIC_HEALTH_URL` 切回
> `https://<your-domain>/api/health`，workflow 与脚本均无需再改。

**手工回退**（不走 GitHub workflow 时，同样经 deploy 账号）：

```bash
ssh -i ~/.ssh/<deploy-key> deploy@<server-host> "sudo -n /opt/biodata-web/deploy.sh '<old-tag>'"
# 或最原始形态：改 /opt/biodata-web/.env 的 IMAGE_TAG=<old-tag> 后
#   sudo docker compose --env-file /opt/biodata-web/.env -f /opt/biodata-web/docker-compose.web.yml -p biodata-web up -d
```

## 6. 备份核对

将 `/data/biodata-web` 纳入加密备份并定期做恢复演练。备份脚本、目标位置、保留期和
最后一次恢复证据属于运维记录，不写入公开仓库；仓库只保留这一条可验证要求。

## 7. 故障排查入口

```bash
sudo docker ps                                   # 容器状态（应 Up ... (healthy)）
sudo docker inspect --format '{{.State.Health.Status}} {{.State.Health.FailingStreak}}' biodata-web
sudo docker logs --tail 100 biodata-web          # 应用日志（json-file 10m×3 轮转）
sudo docker exec -it biodata-web bash            # 进容器（slim，无 curl；用 python urllib）
curl -s http://127.0.0.1:8510/api/health          # 宿主 loopback，验证容器映射
curl --fail --proto '=https' https://<your-domain>/api/health     # 公网只能走 TLS
tail /opt/biodata-web/RELEASES.log               # 部署/回退流水
```

常见症状：

- **403 仅接受本机 loopback Host**：`.env` 的 `BIODATA_TRUSTED_HOSTS` 缺失或没包含访问用的
  Host；改后 `sudo docker compose ... up -d` 重建容器（env_file 变化需重建）。
- **healthy 起不来**：`docker logs` 看是否依赖缺失（锁定文件与镜像不符）或 8510 未监听
  （`BIODATA_WEB_HOST`/`PORT` 被覆盖）。
- **注册/上传报错且日志含 Permission denied**：宿主 `/data/biodata-web` 属主不是 1000:1000。

## 8. 验收证据

每次部署至少保存：镜像 digest、批准人、部署时间、TLS 检查、`account.required=true`、
备份/回退可用性和外部 HTTPS health 结果。真实主机、账号、指纹和输出放受控运维系统，
不写入仓库。

## 9. 账号护栏（T3，2026-08-25）

公网形态的三道闸：**登录强制 + 注册邀请 + LLM 日配额**。源码/本地启动器缺省仍保持
单机兼容；`docker-compose.web.yml` 则是专用 production profile，固定开启账户门与 Secure cookie，
缺少域名、邀请码或正整数配额时启动器 fail-closed 拒绝起服。只拦 `/api/` 前缀，静态前端
与登录页天然可达；未登录调非白名单 `/api/*` 返回 401 `{"ok":false,"error":"auth_required"}`，
前端据此自动锁定整页只留登录框。

### 9.1 环境变量（改 .env 后需 `up -d` 重建容器生效）

| 变量 | production profile | 语义 |
|---|---|---|
| `BIODATA_REQUIRE_ACCOUNT` | compose 固定 `1` | 登录强制门（白名单：health/register/login/logout/whoami） |
| `BIODATA_COOKIE_SECURE` | compose 固定 `1` | session cookie 只经 HTTPS 发送 |
| `BIODATA_INVITE_CODE` | 必填 | 注册邀请码；缺失时启动失败 |
| `BIODATA_LLM_DAILY_PER_USER` | 正整数必填 | 正式通道每账号每日 LLM 轮数 |
| `BIODATA_LLM_DAILY_GLOBAL` | 正整数必填 | 正式通道全站每日熔断 |
| `BIODATA_TRUSTED_HOSTS` | 域名必填 | Host/Origin 同源守卫；不要填任意通配 |
| `FORWARDED_ALLOW_IPS` | compose 固定 `*` | 仅因宿主端口绑定 loopback，才允许 TLS 代理转发 scheme/host |
| `BIODATA_LLM_QUOTA_EXEMPT` | 空 | 豁免用户名（逗号/空白分隔，产品所有者用） |
| `BIODATA_TRIAL_API_KEY` | 空 | 「限量试用」通道专用 key（DeepSeek，低成本；与正式 key 完全隔离；只在进程环境，请求级覆盖链从不注入） |
| `BIODATA_TRIAL_DAILY_PER_USER` | 30 | 试用通道每账号每日轮数（独立桶，更紧） |
| `BIODATA_TRIAL_DAILY_GLOBAL` | 500 | 试用通道全站每日熔断 |
| `BIODATA_TRIAL_BASE_URL` / `BIODATA_TRIAL_MODEL` | 官方默认 | 试用通道端点/模型覆盖（默认 `https://api.deepseek.com/v1` / `deepseek-v4-flash`，2026-09-01 起） |
| `BIODATA_TRIAL_THINKING` | 空 | 试用通道思考档逃逸口（`enabled`/`disabled`/`none`）。**缺省 = disabled**：deepseek-v4-flash 默认思考档拒收 tool_choice="required" 且更慢（2026-08-25 实测）；`none` = 不发 thinking 参数（换始终思考的模型时用——glm-5.3-flash 拒收 disabled，2026-08-27 实测 400） |
| `BIODATA_LLM_QUOTA_FILE` | userdata 层 `llm_quota.json` | 配额账本路径（一般不动） |

### 9.2 计数口径（哪一发请求会烧服务端 LLM）

埋闸端点全集：`/api/recommend`（润色/AI 重排/动作审核意图为真时）、`/api/utterance`、
`/api/action·plan`、`/api/agent·search-rescue`、`/api/act·summary`、`/api/dream`。
**不计**：BYOK（请求自带 key）、mock、LLM 未启用、服务端无 key——这些烧不到服务端配额。
超限返回 429 中文文案；按 UTC 日重置（北京时间每日 08:00 归零）。账本只留当日、故障
放行（可用性优先，provider 侧消费上限是最后防线）。

试用通道（前端「限量试用」预设）：端点/模型锁定服务端托管值，请求级 key/地址/模型
**一律忽略**（防注入防偷换）；凭据只认 `BIODATA_TRIAL_API_KEY`，绝不回落 `LLM_API_KEY`
（也不回落 `BIODATA_EMBED_API_KEY`——智谱 key 对 DeepSeek 端点无效，2026-08-27 换型
GLM 时曾短暂共用，2026-09-01 改回时移除）；默认 `deepseek-v4-flash`（2026-09-01 起）
恒关思考档——其默认思考档拒收 tool_choice="required" 且更慢（2026-08-25 实测）；
换始终思考的模型时用 `BIODATA_TRIAL_THINKING=none`（不发该参数）。

### 9.3 日常运营动作

```bash
# 生成邀请码（私下发给用户；泄漏了换一个新的重启即可）
python -c "import secrets; print(secrets.token_urlsafe(8))"

# 改限额/豁免/邀请码：编辑 .env 对应行后重建
sudo docker compose --env-file /opt/biodata-web/.env -f /opt/biodata-web/docker-compose.web.yml -p biodata-web up -d

# 查看当日用量账本（容器内 /data/.userdata/llm_quota.json，只留当日）
sudo docker exec biodata-web cat /data/.userdata/llm_quota.json
```

## 10. 可选演示：Redis 会话 + RQ 消息队列（2026-09-08）

默认部署形态**不需要**本节——未设开关变量时，会话走 JSON 文件后端、corpus-sync 走进程内
线程 job，行为与历史逐字节一致。本节仅是学习/演示用的可选增强，**未接入 deploy.sh、
不进入生产发布流程**；投产（多实例、Redis 备份与公网口径）须另行评审决策。

两个独立开关（语义真源在代码注释：`redis_sessions.py` / `rq_jobs.py` / `redis_store.py`）：

- `BIODATA_SESSION_STORE=redis`：登录会话改存 Redis，多进程/多实例共享登录态；
- `BIODATA_JOB_BACKEND=rq`：corpus-sync 长任务改投 RQ 队列，由独立 worker 进程执行，
  web 进程读状态（Redis hash）并在任务终态时于本进程补做缓存失效。

前置条件：运行环境装有可选依赖（`pip install -r requirements/requirements-mq.txt`；
默认发布镜像不含，演示需自行扩展镜像或本机源码形态）。compose 叠加示例见
`deploy/web/docker-compose.mq.example.yml`（含 redis 服务与 worker 服务，文件头有完整
用法与回退说明）。
