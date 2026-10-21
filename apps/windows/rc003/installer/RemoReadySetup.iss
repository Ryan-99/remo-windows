; Ready-to-use installer. One elevated setup installs the app, the
; WinUHid virtual keyboard, and VB-CABLE, then starts the voice daemon.
; The unsigned development keyboard filter is not included.

#define AppName "Remo"
#define AppPublisher "Remo contributors"
#define AppVersion "0.1.4"
#define AppExeName "RemoteMicRC003.exe"
#ifndef DistDir
  #define DistDir "..\dist\RemoteMicRC003"
#endif
#define AppIcon "..\..\..\..\Resources\Remo.ico"

[Setup]
AppId={{B6E8B6F0-7B9B-4B7C-9E7E-3B7B2C6B0F5C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Remo
DefaultGroupName=Remo
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#AppIcon}
DisableDirPage=no
UsePreviousAppDir=yes
SetupLogging=yes
OutputBaseFilename=RemoSetup-{#AppVersion}
OutputDir=..\dist\installer
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no
DisableFinishedPage=yes

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "readme-ready.txt"; DestDir: "{app}"; DestName: "README.txt"; Flags: isreadme ignoreversion
Source: "..\..\..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_NOTICES.md"; Flags: ignoreversion
Source: "..\..\..\..\LICENSE.md"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\..\..\COPYRIGHT.md"; DestDir: "{app}"; DestName: "COPYRIGHT.txt"; Flags: ignoreversion
Source: "stop-app.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop-app.ps1"; DestDir: "{tmp}"; Flags: dontcopy
Source: "finish-setup.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--settings"
Name: "{group}\停止 {#AppName}"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\stop-app.ps1"" -AppPath ""{app}"""; WorkingDir: "{app}"; Flags: runminimized
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--settings"

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\finish-setup.ps1"" -AppPath ""{app}"""; StatusMsg: "正在安装虚拟键盘和虚拟声卡..."; Flags: runhidden waituntilterminated
Filename: "{app}\remo-daemon.exe"; Parameters: "--backend ble --execute"; WorkingDir: "{app}"; Flags: nowait runhidden
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "打开 Remo 设置"; Flags: postinstall nowait

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\stop-app.ps1"" -AppPath ""{app}"""; Flags: runhidden

[Code]
const
  TOKEN_QUERY = $0008;
  TokenElevationClass = 20;

function OpenProcessToken(ProcessHandle: THandle; DesiredAccess: DWORD; var TokenHandle: THandle): BOOL;
  external 'OpenProcessToken@advapi32.dll stdcall';
function GetTokenInformation(TokenHandle: THandle; TokenInformationClass: DWORD; var TokenInformation: DWORD; TokenInformationLength: DWORD; var ReturnLength: DWORD): BOOL;
  external 'GetTokenInformation@advapi32.dll stdcall';
function GetCurrentProcess: THandle;
  external 'GetCurrentProcess@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function IsProcessElevated: Boolean;
var
  Token: THandle;
  Elevation: DWORD;
  Returned: DWORD;
begin
  Result := False;
  Token := 0;
  if not OpenProcessToken(GetCurrentProcess, TOKEN_QUERY, Token) then
    Exit;
  try
    if GetTokenInformation(Token, TokenElevationClass, Elevation, SizeOf(Elevation), Returned) then
      Result := Elevation <> 0;
  finally
    CloseHandle(Token);
  end;
end;

function RelaunchParams: String;
var
  I: Integer;
begin
  Result := '/ELEVATED=1';
  for I := 1 to ParamCount do
    Result := Result + ' ' + AddQuotes(ParamStr(I));
end;

function InitializeSetup: Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  Log('elevate srcexe=' + ExpandConstant('{srcexe}'));
  if IsProcessElevated then
    Exit;
  if ExpandConstant('{param:ELEVATED|}') = '1' then
  begin
    MsgBox('没有获得管理员权限，无法安装虚拟键盘和虚拟声卡。', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if not ShellExec('runas', ExpandConstant('{srcexe}'), RelaunchParams, '', SW_SHOWNORMAL, ewNoWait, ErrorCode) then
    MsgBox('安装虚拟键盘和虚拟声卡需要管理员权限。请在系统提示中允许。', mbInformation, MB_OK);
  Result := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  ExtractTemporaryFile('stop-app.ps1');
  Exec('powershell.exe',
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\stop-app.ps1') + '" -AppPath "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
