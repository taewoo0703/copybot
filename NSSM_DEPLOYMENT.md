# NSSM Deployment Guide

이 문서는 Windows에서 NSSM(Non-Sucking Service Manager)으로 `C:\Home\copybot\.venv`의 Python을 사용해 `run.py`를 서비스로 실행하는 방법을 정리합니다.

## 기준 경로

현재 프로젝트 기준으로 아래 경로를 사용합니다.

| 항목 | 값 |
| --- | --- |
| 프로젝트 경로 | `C:\Home\copybot` |
| Python 실행 파일 | `C:\Home\copybot\.venv\Scripts\python.exe` |
| 실행 스크립트 | `C:\Home\copybot\run.py` |
| 작업 디렉터리 | `C:\Home\copybot` |
| 환경 변수 파일 | `C:\Home\copybot_env\.env` |
| 기본 포트 | `.env`의 `PORT`, 기본 예시는 `8000` |
| 서비스 이름 예시 | `copybot` |

> 이 프로젝트의 `utility\BaseSettings.py`는 `.env`를 프로젝트 폴더 안이 아니라 `C:\Home\copybot_env\.env`에서 읽도록 되어 있습니다.

## 1. 사전 준비

관리자 권한 PowerShell을 열고 프로젝트 경로로 이동합니다.

```powershell
cd C:\Home\copybot
```

가상환경과 의존성이 준비되어 있는지 확인합니다.

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

환경 변수 파일을 준비합니다.

```powershell
New-Item -ItemType Directory -Force C:\Home\copybot_env
Copy-Item .\.env.example C:\Home\copybot_env\.env
notepad C:\Home\copybot_env\.env
```

`C:\Home\copybot_env\.env`에서 최소한 아래 항목을 실제 값에 맞게 확인합니다.

```env
PASSWORD=
COPYBOT_CONFIG_PATH=C:/Home/copybot/config/copybot.example.yaml
PORT=8000
USE_WHITELIST=0
WHITELIST=[]
```

서비스 등록 전에 콘솔에서 먼저 실행해 봅니다.

```powershell
C:\Home\copybot\.venv\Scripts\python.exe C:\Home\copybot\run.py
```

브라우저 또는 PowerShell에서 접속 확인:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/
```

확인 후 콘솔 실행은 `Ctrl+C`로 종료합니다.

## 2. NSSM 설치

NSSM을 내려받아 압축을 풀고 `nssm.exe`가 있는 경로를 확인합니다.

예시:

```text
C:\nssm\win64\nssm.exe
```

아래 명령에서 `C:\nssm\win64\nssm.exe` 경로가 다르면 실제 경로로 바꿔서 사용하세요.

## 3. 로그 폴더 생성

서비스 stdout/stderr 로그를 저장할 폴더를 만듭니다.

```powershell
New-Item -ItemType Directory -Force C:\Home\copybot\logs
```

## 4. 서비스 등록

관리자 권한 PowerShell에서 실행합니다.

```powershell
C:\nssm\win64\nssm.exe install copybot C:\Home\copybot\.venv\Scripts\python.exe C:\Home\copybot\run.py
```

NSSM 설정을 추가합니다.

```powershell
C:\nssm\win64\nssm.exe set copybot AppDirectory C:\Home\copybot
C:\nssm\win64\nssm.exe set copybot DisplayName Copybot
C:\nssm\win64\nssm.exe set copybot Description "Copy trading bot FastAPI service"
C:\nssm\win64\nssm.exe set copybot Start SERVICE_AUTO_START
```

로그 리다이렉션을 설정합니다.

```powershell
C:\nssm\win64\nssm.exe set copybot AppStdout C:\Home\copybot\logs\copybot.out.log
C:\nssm\win64\nssm.exe set copybot AppStderr C:\Home\copybot\logs\copybot.err.log
C:\nssm\win64\nssm.exe set copybot AppRotateFiles 1
C:\nssm\win64\nssm.exe set copybot AppRotateOnline 1
C:\nssm\win64\nssm.exe set copybot AppRotateBytes 10485760
```

서비스가 비정상 종료되면 자동 재시작하도록 설정합니다.

```powershell
C:\nssm\win64\nssm.exe set copybot AppExit Default Restart
C:\nssm\win64\nssm.exe set copybot AppThrottle 5000
```

## 5. 서비스 시작

```powershell
C:\nssm\win64\nssm.exe start copybot
```

상태 확인:

```powershell
C:\nssm\win64\nssm.exe status copybot
Get-Service copybot
```

접속 확인:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/
```

브라우저에서는 아래 주소를 엽니다.

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/admin
```

## 6. 서비스 제어

중지:

```powershell
C:\nssm\win64\nssm.exe stop copybot
```

재시작:

```powershell
C:\nssm\win64\nssm.exe restart copybot
```

설정 GUI 열기:

```powershell
C:\nssm\win64\nssm.exe edit copybot
```

서비스 삭제:

```powershell
C:\nssm\win64\nssm.exe stop copybot
C:\nssm\win64\nssm.exe remove copybot confirm
```

## 7. 업데이트 절차

코드를 업데이트하거나 의존성을 변경한 뒤에는 서비스 재시작이 필요합니다.

```powershell
cd C:\Home\copybot
C:\nssm\win64\nssm.exe stop copybot
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
C:\nssm\win64\nssm.exe start copybot
```

## 8. 문제 해결

### 서비스가 바로 종료되는 경우

stderr 로그를 확인합니다.

```powershell
Get-Content C:\Home\copybot\logs\copybot.err.log -Tail 100
```

콘솔에서 같은 명령으로 직접 실행해 에러를 확인합니다.

```powershell
C:\Home\copybot\.venv\Scripts\python.exe C:\Home\copybot\run.py
```

### 환경 변수가 적용되지 않는 경우

이 프로젝트는 아래 파일을 읽습니다.

```text
C:\Home\copybot_env\.env
```

`C:\Home\copybot\.env`가 아니라는 점을 확인하세요.

### 포트 충돌이 나는 경우

현재 포트 사용 프로세스를 확인합니다.

```powershell
netstat -ano | findstr :8000
```

다른 포트를 쓰려면 `C:\Home\copybot_env\.env`의 `PORT` 값을 변경하고 서비스를 재시작합니다.

```powershell
C:\nssm\win64\nssm.exe restart copybot
```

### 서비스 계정 권한 문제

기본 LocalSystem 계정으로 문제가 생기면 `services.msc`에서 `copybot` 서비스의 로그온 계정을 실제 사용자 계정으로 바꿉니다.

특히 아래 경로에 접근 권한이 있어야 합니다.

```text
C:\Home\copybot
C:\Home\copybot_env\.env
C:\Home\copybot\logs
```

### 방화벽 확인

외부 PC에서 접속해야 한다면 Windows 방화벽에서 포트를 허용합니다.

```powershell
New-NetFirewallRule `
  -DisplayName "Copybot 8000" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8000
```

외부 공개가 필요 없다면 방화벽을 열지 말고 로컬 또는 사설망에서만 접근하는 편이 안전합니다.

