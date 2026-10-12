@echo off
setlocal
set "RC003_KIT=%ProgramFiles(x86)%\Windows Kits\10"
set "RC003_SIGNTOOL=%RC003_KIT%\bin\10.0.26100.0\x64\signtool.exe"
if not exist "%~dp0.build\driver\RemoRc003Filter.sys" (
  echo ERROR: build-driver.cmd must run first.
  exit /b 1
)
if not exist "%~dp0.build\driver\RemoRc003Filter.cat" (
  echo ERROR: catalog was not generated.
  exit /b 1
)
"%RC003_SIGNTOOL%" verify /kp /v /c "%~dp0.build\driver\RemoRc003Filter.cat" "%~dp0.build\driver\RemoRc003Filter.sys"
if errorlevel 1 (
  echo ERROR: Catalog signature, trust or SYS membership verification failed. See SignTool output above.
  exit /b 2
)
"%RC003_SIGNTOOL%" verify /kp /v /c "%~dp0.build\driver\RemoRc003Filter.cat" "%~dp0.build\driver\RemoRc003Filter.inf"
if errorlevel 1 exit /b 2
echo PASS: Catalog signature and SYS/INF membership passed kernel-policy verification.
exit /b 0
