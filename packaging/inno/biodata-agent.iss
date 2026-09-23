; ═══════════════════════════════════════════════════════════════════════════
; BioData Agent 图形化安装器
; 编译器：Inno Setup 6.7.3（官方 jrsoftware.org → GitHub is-6_7_3 release 安装）
;
; 构建入口：scripts/build_windows_installer.py（ISCC /D 注入两个必填参数）
;   /DAppVersion=<WEB_API_VERSION>   版本单一真源 src/dataset_recommender/app/webapp.py
;   /DRuntimeDir=<onedir 绝对路径>   PyInstaller 产物（含 BioDataAgent.exe/_internal/）
; 两者缺失/不可用 → 编译期 #error 或构建脚本非零退出（fail-closed）。
;
; 硬性设计（钉在 tests/test_installer_contract.py）：
;   · AppId={{E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}}（固定，同 AppId 原位升级）
;   · AppUserModelID=BioDataAgent.Desktop（Inno 无内置指令，见 [Code] APP_USER_MODEL_ID
;     常量：安装后经 PropertyStore 写入开始菜单/桌面快捷方式）
;   · AppMutex=Local\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79
;     （与 desktop_launcher.MUTEX_NAME 逐字一致；运行中二次安装/升级被 mutex 拦）
;   · PrivilegesRequired=lowest —— 全流程无需管理员、不写 HKLM
;   · DefaultDirName={localappdata}\Programs\BioData Agent（每用户安装）
;   · CloseApplications=yes（Restart Manager 优雅关闭，禁用 force 强杀）
;   · 卸载默认只删 install_root（{app}）；本地数据 %LOCALAPPDATA%\BioDataAgent
;     绝不进 [Files] 删除清单；可选「同时删除本地数据」默认不勾，删除前精确路径校验
;     （GetFullPathName 真实规范化 + 拒绝含 .. 段/非绝对路径/\\?\ 前缀，
;     再与解析出的预期 data root 精确比较；junction/reparse point 拒绝跟随）
;   · 同 AppId 原位升级；旧版安装器遇到更高已装版本 → 终止并说明
;   · 升级前清理 {app} 旧 onedir 内容（[Code] CleanupStaleInstallRoot 在
;     ssInstall 执行——Inno [InstallDelete] 段不支持 Excludes，脚本递归删除时严格
;     跳过 unins000.*，仅升级 IsUpgrade 生效，data_root 不受影响）
;   · 原生支持 /SILENT /VERYSILENT /NORESTART /LOG（Inno 内置，无需额外处理）
;   · 基础安装不下载任何外部文件、不写 API Key；仅用户显式勾选 localmodel task 后，
;     由随包 hash-lock + uv 在线安装隔离运行组件与固定模型；失败不回滚基础程序
; ═══════════════════════════════════════════════════════════════════════════

#ifndef AppVersion
  #error 构建必须注入 /DAppVersion（版本单一真源 = webapp.py 的 WEB_API_VERSION）
#endif
#ifndef RuntimeDir
  #error 构建必须注入 /DRuntimeDir（PyInstaller onedir 产物目录绝对路径）
#endif

[Setup]
; ── 身份（逐字固定，升级/卸载键全靠它）──────────────────────────────
; Inno 常量转义：`{{` 展开为字面 `{`，故 AppId 值为 {E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}。
AppId={{E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}
AppName=BioData Agent
AppVersion={#AppVersion}
AppVerName=BioData Agent {#AppVersion}
AppPublisher=BioData Agent
; AppUserModelID=BioDataAgent.Desktop —— Inno 无内置指令，由 [Code] 常量 APP_USER_MODEL_ID
; 逐字钉住；快捷方式属性写入属后续批。
AppMutex=Local\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79

; ── 每用户安装，无管理员 ─────────────────────────────────────────────
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultDirName={localappdata}\Programs\BioData Agent
DefaultGroupName=BioData Agent

; ── 页面流：欢迎 → 目录 → 开始菜单 → 可选桌面快捷方式 → 摘要 → 进度 → 完成页「立即运行」
; 注意：Inno 脚本注释必须独占一行（分号不能跟在指令值后面）。
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
CloseApplications=yes
RestartApplications=no

; ── 外观 / 卸载信息 / 压缩 ───────────────────────────────────────────
SetupIconFile=..\assets\BioDataAgent.ico
UninstallDisplayIcon={app}\assets\BioDataAgent.ico
UninstallDisplayName=BioData Agent {#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName=BioData Agent
VersionInfoDescription=BioData Agent Setup
Compression=lzma2/normal
SolidCompression=yes
OutputDir=..\..\..\build-out
OutputBaseFilename=BioData-Agent-Setup-{#AppVersion}-win-x64-unsigned-dev
; 估算大小：Inno Setup 6 未显式指定 EstimatedSize 时自动按 [Files] 计算并写入卸载注册信息。

[Languages]
Name: "english"; MessagesFile: "languages\English.isl"
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
; 可选桌面快捷方式（默认不勾，用户显式选择）
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; 可选在线本地模型（默认不勾）：约 2.2GB 权重 + 约 1GB CPU 运行组件（实测，共约 3GB 下载）；基础安装仍零网络。
Name: "localmodel"; Description: "{cm:ModelTask}"; GroupDescription: "{cm:ModelGroup}"; Flags: unchecked

[Files]
; 冻结运行时 onedir（BioDataAgent.exe / BioDataAgentMCP.exe / _internal/**），
; 递归全量落 {app}。本地数据根 %LOCALAPPDATA%\BioDataAgent 不属于安装内容，
; 不在本清单、卸载时也默认保留（见 [Code] 删除边界校验）。
Source: "{#RuntimeDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 品牌图标（快捷方式/卸载显示用，exe 内嵌图标属后续批 spec 交接）
Source: "..\assets\BioDataAgent.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
; 桌面壳批 ds1：快捷方式显式传 --window（安装版默认原生窗口）；
; 裸 exe 不带参数仍走浏览器+托盘，保留为恢复/诊断通道。
Name: "{group}\BioData Agent"; Filename: "{app}\BioDataAgent.exe"; IconFilename: "{app}\assets\BioDataAgent.ico"; Parameters: "--window"
Name: "{group}\卸载 BioData Agent"; Filename: "{uninstallexe}"; IconFilename: "{app}\assets\BioDataAgent.ico"
Name: "{autodesktop}\BioData Agent"; Filename: "{app}\BioDataAgent.exe"; IconFilename: "{app}\assets\BioDataAgent.ico"; Parameters: "--window"; Tasks: desktopicon

[Run]
; localmodel 的执行在 [Code] RunLocalModelInstallIfSelected（ssPostInstall 阶段 Exec、
; ewWaitUntilTerminated、退出码只进日志）：Inno 6 的 [Run] 段没有 ignoreerrors flag，
; 直接挂 [Run] 会在模型安装失败时弹错误框（基础安装本无回滚，但会惊吓用户）。
; 完成页「立即运行」复选框（postinstall）；/SILENT /VERYSILENT 下 skipifsilent 跳过
Filename: "{app}\BioDataAgent.exe"; Description: "{cm:LaunchProgram,BioData Agent}"; Parameters: "--window"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; 注：Inno 的 InstallDelete 段不支持 Excludes 参数，且 {app}\* 会连 unins000.* 一起删。
; 升级前清理因此由脚本 CleanupStaleInstallRoot 实现（ssInstall 阶段执行，
; 严格排除 unins000.*；data_root 位于 install_root 之外不受影响；IsUpgrade 才执行）。

[CustomMessages]
english.ModelGroup=Optional local semantic model
english.ModelTask=Download the local precision reranker now (about 3 GB download / about 5 GB installed; optional)
english.ModelStatus=Downloading and verifying the optional local model. This may take a while; the base app remains usable if it fails...
chinesesimplified.ModelGroup=可选本地语义模型
chinesesimplified.ModelTask=现在下载本地精准重排模型（约下载 3 GB / 安装后约 5 GB；可跳过）
chinesesimplified.ModelStatus=正在下载并校验可选本地模型，可能需要较长时间；失败不影响基础程序…
; ── 卸载器：可选「同时删除我的本地数据」确认与校验提示（默认 No = 保留）──
english.DataAskDelete=Do you also want to delete BioData Agent's local data?%n%nPath: %1%n%nDeleting is IRREVERSIBLE. Click "Yes" to delete it (a normal uninstall keeps it), or "No" to keep it.
english.DeleteRejectedPath=Deletion refused: the target path is not exactly the current user's %LOCALAPPDATA%\BioDataAgent.
english.DeleteRejectedReparse=Deletion refused: the target is a junction/reparse point; following it could delete unrelated data.
english.DeleteRejectedBoundary=Deletion refused: the target equals the install directory or one of its ancestors, or contains the install directory.
english.DeleteFailed=Failed to delete local data: %1 (it may be in use).
english.NewerInstalled=An older installer cannot replace a newer installation.%n%nInstalled version: %1%nThis installer version: %2%n%nSetup will now exit.
english.DirMismatchInstalled=BioData Agent is already installed to a different directory.%n%nExisting installation: %1%nThis installer targets: %2%n%nUninstall the existing installation first, or run without /DIR to keep the current directory.

chinesesimplified.DataAskDelete=是否同时删除 BioData Agent 的本地数据？%n%n路径：%1%n%n删除操作不可恢复。点击「是」将删除（默认卸载会保留），点击「否」保留。
chinesesimplified.DeleteRejectedPath=已拒绝删除：目标路径并非当前用户的 %LOCALAPPDATA%\BioDataAgent。
chinesesimplified.DeleteRejectedReparse=已拒绝删除：目标是 junction/重解析点，跟随它会误删无关数据。
chinesesimplified.DeleteRejectedBoundary=已拒绝删除：目标等于安装目录或其上级，或包含安装目录。
chinesesimplified.DeleteFailed=删除本地数据失败：%1（数据可能正被占用）。
chinesesimplified.NewerInstalled=旧版安装器不能覆盖已安装的更高版本。%n%n已安装版本：%1%n本安装器版本：%2%n%n安装程序即将退出。
chinesesimplified.DirMismatchInstalled=检测到 BioData Agent 已安装在其他目录。%n%n现有安装：%1%n本次安装目标：%2%n%n请先卸载现有安装，或不指定 /DIR 以沿用当前安装目录。

[Code]
const
  UNINSTALL_REGISTRY_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{E249D2BA-8457-4B8A-B2C9-B7CFA234BA79}_is1';
  INVALID_FILE_ATTRIBUTES        = $FFFFFFFF;
  { 安装期把生效 data_root 持久化到卸载注册信息的自定义值名（E2E 数据根隔离钩子）}
  DATA_ROOT_REGISTRY_VALUE = 'BioDataDataRoot';

var
  { 卸载期 data_root 缓存。卸载注册键在 usPostUninstall 前可能已被 Inno 清理
    （matrix10 实测：该阶段 RegQueryStringValue 读不回 BioDataDataRoot），故在
    InitializeUninstall（键仍在）时按 ExpectedDataRoot 解析一次缓存，删除与确认
    一律用缓存值，保证与安装期持久化/隔离钩子一致。 }
  U_DataRoot: string;
  { 边缘修复第 2 项：静默卸载（/SILENT //VERYSILENT）+ /DELETEDATA 且数据目录删不掉
    （被占用/拒绝）时，不弹 MsgBox（无人值守会卡死）、只写日志；置位后由
    CurUninstallStepChanged 在 usPostUninstall 末尾 ExitProcess(1) 返回非零退出码，
    避免「删不掉却 exit 0 误报卸载成功」。 }
  U_DeleteFailed: Boolean;

{ ── AppUserModelID（固定参数，Inno 无内置指令）──────────────────────
  Inno 6.7.3 的 [Setup] 无 AppUserModelID 指令，Pascal Script 亦不支持 COM
  vtable 接口（无法经 IPropertyStore 写入快捷方式属性）——本批以常量逐字钉住
  固定参数 `BioDataAgent.Desktop`，快捷方式属性写入与 exe 内嵌图标一并交由
  后续批。 }
const
  APP_USER_MODEL_ID = 'BioDataAgent.Desktop';

function GetFileAttributesW(lpFileName: WideString): DWORD;
  external 'GetFileAttributesW@kernel32.dll stdcall';

{ 删除边界规范化的底层 API——GetFullPathNameW 做真实路径规范化
  （消 . 段/大小写/尾斜杠差异，解析 8.3 短名与卷路径；不跟随 junction/symlink）。}
function GetFullPathNameW(lpFileName: string; nBufferLength: DWORD; lpBuffer: string; var lpFilePart: DWORD): DWORD;
  external 'GetFullPathNameW@kernel32.dll stdcall';

{ 边缘修复第 2 项：Inno 无「设置卸载退出码」的 Pascal API；静默删数据失败时直接
  ExitProcess(非零) 覆盖卸载器默认的 exit 0。仅在 usPostUninstall 末尾（卸载已
  完成、仅剩收尾）调用，不中断必要的卸载步骤。 }
procedure ExitProcess(uExitCode: DWORD);
  external 'ExitProcess@kernel32.dll stdcall';

{ ── 版本守卫：旧版安装器遇更高版本终止 ─────────────────────────────── }
function TryReadInstalledVersion(var V: string): Boolean;
begin
  Result := RegQueryStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, 'DisplayVersion', V);
  if not Result then
    Result := RegQueryStringValue(HKEY_LOCAL_MACHINE, UNINSTALL_REGISTRY_KEY, 'DisplayVersion', V);
end;

{ 版本串比较（X.Y.Z 逐段数值比较；Inno 无内置 CompareVersions）。
  返回 -1/0/1：A < B / A = B / A > B。}
function ParseVersionPart(const S: string; var Idx: Integer): Integer;
var
  num: string;
begin
  num := '';
  while (Idx <= Length(S)) and (S[Idx] <> '.') do begin
    num := num + S[Idx];
    Inc(Idx);
  end;
  if (Idx <= Length(S)) and (S[Idx] = '.') then Inc(Idx);
  if num = '' then
    Result := 0
  else
    Result := StrToIntDef(num, 0);
end;

function CompareVersionStrings(const A, B: string): Integer;
var
  ai, bi, va, vb: Integer;
begin
  ai := 1;
  bi := 1;
  while (ai <= Length(A)) or (bi <= Length(B)) do begin
    va := ParseVersionPart(A, ai);
    vb := ParseVersionPart(B, bi);
    if va < vb then begin Result := -1; Exit; end;
    if va > vb then begin Result := 1; Exit; end;
  end;
  Result := 0;
end;

{ 边缘修复第 3 项：读已安装版本的 InstallLocation（同版本/旧版本升级时用于换目录拦截）。
  优先 HKCU（PrivilegesRequired=lowest 的每用户安装），回退 HKLM。}
function ReadInstalledLocation(var Loc: string): Boolean;
begin
  Result := RegQueryStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, 'InstallLocation', Loc);
  if not Result then
    Result := RegQueryStringValue(HKEY_LOCAL_MACHINE, UNINSTALL_REGISTRY_KEY, 'InstallLocation', Loc);
end;

{ 边缘修复第 3 项：命令行为是否显式给了 /DIR（token 边界、大小写不敏感，忽略取值）。
  只关心「是否覆盖」，具体值比较用安装目录常量展开值（Inno 已按 /DIR 解析）。
  注意：Pascal 大括号注释体内不能再写花括号常量，内层右括号会提前闭合注释。}
function CommandLineHasDirOverride: Boolean;
var
  Cmd, Token: string;
  I: Integer;
begin
  Result := False;
  Cmd := GetCmdTail() + ' ';
  Token := '';
  for I := 1 to Length(Cmd) do begin
    if (Cmd[I] = ' ') or (Cmd[I] = #9) then begin
      if (Uppercase(Token) = '/DIR') or (Copy(Uppercase(Token), 1, 5) = '/DIR=') then begin
        Result := True;
        Exit;
      end;
      Token := '';
    end else
      Token := Token + Cmd[I];
  end;
end;

{ 边缘修复第 3 项：安装目录比较（大小写不敏感 + 忽略尾斜杠差异，避免 /DIR 带尾斜杠
  与 Inno 存值不一致导致误判）。}
function SameInstallDir(const A, B: string): Boolean;
var
  LA, LB: string;
begin
  LA := A;
  LB := B;
  while (Length(LA) > 0) and ((LA[Length(LA)] = '\') or (LA[Length(LA)] = '/')) do
    SetLength(LA, Length(LA) - 1);
  while (Length(LB) > 0) and ((LB[Length(LB)] = '\') or (LB[Length(LB)] = '/')) do
    SetLength(LB, Length(LB) - 1);
  Result := Uppercase(LA) = Uppercase(LB);
end;

{ 边缘修复第 3 项：InitializeSetup 阶段 app 常量尚未初始化（Inno 在返回后才解析），
  此时展开 app 常量会抛 "app constant before initialized" 致命错误并弹模态框挂死。
  目标安装目录改读内建 /DIR 参数值——param:DIR 常量实证在 InitializeSetup 可用
  （未显式给 /DIR 时返回空串；给 /DIR=值 时返回该值）；为空回退到与 Setup 段
  DefaultDirName 同值的字面常量。}
function TargetInstallDirForCheck: string;
begin
  Result := ExpandConstant('{param:DIR}');
  if Result = '' then
    Result := ExpandConstant('{localappdata}\Programs\BioData Agent');
end;

{ 安装器侧静默防护：/SILENT /VERYSILENT 下不弹 Pascal MsgBox（/SUPPRESSMSGBOXES
  抑制不了它，无人值守会永远卡在没人点的弹窗上），只写日志；交互模式保留弹窗。
  WizardSilent 在 InitializeSetup 阶段即可用（实证 /SILENT 下返回 True）。}
procedure ReportSetupBlocked(const Msg: string; const Kind: TMsgBoxType);
begin
  if WizardSilent then
    Log('InitializeSetup 中止（静默）：' + Msg)
  else
    MsgBox(Msg, Kind, MB_OK);
end;

function InitializeSetup(): Boolean;
var
  Installed, InstalledLoc, TargetDir: string;
begin
  Result := True;
  if TryReadInstalledVersion(Installed) then begin
    if CompareVersionStrings(Installed, '{#AppVersion}') > 0 then begin
      ReportSetupBlocked(FmtMessage(CustomMessage('NewerInstalled'), [Installed, '{#AppVersion}']), mbError);
      Result := False;   { 已装版本更新：终止安装 }
    end;
    { 边缘修复第 3 项：同 AppId 换 /DIR 重装会覆盖卸载键、孤儿化旧安装目录。已安装 +
      显式 /DIR 与 InstallLocation 不同 → 明确提示并终止（要求先卸载旧安装或沿用原目录）。
      InitializeSetup 不能用 app 常量，目标目录经 TargetInstallDirForCheck 读 param:DIR
      回退默认值；静默下 ReportSetupBlocked 只写日志不弹框（防挂死）。}
    if Result and CommandLineHasDirOverride and ReadInstalledLocation(InstalledLoc)
       and (InstalledLoc <> '') then begin
      TargetDir := TargetInstallDirForCheck;
      if not SameInstallDir(InstalledLoc, TargetDir) then begin
        ReportSetupBlocked(FmtMessage(CustomMessage('DirMismatchInstalled'), [InstalledLoc, TargetDir]), mbError);
        Result := False;
      end;
    end;
  end;
end;

{ ── 删除边界：可选「同时删除我的本地数据」的精确路径校验 ──────────────
  NormalizePath 加固：不再是纯字符串变换——GetFullPathName 真实规范化 +
  拒绝含 `..` 段/非绝对盘符路径/`\\?\` 前缀的输入（fail-closed 返回空串）。
  删除前必须与解析出的预期 data root 精确比较。 }
function PathHasDotDot(const P: string): Boolean;
var
  I: Integer;
  Seg: string;
begin
  { 按 \ 或 / 分隔符分词，任一 token 恰好是 `..` 即拒绝（`...` 这类文件名不是 `..`）}
  Result := False;
  Seg := '';
  for I := 1 to Length(P) + 1 do begin
    if (I > Length(P)) or (P[I] = '\') or (P[I] = '/') then begin
      if Seg = '..' then begin
        Result := True;
        Exit;
      end;
      Seg := '';
    end else
      Seg := Seg + P[I];
  end;
end;

function NormalizePath(const P: string): string;
var
  S: string;
  Buf: string;
  Len, Part: DWORD;
begin
  Result := '';
  S := Trim(P);
  if S = '' then Exit;
  { 拒绝 \\?\ 前缀（NT 命名空间绕过）与 UNC（预期根是盘符绝对）。
    Pascal 字符串无反斜杠转义：'\\?' 字面即三字符 \\?，'\\' 字面即二字符 \\。 }
  if (Copy(S, 1, 3) = '\\?') or (Copy(S, 1, 2) = '\\') then Exit;
  { 拒绝非绝对盘符路径（X:\ 或 X:/ 开头；短盘符根同样放行）}
  if Length(S) < 3 then Exit;
  if not (((S[1] >= 'A') and (S[1] <= 'Z')) or ((S[1] >= 'a') and (S[1] <= 'z'))) then Exit;
  if (S[2] <> ':') or ((S[3] <> '\') and (S[3] <> '/')) then Exit;
  { 拒绝含 .. 段（规范化之前先拒绝——.. 是路径穿越绕过的直接手段）}
  if PathHasDotDot(S) then Exit;
  { GetFullPathNameW 真实规范化（消 . 段/大小写/尾斜杠差异；失败 → 空串拒绝）}
  SetLength(Buf, 1024);
  Len := GetFullPathNameW(S, Length(Buf), Buf, Part);
  if (Len = 0) or (Len >= Length(Buf)) then Exit;
  SetLength(Buf, Len);
  Result := Uppercase(Buf);
end;

function IsUpgrade: Boolean;
begin
  { [InstallDelete] 只在升级（已装旧版）时执行——全新安装不清用户指到的目录 }
  Result := RegKeyExists(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY)
         or RegKeyExists(HKEY_LOCAL_MACHINE, UNINSTALL_REGISTRY_KEY);
end;

function ExpectedDataRoot(): string;
var
  Redirected, EnvRoot: string;
begin
  { 数据根隔离钩子（E2E 联调前置）：
    解析优先级——① 进程 env BIODATA_DATA_ROOT（安装/卸载器子进程继承调用方 env，
    矩阵据此隔离）；② 安装期持久化的注册表 BioDataDataRoot（真实用户控制面板卸载：
    安装时若用了自定义根，卸载键仍在时可读回）；③ 默认 %LOCALAPPDATA%\BioDataAgent。
    注意 usPostUninstall 阶段 Inno 可能已清理卸载注册表键——故卸载流程在
    InitializeUninstall（键仍在）时缓存到全局 U_DataRoot。 }
  EnvRoot := GetEnv('BIODATA_DATA_ROOT');
  if EnvRoot <> '' then
    Result := EnvRoot
  else if RegQueryStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, DATA_ROOT_REGISTRY_VALUE, Redirected)
          and (Redirected <> '') then
    Result := Redirected
  else
    Result := AddBackslash(GetEnv('LOCALAPPDATA')) + 'BioDataAgent';
end;

{ 安装期把生效 data_root 写入卸载注册信息（优先 BIODATA_DATA_ROOT env，
  否则默认路径；与 ExpectedDataRoot 的缺省逐字一致）。 }
procedure PersistDataRootForUninstall;
var
  EnvRoot: string;
begin
  EnvRoot := GetEnv('BIODATA_DATA_ROOT');
  if EnvRoot = '' then
    EnvRoot := AddBackslash(GetEnv('LOCALAPPDATA')) + 'BioDataAgent';
  RegWriteStringValue(HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, DATA_ROOT_REGISTRY_VALUE, EnvRoot);
end;

{ ── 升级前清理 install_root 的 onedir 旧内容（排除 unins*）──────────────
  PyInstaller onedir 升级常见旧文件残留：Files 段的 ignoreversion 只覆盖同名文件，
  删不掉旧版本已移除的文件/目录。Inno 的 InstallDelete 段不支持 Excludes 且会连
  unins000.* 一起删（卸载器文件，删除后安装中断会留下无卸载器的坏安装）——这里用
  脚本在 ssInstall 阶段递归删除安装目录内容，严格跳过 unins000.*。
  data_root（%LOCALAPPDATA%\BioDataAgent）位于安装目录之外，不受影响。
  仅升级（IsUpgrade）执行：全新安装不清用户指到的目录。删除失败的部分不致命
  （新文件会覆盖同名项），记日志继续。 }
function DeleteTreeWithExcludes(const Root, Excludes: string): Boolean;
var
  FindRec: TFindRec;
  Full, LowerName: string;
begin
  Result := True;
  if not FindFirst(Root + '*', FindRec) then
    Exit;
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
        LowerName := Lowercase(FindRec.Name);
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
          { junction / symlink 等目录重解析点绝不递归进入，避免升级清理越出安装根。
            保留链接本身即可；[Files] 后续只覆盖本版本明确拥有的路径。 }
          if (FindRec.Attributes and FILE_ATTRIBUTE_REPARSE_POINT) <> 0 then
            Log('升级前清理跳过目录重解析点：' + Root + FindRec.Name)
          else begin
            Full := AddBackslash(Root + FindRec.Name);
            if not DeleteTreeWithExcludes(Full, Excludes) then
              Result := False;
            RemoveDir(Full);   { 删空目录；失败（非空/被锁）不致命，[Files] 随后覆盖 }
          end;
        end else if Pos('|' + LowerName + '|', '|' + Lowercase(Excludes) + '|') = 0 then begin
          if not DeleteFile(Root + FindRec.Name) then
            Result := False;
        end;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure CleanupStaleInstallRoot;
begin
  if not IsUpgrade then
    Exit;
  if not DeleteTreeWithExcludes(AddBackslash(ExpandConstant('{app}')),
        'unins000.exe|unins000.dat|unins000.msg') then
    Log('升级前清理 install_root 部分失败（继续安装，新文件将覆盖同名项）');
end;

{ 用户显式勾选 localmodel task 后才联网安装可选本地模型。
  UX 决策（收口改 Exec 实现）：保留「安装完成才退出」的
  阻塞下载，而不是改成安装后应用内继续。理由：① 执行前把状态写进完成页状态标签
  （下载并校验…可能较长时间、失败不影响基础程序）；② CLI 每个阶段都写 status.json
  （running/python/dependencies/model/ready/error），下次启动设置页轮询展示精确状态，
  天然可重试；③ Setup 的 Cancel 会终止该子进程，跨进程单飞锁含 stale PID 恢复，中断后
  可重试；④ 改后台会引入「Setup 退出后应用才启动 → 需跨进程交接 pending 状态 +
  防双实例」的复杂度，且应用内在线安装入口（设置页 /api/local-model/install + 轮询 +
  取消）已作为带实时进度的替代路径存在。
  为什么用 Exec 而不是 [Run] 段：Inno 6 的 [Run] 段没有 ignoreerrors flag（ISCC 6.7.3
  报 unknown flag），直接挂 [Run] 会在模型安装失败/被取消时向用户弹错误框。Exec
  把非零退出码只写进安装日志——基础安装本就无回滚，失败/取消在设置页可见可重试。}
procedure RunLocalModelInstallIfSelected;
var
  ResultCode: Integer;
begin
  if not WizardIsTaskSelected('localmodel') then
    Exit;
  WizardForm.StatusLabel.Caption := CustomMessage('ModelStatus');
  if not Exec(ExpandConstant('{app}\BioDataAgent.exe'), '--install-local-model',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('localmodel 安装未能启动（基础程序不受影响，可在设置页重试）：'
        + SysErrorMessage(ResultCode))
  else if ResultCode <> 0 then
    Log(Format('localmodel 安装退出码 %d（基础程序不受影响，可在设置页重试）', [ResultCode]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    CleanupStaleInstallRoot;   { 升级前清理 onedir 旧内容（保留 unins*）}
  if CurStep = ssPostInstall then begin
    RunLocalModelInstallIfSelected;   { 用户显式勾选 localmodel 才联网；见该过程注释 }
    PersistDataRootForUninstall;
  end;
end;

{ Child 等于 Ancestor，或 Ancestor 是 Child 的祖先目录（两侧都先规范化）}
function PathIsAncestorOrSame(const Child, Ancestor: string): Boolean;
var
  C, A: string;
begin
  C := NormalizePath(Child);
  A := NormalizePath(Ancestor);
  Result := (C <> '') and (A <> '')
        and ((C = A) or (Pos(A + '\', C) = 1));
end;

{ 边缘修复第 2 项：静默卸载判定。按 token 边界解析 /SILENT 与 /VERYSILENT（大小写
  不敏感），与 CommandLineWantsDeleteData 同口径——不用 WizardSilent 单点（它只覆盖
  /VERYSILENT），也不做子串匹配（避免 --VERYSILENT 等相邻字符误触发）。}
function IsUninstallerSilent: Boolean;
var
  Cmd, Token: string;
  I: Integer;
begin
  Result := False;
  Cmd := GetCmdTail() + ' ';
  Token := '';
  for I := 1 to Length(Cmd) do begin
    if (Cmd[I] = ' ') or (Cmd[I] = #9) or (Cmd[I] = #13) or (Cmd[I] = #10) then begin
      if (Uppercase(Token) = '/SILENT') or (Uppercase(Token) = '/VERYSILENT') then begin
        Result := True;
        Exit;
      end;
      Token := '';
    end else
      Token := Token + Cmd[I];
  end;
end;

{ 删除失败提示：静默卸载只写日志并置 U_DeleteFailed（不弹 MsgBox——/SUPPRESSMSGBOXES
  抑制不了 Pascal MsgBox，无人值守会永远卡在一个没人点的弹窗上）；交互模式保留弹窗。}
procedure ReportDeleteFailure(const Msg: string; const Kind: TMsgBoxType);
begin
  if IsUninstallerSilent then begin
    Log('TryDeleteLocalData 静默失败：' + Msg);
    U_DeleteFailed := True;
  end else
    MsgBox(Msg, Kind, MB_OK);
end;

procedure TryDeleteLocalData;
var
  Target, Expected, AppDir: string;
  Attr: DWORD;
begin
  { Target 用 InitializeUninstall 缓存的 U_DataRoot：usPostUninstall 阶段 Inno 已
    清理卸载注册键，此时再调 ExpectedDataRoot() 会解析漂移（回退默认路径）≠ 缓存值
    → 误触「拒绝删除」分支（matrix10 实测）。缓存值是卸载早期按 env 隔离钩子/注册表/
    默认路径解析的正确目标，删除与校验一律以它为锚。 }
  Target := U_DataRoot;
  AppDir := ExpandConstant('{app}');

  { ① 缓存值必须非空（InitializeUninstall 已按 env 隔离钩子/注册表/默认路径解析；
    为空表示解析失败，拒绝删除以防误删）}
  if Target = '' then begin
    ReportDeleteFailure(CustomMessage('DeleteRejectedPath'), mbCriticalError);
    Exit;
  end;

  { ② 删除目标先经 GetFullPathName 规范化 + 非法形态拒绝闸（含 `..` 段/
    非绝对盘符路径/`\\?\` 前缀 → NormalizePath 返回空串），再与解析出的预期
    data root 精确比较（缺省 %LOCALAPPDATA%\BioDataAgent；env 隔离钩子/注册表
    重定向时等于重定向根）。任一不满足 → 拒绝删除。 }
  Expected := NormalizePath(ExpectedDataRoot());
  Target := NormalizePath(Target);
  if (Expected = '') or (Target = '') or (Target <> Expected) then begin
    ReportDeleteFailure(FmtMessage(CustomMessage('DeleteRejectedPath'), [U_DataRoot]), mbCriticalError);
    Exit;
  end;

  { ③ 拒绝 junction / reparse point 跟随（不删链接指向的真实目录；属性检查用原始
    路径——规范化不跟随链接，二者等价）}
  Attr := GetFileAttributesW(U_DataRoot);
  if Attr = INVALID_FILE_ATTRIBUTES then Exit;   { 目标不存在：无可删除 }
  if (Attr and FILE_ATTRIBUTE_REPARSE_POINT) <> 0 then begin
    ReportDeleteFailure(FmtMessage(CustomMessage('DeleteRejectedReparse'), [U_DataRoot]), mbCriticalError);
    Exit;
  end;

  { ④ 拒绝等于安装目录或其上级，也拒绝安装目录位于目标内部的反向情形 }
  if PathIsAncestorOrSame(Target, AppDir) or PathIsAncestorOrSame(AppDir, Target) then begin
    ReportDeleteFailure(FmtMessage(CustomMessage('DeleteRejectedBoundary'), [U_DataRoot, AppDir]), mbCriticalError);
    Exit;
  end;

  if not DelTree(Target, True, True, True) then begin
    ReportDeleteFailure(FmtMessage(CustomMessage('DeleteFailed'), [Target]), mbError);
    Exit;
  end;
end;

{ ── 卸载删数据开关（非交互验证）────
  卸载器支持命令行参数 /DELETEDATA：卸载完成后无条件执行「同时删除本地数据」（精确
  路径校验仍生效），供 E2E 矩阵 matrix10 非交互断言；不带该参数则维持原 GUI 询问、默认
  不勾（静默卸载下 MsgBox 被抑制 → 默认 No → 数据保留）。
  按 token 边界解析（空格/制表符分隔、大小写不敏感），不再用子串匹配——
  避免 `/DELETEDATA_EXTRA`、`--DELETEDATA` 等相邻字符误触发。 }
function CommandLineWantsDeleteData: Boolean;
var
  Cmd, Token: string;
  I: Integer;
begin
  Result := False;
  Cmd := GetCmdTail() + ' ';   { 尾部补空格让最后一个 token 也结算 }
  Token := '';
  for I := 1 to Length(Cmd) do begin
    if (Cmd[I] = ' ') or (Cmd[I] = #9) or (Cmd[I] = #13) or (Cmd[I] = #10) then begin
      if Uppercase(Token) = '/DELETEDATA' then begin
        Result := True;
        Exit;
      end;
      Token := '';
    end else
      Token := Token + Cmd[I];
  end;
end;

{ 卸载入口：先缓存 data_root（此时卸载注册键仍在，可读回 BioDataDataRoot/隔离钩子）。
  真实用户控制面板卸载用默认路径；E2E 矩阵带 BIODATA_DATA_ROOT env → 隔离根。 }
function InitializeUninstall(): Boolean;
begin
  U_DataRoot := ExpectedDataRoot();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { 可选「同时删除我的本地数据」：卸载完成后询问，默认 No（默认保留）。
    静默卸载（/SILENT /VERYSILENT）下 MsgBox 被抑制 → 返回默认值 No → 数据保留。
    卸载器带 /DELETEDATA 时跳过询问直接删除（E2E matrix10 非交互验证路径）。}
  if CurUninstallStep = usPostUninstall then begin
    if CommandLineWantsDeleteData then
      TryDeleteLocalData
    else if MsgBox(FmtMessage(CustomMessage('DataAskDelete'), [U_DataRoot]),
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      TryDeleteLocalData;
    { 边缘修复第 2 项：静默 + /DELETEDATA 且删数据失败（被占用/拒绝）→ 已只写日志
      置 U_DeleteFailed；此处覆盖默认 exit 0 返回非零，避免无人值守误报「卸载成功」。}
    if U_DeleteFailed and IsUninstallerSilent then
      ExitProcess(1);
  end;
end;
