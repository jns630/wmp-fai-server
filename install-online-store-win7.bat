@echo off
REM ===========================================================================
REM  WMP FAI Server - Online Store registration for Windows 7 / 8 / 8.1
REM ===========================================================================
REM
REM  Double-click this file. It does three things, in order:
REM
REM    1. adds the hosts entries WMP needs to reach this server
REM    2. installs the Online Store into the registry
REM    3. prints what to do next
REM
REM  WHY A SEPARATE FILE AND NOT JUST RUNNING THE EXE
REM  ------------------------------------------------
REM  Two of the three steps need Administrator (the HKLM half of the store
REM  registration, and the hosts file), and the EXE is double-clicked rather
REM  than run from a prompt. So the elevation, the hosts editing and the
REM  reporting all have to live somewhere. This file is that somewhere, and it
REM  calls the EXE's own 'install-online-store' command for the actual
REM  registration - the EXE remains the single source of truth for exactly what
REM  it writes, so there is no second copy of that logic to drift.
REM
REM  USAGE
REM    install-online-store-win7.bat            install (asks for admin)
REM    install-online-store-win7.bat /D         preview only, changes nothing
REM    install-online-store-win7.bat uninstall  remove it again
REM    install-online-store-win7.bat uninstall /D
REM
REM  /D is worth using first. It prints every change without making any, which
REM  is the sane thing to do before a script edits a system file you did not
REM  write.
REM
REM  This file must stay next to WMP-FAI-Server-Win7Test.exe. It finds the EXE
REM  beside itself and refuses to guess anywhere else.
REM ===========================================================================

REM Deliberately EnableDelayedExpansion: the `for /f` below expands !PS_MATCH! at
REM run time, after it has been set, which a plain %PS_MATCH% cannot do.
setlocal EnableExtensions EnableDelayedExpansion
title WMP FAI Server - Online Store registration (Windows 7)

set "SCRIPT_DIR=%~dp0"
set "EXE=%SCRIPT_DIR%WMP-FAI-Server-Win7Test.exe"
set "HOSTS=%SystemRoot%\System32\drivers\etc\hosts"
REM WMPFAITEST_HOSTS / WMPFAITEST_NOPAUSE / WMPFAITEST_SKIP_ADMIN exist so the
REM add and remove paths can be exercised against a throwaway hosts file
REM instead of the real one. They are set by this project's own test harness
REM and are ignored when unset, so the real behaviour is unchanged.
if defined WMPFAITEST_HOSTS set "HOSTS=%WMPFAITEST_HOSTS%"

set "ACTION=install"
set "DRYRUN=0"
:parse_args
if "%~1"=="" goto after_args
if /i "%~1"=="/D"          ( set "DRYRUN=1" & shift & goto parse_args )
if /i "%~1"=="/DRYRUN"    ( set "DRYRUN=1" & shift & goto parse_args )
if /i "%~1"=="uninstall"  ( set "ACTION=uninstall" & shift & goto parse_args )
if /i "%~1"=="install"    ( set "ACTION=install" & shift & goto parse_args )
if /i "%~1"=="/?"         goto usage
if /i "%~1"=="-h"         goto usage
if /i "%~1"=="/help"      goto usage
echo Unrecognised option: %~1
echo.
goto usage

:after_args

echo ============================================================
echo   WMP FAI Server - Online Store (%ACTION%)
echo ============================================================
echo.

if not exist "%EXE%" (
  echo [X] Cannot find the server executable.
  echo     Looked for: "%EXE%"
  echo     This file has to sit in the same folder as
  echo     WMP-FAI-Server-Win7Test.exe.
  echo.
  call :pause_if_interactive
  exit /b 1
)

if "%DRYRUN%"=="1" (
  echo MODE: DRY RUN - nothing below will be changed.
  echo.
) else (
  call :ensure_admin
  if errorlevel 1 exit /b 1
)

echo --- hosts entries -------------------------------------------------
echo WMP resolves these names in DNS, which no longer exists, so they have to
echo point at this machine. Missing artwork on Windows 7 is almost always a
echo missing 'images.metaservices.microsoft.com' line here.
echo.
REM The hostname is passed through a VARIABLE, not as %1. Inside a `for %%H in
REM (...)` loop a CALL re-enters the same %1 slot and `%%~1` then expands to
REM nothing at all - it printed literally as "%~1" on the first run of this
REM script, and every host was reported as "would add" for a host called "%~1".
REM The record of what THIS script added is written as the entries are added, so
REM uninstall can remove exactly those and nothing else. See :hosts_remove for
REM why that matters: the hosts file is shared state this project may not have
REM created. Without it, uninstalling a store you did not install silently
REM deletes a pre-existing entry belonging to something else.
set "RECORD=%SCRIPT_DIR%online-store-hosts-installed.txt"
if "%DRYRUN%"=="0" if not exist "%RECORD%" type nul > "%RECORD%"
if /i "%ACTION%"=="uninstall" (
  call :hosts_remove_dispatch
) else (
  for %%H in (musicmatch-ssl.xboxlive.com images.metaservices.microsoft.com redir.metaservices.microsoft.com toc.music.metaservices.microsoft.com info.music.metaservices.microsoft.com) do (
    set "HOSTNAME=%%H"
    call :hosts_entry
  )
)
echo.

REM The EXE is invoked through a subroutine rather than inline in an `if (...)`
REM block. A path containing a bracket - "New folder (12)", "(x86)" - ends the
REM block early, and cmd then reports "<path>\WMP-FAI-Server-Win7Test.exe was
REM unexpected at this time", which looks like a broken EXE and is not one.
REM Delayed expansion cannot rescue it either: the value is expanded when the
REM block is PARSED, and a quoted name is still scanned for brackets.
set "DRY_FLAG=--dry-run"
if "%DRYRUN%"=="0" set "DRY_FLAG="

echo --- registry ------------------------------------------------------
if /i "%ACTION%"=="uninstall" (call :run_exe uninstall-online-store) else (call :run_exe install-online-store)
if errorlevel 1 (
  echo.
  echo [X] The server command failed. Nothing further was attempted.
  call :pause_if_interactive
  exit /b 1
)


echo.
if /i "%ACTION%"=="uninstall" (
  if "%DRYRUN%"=="1" (
    echo ==== WOULD UNINSTALL - nothing was changed ====
  ) else (
    echo ============================================================
    echo   Uninstalled.
    echo ============================================================
    echo Unrelated Windows Media Player settings were not touched.
    echo To put it back, run this file again.
  )
) else (
  if "%DRYRUN%"=="1" (
    echo ==== WOULD INSTALL - nothing was changed ====
    echo Re-run without /D to actually do it.
  ) else (
    echo ============================================================
    echo   Installed.
    echo ============================================================
    echo NEXT:
    echo   1. Start the server ^(double-click WMP-FAI-Server-Win7Test.exe^).
    echo   2. Open http://127.0.0.1/online-store/ in a browser. It must show
    echo      the store front page. If it does not, fix that before step 3.
    echo   3. Start Windows Media Player. The Online Stores tab may only
    echo      appear after a full restart of the player.
    echo.
    echo DISCOGS RESULTS MISSING FROM THE ALBUM LIST?
    echo   Discogs needs a personal token and is OFF without one. It fails
    echo   silently - no error, the provider is just absent from the list.
    echo   Fix: copy local_settings.py ^(containing DISCOGS_TOKEN = '...'^) so it
    echo   sits BESIDE the .exe, then restart the server.
    echo   Check with:  WMP-FAI-Server-Win7Test.exe online-store-status
    echo   It prints "discogs : configured" or "discogs : NO TOKEN".
    echo.
    echo IF THERE IS STILL NO PICTURE ON AN ALBUM:
    echo   - check fai_server.log for a line beginning [IMAGE]
    echo     'served' means the image was delivered; 'rejecting unusable url'
    echo     means the cover URL arrived in a shape the server did not accept.
    echo   - run this file with /D to see exactly what the hosts file holds.
  )
)
echo.
call :pause_if_interactive
exit /b 0

REM ---------------------------------------------------------------------------
:hosts_entry
REM Ensure (or, when uninstalling, drop) one hosts line for %HOSTNAME%.
REM
REM The match is anchored on the hostname at the end of a non-comment line, so a
REM commented-out example line does not count as present, and a similarly named
REM host does not produce a false match. The regex is in findstr's own dialect:
REM no \d, \s or alternation, and [ ] for a literal space.
REM Dispatch on the ACTION happens in the MAIN flow, before this subroutine is
REM called at all. An earlier version had `goto hosts_remove` in here and that
REM label contained the five-host loop, so one call to :hosts_entry walked the
REM whole list again, five times over: 25 lines of output, and it deleted a
REM pre-existing entry the script had never added.
if "%ACTION%"=="uninstall" goto :eof

REM Detection is done in PowerShell, not findstr. Three findstr regexes were
REM tried against a real hosts file and every one was wrong in a way that
REM would have shipped:
REM   "[ ][ ]*host[ ][ ]*$"      never matches - with /C: the whole string is
REM                              ONE literal pattern, and $ anchors to the end
REM                              of that pattern, not the end of the line.
REM   "^[0-9][0-9.]*[ ]*host[ ]*$"  matches space-separated lines but NOT
REM                              tab-separated ones - and the lines this script
REM                              writes are tab-separated. A check that reports
REM                              half the hosts file as "absent" is worse than no
REM                              check at all: it duplicates live entries, and
REM                              the last one wins, silently.
REM PowerShell's -match is a real regex, accepts both, and is already needed by
REM the editing half of this subroutine.
set "PRESENT="
set "PS_MATCH=$h='%HOSTS%'; $n=[regex]::Escape('%HOSTNAME%'); if(Get-Content $h -ErrorAction SilentlyContinue | Where-Object { $_ -match ('^[0-9.]+[\s\t]+'+$n+'[\s\t]*$') }){ 'yes' } else { 'no' }"
for /f %%R in ('powershell -NoProfile -Command "!PS_MATCH!" 2^>nul') do set "PRESENT=%%R"
if not defined PRESENT set "PRESENT=no"
if "%PRESENT%"=="yes" (
  echo   [present] %HOSTNAME%
  exit /b 0
)
if "%DRYRUN%"=="1" (
  echo   [WOULD ADD] 127.0.0.1  %HOSTNAME%
  exit /b 0
)
REM Redirection first, so no trailing space lands in the file. The hosts file
REM often lacks a final newline, and appending without one would fuse the new
REM entry onto the last existing line and break that entry too.
powershell -NoProfile -Command "$h='%HOSTS%'; $c=Get-Content -Raw $h -ErrorAction SilentlyContinue; if($c -and -not $c.EndsWith([char]10)){ Add-Content $h '' -NoNewline }" >nul 2>&1
>>"%HOSTS%" echo 127.0.0.1	%HOSTNAME%
REM Recorded only on a real run, never a dry one, so the list always describes
REM what is actually in the file.
if "%DRYRUN%"=="0" >>"%RECORD%" echo %HOSTNAME%
echo   [added] %HOSTNAME%
exit /b 0

:hosts_remove_dispatch
REM Called ONCE from the main flow, not once per host. The hosts file is
REM SHARED state that this script may not have created, so uninstall removes only
REM what this script recorded adding, and falls back to all five - saying so -
REM when no record exists. A partial removal would leave the store
REM half-registered, which looks like a broken uninstall; a silent full removal
REM would delete somebody else's entry.
set "RECORD=%SCRIPT_DIR%online-store-hosts-installed.txt"
if exist "%RECORD%" (
  for /f "usebackq tokens=1" %%L in ("%RECORD%") do (
    set "HOSTNAME=%%L"
    call :hosts_remove_one
  )
  del "%RECORD%" >nul 2>&1
  echo   -- removed only the entries this script had added --
) else (
  echo   no record of what was added - removing all five.
  for %%H in (musicmatch-ssl.xboxlive.com images.metaservices.microsoft.com redir.metaservices.microsoft.com toc.music.metaservices.microsoft.com info.music.metaservices.microsoft.com) do (
    set "HOSTNAME=%%H"
    call :hosts_remove_one
  )
)
exit /b 0

REM ---------------------------------------------------------------------------
:hosts_remove_one
set "PRESENT="
set "PS_MATCH=$h='%HOSTS%'; $n=[regex]::Escape('%HOSTNAME%'); if(Get-Content $h -ErrorAction SilentlyContinue | Where-Object { $_ -match ('^[0-9.]+[\s\t]+'+$n+'[\s\t]*$') }){ 'yes' } else { 'no' }"
for /f %%R in ('powershell -NoProfile -Command "!PS_MATCH!" 2^>nul') do set "PRESENT=%%R"
if not defined PRESENT set "PRESENT=no"
if "%PRESENT%"=="no" (
  echo   [absent] %HOSTNAME%
  exit /b 0
)
if "%DRYRUN%"=="1" (
  echo   [WOULD REMOVE] %HOSTNAME%
  exit /b 0
)
REM Rewritten in place, keeping every other line. Set-Content is used rather
REM than a temp file because the hosts directory is not writable except by an
REM administrator, which is why this script elevates before reaching here.
REM The removal filter must be the exact inverse of the detection regex
REM (^[0-9.]+[\s\t]+host[\s\t]*$) or the two disagree, and an entry that install
REM reported as "present" survives an uninstall. The earlier version of this
REM line used [ ] (spaces only) and had exactly that bug for tab-separated
REM lines, which is the format this script itself writes.
powershell -NoProfile -Command "$h='%HOSTS%'; $n=[regex]::Escape('%HOSTNAME%'); (Get-Content $h) | Where-Object { -not ($_ -match ('^[0-9.]+[\s\t]+'+$n+'[\s\t]*$')) } | Set-Content $h -Encoding ASCII" >nul 2>&1
echo   [removed] %HOSTNAME%
exit /b 0

REM ---------------------------------------------------------------------------
:ensure_admin
if defined WMPFAITEST_SKIP_ADMIN exit /b 0
REM Admin test. `net session` is the usual one-liner and it is WRONG: it
REM returned 0 on this machine while the token was explicitly
REM non-administrator, which would have let the script carry on to the hosts
REM file and fail there with a far less helpful message than "you need to be an
REM administrator". `fltmc` is the other common trick and is not dependable
REM either. This asks Windows for the token's group membership, which is the
REM actual question being asked.
fltmc >nul 2>&1
if not errorlevel 1 exit /b 0
powershell -NoProfile -Command "if((New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 exit /b 0
echo This needs Administrator. A prompt will appear - accept it.
echo.
REM Re-launching this same script elevated, with the original arguments, so a
REM dry run stays a dry run after the elevation.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%*'" >nul 2>&1
if errorlevel 1 (
  echo [X] Could not request Administrator rights.
  echo     Right-click this file and choose "Run as administrator".
  exit /b 1
)
REM The elevated copy now has its own window; this one has nothing left to do.
exit /b 0

:run_exe
REM Invoke the server's own subcommand. The name arrives as %1 and is a bare
REM token from this file, never user input.
"%EXE%" %~1 %DRY_FLAG%
exit /b %errorlevel%

REM Every interactive pause goes through this one subroutine, so an automated
REM run does not sit waiting for a keypress nobody is there to press.
:pause_if_interactive
if defined WMPFAITEST_NOPAUSE exit /b 0
pause
exit /b 0

:usage
echo Usage:
echo   install-online-store-win7.bat              install the store
echo   install-online-store-win7.bat /D           preview, change nothing
echo   install-online-store-win7.bat uninstall    remove the store
echo   install-online-store-win7.bat uninstall /D preview the removal
echo.
call :pause_if_interactive
exit /b 2
