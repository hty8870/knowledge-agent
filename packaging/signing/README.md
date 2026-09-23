# 安装器签名合同（BioData Agent）

本文档是 Windows 安装器正式发布签名的**唯一合同**：顺序、凭据缺席时的降级规则、
禁止事项与验证闭环。配套机器可读逻辑在 `scripts/build_supply_chain.py`
（`unsigned-dev` 命名强制、sidecar 生成、验证报告模板）与
`tests/test_supply_chain.py`（契约测试钉死）。

## 0. 术语

- **签名凭据**：受信任 CA（如 Sectigo/GlobalSign/DigiCert）签发的、用于 Windows
  Authenticode 代码签名的证书 + 私钥（PFX/P12），及其访问口令。只有本段定义的内容
  才算凭据；自签证书、`makecert`/`New-SelfSignedCertificate` 产物**不算**。
- **unsigned-dev**：无签名凭据时所有安装器产物的强制标记（见 §4）。
- **SignedUninstaller**：Inno Setup 为卸载器生成的卸载支持 exe（`{app}\unins000.exe` 的
  配套辅助文件），须与 `Setup.exe` 一并签名。

## 1. 正式发布顺序（有凭据时，全部步骤在本机/隔离构建机执行）

签名在**产物冻结之后**进行：签名对象是逐字节确定的内容，签名后再改任何字节都会使
签名失效，因此顺序不可调换。

1. **冻结**：`scripts/build_windows_runtime.py` 构建冻结运行时，产出
   `dist/BioDataAgent/` 与 `runtime-manifest.json`（逐文件相对路径+大小+SHA-256）。
2. **manifest 校验**：`runtime-manifest.json` 的每个文件 SHA-256 与磁盘逐位一致；
   0 绝对用户路径。
3. **签名安装器二进制**：用 `signtool sign` 对**编译前的 exe/dll 或最终安装器**做
   Authenticode 签名：
   ```powershell
   & $Signtool sign /fd SHA256 /tr <RFC3161 时间戳服务器> /td SHA256 `
     /sha1 <证书指纹> /v <target>
   ```
   - `/fd SHA256`：文件哈希用 SHA-256（不再用默认 SHA1）；
   - `/tr … /td SHA256`：**RFC3161 时间戳**（可审计时间点，证书过期后签名仍有效）；
   - `/v`：详细日志，保留到验证报告。
4. **verify**：`signtool verify /pa /v <target>` 确认签名链可信、时间戳有效。
5. **编译 Inno Setup 安装器**（`build_windows_installer.py` 落地后）：
   `ISCC.exe` 编译出 `Setup.exe`，产物名带版本与 `unsigned-dev` 之外的真实后缀。
6. **签 `Setup.exe` 与 `SignedUninstaller`**：两件都要签（第 3 步同参数），缺一不可；
   卸载器不签会在「控制面板 → 卸载」链路触发 SmartScreen 提示。
7. **再验**：对 `Setup.exe`、`SignedUninstaller` 各跑一次 `signtool verify /pa /v`。
8. **sidecar**：`scripts/build_supply_chain.py --installer <Setup.exe>` 生成
   `<Setup>.sha256`（格式 `<64hex>  <文件名>`，与 `scripts/build_release.py` 一致）。
9. **Defender 扫描**：Windows Defender 实时防护/`MpCmdRun.exe -Scan -ScanType 3 -File <Setup.exe>`
   结果记入验证报告，0 检出。

## 2. 验证报告

`scripts/build_supply_chain.py` 输出 `installer-verification-report.json.template`，
正式发布时逐项填 `results.*`（frozen manifest / Setup 签名 / 卸载器签名 / sidecar
匹配 / Defender 扫描 / 迁移与 E2E 测试）。签名信息只记**脱敏**字段：subject、
thumbprint、证书提供方、时间戳协议与时间——**绝不记录** PFX 路径、口令或私钥任何片段。

## 3. 无签名凭据时的降级规则（无证书模式）

- 全部实现照跑：冻结、manifest、供应链文件（SBOM/NOTICEs/构建工具记录）、Inno 编译、
  sidecar、Defender 扫描、迁移/E2E 测试**都不因缺凭据而停摆**。
- 唯一区别：**产物名必须含 `unsigned-dev`**（`installer_artifact_name` / `assert_unsigned_dev_naming`
  强制；安装器构建脚本按此命名）。未带该标记的产物在无凭据环境一律视为配置错误，拒绝出包。
- 验证报告 `signature.status = "unsigned-dev"`；任何人不得把 `unsigned-dev` 产物
  当作正式发布物分发或对外宣称已签名。

## 4. 禁止事项（红线）

- **禁止自签冒充正式**：自签证书只可用于本地功能自测，产物**必须**继续带
  `unsigned-dev`；用自签证书声称「已签名」即违反本合同。
- **禁止凭据入库/入日志**：PFX/P12 文件、口令、证书私钥**永不**进入 git 仓库、
  提交历史、CI 日志、验证报告、内部笔记或任何对话/交付物。CI 里的签名步骤只引用
  受管 secret 名称（如 `WINDOWS_SIGNING_PFX`、`WINDOWS_SIGNING_PASSWORD`），
  且配置为仅运行在受保护的发布环境。
- **禁止跳过验证**：签完不验、验完不再签名后改动字节、只签 Setup 不签卸载器，均违规。
- **禁止手工抹除标记**：无凭据时改名去掉 `unsigned-dev`，等同伪造签名状态。
- **禁止顺序颠倒**：先编译安装器再改冻结内容、先分发再补签名，均使签名失去意义。

## 5. 凭据获取（待办）

- 选择受信任 CA 的**代码签名证书**（非 EV 亦可，EV 在 Windows 10+ 上更快获得 SmartScreen
  信任），建议加 EV + 硬件令牌或云 HSM。
- 拿到后：证书指纹登记到本仓库**外**的受管密钥库；CI 环境变量只引用 secret 名。
- 本地签名机与 CI 使用同一份 `signtool` 参数模板（本文件 §1），避免两处漂移。
