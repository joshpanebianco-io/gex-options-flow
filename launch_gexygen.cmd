@echo off
rem GEXYGEN one-click launcher: opens the dashboard AND starts the session
rem collector. Safe to click any time — the collector has a single-instance
rem guard (a second copy prints "already running" and exits), and on weekends
rem it announces "weekend" and closes itself.
cd /d "%~dp0"
rem browser immediately; collector minimized to the taskbar so it never
rem covers the dashboard (cmd /c: without it, start would run the batch
rem with /K and the window would stay open forever after it finishes)
start "" "dashboard.html"
start /min "GEXYGEN Collector" cmd /c start_collector.cmd
