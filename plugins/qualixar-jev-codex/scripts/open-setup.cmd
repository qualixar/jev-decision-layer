@echo off
setlocal
set "JEV_PYTHON="
set "JEV_CANDIDATE="
for %%I in ("%~dp0..") do set "JEV_PLUGIN_ROOT=%%~fI"

if "%~1"=="--help" goto help
if "%~1"=="" goto workspace_missing
if not "%~2"=="" goto workspace_invalid

call :find_trusted_python
if not defined JEV_PYTHON goto python_missing

"%JEV_PYTHON%" -I -S -B -c "import runpy,sys; root=sys.argv[1]; sys.path.insert(0,root); sys.argv=['src.adl.api.setup_server','--workspace',sys.argv[2]]; runpy.run_module('src.adl.api.setup_server',run_name='__main__')" "%JEV_PLUGIN_ROOT%\runtime" "%~1"
exit /b %errorlevel%

:find_trusted_python
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :test_python "%%~fD\python.exe"
if defined JEV_PYTHON exit /b 0
for /d %%D in ("%ProgramFiles%\Python3*") do call :test_python "%%~fD\python.exe"
if defined JEV_PYTHON exit /b 0
for /d %%D in ("%ProgramFiles(x86)%\Python3*") do call :test_python "%%~fD\python.exe"
if defined JEV_PYTHON exit /b 0
for /d %%V in ("%RUNNER_TOOL_CACHE%\windows\Python\*") do for /d %%A in ("%%~fV\*") do call :test_python "%%~fA\python.exe"
if defined JEV_PYTHON exit /b 0
for /d %%V in ("%RUNNER_TOOL_CACHE%\Python\*") do for /d %%A in ("%%~fV\*") do call :test_python "%%~fA\python.exe"
exit /b 0

:test_python
if defined JEV_PYTHON exit /b 0
if not exist "%~1" exit /b 0
set "JEV_CANDIDATE=%~f1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "$s=Get-AuthenticodeSignature -LiteralPath $env:JEV_CANDIDATE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') { exit 1 }" >nul 2>nul
if errorlevel 1 exit /b 0
"%JEV_CANDIDATE%" -I -S -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "JEV_PYTHON=%JEV_CANDIDATE%"
exit /b 0

:help
echo Usage: open-setup.cmd ^<workspace-path^>
echo Opens the private local setup wizard for one workspace.
exit /b 0

:workspace_missing
echo WORKSPACE_PATH_REQUIRED 1>&2
exit /b 2

:workspace_invalid
echo WORKSPACE_PATH_REQUIRED 1>&2
exit /b 2

:python_missing
echo TRUSTED_PYTHON_3_11_REQUIRED 1>&2
exit /b 3

:python_version
echo TRUSTED_PYTHON_3_11_REQUIRED 1>&2
exit /b 3
