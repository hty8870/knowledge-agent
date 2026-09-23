# opt-in 安装 BioData 本地 git 钩子：把 core.hooksPath 指向仓库内版本化的 .githooks/。
# 这是无 git remote（GitHub CI 永不触发）的仓库里唯一接近「自动门」的东西——完全可选，不装不影响任何流程。
#
# 用法：  pwsh -File scripts/install-hooks.ps1
# 卸载：  git config --unset core.hooksPath
# 绕过单次提交：  git commit --no-verify
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

git config core.hooksPath .githooks
Write-Output "[install-hooks] 已启用本地 git 钩子：core.hooksPath = .githooks"
Write-Output "[install-hooks] pre-commit 将对**暂存内容**做：锚定 secret 值扫描 + .py 语法编译 + .js node --check。"
Write-Output "[install-hooks] 工具容错、非阻断式缺失（缺 node/python 只跳过对应检查）。"
Write-Output "[install-hooks] 卸载：git config --unset core.hooksPath    绕过单次：git commit --no-verify"
