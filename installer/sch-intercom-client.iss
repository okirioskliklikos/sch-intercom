; School Intercom — Classroom Client: εγκαταστάτης ανά χρήστη (Φάση 5A, docs/phase5.md §4).
; Φτιάχνεται από το tools\build_release.py (ISCC /DAppVersion=... /DSourceDir=...).
;
; Παράμετροι (και για αθόρυβη εγκατάσταση /VERYSILENT /SUPPRESSMSGBOXES):
;   /ROOM=<αίθουσα> /PC=<όνομα> [/MANAGERHOST=<IP>] [/MANAGERCERT=<αρχείο.pem>]
;   /DEFENDER (εξαίρεση Defender, ζητά διαχειριστή) /NOENROLL /NOSTART
;   /DIR=, /DATADIR=, /RUNNAME= μόνο για τις αυτόματες δοκιμές.
; Χωρίς /MANAGERCERT χρησιμοποιείται το manager-ca.pem δίπλα στο setup.exe.
; Απεγκατάσταση: ρωτά για «Πλήρη αφαίρεση» (προεπιλογή Όχι)· αθόρυβα μόνο με /PURGE.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error "Δώστε /DSourceDir=<dist\release\έκδοση> (tools\build_release.py)"
#endif

[Setup]
AppId={{6B0E7C55-2F3D-4C41-9E0B-5C1D2A7E9F10}
AppName=School Intercom Client
AppVersion={#AppVersion}
AppVerName=School Intercom Client {#AppVersion}
AppPublisher=School Intercom
DefaultDirName={localappdata}\sch-intercom
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=sch-intercom-client-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
SetupLogging=yes
UninstallDisplayName=School Intercom Client

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\Greek.isl")
Name: "el"; MessagesFile: "compiler:Languages\Greek.isl"
#else
Name: "el"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "defender"; Description: "Εξαίρεση του φακέλου στο Windows Defender (ζητά κωδικό διαχειριστή, μία φορά)"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{code:RunName}"; ValueData: """{app}\versions\{#AppVersion}\python\pythonw.exe"" -m client.supervisor --home ""{app}"""; Flags: uninsdeletevalue

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  InfoPage: TInputQueryWizardPage;
  NewIdentity: Boolean;

function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/' + Name) = 0 then
      Result := True;
end;

function Clean(const Value: String): String;
begin
  Result := Trim(Value);
  StringChangeEx(Result, '"', '', True);
end;

function DataDir: String;
begin
  Result := ExpandConstant('{param:DATADIR|}');
  if Result = '' then
    Result := ExpandConstant('{%USERPROFILE}') + '\.school-intercom';
end;

function RunName(Param: String): String;
begin
  Result := ExpandConstant('{param:RUNNAME|SchoolIntercomClient}');
end;

function ManagerCert: String;
begin
  Result := ExpandConstant('{param:MANAGERCERT|}');
  if Result = '' then
    Result := ExpandConstant('{src}\manager-ca.pem');
end;

function Python(Windowed: Boolean): String;
begin
  Result := ExpandConstant('{app}\versions\{#AppVersion}\python\');
  if Windowed then
    Result := Result + 'pythonw.exe'
  else
    Result := Result + 'python.exe';
end;

function HomeArgs: String;
begin
  Result := '-m client.ctl --home "' + ExpandConstant('{app}') + '" --data-dir "' + DataDir + '"';
end;

function Room: String;
begin
  Result := Clean(InfoPage.Values[0]);
end;

function ComputerName: String;
begin
  Result := Clean(InfoPage.Values[1]);
end;

function ManagerHost: String;
begin
  Result := Clean(InfoPage.Values[2]);
end;

procedure InitializeWizard;
begin
  NewIdentity := not FileExists(DataDir + '\client.json');
  InfoPage := CreateInputQueryPage(wpWelcome, 'Στοιχεία υπολογιστή',
    'Πού βρίσκεται αυτός ο υπολογιστής;',
    'Ο Manager μπορεί να τα αλλάξει αργότερα. Αφήστε κενή τη διεύθυνση Manager για αυτόματο εντοπισμό στο δίκτυο.');
  InfoPage.Add('Αίθουσα (π.χ. C2627-Γ1):', False);
  InfoPage.Add('Όνομα υπολογιστή (π.χ. Board1):', False);
  InfoPage.Add('Διεύθυνση Manager (προαιρετικά):', False);
  InfoPage.Values[0] := ExpandConstant('{param:ROOM|}');
  InfoPage.Values[1] := ExpandConstant('{param:PC|}');
  InfoPage.Values[2] := ExpandConstant('{param:MANAGERHOST|}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Επανεγκατάσταση: η ταυτότητα και τα στοιχεία μένουν· καμία ερώτηση. }
  Result := (PageID = InfoPage.ID) and not NewIdentity;
end;

function NeedsDetails: Boolean;
begin
  Result := NewIdentity and not HasSwitch('NOENROLL');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = InfoPage.ID) and NeedsDetails and ((Room = '') or (ComputerName = '')) then
  begin
    MsgBox('Συμπληρώστε αίθουσα και όνομα υπολογιστή.', mbError, MB_OK);
    Result := False;
  end;
end;

function RunCli(const Args: String; Show: Integer; var Code: Integer): Boolean;
var
  Cli: String;
begin
  Cli := ExpandConstant('{app}\sch-intercom-client.cmd');
  Result := FileExists(Cli) and
    Exec(ExpandConstant('{cmd}'), '/C ""' + Cli + '" ' + Args + '"', '', Show, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if not FileExists(ManagerCert) and not FileExists(DataDir + '\manager-ca.pem') then
  begin
    Result := 'Λείπει το πιστοποιητικό του Manager. Βάλτε το manager-ca.pem δίπλα στο setup.exe ή δώστε /MANAGERCERT=<αρχείο>.';
    exit;
  end;
  if NeedsDetails and ((Room = '') or (ComputerName = '')) then
  begin
    Result := 'Συμπληρώστε αίθουσα και όνομα υπολογιστή (/ROOM= και /PC=).';
    exit;
  end;
  { Παλιά εγκατάσταση σε λειτουργία: διακοπή πριν από την αντιγραφή αρχείων. }
  RunCli('--stop', SW_HIDE, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Args: String;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  Args := HomeArgs + ' --install-finish {#AppVersion} --run-name "' + RunName('') + '"';
  if FileExists(ManagerCert) then
    Args := Args + ' --manager-cert "' + ManagerCert + '"';
  if NewIdentity or (ExpandConstant('{param:MANAGERHOST|}') <> '') then
    Args := Args + ' --director-host "' + ManagerHost + '"';
  if not Exec(Python(False), Args, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    SuppressibleMsgBox('Η ρύθμιση του Client απέτυχε (κωδικός ' + IntToStr(Code) + '). Δείτε το log της εγκατάστασης.',
      mbError, MB_OK, IDOK);

  if WizardIsTaskSelected('defender') or HasSwitch('DEFENDER') then
  begin
    if not ShellExec('runas', 'powershell.exe',
        '-NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath ''' + ExpandConstant('{app}') + '''"',
        '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      SuppressibleMsgBox('Η εξαίρεση στο Windows Defender δεν ορίστηκε (χρειάζεται διαχειριστής). Η εγκατάσταση συνεχίζει.',
        mbInformation, MB_OK, IDOK)
    else
      Log('Defender: εξαίρεση για ' + ExpandConstant('{app}'));
  end;

  if NeedsDetails and WizardSilent then
    { Αθόρυβη εγκατάσταση: χωρίς κονσόλα που περιμένει Enter (θα κολλούσε)· ο κωδικός φαίνεται
      στο παράθυρο του Client (tray) και στο enrollment-pending.json. }
    Exec(Python(False), HomeArgs + ' --enroll --room "' + Room + '" --computer-name "' + ComputerName + '"',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)
  else if NeedsDetails then
    Exec(Python(False), HomeArgs + ' --enroll --room "' + Room + '" --computer-name "' + ComputerName + '" --pause',
      ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, Code)
  else if not NewIdentity and (Room <> '') and (ComputerName <> '') then
    { 5C2: επανεγκατάσταση με /ROOM= και /PC=· αίτημα αλλαγής στον Director (χωρίς κωδικό). }
    Exec(Python(False), HomeArgs + ' --request-change --room "' + Room + '" --computer-name "' + ComputerName + '"',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);

  if not HasSwitch('NOSTART') then
    Exec(Python(True), '-m client.supervisor --home "' + ExpandConstant('{app}') + '"',
      ExpandConstant('{app}'), SW_HIDE, ewNoWait, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Purge: Boolean;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  Purge := HasSwitch('PURGE');
  if not Purge and not UninstallSilent then
    Purge := MsgBox('Πλήρης αφαίρεση;' + #13#10#13#10 +
      'Ναι: διαγράφονται και η ταυτότητα και τα κλειδιά αυτού του υπολογιστή. Μετά από νέα εγκατάσταση θα χρειαστεί νέα έγκριση στον Manager.' + #13#10 +
      'Όχι (συνιστάται): η ταυτότητα μένει για μελλοντική επανεγκατάσταση.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if Purge then
    RunCli('--purge --yes', SW_HIDE, Code)
  else
    RunCli('--stop', SW_HIDE, Code);
end;
