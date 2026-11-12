@echo off
setlocal EnableExtensions

set "TOKEN=%~1"
set "CHAIN=%~2"
set "LIMIT=%~3"
set "CSV=%~4"

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
call "%ROOT%\run.cmd" analyze --file "%HOLDERS%" --filter
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
echo usage: scan_target.bat ^<token-contract^> [chain] [limit] [csv-fallback]
echo   chain    bsc ^(default^) or ethereum
echo   limit    holders to request, default 1000
echo   csv      local holder export, used when the Etherscan API is unavailable
echo            ^(tokenholderlist is a Pro endpoint^)
echo.
echo examples:
echo   scan_target.bat 0x55d398326f99059fF775485246999027B3197955
echo   scan_target.bat 0x55d398326f99059fF775485246999027B3197955 bsc 1000 data\dune_holders_sample.csv
exit /b 2
