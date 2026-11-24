@echo off
rem One command: local LLM + commentary server + the real match on YouTube.
cd /d %~dp0
start "local LLM" cmd /k start_llm.cmd
start "commentary server" cmd /k python server.py
rem Without the extension: on the YouTube tab press F12, open Console, paste (the loader is now on the clipboard), Enter.
clip < console-snippet.txt
timeout /t 6 /nobreak >nul
start "" "https://www.youtube.com/watch?v=RgqKdplLIk4&t=2190s"
