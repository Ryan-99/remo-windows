@echo off
setlocal
set "RC003_VSDEVCMD=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"
if not exist "%RC003_VSDEVCMD%" set "RC003_VSDEVCMD=%ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat"
if not exist "%RC003_VSDEVCMD%" (
  echo ERROR: Visual Studio Build Tools VsDevCmd.bat not found.
  exit /b 1
)
call "%RC003_VSDEVCMD%" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 1
if not exist "%~dp0.build\host" mkdir "%~dp0.build\host"
pushd "%~dp0.build\host"
cl.exe /nologo /W4 /WX /TC /std:c17 /D RC003_HOST_TEST=1 /Od /RTC1 /Zi "%~dp0remap.c" "%~dp0tests\remap_host_test.c" /Fe:remap_host_test.exe
if errorlevel 1 goto failed
remap_host_test.exe
if errorlevel 1 goto failed
popd
exit /b 0
:failed
popd
exit /b 1
