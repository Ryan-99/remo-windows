@echo off
setlocal
rem Explicit CLI build: no VS toolset registration changes, signing or installation.
set "RC003_VS=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"
if not exist "%RC003_VS%" set "RC003_VS=%ProgramFiles(x86)%\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat"
if not exist "%RC003_VS%" exit /b 1
call "%RC003_VS%" -arch=x64 -host_arch=x64 -vcvars_ver=14.44 >nul
if errorlevel 1 exit /b 1
set "RC003_KIT=%ProgramFiles(x86)%\Windows Kits\10"
set "RC003_VER=10.0.26100.0"
set "RC003_WDF=1.35"
if not exist "%~dp0.build\driver" mkdir "%~dp0.build\driver"
pushd "%~dp0.build\driver"
"%RC003_KIT%\Tools\%RC003_VER%\x64\InfVerif.exe" /w "%~dp0RemoRc003Filter.inf"
if errorlevel 1 goto failed
cl.exe /nologo /c /W4 /WX /external:W0 /kernel /GS /Zl /O2 /Zi /TC /D_AMD64_ /DAMD64 /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /DNTDDI_VERSION=0x0A00000C /external:I"%RC003_KIT%\Include\%RC003_VER%\km" /external:I"%RC003_KIT%\Include\%RC003_VER%\shared" /external:I"%RC003_KIT%\Include\wdf\kmdf\%RC003_WDF%" "%~dp0driver.c" "%~dp0remap.c"
if errorlevel 1 goto failed
link.exe /nologo /WX /DRIVER /KERNEL /NODEFAULTLIB /SUBSYSTEM:NATIVE,10.00 /ENTRY:FxDriverEntry /MACHINE:X64 /INCREMENTAL:NO /OPT:REF /OPT:ICF /DYNAMICBASE /NXCOMPAT /DEBUG /OUT:RemoRc003Filter.sys /LIBPATH:"%RC003_KIT%\Lib\%RC003_VER%\km\x64" /LIBPATH:"%RC003_KIT%\Lib\wdf\kmdf\x64\%RC003_WDF%" driver.obj remap.obj ntoskrnl.lib hal.lib wmilib.lib WdfLdr.lib WdfDriverEntry.lib BufferOverflowFastFailK.lib
if errorlevel 1 goto failed
copy /y "%~dp0RemoRc003Filter.inf" . >nul
if errorlevel 1 goto failed
"%RC003_KIT%\bin\%RC003_VER%\x86\Inf2Cat.exe" /driver:. /os:10_GE_X64 /uselocaltime
if errorlevel 1 goto failed
echo PASS: unsigned development SYS, INF and CAT built. This command does not install a driver.
popd
exit /b 0
:failed
popd
exit /b 1
