@echo off
rem ══════════════════════════════════════════════════════════════
rem  大肥鱼 · 竖屏直播专用窗口（9:16 无黑边、无标签页、无标题栏）
rem  双击运行；直播伴侣里用「窗口采集」选「肥波 · 直播间」
rem  关闭直播画面：直接关掉那个窗口
rem ══════════════════════════════════════════════════════════════
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\fit_9x16_window.ps1"
pause
