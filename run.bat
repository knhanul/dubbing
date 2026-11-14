@echo off
rem 영상 합성기 실행 (처음 한 번은 가상환경을 만들고 패키지를 설치합니다)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 파이썬 가상환경을 만드는 중...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo 파이썬을 찾을 수 없습니다. https://www.python.org 에서 Python 3.11 이상을 설치하세요.
        echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
        pause
        exit /b 1
    )
    echo [2/2] 필요한 패키지를 설치하는 중... (몇 분 걸릴 수 있습니다)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 패키지 설치에 실패했습니다. 인터넷 연결을 확인하세요.
        pause
        exit /b 1
    )
)
start "" ".venv\Scripts\pythonw.exe" -m composer %*
