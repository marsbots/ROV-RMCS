> 이 문서는 레거시 프로젝트 '수중드론(ROV) 통합관제시스템'에서 이식되었습니다. RMCS 현재 아키텍처(브로커+차량 에이전트)와 일부 다를 수 있으니 참고용으로 사용하세요.

# SITL 테스트 시나리오 (Phase 1~)

## Phase 1: 단일 ROV 원격 제어

### 시나리오 1.1: 기본 연결
- **전제**: ROV 1대 (ArduSub SITL) + BlueOS + mavlink-router
- **동작**: 관제센터 PC에서 Cockpit으로 연결
- **기대**: Heartbeat 수신, 텔레메트리 표시, Arm/Disarm 가능

### 시나리오 1.2: 조이스틱 제어
- **전제**: 시나리오 1.1 성공
- **동작**: 조이스틱으로 수평/수직 이동
- **기대**: ROV가 명령에 따라 움직임 (SITL에서는 가상 이동)

### 시나리오 1.3: 모드 전환
- **전제**: 시나리오 1.1 성공
- **동작**: MANUAL → STABILIZE → DEPTH_HOLD → MANUAL
- **기대**: 각 모드에서 정상 동작

### 시나리오 1.4: 로컬 ↔ 원격 전환
- **전제**: 로컬 노트북 + 관제센터 PC 모두 연결 가능
- **동작**: 한쪽 연결 → 다른쪽 연결 시도
- **기대**: 제어권이 마지막 연결자에게 이전

## SITL 환경 구축 (참고)

### ArduSub SITL 실행
```bash
# ArduPilot 소스에서
cd ardupilot
Tools/autotest/sim_vehicle.py -v ArduSub --console --map
```

### BlueOS + SITL 연동
BlueOS Companion 이미지에서 SITL 모드 활성화:
```
BlueOS Web UI → Vehicle → SITL → Enable
```

### SITL + mavlink-router
SITL이 UDP 14550으로 MAVLink를 출력하므로, 관제센터 PC에서 직접 수신 가능:
```
관제센터 PC Cockpit → UDP 14550 → SITL ROV
```

## Phase 2: 다중 ROV (추후)
- 시나리오 2.1: 2대 ROV 동시 연결
- 시나리오 2.2: ROV 선택 → 제어 전환
- 시나리오 2.3: 제어권 충돌 방지 (lock)
- 시나리오 2.4: 지도에 다중 ROV 위치 표시