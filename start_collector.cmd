@echo off
rem GEXYGEN session collector — schedule Mon-Fri ~20:55 AEST via Task Scheduler.
rem The --rth-only gate sleeps until the 07:00 ET session window (premarket,
rem fresh OCC-settlement OI lands ~09:00 ET) and exits after 16:05 ET, so US
rem DST needs no schedule changes. Logging is handled by --log inside Python,
rem so the identical command works when hosted elsewhere:
rem     python3 collector.py --loop 5 --rth-only --log runs_console.log
cd /d "%~dp0"
echo GEXYGEN collector starting - a snapshot block prints every ~5 min during
echo the 07:00-16:05 ET session (21:00-06:05 AEST). Leave this window open.
echo.
py collector.py --loop 5 --rth-only --log runs_console.log
echo.
echo Collector finished. This window closes automatically:
timeout /t 10
