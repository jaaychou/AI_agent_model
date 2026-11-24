@echo off
setlocal
for %%I in ("%~dp0..") do set "AI_HOME=%%~fI"
if not exist "%AI_HOME%\venv-webui\Scripts\open-webui.exe" (
  echo [WebUI] Cannot find open-webui.exe under "%AI_HOME%".
  pause
  exit /b 1
)
curl.exe -fsS --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>nul
if errorlevel 1 (
  echo [WebUI] Ollama is not responding at http://127.0.0.1:11434.
  echo [WebUI] Start the Ollama launcher first, then retry this launcher.
  pause
  exit /b 1
)
curl.exe -fsS --max-time 2 http://127.0.0.1:8080/ >nul 2>nul
if not errorlevel 1 (
  echo [WebUI] Web interface is already responding at http://localhost:8080.
  echo [WebUI] Do not start a second instance. Open the URL in your browser.
  pause
  exit /b 0
)
set "HF_ENDPOINT=https://hf-mirror.com"
set "DATA_DIR=%AI_HOME%\webui_data"
set "OLLAMA_BASE_URL=http://127.0.0.1:11434"
echo [WebUI] Starting Open WebUI at http://localhost:8080.
echo [WebUI] Keep this window open while the service is needed.
"%AI_HOME%\venv-webui\Scripts\open-webui.exe" serve --host 127.0.0.1 --port 8080
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo [WebUI] Service exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
