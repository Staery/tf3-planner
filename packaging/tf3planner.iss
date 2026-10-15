; Inno Setup script for the TF3 Planner desktop app (optional; the portable zip works without it).
;
;   1. packaging\build.ps1            -> dist\TF3Planner\
;   2. "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\tf3planner.iss
;                                     -> dist\TF3Planner-Setup-<version>.exe
;
; Per-user install (no admin rights): %LOCALAPPDATA%\Programs\TF3Planner. Plans, logs and settings
; live in %LOCALAPPDATA%\TF3Planner\data and are kept on uninstall (delete the folder by hand).

#define AppName "TF3 Planner"
#define AppVersion "0.9.0"
#define AppExe "TF3Planner.exe"

[Setup]
AppId={{6B0E4F7C-3C1B-4C38-9D0A-7A3F0E1B3493}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Staery
DefaultDirName={localappdata}\Programs\TF3Planner
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TF3Planner-Setup-{#AppVersion}
SetupIconFile=..\tf3planner\control\tf3planner.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "installmod"; Description: "Install the in-game mod into Transport Fever 3 (local mods folder) / Установить мод в игру"; GroupDescription: "Transport Fever 3:"; Flags: unchecked

[Files]
Source: "..\dist\TF3Planner\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\tf3planner-cli.exe"; Parameters: "-m tf3planner.control.deploy"; Flags: runhidden waituntilterminated; Tasks: installmod; StatusMsg: "Installing the in-game mod..."
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; stop a running server (it listens on 127.0.0.1:8010) before files are removed
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""try {{ Invoke-WebRequest -UseBasicParsing -Method Post http://127.0.0.1:8010/api/control/shutdown | Out-Null }} catch {{}}"""; Flags: runhidden; RunOnceId: "StopServer"
