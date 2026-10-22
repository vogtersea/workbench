; 工作志桌面版安装包（Inno Setup 6）
; 编译：ISCC.exe installer/workbench.iss   -> dist/Workbench-Setup.exe
; 设计取舍：按用户安装（PrivilegesRequired=lowest）到 %LOCALAPPDATA%\Programs，
;           不写系统目录、不需要管理员权限；数据目录由首次启动时用户自己选。

#define MyAppName "工作志"
#define MyAppVersion "1.2.1"
#define MyAppPublisher "vogtersea"
#define MyAppExeName "Workbench.exe"

[Setup]
AppId={{9F3B2C71-2B4E-4E2E-9E0B-6A1D6C0F5A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Workbench
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Workbench-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
; 简体中文语言包随仓库携带（Inno Setup 官方 issrc 的 Files/Languages/ChineseSimplified.isl）
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\dist\Workbench\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动{#MyAppName}"; Flags: nowait postinstall skipifsilent
