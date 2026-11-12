@echo off
setlocal EnableExtensions

set "TOKEN="
set "CHAIN="
set "LIMIT="
set "CSV="
set "QUIET="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--quiet" (
  set "QUIET=--quiet"
  shift
  goto parse
)
if /i "%~1"=="/quiet" (
  set "QUIET=--quiet"
  shift
  goto parse
)
if not defined TOKEN (
  set "TOKEN=%~1"
  shift
  goto parse
)
if not defined CHAIN (
  set "CHAIN=%~1"
  shift
  goto parse
)
if not defined LIMIT (
  set "LIMIT=%~1"
  shift
  goto parse
)
if not defined CSV (
  set "CSV=%~1"
  shift
  goto parse
)
echo scan_target: unexpected argument "%~1"
goto usage

:parsed
if "%TOKEN%"=="" goto usage
if "%CHAIN%"=="" set "CHAIN=bsc"
if "%LIMIT%"=="" set "LIMIT=1000"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "HOLDERS=%ROOT%\out\holders_%TOKEN%.txt"

set "CSV_ARG="
if not "%CSV%"=="" set CSV_ARG=--csv "%CSV%"

echo [1/2] discovering top %LIMIT% holders of %TOKEN% on %CHAIN%
call "%ROOT%\run.cmd" discover --token "%TOKEN%" --chain "%CHAIN%" --limit %LIMIT% --out "%HOLDERS%" %CSV_ARG%
if errorlevel 1 (
  echo.
  echo scan_target: discovery failed; nothing was analysed.
  echo scan_target: on a free Etherscan plan pass a CSV export as the 4th argument:
  echo   scan_target.bat %TOKEN% %CHAIN% %LIMIT% path\to\holders.csv
  exit /b 2
)
if not exist "%HOLDERS%" (
  echo scan_target: discovery reported success but wrote no holder file.
  exit /b 2
)

echo.
echo [2/2] analysing %HOLDERS% with the target filter
call "%ROOT%\run.cmd" analyze --file "%HOLDERS%" --filter %QUIET%
if errorlevel 1 (
  echo.
  echo scan_target: analysis failed.
  exit /b 3
)

echo.
echo scan_target: done
echo   holders : %HOLDERS%
echo   report  : %ROOT%\out\report.csv
echo   matches : %ROOT%\out\report_matches.csv
exit /b 0

:usage
echo usage: scan_target.bat ^<token-contract^> [chain] [limit] [csv-fallback] [--quiet]
echo   chain    bsc ^(default^) or ethereum
echo   limit    holders to request, default 1000
echo   csv      local holder export, used when the Etherscan API is unavailable
echo            ^(tokenholderlist is a Pro endpoint^)
echo   --quiet  suppress per-wallet progress lines in large batches
echo            may appear anywhere after the token
echo.
echo examples:
echo   scan_target.bat 0x55d398326f99059fF775485246999027B3197955
echo   scan_target.bat 0x55d398326f99059fF775485246999027B3197955 bsc 1000 data\dune_holders_sample.csv
echo   scan_target.bat 0x55d398326f99059fF775485246999027B3197955 bsc 500 data\dune_holders_sample.csv --quiet
exit /b 2
