; Inno Setup 6 安裝腳本。由 build.ps1 呼叫；需先以 PyInstaller 產生 ..\dist\AirSender\。

#define MyAppName "AirSender"
#define MyAppVersion "0.3.1"
#define MyAppPublisher "AirSender"
#define MyAppExeName "AirSender.exe"

[Setup]
AppId={{7E1D3C2A-5B6F-4F0E-9C44-2A1B7D8E9F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 需要系統管理員以加入 Windows 防火牆規則（AirPlay 2 需要 HomePod 反向連回電腦）。
; 使用者仍可選擇只為目前使用者安裝，此時防火牆規則可之後在程式選單中加入。
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=..\assets\airsender.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
#ifexist AddBackslash(CompilerPath) + "Languages\ChineseTraditional.isl"
Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "登入 Windows 時自動啟動 {#MyAppName}"; GroupDescription: "其他："

[Files]
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} 命令列"; Filename: "{app}\AirSenderCLI.exe"; Parameters: "--list"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 與程式內「登入 Windows 時自動啟動」使用同一個登錄值
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
; 防火牆入站允許規則（需系統管理員；非管理員安裝時略過，可之後在程式選單中加入）
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode; StatusMsg: "設定 Windows 防火牆..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#MyAppName}"" dir=in action=allow program=""{app}\{#MyAppExeName}"" enable=yes profile=any"; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode; StatusMsg: "設定 Windows 防火牆..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#MyAppName}"" dir=in action=allow program=""{app}\AirSenderCLI.exe"" enable=yes profile=any"; Flags: runhidden waituntilterminated; Check: IsAdminInstallMode; StatusMsg: "設定 Windows 防火牆..."
Filename: "{app}\{#MyAppExeName}"; Description: "啟動 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F & taskkill /IM AirSenderCLI.exe /F"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden; RunOnceId: "FwDelete"; Check: IsAdminInstallMode

[UninstallDelete]
; PyInstaller 執行期可能在 _internal 產生快取；連同安裝目錄整個清掉
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
// 解除安裝結束時詢問是否一併刪除使用者資料（設定檔與記錄）。
// 靜默解除安裝（/SILENT、/VERYSILENT）不詢問也不刪除，除非加上 /REMOVEUSERDATA=1。
function ShouldRemoveUserData(): Boolean;
begin
  if UninstallSilent then
    Result := ExpandConstant('{param:REMOVEUSERDATA|0}') = '1'
  else
    Result := MsgBox(
      '是否一併刪除 AirSender 的設定檔與記錄檔？' + #13#10 + #13#10 +
      ExpandConstant('{userappdata}\{#MyAppName}') + #13#10 +
      ExpandConstant('{localappdata}\{#MyAppName}') + #13#10 + #13#10 +
      '選「否」會保留設定，之後重新安裝可沿用。',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if ShouldRemoveUserData() then
    begin
      DelTree(ExpandConstant('{userappdata}\{#MyAppName}'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\{#MyAppName}'), True, True, True);
    end;
  end;
end;
