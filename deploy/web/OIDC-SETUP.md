# GitHub Actions → registry → SSH 部署配置模板

> 本文件只保留可公开的机制和占位符。真实主机、账号 ID、host key、实例 ID、namespace、
> 健康 URL 和运维证据必须存放在受控运维系统，不进入 Git。

## 0. 信任链

```text
push master
  → CI + Web image 成功
  → AUTO_DEPLOY=true 时才允许自动路径（默认 false）
  → registry 推送经过验证的不可变镜像
  → production environment job
  → SSH 到专用 deploy 用户
  → sudo /usr/bin/docker pull + docker tag（sudoers 白名单限定）
  → sudo /opt/biodata-web/deploy.sh <validated-tag>
  → 内部 health/account guard + 外部 health（scheme 跟随 PUBLIC_HEALTH_URL）
```

日常应保持 `AUTO_DEPLOY=false`。`workflow_dispatch` 只部署已经存在的镜像 tag；它仍必须受
production environment 的 branch/tag restriction 和 required reviewer 约束。GitHub 事件、CI
绿灯或普通仓库 write 权限都不自动等于生产授权。

## 1. Registry

准备专用 registry namespace 和只具备所需 push/pull 权限的凭据。GitHub production
environment 保存：

- secret `DEPLOY_SSH_KEY`（专用 deploy 用户私钥，不复用个人/管理员 key）；
- secret `TCR_REGISTRY_PASSWORD`（或所用 registry 的等价 secret）；
- variable `TCR_REGISTRY=registry.example.com`；
- variable `TCR_NAMESPACE=<namespace>`；
- variable `TCR_USERNAME=<registry-user>`。

服务器 root 完成 registry 登录；deploy 用户不得读取 root 的 Docker 凭据。

## 2. GitHub production environment

| variable | 示例/要求 |
|---|---|
| `DEPLOY_HOST` | `<server-host>`，仅主机名/IP，不带 shell 字符 |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_HOST_KEY` | `<server-host> ssh-ed25519 <public-host-key>` |
| `PUBLIC_HEALTH_URL` | `https://<your-domain>/api/health`；域名 ICP 备案过渡期可为 `http://<server-host>/api/health`（2026-09-09 起，备案后切回 HTTPS） |
| `AUTO_DEPLOY` | `false`（默认） |

不再使用的 cloud/OIDC secret 应删除，而不是长期留在 environment。

Protection rules：

- Deployment branches/tags 限制为 protected `master` 和明确发行 tag；
- 至少一个 required reviewer；
- 防止管理员/普通 write 协作者绕过；
- environment secret 只允许受信 workflow 使用。

如果当前 GitHub 套餐不能强制这些规则，不得声称 protection 已生效；应迁移到支持强制审批的
组织/套餐，或把生产部署移到有独立审批的外部系统。

## 3. 服务器一次性准备

1. 创建锁密码、仅密钥登录的 `deploy` 用户；禁端口转发、agent forwarding、tunnel 和 TTY。
2. root 安装以下文件：
   - `/opt/biodata-web/deploy.sh`：root:root 0755；
   - `/opt/biodata-web/.env`：root:root 0600；
   - `/opt/biodata-web/docker-compose.web.yml`：root:root 0644。
3. sudoers 只放行部署必需的三条命令（2026-08-30 简化：wrapper/policy 通道退役，
   白名单直接对齐 deploy.sh 的真实需要；registry image 路径由 workflow 侧校验）：

   ```sudoers
   deploy ALL=(root) NOPASSWD: /usr/bin/docker pull *, /usr/bin/docker tag *, /opt/biodata-web/deploy.sh *
   ```

4. 用 `visudo -cf` 校验；以下命令必须被拒绝：`sudo docker run ...`、`sudo docker exec ...`、
   `sudo cat /opt/biodata-web/.env`、`sudo bash`。
5. 安装 TLS 反向代理；应用端口只绑定 `127.0.0.1`。使用
   `nginx.biodata.conf.example` 作为模板，并在启用前执行 `nginx -t`。
   （域名 ICP 备案过渡期可暂缺 TLS 代理，公网经宿主 80 端口 HTTP 直连；备案后补回。）

## 4. 首次手动验收

1. 保证已有可回退镜像；
2. 保持 `AUTO_DEPLOY=false`；
3. 从受保护 `master` 手动运行 Deploy web，输入合法既有 tag；
4. 预期：workflow 校验输入与 registry image 路径，服务器 pull/tag 后 deploy.sh 完成部署，
   内部和外部 health 均通过；
5. 验证：
   - `https://<your-domain>/api/health` 返回 `account.required == true`
     （过渡期：`http://<server-host>/api/health` 同断言）；
   - HTTP 自动重定向 HTTPS（TLS 落地后要求）；
   - session cookie 带 `Secure`/`HttpOnly`/`SameSite=Strict`（TLS 落地后要求）；
   - `127.0.0.1:8510` 仅在服务器本机可达（公网入口经宿主 80/443）；
   - 非法 tag、非法 registry image 路径均 fail-closed；
   - deploy 用户只能 sudo 白名单三条命令，无法读取 `.env` 或获得任意 root shell。

## 5. 回退

回退走同一条 deploy.sh 通道（镜像须已在服务器本机镜像库）：

```bash
ssh -i ~/.ssh/<deploy-key> deploy@<server-host> \
  "sudo -n /opt/biodata-web/deploy.sh '<verified-old-tag>'"
```

deploy.sh 校验 tag 语法、等待 healthy、断言账户门，并在失败时自动回到上一 tag。

## 6. 轮换与审计

- SSH key、registry password、邀请码和服务端 API key 分别轮换；
- 不在 workflow 日志打印 secrets、host key 或 `.env`；
- 每次部署记录批准人、commit、镜像 digest、tag、前后版本、health、回退结果；
- 定期复核 `sudo -l -U deploy` 与 environment protection；
- 删除不用的 secret/variable，不用“留着也没事”替代撤权。
