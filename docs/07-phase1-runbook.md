> 이 문서는 레거시 프로젝트 '수중드론(ROV) 통합관제시스템'에서 이식되었습니다. RMCS 현재 아키텍처(브로커+차량 에이전트)와 일부 다를 수 있으니 참고용으로 사용하세요.

# Phase 1 Runbook — 원격 서버 기반 ROV 제어 검증 (수정판)

> 목표: ROV가 외부 서버(맥미니)에 연결되고, 파일럿이 원격에서 웹 브라우저로 ROV를 제어 가능
> 환경: ROV 1대 (ArduSub+Pixhawk+BlueOS), 맥미니 서버 (도메인 연결), 파일럿 PC (원격)
> 네트워크: LTE/5G/Starlink/인터넷 (ROV와 파일럿 모두 원격)

---

## 0. 아키텍처 (원격 서버 기반)

```
[ROV (현장)]                    [맥미니 서버 (회사 도메인)]          [파일럿 (원격)]
  ArduSub+Pixhawk+BlueOS  ──→  FastAPI 백엔드 (8000)  ←──  웹 브라우저
  mavlink-router (TCP)      →  WebSocket 텔레메트리  ←──  조이스틱 (Gamepad API)
  영상 (RTSP/WebRTC)        →  MediaMTX (추후)      ←──  영상 수신
                                │
                                └── 도메인 (예: rov.mars-company.com)
                                    HTTPS/WSS (Let's Encrypt)
```

**핵심 원칙:**
- ROV는 **맥미니 서버에만** 연결 (도메인 기반)
- 파일럿은 **웹 브라우저**로 서버에 접속 (설치 불필요)
- 조이스틱은 파일럿 PC의 USB 조이스틱 → 브라우저 Gamepad API
- 서버는 **Registry + Telemetry + Control Routing** 역할

**포트 정리:**
| 포트 | 용도 | 연결 주체 |
|---|---|---|
| 5760 | ROV MAVLink 수신 | ROV → 서버 |
| 5761 | (Phase 2) Cockpit 브릿지 | 파일럿 Cockpit → 서버 |
| 8000 | REST API + WebSocket + 웹 UI | 파일럿 브라우저 → 서버 |
| 443 | HTTPS/WSS (프로덕션) | 파일럿 브라우저 → 서버 (Nginx/Caddy) |

---

## 1. 맥미니 서버 준비

### 1.1 소프트웨어 설치
```bash
# Homebrew (없으면)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.11+
brew install python@3.11

# 프로젝트 클론/복사
cd ~/projects  # 또는 원하는 경로
# 통합관제시스템 폴더를 맥미니로 복사

# 의존성 설치
cd 통합관제시스템/backend
pip3 install -r requirements.txt
```

### 1.2 도메인 설정
회사 도메인에 A 레코드 추가:
```
rov.mars-company.com  →  <맥미니_공인_IP>
```

또는 DDNS (동적 IP인 경우):
```
rov.mars-company.com  →  DDNS 서비스 (No-IP, DuckDNS 등)
```

### 1.3 방화벽 / 포트 포워딩
맥미니가 공유기 뒤에 있다면 포트 포워딩 필요:
```
공유기 → 포트 포워딩:
  5760 (TCP) → 맥미니:5760
  8000 (TCP) → 맥미니:8000
  443  (TCP) → 맥미니:443  (HTTPS, 추후)
```

맥미니 자체 방화벽:
```bash
# macOS 방화벽 (System Settings → Network → Firewall)
# 또는 pfctl로 포트 허용
```

### 1.4 (선택) HTTPS 설정 — 프로덕션용
Caddy (자동 HTTPS) 또는 Nginx + Let's Encrypt:
```bash
# Caddy 설치
brew install caddy

# Caddyfile
rov.mars-company.com {
    reverse_proxy localhost:8000
}

# 실행
caddy run
```

---

## 2. 백엔드 실행 (맥미니)

```bash
cd ~/projects/통합관제시스템/backend
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

예상 출력:
```
[INFO] ROV listener on TCP 5760
[INFO] Cockpit listener on TCP 5761
[INFO] Backend started. ROV port=5760, Cockpit port=5761, API port=8000
```

상태 확인:
```bash
curl http://localhost:8000/api/status
# {"rov_connected":false,"cockpit_connected":false,"registered_rovs":0,"active_leases":0}
```

외부에서 확인:
```bash
curl http://rov.mars-company.com:8000/api/status
```

---

## 3. ROV BlueOS 설정

ROV가 **맥미니 서버 도메인**으로 MAVLink를 전송합니다.

### 3.1 BlueOS 웹 UI 접속
브라우저에서 `http://<ROV_IP>` 접속 (ROV와 같은 네트워크에서)

### 3.2 엔드포인트 추가
"MAVLink" → "Endpoints" → "Add Endpoint":

| 필드 | 값 |
|---|---|
| Type | **TCP Client** |
| Address | `rov.mars-company.com` (도메인) 또는 맥미니 공인 IP |
| Port | `5760` |

**기존 로컬 엔드포인트(UDP 14550)는 제거** — ROV는 서버에만 연결.

### 3.3 적용 및 확인
- "Apply" / "Save"
- 엔드포인트 상태가 **Connected**로 표시되는지 확인
- 맥미니 서버 콘솔에 `[INFO] ROV connected from ...` 출력 확인
- `curl http://rov.mars-company.com:8000/api/status` → `rov_connected: true`

---

## 4. 파일럿 원격 접속 (웹 브라우저)

파일럿은 **자신의 PC에서 웹 브라우저**로 서버에 접속합니다.

### 4.1 접속
```
http://rov.mars-company.com:8000          (개발)
https://rov.mars-company.com              (프로덕션, Caddy 설정 후)
```

### 4.2 웹 UI 기능 (Phase 1)
- ROV 목록 + 연결 상태
- 선택한 ROV의 텔레메트리 (배터리, 수심, 자세)
- 조이스틱 제어 (브라우저 Gamepad API — USB 조이스틱 연결)
- 제어권 획득/해제 버튼

### 4.3 조이스틱 연결 (파일럿 PC)
1. USB 조이스틱을 파일럿 PC에 연결
2. 웹 UI에서 "Joystick" 섹션 확인
3. 브라우저가 Gamepad API로 조이스틱 감지
4. Arm/Disarm, 이동, 모드 전환 테스트

---

## 5. 독립 검증 (WebSocket)

파일럿 PC에서 WebSocket 텔레메트리를 검증합니다.

```bash
# 파일럿 PC에서 (Python 설치 필요)
pip install websockets
python verify_rov.py --host rov.mars-company.com --port 8000
```

또는 브라우저 개발자 도구 콘솔:
```javascript
const ws = new WebSocket('ws://rov.mars-company.com:8000/ws/telemetry');
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

---

## 6. Phase 1 검증 기준 (Acceptance Criteria)

모든 항목이 ✅ 이어야 Phase 1 완료:

- [ ] ROV BlueOS 엔드포인트 → 맥미니 서버 **Connected** (도메인 기반)
- [ ] 서버 `rov_connected: true`
- [ ] 파일럿 PC 웹 브라우저에서 서버 접속 가능
- [ ] 텔레메트리 (배터리, 수심, 자세) 실시간 수신
- [ ] 조이스틱으로 ROV 제어 가능 (Arming, 이동, 모드 전환)
- [ ] 영상 스트림 수신 (영상 장비 있는 경우)
- [ ] LTE/인터넷 환경에서 제어 지연 < 200ms
- [ ] `verify_rov.py`로 독립 검증 성공
- [ ] ROV 재연결 시 자동 복원

---

## 7. 트러블슈팅

### 연결이 안 될 때
| 증상 | 원인 | 해결 |
|---|---|---|
| BlueOS 엔드포인트 "Disconnected" | 도메인/DNS 문제 또는 방화벽 | DNS 확인 (`nslookup rov.mars-company.com`), 5760 포트 포워딩 확인 |
| 서버 `rov_connected: false` | ROV 네트워크 미연결 | ROV 공유기 인터넷 연결 확인, BlueOS 웹 UI 접속 |
| 파일럿 브라우저 접속 불가 | 8000 포트 포워딩 미설정 | 공유기 포트 포워딩, 맥미니 방화벽 확인 |
| 텔레메트리 안 옴 | WebSocket 연결 실패 | 브라우저 콘솔 에러 확인, CORS 설정 확인 |
| 조이스틱 미인식 | Gamepad API 미지원 브라우저 | Chrome/Edge 사용, HTTPS 필요할 수 있음 |
| 제어 지연 > 200ms | 네트워크 품질 | LTE → 5G/Starlink 전환, VPN 최적화 |

### 네트워크 진단
```bash
# DNS 확인
nslookup rov.mars-company.com

# 서버 상태
curl http://rov.mars-company.com:8000/api/status

# ROV → 서버 연결 테스트 (ROV 네트워크에서)
curl http://rov.mars-company.com:8000/api/status
```

---

## 8. Phase 1 완료 후 다음 단계 (Phase 2)

Phase 1 검증 기준 모두 ✅ → **Phase 2 (다중 ROV + 직접 연결 모드)**:

1. ROV 2~3대 연결 설정 (모두 서버 도메인:5760)
2. 각 ROV `SYSID_THISMAV` 고유 설정 (1, 2, 3)
3. `GET /api/rovs/{sysid}/connection`에서 ROV 공인 IP 반환
4. 파일럿이 ROV에 직접 MAVLink 연결 (저지연, 서버 경유 X)
5. 관제센터는 텔레메트리 모니터링 + 비상 탈취만 담당
6. 웹 UI에 다중 ROV 선택 + 지도 표시

---

## 부록: 웹 UI (Phase 1 최소 기능)

Phase 1에서 제공하는 웹 UI (`frontend/index.html`):

- ROV 목록 + 연결 상태
- 선택한 ROV의 텔레메트리 (배터리, 수심, 자세, 모드)
- 제어권 획득/해제 버튼
- 조이스틱 상태 표시 (Gamepad API)
- Arm/Disarm 버튼

Phase 2+에서 Cockpit 수준의 UI로 확장.