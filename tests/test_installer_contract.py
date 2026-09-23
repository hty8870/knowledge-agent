# -*- coding: utf-8 -*-
"""安装器静态契约测试（离线、不编译、不执行安装器）。

钉住 packaging/inno/biodata-agent.iss 的关键参数、版本单一真源、无管理员约束、
删除边界代码存在性、语言文件与品牌资产。全部为只读静态断言。
"""
from __future__ import annotations

import importlib.util
import json
import re
import struct
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ISS = _ROOT / "packaging" / "inno" / "biodata-agent.iss"
_WEBAPP = _ROOT / "src" / "dataset_recommender" / "app" / "webapp.py"
_LAUNCHER = _ROOT / "src" / "dataset_recommender" / "app" / "desktop_launcher.py"
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows_installer.py"
_INSTALLER_WORKFLOW = _ROOT / ".github" / "workflows" / "installer-build.yml"
_MAKE_ICO = _ROOT / "packaging" / "assets" / "make_ico.py"
_ICO = _ROOT / "packaging" / "assets" / "BioDataAgent.ico"
_LANG_DIR = _ROOT / "packaging" / "inno" / "languages"

# 逐字钉死的安装器恒量（契约固定参数）
# 注意：Inno 常量转义 `{{`→`{`，AppId 指令以双开单闭书写，值为 {E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}
APP_ID = "{{E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}"
APP_GUID = "E249D2BA-8457-4B8A-B2C9-B7CFA234BA79"
MUTEX = r"Local\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79"
USER_MODEL_ID = "BioDataAgent.Desktop"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("build_windows_installer", _BUILD_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _section(iss_text: str, name: str) -> str:
    """抽取 [name] 段到下一个 [ 段标题之间的文本。"""
    lines = iss_text.splitlines()
    out, inside = [], False
    for line in lines:
        if line.startswith("["):
            if inside:
                break
            inside = line.strip().lower() == f"[{name.lower()}]"
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def iss_text() -> str:
    return _ISS.read_text(encoding="utf-8")


# ── 身份与恒量逐字 ─────────────────────────────────────────────────────
class TestIdentity:
    def test_appid_verbatim(self, iss_text):
        assert f"AppId={APP_ID}" in iss_text

    def test_appname_verbatim(self, iss_text):
        assert "AppName=BioData Agent" in iss_text

    def test_appusermodelid_verbatim(self, iss_text):
        # 固定参数逐字出现（Inno 无内置指令 → 注释钉名 + [Code] 常量钉值）
        assert f"AppUserModelID={USER_MODEL_ID}" in iss_text
        code_section = _section(iss_text, "Code")
        assert f"APP_USER_MODEL_ID = '{USER_MODEL_ID}'" in code_section

    def test_mutex_verbatim(self, iss_text):
        assert f"AppMutex={MUTEX}" in iss_text

    def test_mutex_matches_w2_launcher_constant(self):
        """desktop_launcher.MUTEX_NAME 与本 iss AppMutex 逐字一致。"""
        launcher_text = _LAUNCHER.read_text(encoding="utf-8")
        match = re.search(r'MUTEX_NAME\s*=\s*r?"([^"]+)"', launcher_text)
        assert match, "desktop_launcher.py 未找到 MUTEX_NAME 常量"
        assert match.group(1) == MUTEX

    def test_appid_guid_consistent_with_mutex(self):
        """AppId 的 GUID（去连字符）必须作为 mutex 后缀同源出现。"""
        assert APP_GUID.replace("-", "").upper() in MUTEX


# ── 无管理员 / 目录 / 架构 ─────────────────────────────────────────────
class TestNonAdminAndLayout:
    def test_privileges_lowest(self, iss_text):
        assert "PrivilegesRequired=lowest" in iss_text

    def test_no_privileges_override(self, iss_text):
        assert "PrivilegesRequiredOverridesAllowed" not in iss_text

    def test_no_runas_anywhere(self, iss_text):
        """无任何提权运行（[Run]/[UninstallRun]/[ShellExec] 均不得出现 runas）。"""
        assert "runas" not in iss_text.lower()

    def test_no_registry_hklm_write(self, iss_text):
        """无 [Registry] 段（PrivilegesRequired=lowest 时 Inno 自动注册 HKCU 卸载信息）。"""
        assert "[Registry]" not in iss_text

    def test_architectures_x64compatible(self, iss_text):
        assert "ArchitecturesAllowed=x64compatible" in iss_text
        assert "ArchitecturesInstallIn64BitMode=x64compatible" in iss_text

    def test_default_dirname(self, iss_text):
        assert r"DefaultDirName={localappdata}\Programs\BioData Agent" in iss_text


# ── 页面流 / 关闭行为 / 完成页 ─────────────────────────────────────────
class TestWizardAndClose:
    def test_pages_enabled(self, iss_text):
        assert "DisableWelcomePage=no" in iss_text
        assert "DisableDirPage=no" in iss_text
        assert "DisableProgramGroupPage=no" in iss_text

    def test_closeapplications_yes_and_no_force(self, iss_text):
        assert "CloseApplications=yes" in iss_text
        assert "CloseApplications=force" not in iss_text

    def test_restartapplications_no(self, iss_text):
        assert "RestartApplications=no" in iss_text

    def test_finish_page_run_postinstall(self, iss_text):
        run_section = _section(iss_text, "Run")
        assert "postinstall" in run_section
        assert "skipifsilent" in run_section
        assert "BioDataAgent.exe" in run_section

    def test_desktopicon_task_optional_unchecked(self, iss_text):
        tasks = _section(iss_text, "Tasks")
        assert "desktopicon" in tasks
        assert "Flags: unchecked" in tasks

    def test_local_model_is_explicit_online_optional_task(self, iss_text):
        tasks = _section(iss_text, "Tasks")
        run = _section(iss_text, "Run")
        code = _section(iss_text, "Code")
        assert 'Name: "localmodel"' in tasks and 'Flags: unchecked' in tasks
        assert "3 GB" in iss_text and "5 GB" in iss_text
        # 执行在 [Code]（Inno 6 的 [Run] 段无 ignoreerrors flag，ISCC 6.7.3 报 unknown flag）：
        # 显式 task 门控 + 阻塞 Exec + 非零退出码只进日志（失败不弹错误框、基础安装无回滚）。
        assert "RunLocalModelInstallIfSelected" in code
        assert "WizardIsTaskSelected('localmodel')" in code
        assert "'--install-local-model'" in code
        assert "ewWaitUntilTerminated" in code
        assert "ResultCode <> 0" in code
        # [Run] 段不得再挂模型命令；正常 postinstall 仍只是 --window。
        assert "--install-local-model" not in run
        assert 'Parameters: "--window"' in run
        # Inno 6 的 [Run]/[Tasks] Flags 白名单回归钉：禁止再把 ignoreerrors 写回任何 Flags 参数。
        for line in iss_text.splitlines():
            if "Flags:" in line and not line.lstrip().startswith(";"):
                assert "ignoreerrors" not in line


# ── 卸载只删 install_root / 数据根不进删除清单 ─────────────────────────
class TestUninstallBoundary:
    def test_no_undelete_section(self, iss_text):
        """不存在 [UninstallDelete] —— 卸载默认只删 [Files] 覆盖的 install_root。"""
        assert "[UninstallDelete]" not in iss_text

    def test_files_excludes_data_root(self, iss_text):
        files_section = _section(iss_text, "Files")
        # 剔除注释行（`;` 开头），只校验实际指令
        directives = "\n".join(
            line for line in files_section.splitlines() if line.strip() and not line.lstrip().startswith(";")
        )
        assert r"{localappdata}\BioDataAgent" not in directives
        assert "LOCALAPPDATA" not in directives.upper()
        assert "BioDataAgent.ico" in directives      # 品牌图标随装
        assert "recursesubdirs" in directives.lower()  # onedir 全量落位

    def test_delete_boundary_code_present(self, iss_text):
        code_section = _section(iss_text, "Code")
        # 精确路径校验（等于当前用户 %LOCALAPPDATA%\BioDataAgent）
        assert "ExpectedDataRoot" in code_section
        assert "NormalizePath" in code_section
        # 拒绝 junction / reparse point 跟随
        assert "FILE_ATTRIBUTE_REPARSE_POINT" in code_section
        assert "GetFileAttributesW" in code_section
        # 拒绝等于安装目录或上级
        assert "PathIsAncestorOrSame" in code_section
        assert "DelTree" in code_section
        # 删除前提示不可恢复语义（自定义消息 + 拒绝分支）
        assert "DeleteRejected" in code_section or "DeleteRejected" in iss_text

    def test_delete_option_default_unchecked(self, iss_text):
        """可选「同时删除我的本地数据」默认不勾：MsgBox 默认按钮 = No（MB_DEFBUTTON2）。"""
        code_section = _section(iss_text, "Code")
        assert "MB_DEFBUTTON2" in code_section
        assert "IDYES" in code_section
        # 确认文案含不可恢复语义（英文 + 中文各钉一处）
        assert "IRREVERSIBLE" in iss_text or "irreversible" in iss_text
        assert "不可恢复" in iss_text

    def test_data_root_redirect_hook(self, iss_text):
        """数据根隔离钩子（E2E 联调前置）——安装期读 BIODATA_DATA_ROOT env 持久化
        到卸载注册信息；卸载器 InitializeUninstall 缓存解析结果（env 优先 → 注册表 →
        默认路径），usPostUninstall 用缓存值（该阶段注册键可能已被 Inno 清理）。"""
        code_section = _section(iss_text, "Code")
        assert "PersistDataRootForUninstall" in code_section
        assert "DATA_ROOT_REGISTRY_VALUE" in code_section
        assert "BIODATA_DATA_ROOT" in code_section
        assert "GetEnv('BIODATA_DATA_ROOT')" in code_section
        assert "RegWriteStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, DATA_ROOT_REGISTRY_VALUE" in code_section
        assert "RegQueryStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, DATA_ROOT_REGISTRY_VALUE" in code_section
        assert "ssPostInstall" in code_section
        # 卸载侧：InitializeUninstall 缓存到 U_DataRoot（注册键清理前读回）
        assert "InitializeUninstall" in code_section
        assert "U_DataRoot := ExpectedDataRoot()" in code_section
        assert "U_DataRoot" in code_section

    def test_uninstall_delete_data_switch(self, iss_text):
        """卸载删数据开关（非交互验证）——卸载器 /DELETEDATA 命令行参数直接触发
        删除（精确路径校验仍生效），不带则维持默认询问保留。"""
        code_section = _section(iss_text, "Code")
        assert "CommandLineWantsDeleteData" in code_section
        assert "/DELETEDATA" in code_section
        assert "GetCmdTail" in code_section

    def test_deletedata_parsed_by_token_not_substring(self, iss_text):
        """/DELETEDATA 必须按 token 边界解析（不再子串匹配）——
        `/DELETEDATA_EXTRA`、`--DELETEDATA` 等相邻字符不得误触发。"""
        code_section = _section(iss_text, "Code")
        # 旧实现（子串匹配）不得残留
        assert "Pos('/DELETEDATA'" not in code_section
        # 新实现：逐 token 与 '/DELETEDATA' 精确比较（大小写不敏感）
        assert "Uppercase(Token) = '/DELETEDATA'" in code_section
        assert "Token := ''" in code_section

    def test_install_delete_upgrade_cleanup_present(self, iss_text):
        """升级前清理 install_root 的 onedir 旧内容——[InstallDelete] 段被
        [Code] CleanupStaleInstallRoot 取代（Inno [InstallDelete] 段不支持 Excludes）：
        递归删除 {app} 内容、严格跳过 unins000.*、仅 IsUpgrade 执行。"""
        code_section = _section(iss_text, "Code")
        assert "function DeleteTreeWithExcludes" in code_section
        assert "procedure CleanupStaleInstallRoot" in code_section
        assert "CleanupStaleInstallRoot" in code_section
        # 排除 unins*（卸载器文件不得被清理删除）
        assert "unins000.exe|unins000.dat|unins000.msg" in code_section
        # 仅升级执行 + data_root 不碰（{app} 之外）
        assert "if not IsUpgrade then" in code_section
        assert "ssInstall" in code_section
        # 目录 junction/reparse point 必须在递归调用前被跳过，防越出 {app} 清理。
        cleanup = code_section[
            code_section.index("function DeleteTreeWithExcludes"):
            code_section.index("procedure CleanupStaleInstallRoot")
        ]
        directory_pos = cleanup.index("FILE_ATTRIBUTE_DIRECTORY")
        reparse_pos = cleanup.index("FILE_ATTRIBUTE_REPARSE_POINT", directory_pos)
        recurse_pos = cleanup.index("DeleteTreeWithExcludes(Full", reparse_pos)
        assert directory_pos < reparse_pos < recurse_pos
        assert "升级前清理跳过目录重解析点" in cleanup
        # [InstallDelete] 段不应再出现实际删除条目（Inno 不支持 Excludes）
        delete_section = _section(iss_text, "InstallDelete")
        assert "filesandordirs" not in delete_section.lower()
        assert r'Name: "{app}\*"' not in delete_section

    def test_normalize_path_canonicalizes_and_rejects(self, iss_text):
        """删除边界规范化——GetFullPathNameW 真实规范化；拒绝含 .. 段、
        非绝对路径、\\\\?\\ 前缀的输入（fail-closed 返回空串）。"""
        code_section = _section(iss_text, "Code")
        assert "GetFullPathNameW@kernel32.dll" in code_section
        assert "function PathHasDotDot" in code_section
        assert "function NormalizePath" in code_section
        # 拒绝形态逐字出现
        assert "\\\\?\\" in code_section
        assert "PathHasDotDot(S)" in code_section
        # 非绝对盘符检查（X:\ 开头）
        assert "(S[2] <> ':')" in code_section
        # 删除前必须与解析出的预期 data root 精确比较
        assert "Target <> Expected" in code_section


# ── 版本单一真源（fail-closed）─────────────────────────────────────────
class TestVersionSingleSource:
    def test_webapi_version_single_definition(self):
        text = _WEBAPP.read_text(encoding="utf-8")
        hits = re.findall(r'^WEB_API_VERSION\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
        assert len(hits) == 1, f"WEB_API_VERSION 必须恰好一处定义，实际 {len(hits)}"
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", hits[0]), hits[0]

    def test_build_script_parses_webapp_as_source(self):
        """构建脚本必须从 webapp.py 解析版本（单一真源），而非硬编码。"""
        src = _BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "WEB_API_VERSION" in src
        assert "parse_webapi_version" in src
        assert "/DAppVersion=" in src

    def test_iss_fail_closed_define_guard(self, iss_text):
        """iss 对未注入的 AppVersion / RuntimeDir 编译期 #error（fail-closed）。"""
        assert "#ifndef AppVersion" in iss_text
        assert "#error" in iss_text
        assert "#ifndef RuntimeDir" in iss_text

    def test_parse_webapi_version_roundtrip(self):
        mod = _load_script_module()
        assert mod.parse_webapi_version(_WEBAPP) == re.findall(
            r'^WEB_API_VERSION\s*=\s*"([^"]+)"\s*$', _WEBAPP.read_text(encoding="utf-8"), re.MULTILINE
        )[0]

    def test_parse_webapi_version_fail_closed(self, tmp_path):
        mod = _load_script_module()
        bogus = tmp_path / "webapp.py"
        # 0 处定义
        bogus.write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(ValueError):
            mod.parse_webapi_version(bogus)
        # 2 处定义
        bogus.write_text('WEB_API_VERSION = "1.0.0"\nWEB_API_VERSION = "2.0.0"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            mod.parse_webapi_version(bogus)
        # 形状非法
        bogus.write_text('WEB_API_VERSION = "2.4"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            mod.parse_webapi_version(bogus)


# ── 构建产物命名 / 签名姿态 ────────────────────────────────────────────
class TestBuildArtifacts:
    def test_output_naming_unsigned_dev(self):
        src = _BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "BioData-Agent-Setup-{version}-win-x64-unsigned-dev.exe" in src
        assert "unsigned-dev" in src

    def test_sha256_sidecar(self):
        src = _BUILD_SCRIPT.read_text(encoding="utf-8")
        assert ".sha256" in src
        assert "sha256_file" in src

    def test_iscc_required_and_official(self):
        src = _BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "Inno Setup 6" in src
        assert "/O" in src and "/F" in src


# ── 语言文件 ───────────────────────────────────────────────────────────
class TestLanguages:
    def test_english_isl_vendored(self):
        f = _LANG_DIR / "English.isl"
        assert f.is_file()
        assert "LanguageName=English" in f.read_text(encoding="utf-8")

    def test_chinese_simplified_isl_vendored(self):
        f = _LANG_DIR / "ChineseSimplified.isl"
        assert f.is_file()
        assert "LanguageName=简体中文" in f.read_text(encoding="utf-8")

    def test_iss_references_vendored_languages(self, iss_text):
        lang = _section(iss_text, "Languages")
        assert "languages\\English.isl" in lang
        assert "languages\\ChineseSimplified.isl" in lang


# ── 品牌资产（ICO 派生）────────────────────────────────────────────────
class TestBrandAsset:
    def test_ico_exists_with_expected_layers(self):
        assert _ICO.is_file(), "BioDataAgent.ico 缺失（运行 packaging/assets/make_ico.py）"
        data = _ICO.read_bytes()
        _res, _typ, count = struct.unpack_from("<HHH", data, 0)
        assert _typ == 1  # ICO 类型
        sizes = []
        for i in range(count):
            w, h = struct.unpack_from("<BB", data, 6 + 16 * i)
            sizes.append((w or 256, h or 256))
        expected = [16, 20, 24, 32, 48, 64, 128, 256]
        assert [s[0] for s in sizes] == expected, f"ICO 图层不符：{sizes}"
        assert count == 8

    def test_make_ico_script_exists(self):
        src = _MAKE_ICO.read_text(encoding="utf-8")
        assert "webapp.py" in src          # 引用 favicon 真源
        assert "LANCZOS" in src            # 确定性降采样
        assert "#0d9488" in src.lower() or "13, 148, 136" in src  # 品牌青色

    def test_iss_uses_ico(self, iss_text):
        assert "SetupIconFile=..\\assets\\BioDataAgent.ico" in iss_text
        assert "{app}\\assets\\BioDataAgent.ico" in iss_text


# ── 安装器边缘修复（安装器边缘项）静态契约 ─────────────
class TestEdgeFixesStatic:
    def test_silent_uninstall_delete_failure_no_msgbox_nonzero_exit(self, iss_text):
        """第 2 项：静默卸载 + /DELETEDATA 删不掉 → 不弹 Pascal MsgBox（/SUPPRESSMSGBOXES
        抑制不了它，无人值守会永远卡在没人点的弹窗），只写日志并 ExitProcess(非零)；
        交互模式保留弹窗。"""
        code = _section(iss_text, "Code")
        assert "IsUninstallerSilent" in code
        assert "U_DeleteFailed" in code
        assert "ReportDeleteFailure" in code
        assert "ExitProcess@kernel32.dll" in code
        assert "ExitProcess(1)" in code
        # 静默分支只写日志（Log），不调 MsgBox；交互分支才 MsgBox
        assert "Log('TryDeleteLocalData 静默失败" in code
        # 交互模式保留 MsgBox（ReportDeleteFailure 的 else 分支）。把顺序断言锚到
        # ReportDeleteFailure 过程内部：InitializeSetup 也新增了同签名 MsgBox(Msg, Kind,
        # MB_OK)（ReportSetupBlocked，位于本过程之前），若用全局首个出现会被误伤。
        rdf_pos = code.index("procedure ReportDeleteFailure")
        log_pos = code.index("Log('TryDeleteLocalData 静默失败", rdf_pos)
        msg_pos = code.index("MsgBox(Msg, Kind, MB_OK)", log_pos)
        assert log_pos < msg_pos

    def test_initialize_setup_dir_mismatch_guard(self, iss_text):
        """第 3 项：同 AppId 已安装 + 显式 /DIR 与 InstallLocation 不同 → 明确提示并终止，
        不再静默覆盖卸载键孤儿化旧安装目录。

         修复：原实现在此处用 `ExpandConstant('{app}')` 取目标目录，但 {app}
        在 InitializeSetup 返回后才初始化，会抛 "app constant before initialized" 致命错误
        并在无 /SUPPRESSMSGBOXES 时弹模态框挂死（此前 300s 超时的根因）。改为
        `{param:DIR}`（实证可读内建 /DIR）回退 DefaultDirName 同值，并给两个中止提示
        加 WizardSilent 静默防护（静默只 Log 不弹框）。"""
        code = _section(iss_text, "Code")
        assert "ReadInstalledLocation" in code
        assert "CommandLineHasDirOverride" in code
        assert "SameInstallDir" in code
        assert "InstallLocation" in code
        # 提示文案在 [Code] 使用，且 [CustomMessages] 中英双语都有定义（否则 Inno 编译失败）
        assert "DirMismatchInstalled" in code
        assert "english.DirMismatchInstalled=" in iss_text
        assert "chinesesimplified.DirMismatchInstalled=" in iss_text
        # 修复回归锁：InitializeSetup 不得再展开 {app}（旧实现致命错误的源头），
        # 目标目录改走 TargetInstallDirForCheck / {param:DIR}，并带 WizardSilent 静默防护。
        assert "TargetInstallDirForCheck" in code
        assert "ExpandConstant('{param:DIR}')" in code
        assert "WizardSilent" in code
        assert "ReportSetupBlocked" in code
        assert "SameInstallDir(InstalledLoc, ExpandConstant('{app}')" not in code
        # 版本守卫（NewerInstalled）同样走 ReportSetupBlocked 的静默防护
        assert "NewerInstalled" in code


# ── runtime-manifest 校验（fail-closed）─────────────
class TestRuntimeManifestValidation:
    """钉住 build_windows_installer.verify_runtime_manifest：把 `--build-runtime` 只认目录存在的
    fail-open 收紧成「manifest 存在 + 关键字段一致 + 实物逐文件对账」的 fail-closed。"""

    @staticmethod
    def _build_manifest(app_dir: Path) -> dict:
        import hashlib

        files, total = [], 0
        for p in sorted(app_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(app_dir).as_posix()
            size = p.stat().st_size
            files.append({"path": rel, "size": size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
            total += size
        return {
            "format": "biodata-runtime-manifest/v1",
            "app": app_dir.name,
            "onedir_root": app_dir.name,
            "file_count": len(files),
            "total_bytes": total,
            "files": files,
        }

    @staticmethod
    def _make_runtime(tmp_path: Path, override: dict | None = None) -> Path:
        out = tmp_path / "out"
        app_dir = out / "dist" / "BioDataAgent"
        (app_dir / "_internal").mkdir(parents=True)
        (app_dir / "BioDataAgent.exe").write_bytes(b"exe")
        (app_dir / "_internal" / "a.py").write_bytes(b"a")
        (app_dir / "_internal" / "b.json").write_bytes(b"{}")
        manifest = TestRuntimeManifestValidation._build_manifest(app_dir)
        if override:
            manifest.update(override)
        (out / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return app_dir

    def _mod(self):
        return _load_script_module()

    def test_matches_ok(self, tmp_path):
        app_dir = self._make_runtime(tmp_path)
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert ok, detail
        assert "校验通过" in detail

    def test_missing_manifest_fails_closed(self, tmp_path):
        app_dir = tmp_path / "out" / "dist" / "BioDataAgent"
        (app_dir / "_internal").mkdir(parents=True)
        (app_dir / "BioDataAgent.exe").write_bytes(b"exe")
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok, detail
        assert "缺少运行时清单" in detail

    def test_format_mismatch_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path, {"format": "biodata-runtime-manifest/v9"})
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "format 不符" in detail, detail

    def test_app_mismatch_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path, {"app": "SomethingElse"})
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "app 不符" in detail, detail

    def test_onedir_root_mismatch_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path, {"onedir_root": "OtherDir"})
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "onedir_root 不符" in detail, detail

    def test_missing_key_field_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path)
        out = app_dir.parents[1]
        manifest = json.loads((out / "runtime-manifest.json").read_text(encoding="utf-8"))
        del manifest["total_bytes"]
        (out / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "缺关键字段" in detail, detail

    def test_file_count_inconsistent_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path, {"file_count": 999})
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "自相矛盾" in detail, detail

    def test_content_drift_fails(self, tmp_path):
        # 实物多写一个文件 → 清单 file_count/total_bytes 与实物对不上 → 拒收
        app_dir = self._make_runtime(tmp_path)
        (app_dir / "_internal" / "drift.py").write_bytes(b"drift")
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "不一致" in detail, detail

    def test_same_size_content_substitution_fails_hash_check(self, tmp_path):
        app_dir = self._make_runtime(tmp_path)
        target = app_dir / "BioDataAgent.exe"
        target.write_bytes(b"bad")  # 与原始 b"exe" 同长度，stat 汇总完全不变
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "SHA-256 不符" in detail, detail

    def test_noncanonical_manifest_path_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path)
        manifest_path = app_dir.parents[1] / "runtime-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "_internal/../BioDataAgent.exe"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "规范化" in detail, detail

    def test_case_insensitive_duplicate_manifest_path_fails(self, tmp_path):
        app_dir = self._make_runtime(tmp_path)
        manifest_path = app_dir.parents[1] / "runtime-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        duplicate = dict(manifest["files"][0])
        duplicate["path"] = duplicate["path"].upper()
        manifest["files"].append(duplicate)
        manifest["file_count"] += 1
        manifest["total_bytes"] += duplicate["size"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        ok, detail = self._mod().verify_runtime_manifest(app_dir)
        assert not ok and "路径重复" in detail, detail


class TestInstallerWorkflow:
    def test_ci_builds_and_uploads_unsigned_installer(self):
        workflow = _INSTALLER_WORKFLOW.read_text(encoding="utf-8")
        assert "innosetup-6.7.3.exe" in workflow
        assert "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" in workflow
        assert "scripts\\build_windows_installer.py" in workflow
        assert "biodata-agent-windows-installer-unsigned-dev" in workflow
        assert "TODO(W4)" not in workflow

    @pytest.mark.parametrize(
        "trigger",
        [
            '"src/**"',
            '"web/static/**"',
            '"database/base/**"',
            '"database/external/*.json"',
            '"src/dataset_recommender/app/mcp_server.py"',
            '"prompts/**"',
            '"scripts/build_windows_installer.py"',
        ],
    )
    def test_ci_trigger_covers_shipped_runtime_inputs(self, trigger):
        workflow = _INSTALLER_WORKFLOW.read_text(encoding="utf-8")
        assert trigger in workflow
