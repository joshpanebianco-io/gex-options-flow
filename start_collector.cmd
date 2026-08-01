@echo off
rem GEXYGEN session collector — schedule Mon-Fri ~22:25 AEST via Task Scheduler.
rem The --rth-only gate sleeps until the 08:30 ET session window (premarket,
rem fresh OCC-settlement OI lands ~09:00 ET) and exits after 16:05 ET, so US
rem DST needs no schedule changes. Logging is handled by --log inside Python,
rem so the identical command works when hosted elsewhere:
rem     python3 collector.py --loop 10 --rth-only --log runs_console.log
cd /d "%~dp0"
echo GEXYGEN collector starting - a snapshot block prints every ~10 min during
echo the 08:30-16:05 ET session (22:30-06:05 AEST). Leave this window open.
echo.
py collector.py --loop 10 --rth-only --log runs_console.log
echo.
echo Collector finished. This window closes automatically:
timeout /t 10
