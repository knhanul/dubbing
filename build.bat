@echo off
rem 영상 합성기 Windows 실행 파일(exe) 만들기 - 결과: dist\VideoComposer\VideoComposer.exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo 파이썬을 찾을 수 없습니다. Python 3.11 이상을 설치하세요.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed ^
    --name VideoComposer ^
    --collect-data imageio_ffmpeg ^
    --collect-data docx ^
    --collect-all _sounddevice_data ^
    --collect-all _soundfile_data ^
    --collect-all faster_whisper ^
    --collect-all ctranslate2 ^
    --collect-all onnxruntime ^
    --collect-data tokenizers ^
    --add-data "schema;schema" ^
    --add-data "examples;examples" ^
    run_app.py
if errorlevel 1 goto :fail

echo.
echo 완료! dist\VideoComposer 폴더 전체를 복사해서 쓰세요.
echo 실행 파일: dist\VideoComposer\VideoComposer.exe
pause
exit /b 0

:fail
echo 빌드에 실패했습니다. 위 오류 메시지를 확인하세요.
pause
exit /b 1
