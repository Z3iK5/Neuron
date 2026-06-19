; SPDX-License-Identifier: Apache-2.0
; Inno Setup script for the Neuron Desktop app (D3, Windows).
;
; Compiles the PyInstaller one-folder bundle (dist\Neuron) into a per-user
; installer (PrivilegesRequired=lowest -> no admin / UAC prompt), with Start-menu
; and optional desktop shortcuts and an uninstaller. Build from the `neuron\`
; directory (so SourceDir=.. resolves to it):
;     ISCC.exe packaging\neuron.iss
; Output: Neuron-Setup-x64.exe. Code signing is a follow-up (see docs/desktop.md).

#define MyAppName "Neuron"
#define MyAppVersion "0.0.8"
#define MyAppPublisher "Neuron"
#define MyAppExeName "Neuron.exe"

[Setup]
; A stable AppId so future versions upgrade in place rather than installing twice.
AppId={{B6F3A1C8-2E47-4D9A-9F1B-7C5E2A8D4B30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SourceDir=..
OutputDir=.
OutputBaseFilename=Neuron-Setup-x64
SetupIconFile=packaging\icons\neuron.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "dist\Neuron\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
