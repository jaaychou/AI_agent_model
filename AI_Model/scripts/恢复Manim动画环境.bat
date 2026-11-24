@echo off
chcp 65001 >nul
REM ============================================================
REM  Restore 3b1b Manim (ManimGL) animation environment
REM  Run this on a new machine / after switching AI. Safe to rerun.
REM ============================================================
set ROOT=D:\Program\AI_Model_agent\AI_Model
set PY=%ROOT%\venv-comfy\Scripts\python.exe

echo [1/6] Create tools dir
if not exist "%ROOT%\tools" mkdir "%ROOT%\tools"

echo [2/6] Clone ManimGL engine (3b1b/manim)
if not exist "%ROOT%\tools\manim\.git" (
  git clone --depth 1 https://github.com/3b1b/manim.git "%ROOT%\tools\manim"
)

echo [3/6] Clone Essence of Linear Algebra scenes (3b1b/videos, sparse)
if not exist "%ROOT%\tools\manim_videos\.git" (
  git clone --depth 1 --filter=blob:none --sparse https://github.com/3b1b/videos.git "%ROOT%\tools\manim_videos"
  pushd "%ROOT%\tools\manim_videos"
  git sparse-checkout set "_2016/eola"
  popd
)

echo [4/6] Create venv-manim and install ManimGL
if not exist "%ROOT%\venv-manim\Scripts\python.exe" (
  "%PY%" -m venv "%ROOT%\venv-manim"
)
"%ROOT%\venv-manim\Scripts\python.exe" -m pip install --no-cache-dir -e "%ROOT%\tools\manim" --disable-pip-version-check
REM PyAV is needed by manimlib import; install explicitly (sometimes skipped by -e)
"%ROOT%\venv-manim\Scripts\python.exe" -m pip install --no-cache-dir av --disable-pip-version-check

echo [5/6] Install ffmpeg and point Manim to it
"%PY%" -m pip install --no-cache-dir imageio-ffmpeg --disable-pip-version-check
if not exist "%ROOT%\tools\ffmpeg" mkdir "%ROOT%\tools\ffmpeg"
for /f "delims=" %%F in ('"%PY%" -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"') do copy /Y "%%F" "%ROOT%\tools\ffmpeg\ffmpeg.exe" >nul
REM Write custom_config.yml with absolute ffmpeg path (forward slashes)
> "%ROOT%\tools\manim\custom_config.yml" echo file_writer:
>> "%ROOT%\tools\manim\custom_config.yml" echo   ffmpeg_bin: "D:/Program/AI_Model_agent/AI_Model/tools/ffmpeg/ffmpeg.exe"

echo [6/6] Verify
"%ROOT%\venv-manim\Scripts\python.exe" -c "import manimlib; print('MANIMLIB OK')"

echo.
echo DONE. Now tell the Agent: "make a linear algebra animation" to test.
pause
