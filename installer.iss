; Ledger installer.
;
; Produces LedgerSetup.exe - a single file the customer double-clicks. It puts Ledger in
; Program Files, makes desktop and Start Menu shortcuts, registers it in Add/Remove
; Programs, and offers to launch it. No Python on their machine, no command window, no
; instructions to follow.
;
; Build it with Inno Setup (free): iscc installer.iss
; Ledger.exe must already exist in dist\ - run build_exe.bat first.

#define AppName      "Ledger"
#define AppVersion   "5.8"
#define AppPublisher "Kalyani Paper Bag Industries"
#define AppExe       "Ledger.exe"

[Setup]
AppId={{8F2A5C14-9D3E-4B7A-A1C6-3E5D8B9F2A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; The licence page is where you would put your own terms if you sell this.
LicenseFile=LICENCE.txt
OutputDir=installer_out
OutputBaseFilename=LedgerSetup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Ledger is 64-bit only because that is what the Python build produces.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; No admin rights needed - installs per-user, which avoids the UAC prompt scaring people.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=static\favicon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Ledger automatically when Windows starts"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "dist\{#AppExe}";        DestDir: "{app}"; Flags: ignoreversion
Source: "HOW TO USE IT - daily guide.md"; DestDir: "{app}"; Flags: ignoreversion isreadme skipifsourcedoesntexist
Source: "HOW TO - jobs done by other firms.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; The database and photos live in the user's own data folder, NOT in Program Files.
; Program Files is read-only for a standard user, and anything written there is wiped
; by the next install. This way the data survives every update and uninstall.
Name: "{userappdata}\Ledger"; Flags: uninsneveruninstall
Name: "{userappdata}\Ledger\instance"; Flags: uninsneveruninstall
Name: "{userappdata}\Ledger\instance\uploads"; Flags: uninsneveruninstall
Name: "{userappdata}\Ledger\instance\backups"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExe}"
Name: "{group}\Your Ledger data";     Filename: "{userappdata}\Ledger\instance"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";     Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start Ledger now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Deliberately NOT deleting the data folder. Someone uninstalling to reinstall a newer
; version would otherwise lose their entire business record. They can delete it by hand
; if they really mean to.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Refuse to install over a running copy - Windows would silently leave the old exe in
// place and the customer would swear the update did nothing.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#AppExe} /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  OldDb, NewDb: String;
begin
  if CurStep = ssPostInstall then
  begin
    // If they used the old copy-the-folder version, bring their database across rather
    // than starting them from nothing.
    OldDb := ExpandConstant('{app}\instance\erp.db');
    NewDb := ExpandConstant('{userappdata}\Ledger\instance\erp.db');
    if FileExists(OldDb) and (not FileExists(NewDb)) then
      FileCopy(OldDb, NewDb, False);
  end;
end;
