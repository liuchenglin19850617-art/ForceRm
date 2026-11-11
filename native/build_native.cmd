@echo off
setlocal
cd /d "%~dp0"

echo [1/2] Compiling Java logger...
where javac >nul 2>nul
if %errorlevel%==0 (
  javac -encoding UTF-8 ProcUninstLog.java
  if %errorlevel%==0 (echo ProcUninstLog.class created) else (echo Java compile failed)
) else (
  echo javac not found, skipped
)

echo [2/2] Compiling C++ helper...
where cl >nul 2>nul
if %errorlevel%==0 (
  cl /nologo /O2 /EHsc /DUNICODE /D_UNICODE /utf-8 ProcUninst.cpp /Fe:ProcUninst.exe /link psapi.lib
  goto :done
)
where g++ >nul 2>nul
if %errorlevel%==0 (
  g++ -O2 -municode -static ProcUninst.cpp -o ProcUninst.exe -lpsapi
  goto :done
)
echo No C++ compiler ^(cl or g++^) found. ProcUninst.exe not built.
echo ForceRm uses its built-in ctypes implementation instead.

:done
endlocal
