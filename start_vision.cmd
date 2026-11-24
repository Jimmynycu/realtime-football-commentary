@echo off
rem Vision mode: local vision model reads the picture of any football video. http://127.0.0.1:8770
cd /d %~dp0
uv run --no-project --python 3.11 --with llama-cpp-python==0.3.4 --with pillow --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --index-strategy unsafe-best-match python vision_server.py
