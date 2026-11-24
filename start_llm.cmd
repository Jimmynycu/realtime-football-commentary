@echo off
rem Free local LLM (Qwen2.5-1.5B) with an OpenAI-compatible API on http://127.0.0.1:8081/v1
rem server.py picks it up automatically when it is running. First run installs the runtime (uv) and the model.
cd /d %~dp0
uv run --no-project --python 3.11 --with llama-cpp-python==0.3.4 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --index-strategy unsafe-best-match python llm_server.py
