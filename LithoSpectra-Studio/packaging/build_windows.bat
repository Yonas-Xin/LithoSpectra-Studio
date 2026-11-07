@echo off
chcp 65001 >nul
setlocal

rem ===========================================================================
rem  LithoSpectra - one-click Windows build (desktop app with Edge WebView2).
rem  Double-click this file in Explorer; nothing else is needed.
rem
rem  Starting from a machine that only has Miniconda, this script will
rem  create the conda env from environment.yml, install pyinstaller into it,
rem  and then build. The web pages under web\dist are compiled with npm when
rem  Node.js is installed; otherwise an existing web\dist is used.
rem  See packaging\WINDOWS.md for the full walkthrough.
rem
rem  Two deliberate constraints on this launcher:
rem    * ASCII only. cmd.exe keeps re-reading the batch file while it runs, and
rem      "chcp" mid-file can shift how later bytes are decoded. Every message
rem      meant for a human (in Chinese) is printed by build_windows.py instead.
rem    * goto flow, no parenthesised blocks around "call". Blocks expand their
rem      variables at parse time and nest badly around "call ... activate".
rem ===========================================================================

rem --- Configuration ---------------------------------------------------------
rem  conda environment holding gdal / numpy / fastapi / pywebview / pyinstaller.
rem  Can also be passed as the first argument.
set "ENV_NAME=samplelib"
rem ---------------------------------------------------------------------------

title LithoSpectra - Windows build
set "HERE=%~dp0"
if not "%~1"=="" set "ENV_NAME=%~1"

echo.
echo ============================================================
echo   LithoSpectra - Windows build
echo   conda env : %ENV_NAME%
echo   packaging : %HERE%
echo ============================================================
echo.

set "CONDA_BAT="
call :find_conda
if not defined CONDA_BAT goto :no_conda

echo [step 1/3] activating conda env "%ENV_NAME%"
echo            via %CONDA_BAT%
call "%CONDA_BAT%" activate "%ENV_NAME%"
if not errorlevel 1 goto :have_env

rem  No such env yet. Build it from environment.yml - this is the whole point
rem  of "one-click": a fresh machine with only Miniconda should get here and
rem  still end up with an exe. Takes 5-15 minutes, mostly downloading GDAL.
echo.
echo   conda env "%ENV_NAME%" not found - creating it from environment.yml.
echo   This downloads GDAL and takes 5-15 minutes. Leave it running.
echo.
call "%CONDA_BAT%" env create -f "%HERE%..\environment.yml" -n "%ENV_NAME%"
if errorlevel 1 goto :env_failed
call "%CONDA_BAT%" activate "%ENV_NAME%"
if errorlevel 1 goto :env_failed
goto :have_env

:env_failed
echo.
echo   WARNING: could not create or activate "%ENV_NAME%".
echo   Try it by hand and read the error:
echo       conda env create -f "%HERE%..\environment.yml"
echo   Falling back to whatever python is on PATH.
echo.
goto :check_python

:have_env
echo            env ready.

:check_python
where python >nul 2>nul
if not errorlevel 1 goto :check_pyinstaller
echo.
echo   ERROR: no python on PATH.
echo   Install Miniconda first: https://docs.conda.io/en/latest/miniconda.html
echo   Then double-click this file again.
goto :the_end

:check_pyinstaller
rem  PyInstaller is a build-only dependency; environment.yml lists it, but an
rem  env created before it was added will not have it. Install on demand so
rem  nobody has to know that.
echo [step 2/3] checking pyinstaller
python -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto :run_build
echo            not installed - installing now
python -m pip install pyinstaller
if errorlevel 1 goto :pyinstaller_failed
goto :run_build

:pyinstaller_failed
echo.
echo   ERROR: pip install pyinstaller failed. Check the network / proxy.
goto :the_end

:run_build
rem  Force UTF-8 so the Chinese log from build_windows.py renders correctly.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "ENV_UPDATED="

:run_build_once
echo [step 3/3] running packaging\build_windows.py
echo.
python "%HERE%build_windows.py"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="3" goto :deps_missing
if not "%RC%"=="0" goto :build_failed
echo ============================================================
echo   BUILD OK
echo ============================================================
goto :the_end

:deps_missing
rem  Exit code 3 = the env exists but predates some entry in environment.yml
rem  (pywebview, pythonnet, onnxruntime, ...). Bring it up to date once and
rem  try the build again; a second miss is a real failure.
if defined ENV_UPDATED goto :build_failed
if not defined CONDA_BAT goto :build_failed
set "ENV_UPDATED=1"
echo   conda env "%ENV_NAME%" is missing packages listed in environment.yml.
echo   Updating it (this downloads the missing packages, a few minutes)...
echo.
call "%CONDA_BAT%" env update -n "%ENV_NAME%" -f "%HERE%..\environment.yml"
if errorlevel 1 goto :update_failed
echo.
echo   env updated - building again.
echo.
goto :run_build_once

:update_failed
echo.
echo   ERROR: conda env update failed. Check the network / proxy, or run by hand:
echo       conda env update -n %ENV_NAME% -f "%HERE%..\environment.yml"
goto :the_end

:build_failed
echo ============================================================
echo   BUILD FAILED - exit code %RC%
echo   Scroll up: the last lines say which step gave up.
echo ============================================================

:the_end
echo.
pause
endlocal
exit /b

rem ===========================================================================
rem  Locate conda.bat: current shell first, then PATH, then the usual roots.
rem ===========================================================================
:find_conda
if not defined CONDA_EXE goto :conda_on_path
for %%D in ("%CONDA_EXE%") do if exist "%%~dpD..\condabin\conda.bat" set "CONDA_BAT=%%~dpD..\condabin\conda.bat"
if defined CONDA_BAT goto :eof

:conda_on_path
for /f "delims=" %%I in ('where conda.bat 2^>nul') do if not defined CONDA_BAT set "CONDA_BAT=%%I"
if defined CONDA_BAT goto :eof

for %%R in (
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\anaconda3"
    "%USERPROFILE%\miniforge3"
    "%USERPROFILE%\mambaforge"
    "%LOCALAPPDATA%\miniconda3"
    "%LOCALAPPDATA%\anaconda3"
    "%LOCALAPPDATA%\miniforge3"
    "%ProgramData%\miniconda3"
    "%ProgramData%\Miniconda3"
    "%ProgramData%\anaconda3"
    "%ProgramData%\Anaconda3"
    "%ProgramFiles%\Miniconda3"
    "%ProgramFiles%\Anaconda3"
    "C:\miniconda3"
    "C:\anaconda3"
) do if exist "%%~R\condabin\conda.bat" if not defined CONDA_BAT set "CONDA_BAT=%%~R\condabin\conda.bat"
goto :eof
