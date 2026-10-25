@echo off
rem Cyber-Tiel-Coder-35B-A3B - local OpenAI-compatible API on 127.0.0.1:8090
rem   OpenAI:  POST /v1/chat/completions    Anthropic: POST /v1/messages
rem   Model list: GET /v1/models            Health:    GET /health
rem Stop = close this window (or Ctrl+C).
cd /d "%~dp0"
python -m serve.server --engine strata --config strata.json --port 8090
pause
