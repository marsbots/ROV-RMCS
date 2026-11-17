# ROVOps — 원격 모니터링 및 제어 시스템 (RMCS)

테더형 ROV(ArduSub + BlueOS + Pixhawk/Raspberry Pi)를 위한 원격 운용 플랫폼 프로토타입입니다.

> English: [README.md](README.md)

## 현재 상태 (중요)

- **프로토타입/개발 단계**입니다. 서버·에이전트·프로토콜/안전 로직과 참고용 브라우저 UI, 스크립트/테스트가 구현되어 있습니다.
- HQ macOS 환경에서 `pytest 26/26`, `e2e_smoke.py`(실서버 + fake autopilot + agent, WebSocket 파일럿 세션 흐름) **통과**를 확인했습니다.
- **아직 완료되지 않은 것:** 실기체(RPi4+ArduSub+Pixhawk) 현장/외부망 end-to-end 검증, 영상 파이프라인(H.264 기본/저조도/RealSense D405) 실연동, `rmcs.marsbot.kr` 등 외부 접속용 DNS/TLS/WSS 상시 서비스 배포·운영 설정.
- **안전 원칙:** 제어 서버는 **브로커**이며 내부 제어 루프가 아닙니다. 최종 안전 책임은 **차량 에이전트의 로컬 watchdog/E-stop + ArduSub/Pixhawk failsafe**에 있습니다. 실기체 테스트는 반드시 **SITL → 벤치(프로펠러 제거) → 수중** 순으로 진행하세요.

## 이 프로젝트가 하는 일

- **제어 서버** (`server/`): FastAPI 기반 브로커. 차량 에이전트는 서버로 아웃바운드 연결(`WS /ws/vehicle`)하고, 파일럿은 세션(`WS /ws/pilot/{session_id}`)에 연결합니다. 인증/인가, 차량 등록·상태, 세션/lease, 명령 검증, 텔레메트리 중계를 담당합니다.
- **차량 에이전트** (`agent/`): 차량(BlueOS Extension)에서 실행. 서버로 아웃바운드 WebSocket을 열고, 로컬 MAVLink를 브리지하며, 로컬 명령 watchdog/E-stop을 백스톱으로 강제합니다.
- **프로토콜/안전**: JSON 제어 평면 + 바이너리 MAVLink 패스스루(단일 컨트롤러 lease로 게이트). 서버는 속도 제한, 명령 TTL, 타임아웃/연결끊김/lease 상실 시 neutral burst, 서버 측 래치(latched) E-stop을 적용하고 에이전트를 통해 재확인합니다.
- **영상**: 제어 평면과 분리된 **out-of-band**. 에이전트가 `hello`에 광고하는 `VIDEO_WHEP_URL`/`VIDEO_RTSP_URL`을 파일럿이 사용합니다(기본 권장: MediaMTX WHEP). 서버는 영상을 중계하지 않습니다.
- **감사 로그**: `data/audit.jsonl`에 인증, 세션, lease, arm/mode, E-stop, 명령 거부 등을 기록합니다.

## 아키텍처 (개념)

1. **Vehicle/Companion (현장, RPi4 + BlueOS + ArduSub/Pixhawk)**
   - `rovops_agent`가 `SERVER_URL`의 `/ws/vehicle?token=...`로 **아웃바운드 접속** (NAT 친화적)
   - 로컬 MAVLink 엔드포인트(예: `udpin:0.0.0.0:14599`)와 브리지, 로컬 안전 백스톱 수행
   - 영상은 별도 파이프라인(MediaMTX 등)으로 제공하고 URL만 광고
2. **원격관제 서버 (HQ/맥미니)**
   - 차량 온라인 관리, 파일럿 로그인·차량 선택·세션 생성, lease/명령 검증·감사
   - 외부 운영 시 HTTPS/WSS(TLS), 리버스 프록시, 비밀/토큰 관리 필요
3. **파일럿 클라이언트**
   - 참고용 웹 UI: `server/app/static/` (로그인/세션/텔레메트리/E-stop/lease)
   - 주 방향: 자체 로컬 관제 GCS(MARSUB-PI)에서 REST+WS API 연동

## 저장소 구조

```
.
├─ server/                 # FastAPI 제어 서버
│  ├─ app/                 # 라우트, 허브, 인증, MAVLink, 안전 로직
│  ├─ config/vehicles.json # 차량 레지스트리(예시/개발용 토큰 포함)
│  ├─ static/              # 참고용 브라우저 Pilot UI
│  └─ tests/               # 서버 테스트
├─ agent/                  # 차량 에이전트(BlueOS Extension)
│  ├─ rovops_agent/        # 클라이언트, 설정, MAVLink 브리지, 헬스
│  └─ tests/               # 에이전트 테스트
├─ scripts/
│  ├─ fake_autopilot.py    # e2e용 가짜 오토파일럿
│  └─ e2e_smoke.py         # end-to-end 스모크 테스트
├─ docs/                   # 보안/영상/배포/라이선스 문서
├─ README.md               # English
├─ README.ko.md            # 한국어(본 문서)
└─ DEV_GUIDE.md            # HQ macOS 개발 가이드
```

## 개발 환경 (HQ macOS)

- 프로젝트 위치: `/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)` (SMB 마운트)
- Python 3.11+ (3.14 테스트), 로컬 venv 권장: `~/.venvs/rmcs`
- 저장소 내 `.venv`는 Windows 생성물이므로 **macOS에서 사용 금지**

```bash
python3 -m venv ~/.venvs/rmcs
~/.venvs/rmcs/bin/pip install -r server/requirements.txt
~/.venvs/rmcs/bin/pip install -r server/requirements-dev.txt
```

### 서버 실행
```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
export JWT_SECRET="<long-random-secret>"
export VEHICLES_FILE="server/config/vehicles.json"
~/.venvs/rmcs/bin/python -m uvicorn app.main:app --app-dir server --reload --port 8080
```

### 에이전트 실행 (SITL/로컬 테스트)
```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)/agent"
export SERVER_URL="http://localhost:8080"
export VEHICLE_TOKEN="<provisioned-device-token>"
export MAVLINK_ENDPOINT="udpin:0.0.0.0:14599"
~/.venvs/rmcs/bin/python -m rovops_agent.main
```

### 테스트
```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
~/.venvs/rmcs/bin/python -m pytest server/tests/ agent/tests/ -q
~/.venvs/rmcs/bin/python scripts/e2e_smoke.py
```

## 실기체 접속을 위한 절차 (권장)

> 아래는 코드/문서 기준의 준비 절차입니다. 실제 배선·BlueOS/ArduSub 설정·네트워크 환경은 현장에 맞게 확인해야 합니다.

### 0. 사전 조건
- ROV: ArduSub + Pixhawk + Raspberry Pi 4 + BlueOS 구성, 차량 전원/통신 정상
- 서버(HQ 맥미니)와 차량이 IP 통신 가능해야 함 (같은 네트워크 또는 외부 접속 경로/WSS)
- 제어 서버 포트(예: 8080)와 MAVLink/영상용 포트 방화벽 정책 확인
- **반드시 안전 순서 준수:** SITL → 벤치(프로펠러 제거, arm/E-stop/neutral 확인) → 수중

### 1. 서버 준비 (맥미니/HQ)
1. `.env`/환경변수 설정 (`.env.example` 참고)
   - `JWT_SECRET`: 충분히 긴 랜덤 값으로 교체
   - `DEMO_USER/DEMO_PASSWORD`, `ADMIN_USER/ADMIN_PASSWORD`: 운영 계정으로 교체 (기본값 없음)
2. `server/config/vehicles.json`에 실차량 등록
   - 고유 `id`, `sysid` 등 메타 정보 설정
   - **개발용 토큰 교체**: `token` 또는 `token_env`(예: `ROV_TOKEN_ROV_01`)로 실 토큰 주입
3. 서버 실행 및 헬스 확인
   - `GET /healthz` 응답 확인
   - 로컬 확인: `http://<서버IP>:8080` (참고용 UI)
4. (외부 접속/실운영 시) 리버스 프록시 + TLS(HTTPS/WSS) 구성, `PUBLIC_WS_BASE` 등 공개 URL 설정, CORS 제한, 감사 로그 보관/모니터링 구성

### 2. 차량(BlueOS/RPi4) 측 준비
1. **MAVLink 라우팅**: BlueOS MAVLink Router에서 에이전트가 수신할 UDP 엔드포인트 추가 (예: Client, `127.0.0.1:14599`) — 에이전트 `MAVLINK_ENDPOINT`와 일치 필요
2. **에이전트 배포/실행** (BlueOS Extension 또는 컨테이너/직접 실행)
   - `agent/Dockerfile` 기반 빌드 가능, 컨테이너는 **host 네트워킹** 필요(로컬 UDP 14599 바인딩)
   - 필수 환경변수:
     - `SERVER_URL`: `http://<서버IP>:8080` (로컬망) 또는 `https://<도메인>` (운영/TLS 시 `wss`로 연결)
     - `VEHICLE_TOKEN`: 서버에 등록한 차량 디바이스 토큰
     - `MAVLINK_ENDPOINT`: `udpin:0.0.0.0:14599` (기본)
   - 선택/튜닝: `VEHICLE_SYSID`, `COMMAND_TTL_MS`, `NEUTRAL_HZ`, `ESTOP_HZ`, `VIDEO_WHEP_URL`/`VIDEO_RTSP_URL` 등 (`agent/.env.example`, `agent/README.md` 참고)
3. 에이전트 헬스 확인: 차량 로컬 `http://127.0.0.1:8081/healthz`
4. 서버에서 차량 온라인 여부 확인: `GET /api/vehicles` 상태 또는 UI에서 online 확인

### 3. 파일럿 접속/제어 테스트
1. 파일럿(참고용 웹 UI 또는 자체 GCS)에서 로그인 → 차량 선택 → 세션 생성
2. **lease 획득** 후에만 motion/arm/mode 제어 가능함을 확인
3. 텔레메트리 수신, arm/mode 변경, **E-stop(래치) → 해제 → 재제어** 흐름 확인
4. 연결 끊김/타임아웃 시 neutral 및 failsafe 동작 확인 (벤치 단계에서 우선)

### 4. 영상 연동 (별도 작업)
- 제어와 분리된 out-of-band 영상 경로를 구성해야 합니다(예: MediaMTX로 H.264 WHEP 제공).
- 에이전트에 `VIDEO_WHEP_URL`(및 필요 시 `VIDEO_RTSP_URL`) 설정 → 서버/파일럿에 광고.
- 저조도 카메라, Intel RealSense D405(RGB/Depth) 등 다중 스트림/활용 범위는 별도 설계·검증이 필요합니다 (`docs/03-video.md` 참고).

## 알려진 제한/주의사항

- 실기체·외부망 end-to-end 미검증(현 단계). SITL/벤치/수중 단계적 검증 필요.
- 기본 계정/개발 토큰/기본 `JWT_SECRET`은 **배포 전 반드시 교체**.
- `vehicles.json`의 `token` 평문 저장은 개발 편의용이며, 운영에서는 `token_env`/비밀관리 사용 권장.
- 라이선스(`LICENSE`) 미확정. QGC/Cockpit(AGPL/GPL) 연동/노출 시 라이선스 검토 필요 (`docs/05-licensing.md`).

## 관련 문서

- [DEV_GUIDE.md](DEV_GUIDE.md) — HQ macOS 개발 가이드
- [docs/02-security.md](docs/02-security.md) — 인증/토큰/세션/감사
- [docs/03-video.md](docs/03-video.md) — 영상(out-of-band)
- [docs/04-deployment.md](docs/04-deployment.md) — 배포
- [docs/05-licensing.md](docs/05-licensing.md) — 라이선스 메모

## 라이브 배포 (2026-11-17 기준)

- URL: https://rmcs.marsbot.kr/ (본사 맥미니, uvicorn :8080 + Caddy 리버스 프록시, TLS 종단)
- LaunchAgent: `ai.openclaw.rmcs-server` (KeepAlive, 재부팅 자속)
- 로그: `~/.openclaw/logs/rmcs-server.log` / `rmcs-server.err.log`
- 기본 계정 없음: `ADMIN_USER`/`ADMIN_PASSWORD`/`DEMO_USER`/`DEMO_PASSWORD` 환경변수로 설정
- 헬스: `GET /healthz` — 차량 온라인 수·세션 수 반환
- 차량 접속: 차량 측 agent가 서버로 아웃바운드 연결(`/ws/vehicle`) — 차량 미접속 시 `vehicles_online: 0`이 정상
