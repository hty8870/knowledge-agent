# 自动化质量门与候选发布

本文说明 BioData Agent 当前已经落入仓库的本地/CI 质量门、依赖锁定和候选发布流程。它是操作说明，不是远程运行证明：仓库里存在 `.github/workflows/*.yml`，不等于这些工作流已经在 GitHub 执行，也不等于系统已经部署。

## 1. 当前边界

- `automation/quality-gates.json` 是质量门清单的单一真源；`scripts/quality_gate.py` 负责校验清单并按 profile 执行。
- 依赖安装与质量门执行分开。安装依赖可能需要网络；进入质量门后，runner 会清空常见密钥和进程注入变量，使用空的专用 LLM env 文件，设置离线/禁下载环境，并把外部代理指向不可用的本机端口。mock 与 `BIODATA_CI_OFFLINE=1` 的诊断路径不会解析 DNS、建连或发 HTTP。
- 这套“离线门”是受审查命令白名单、环境清理和网络 tripwire 组成的纵深防御，不是操作系统级网络沙箱；新增门必须审查其代码路径，不能据此声称任意子进程在物理上绝不联网。
- 所有必需工具、超时和命令失败都按 fail-closed 处理，不会把降级环境记为通过。
- `.github/workflows/ci.yml` 描述推送、Pull Request 和手动触发时应执行的 CI；`.github/workflows/release-candidate.yml` 描述 tag 或手动触发时应生成的候选包。
- 当前候选发布只生成并上传经过验证的 ZIP、SHA-256 sidecar 和质量报告。它不会创建 GitHub Release，不会读取部署凭据，也不会部署任何主机。
- 产品默认以本机 loopback 形态运行（受信任单用户）；另提供网页版形态，部署走 [deploy/web/README.md](../deploy/web/README.md) 的通用模板（版本化 Docker 镜像 + 可选的登录/邀请/配额护栏）。`.github/workflows/web-image.yml` 只构建镜像并做容器冒烟与漏洞扫描，不执行任何部署。公开、多用户或互联网部署若要继续扩大，认证、授权、限流、上传配额、TLS/反向代理、监控、持久数据备份和自动化部署回滚仍需按目标环境补齐，详见 [SECURITY.md](../SECURITY.md)。

### 1.1 私库到公开仓的确定性镜像

公开仓不是人工清洗后的第二份源码。镜像合同由三份受版本控制的输入组成：

- `packaging/public-mirror/files.txt`：公开输出的完整路径集；
- `packaging/public-mirror/policy.json`：三个显式映射、禁止路径/文本规则与有限豁免；
- `.deliveryignore`：私库 tracked 文件的显式 private 分类。

构建器会要求每个 tracked path 必须属于公开源或 private 分类，并扫描用户主目录路径、未声明邮箱、真实公网 IP 与内部资料引用。日常流程是：

```powershell
python scripts/build_public_mirror.py validate
python scripts/build_public_mirror.py apply --public-root <public-worktree>
python scripts/verify_public_mirror.py --private-root <private-worktree> --public-root <public-worktree>
```

`apply` 只接受干净的私库提交和干净的 public 工作树，产生的 `public-mirror.json` 记录源 commit、运行时版本、策略摘要和全树摘要。public 工作树里的手工修改会被精确路径+字节比较拒绝；问题必须回源端修复。

## 2. 环境与哈希锁

`requirements/requirements.txt` 只含生产运行依赖，不含 pytest。日常开发或需要复现 CI/候选发布环境时，应使用 `requirements/requirements-ci.lock`：

```powershell
python -m venv .venv-ci
$Python = (Resolve-Path '.\.venv-ci\Scripts\python.exe').Path
& $Python -m pip install --require-hashes --only-binary=:all: -r requirements\requirements-ci.lock
& $Python -m pip check
```

`requirements/requirements-ci.txt` 是人工维护的输入，`requirements/requirements-ci.lock` 是带完整传递依赖和 SHA-256 哈希的安装合同。CI 只安装 lock，不直接安装输入文件，也不缓存整个虚拟环境。

只有在依赖变更经过审查时才重生成 lock。当前生成口径为：

```powershell
uv pip compile requirements\requirements-ci.txt `
    --universal `
    --python-version 3.10 `
    --generate-hashes `
    --output-file requirements\requirements-ci.lock
```

重生成后必须审查包名、版本、来源和哈希差异，再在全新虚拟环境用 `--require-hashes --only-binary=:all:` 安装、执行 `pip check`，并运行 `fast` 与 `full`。`--only-binary=:all:` 防止安装器临时转向源码包并解析锁文件之外的构建依赖；不要手工删除哈希来让安装“先通过”。

## 3. 统一质量门

先查看清单和解析后的命令：

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python scripts\quality_gate.py --list
& $Python scripts\quality_gate.py --profile full --dry-run
```

两个 profile 的职责不同：

| Profile | 用途 | 包含的门 |
|---|---|---|
| `fast` | 每个受支持 CI 平台都执行的快速、确定性契约检查 | Python AST 编译、浏览器 JS 语法、自动化/工作流/发布/项目 skill 契约测试 |
| `full` | 主 Windows/Python 环境的权威离线验证 | `fast` 全部内容，加依赖一致性、全量 pytest、项目 smoke、Web smoke、真实 MCP stdio selfcheck、冻结/held-out/dev 三档推荐评测、安装器契约 |

本地执行并保留机器可读报告：

```powershell
& $Python scripts\quality_gate.py `
    --profile fast `
    --report-json artifacts\quality-fast-local.json

& $Python scripts\quality_gate.py `
    --profile full `
    --report-json artifacts\quality-full-local.json
```

默认 profile 是 `fast`，但交付候选包前必须显式运行 `full`。报告中的 `passed` 只证明对应命令在该快照、该解释器和该时间点通过；它不证明远程 CI 已运行。

`full` 还需要 Node.js 和 PowerShell。可通过 `BIODATA_PYTHON`、`BIODATA_NODE`、`BIODATA_POWERSHELL` 指定可执行文件；显式覆盖指向不存在的文件时，runner 会直接失败，不会静默改用其他版本。更细的门和冻结指标见 [质量门与验证矩阵](QUALITY_GATES.md)。

### 3.1 可选本地 pre-commit 钩子

无论 GitHub CI 是否已实际运行，交付候选包前都必须在本地显式跑 `full` 门。装一个**可选**的本地钩子作补充：

```powershell
pwsh -File scripts\install-hooks.ps1        # 设 core.hooksPath=.githooks
git config --unset core.hooksPath           # 卸载
git commit --no-verify                       # 单次绕过
```

`pre-commit` 只做**快、确定、工具容错**的三件事：暂存内容扫锚定 secret 值（`sk-`/`ghp_`/`AKIA`/JWT 等，只报模式名+位置、不回显值）、暂存 `.py` 语法编译、暂存 `.js` 跑 `node --check`（缺 node 只跳过、不阻断）。它**不**跑全量 pytest、也**不**扫交付专用词表（那道扫描由 `make_delivery` 交付复核执行）。完全可选，不装不影响任何流程。

### 3.2 门的安全/确定性护栏（本轮新增）

- **交付 secret 值扫描**：`scripts/make_delivery.py --check` 除内部专名外，现按锚定模式扫真 key 误粘（真源 `src/dataset_recommender/secret_patterns.py`，`scripts/secret_patterns.py` 为兼容壳；交付门、`quality_gate.py` report 脱敏与 MCP 调用留痕值级脱敏三方共用）；只报模式名+位置、绝不回显值；并对交付集里 UTF-8 解码失败的文本文件显式告警（杜绝静默放行）。
- **质量门 report 脱敏**：`quality_gate.py` 落盘 report 前对命令 stdout/stderr 尾做同批 secret redaction（env 名脱敏之外再补命令**输出**里的 secret 形状值）。
- **冻结基线字节守卫 / 前端加载期引用门 / href 方案守卫 / 冻结评测 workflow 隔离 / .env 模板 provider-key 前提 / 缓存令牌单一真源** 等确定性契约测试随 `full` 的 pytest 一并执行，见 [质量门与验证矩阵](QUALITY_GATES.md)。

## 4. GitHub CI 合同

`.github/workflows/ci.yml` 定义三条质量门执行腿，另有三条原生系统的 source zip 首启腿：

| 标签 | Runner | Python | Profile |
|---|---|---:|---|
| `windows-primary-full` | `windows-2025` | 3.12 | `full` |
| `ubuntu-min-fast` | `ubuntu-24.04` | 3.10 | `fast` |
| `ubuntu-current-fast` | `ubuntu-24.04` | 3.14 | `fast` |

首启矩阵分别在 `windows-2025`、`macos-15`、`ubuntu-24.04` 上构建并解压候选 zip，调用各自面向用户的 `打开前端.bat` / `.command` / `.sh`。启动器必须新建项目内 `.venv`、成功导入全部 Web 运行依赖，并证明 `import pytest` 失败；探针在向导、浏览器和服务启动前退出。该矩阵会访问依赖索引模拟真实首装，和安装后离线运行的质量门职责不同。

工作流从空 token 权限开始，具体 job 只获得 `contents: read`；第三方 action 固定到完整 commit SHA；checkout 不持久化凭据。质量腿上传 JSON 报告，稳定的 `gate` job 同时汇总质量矩阵和三平台首启矩阵，适合作为以后受保护分支的 required check。

要让它真正成为合并门，还需要在 GitHub 上完成以下外部配置：

1. 把项目连接到正确的远程仓库并推送工作流；
2. 在 GitHub Actions 页面确认三条执行腿真实通过；
3. 在目标分支保护规则中把精确 job 名 `gate` 设为 required check；
4. 检查 Dependabot 提交的 pip 与 GitHub Actions 更新，不自动合并未经本项目门验证的更新。

仓库内没有远程运行记录，因此交付说明不得把上述外部步骤写成“已完成”。

## 5. 构建与验证候选包

> **受众边界（两套产物勿混淆）**：`scripts/build_release.py` 产出的是**候选发布包**，受众为「可复查源码 + 受信本机运行」（`ARTIFACT_AUDIENCE`），因此**有意**纳入**源码级契约与流程文档**，供复查者审计与本机运行——这些复查者本就持有完整仓库。**对外客户交付**是**另一套**产物，由 `scripts/make_delivery.py` + `.deliveryignore` 产出，会在此基础上**进一步剥除**内部契约/流程文档并对留存文本做敏感词复核（真名/内部专名/本机路径）。给客户的永远走 `make_delivery`，不要把候选发布包直接当对外交付。

候选包构建器采用 allowlist：代码、测试、运行说明、`database/base/`、十份经过审查的公开 external 快照、指定公开文档和冻结评测输入可以进入 ZIP；运行时 `upload_...` 数据、其他 external 数据、生成报告和内部实施文档不会因位于相邻目录而被带入。`.env`、`.git`、`.github`、模型、虚拟环境、缓存、日志、输出、协作台账、个人材料和含用户主目录绝对路径的文件也会被拒绝。构建前应冻结一致快照、停止并发写入，并记录当前 commit 和 dirty 状态；构建器会把 `source_commit`、`source_dirty` 和 `product_version` 写入 manifest，但不会替操作者拒绝 dirty 工作树。

本地流程：

```powershell
& $Python scripts\quality_gate.py `
    --profile full `
    --report-json artifacts\quality-release.json

$Evidence = 'artifacts\candidate-1.2.0-20260714'
& $Python scripts\build_release.py build `
    --output-dir $Evidence `
    --expected-version 1.2.0

& $Python scripts\build_release.py verify `
    --archive "$Evidence\biodata-agent-release-candidate.zip"
```

归档根目录内的 `release-manifest.json` 逐文件记录路径、大小和 SHA-256，并给出整体内容摘要；同目录的 `.zip.sha256` 校验整个归档。`verify` 会拒绝路径穿越、非规范/Windows 不安全名称、大小写或 Unicode 可移植路径冲突、链接、重复路径、未列入 manifest 的文件、被排除路径、文件数/压缩与解压大小越界，以及大小或哈希不一致。tag 工作流还会要求 `vX.Y.Z` 与 `WEB_API_VERSION` 完全相同。

构建器默认拒绝覆盖同名 ZIP/sidecar，交付应始终使用新的证据目录。只有明确传入 `--replace-existing` 才会替换完整同名二元组；该路径先验证临时包，提升后再次验证，对进程捕获到的构建、验证或取消异常恢复原二元组。断电或进程强杀无法纳入这个双文件事务承诺；正式交付因此仍不应依赖覆盖来保存上一候选。

必须再对“解包后的候选物”做 smoke。解压目录要位于仓库外，避免仓库级扫描工具把候选包内容误当成当前仓库的一部分：

```powershell
$Archive = (Resolve-Path "$Evidence\biodata-agent-release-candidate.zip").Path
$Unpacked = Join-Path ([IO.Path]::GetTempPath()) ("biodata-agent-" + [guid]::NewGuid())
if (Test-Path -LiteralPath $Unpacked) { throw "临时目录已存在：$Unpacked" }

Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked
Push-Location -LiteralPath $Unpacked
try {
    & $Python -B scripts\smoke_test.py
    & $Python -B scripts\web_smoke_test.py
    & $Python -B mcp_server.py --selfcheck
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Unpacked -Recurse -Force
}
```

这里复用的 `$Python` 必须是已经安装 `requirements/requirements-ci.lock` 的绝对路径；不要改用解包目录中不存在的虚拟环境。删除临时目录前要确认 `$Unpacked` 是本次新建且位于预期临时根下。

## 6. 回滚与证据保留

候选发布本身不修改源树，默认也拒绝覆盖既有完整归档二元组。每次交付应使用独立的、带时间或版本标识的证据目录，并至少保留：

- 变更前 commit、`git status --short` 和受影响文件哈希；
- `quality-full` JSON 报告及其执行环境；
- 候选 ZIP、`.zip.sha256` 和归档内 `release-manifest.json`；
- 解包 smoke 的命令、退出码和最小相关输出；
- 上一个已验证候选包，以及它自己的 sidecar/manifest。

手工把变更同步到另一份主线前，应先按精确文件清单复制旧文件到仓库外的时间戳备份目录；新文件要显式标记为“原先不存在”。恢复时只按该清单逐文件还原/删除，并在恢复后重新计算哈希和运行受影响门。不要使用 `git reset --hard`、整目录覆盖或清理未知文件代替有证据的回滚。

由于当前没有生产部署工作流，“部署回滚到上一版本”尚未实现。将来接入目标环境时，应增加受保护 environment、短期身份凭据、部署后健康观察和“上一份已验证候选包”回滚步骤；在这些能力落地前，不得把候选包流水线称为自动部署。

## 7. 失败处理

任一门、构建、归档复验或解包 smoke 失败时：

1. 停止上传、发布和部署；
2. 保留失败报告、退出码和最小相关输出；
3. 区分环境缺失、既有失败和本次回归；
4. 修复根因后从 `full` 重新开始，不跳过失败门、不降低阈值、不删除哈希；
5. 如果快照在验证过程中被其他进程修改，丢弃这次证据并从新的稳定快照重跑。
