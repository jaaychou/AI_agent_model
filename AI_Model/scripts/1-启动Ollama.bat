@echo off
setlocal
for %%I in ("%~dp0..") do set "AI_HOME=%%~fI"
if not exist "%AI_HOME%\Ollama\ollama.exe" (
  echo [Ollama] Cannot find ollama.exe under "%AI_HOME%".
  pause
  exit /b 1
)
curl.exe -fsS --max-time 2 http://127.0.0.1:11434/api/tags >nul 2>nul
if not errorlevel 1 (
  echo [Ollama] Service is already running at http://127.0.0.1:11434.
  echo [Ollama] This launcher will not start a duplicate service.
  pause
  exit /b 0
)
set "OLLAMA_MODELS=%AI_HOME%\models"
set "OLLAMA_LLM_LIBRARY=vulkan"
echo [Ollama] Starting local model service with Vulkan.
echo [Ollama] Keep this window open while the service is needed.
"%AI_HOME%\Ollama\ollama.exe" serve
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo [Ollama] Service exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
