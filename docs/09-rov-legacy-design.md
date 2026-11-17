> 이 문서는 레거시 프로젝트 '수중드론(ROV) 통합관제시스템'에서 이식되었습니다. RMCS 현재 아키텍처(브로커+차량 에이전트)와 일부 다를 수 있으니 참고용으로 사용하세요.

# 수중드론(ROV) 통합관제시스템 — 기술 검토 및 구현 계획

> 작성일: 2026-10-21
> 작성자: Sisyphus (AI Agent)
> 대상: 임요준 (마르스)
> 경로: `B:\OneDrive - 마르스\문서 - 마르스\검토\임요준\드론\수중드론(ROV)\통합관제시스템`

---

## 1. 프로젝트 개요

### 1.1 목표

여러 대의 ROV(수중드론)를 **개별 조종이 아닌 통합관제**로 운영하기 위한 시스템을 구축한다.

**1단계 (현재 목표)**:
- 여러 대의 ROV가 통합관제시스템에 원격 접속된 상태
- 파일럿이 원하는 ROV를 선택하여 **로컬에서 조종하듯이** 원격에서 직접 조이스틱으로 제어
- ROV 선택 → 해당 ROV의 영상/텔레메트리/조이스틱 제어가 로컬과 동일하게 동작
- 로컬 제어와 원격 제어는 **상호 배타적** (한 번에 하나만 활성)

**추후 확장 (2단계 이후)**:
- 그룹 경로 비행, 멀티 경로 비행
- AI 자율 비행
- 임무 자동화 (구역 분할 탐색, 그리드 스캔)
- 부표(Surface Buoy) 기반 통신 중계

### 1.2 현재 보유 자산

- ROV: ArduSub + Pixhawk 기반 (MAVLink 프로토콜)
- GCS: QGroundControl, Cockpit (PC 포팅 진행 중)
- 통신: tether (Ethernet) + IP 기반
- 네트워크: 사무실/수중시설 내부 LAN

### 1.3 핵심 결정사항

| 결정 | 내용 | 근거 |
|---|---|---|
| 펌웨어 | **ArduSub 유지** | 수중 특화 기능, Blue Robotics 생태계, 커뮤니티 |
| 프로토콜 | **MAVLink 유지** | ArduSub 기본 프로토콜, 오픈소스 GCS 호환 |
| Companion OS | **BlueOS** | MAVLink 라우팅/영상/웹UI 내장, OTA 업데이트 |
| 관제센터 SW | **Cockpit 포팅 + 커스텀 백엔드** | ROV 특화 UI 활용, 다중차량 선택 로직 추가 |
| 통신 레이어 | **mavlink-router (BlueOS 내장)** | 설정만 변경, 기체 소프트웨어 변경 불필요 |
| 검증 환경 | **로컬 LAN 먼저** | 부표/LTE 없이 사무실 네트워크에서 검증 |

---

## 2. 오픈소스 통합관제 후보 분석

### 2.1 후보 비교

| 프로젝트 | 프로토콜 | 동시 다중 제어 | ROV 특화 | 웹 기반 | 라이선스 | 성숙도 | 적합성 |
|---|---|---|---|---|---|---|---|
| **Neptus (LSTS)** | IMC | ✅ | 부분 | ❌ (Java) | EUPL v1.1 | ★★★★★ | ❌ IMC 프로토콜, MAVLink 비호환 |
| **Karshipta** | MAVLink | ✅ (임무) | ❌ | ✅ | 오픈소스 | ★★☆☆☆ | △ 임무 디스패치 중심, 실시간 수동 제어 불명확 |
| **WebGCS** | MAVLink | △ (선택 제어) | ❌ | ✅ | LGPL-3.0 | ★★☆☆☆ | △ 기본 수준, 영상/임무 부족 |
| **QGroundControl** | MAVLink | ❌ (1대씩) | 부분 | ❌ | Apache-2.0 | ★★★★☆ | ❌ 동시 제어 불가, 영상 1스트림 |
| **Cockpit (Blue Robotics)** | MAVLink | ❌ | ✅✅ | ✅ | AGPL-3.0 | ★★★★☆ | △ 단일기체 최적화, 다중 표시 개발 중 |
| **OpenMACE** | 커스텀 | ✅ | ❌ | 부분 | 오픈소스 | ★★☆☆☆ | ❌ 프레임워크 수준, 제품 아님 |

### 2.2 결론

**완전한 오픈소스 통합관제 시스템은 존재하지 않습니다.** 자체 구축이 필요하며, Cockpit 포팅 + MAVSDK 백엔드 + 커스텀 UI로 구현합니다.

---

## 3. 프로토콜 및 플랫폼 이해

### 3.1 MAVLink이란?

MAVLink(Micro Air Vehicle Link)는 **ArduPilot/PX4 생태계의 공통 통신 프로토콜**입니다.

- Pixhawk, Cube, mRo 등 MAVLink 호환 보드에서 동작
- ArduSub, ArduCopter, ArduPlane, ArduRover, PX4 등이 모두 MAVLink 사용
- 메시지 기반: 텔레메트리, 명령, 임무, 상태 등을 정의된 메시지로 교환
- UDP/TCP/Serial 위에서 동작

### 3.2 다른 프로토콜

| 프로토콜 | 사용처 | 특징 |
|---|---|---|
| **MAVLink** | ArduPilot, PX4 | 가장 보편적 오픈소스 드론 프로토콜 |
| **IMC** | LSTS/DUNE (포르투 대학) | 학술 연구용. Neptus C4I와 연동. MAVLink와 비호환 |
| **ROS messages** | ROS/ROS2 기반 시스템 | 연구·자율주행. MAVLink와 별개 생태계 |
| **상용 프로토콜** | DJI, Autel 등 | 폐쇄적, SDK를 통해서만 접근 |

### 3.3 ArduPilot vs PX4

| | ArduPilot | PX4 |
|---|---|---|
| 시작 | 2009 (Chris Anderson) | 2011 (Dronecode/Linux Foundation) |
| 관리 | 커뮤니티 (ArduPilot.org) | Linux Foundation (Dronecode) |
| 차량 유형 | Copter, Plane, Rover, Sub, Blimp, Boat, 등 | 주로 Copter, Plane, VTOL |
| 수중 지원 | ✅ ArduSub (성숙) | ❌ 제한적 |
| 아키텍처 | 모놀리식 (하나의 코드베이스) | 모듈화 (uORB, 모듈 분리) |
| 라이선스 | GPLv3 | BSD-3-Clause |
| 프로토콜 | MAVLink | MAVLink |

**역사**: 원래 ArduPilot이 먼저 시작되었고, PX4는 Dronecode 프로젝트로 분리되었습니다. 둘 다 MAVLink를 사용하지만 코드베이스는 완전히 다릅니다.

**ROV에는 ArduSub이 압도적으로 유리합니다.** PX4는 항공 중심이며 수중 기능이 부족합니다.

### 3.4 오토파일럿 보드란?

오토파일럿 보드는 **비행/항법 제어 컴퓨터**입니다.

- **하드웨어**: Pixhawk, Cube, mRo 등 = 오토파일럿 보드
- **펌웨어**: ArduSub, PX4 = 보드 위에서 도는 소프트웨어
- **비유**: PC(하드웨어) + Windows/Linux(OS) + 앱(펌웨어)

ArduSub/PX4는 MAVLink를 기반으로 만들어졌으며, MAVLink 호환 보드라면 어떤 보드든 동작합니다.

### 3.5 Pixhawk 외 보드 선택지

| 보드 | 제조사 | 특징 | 용도 |
|---|---|---|---|
| **Pixhawk 4** | Holybro | 가장 보편적, 커뮤니티 지원 최다 | 일반 |
| **Pixhawk 6C/6X** | Holybro | H7 프로세서, 더 많은 I/O | 고성능 |
| **Cube Orange/Black** | CubePilot | 산업용, 고신뢰성, 더 비쌈 | 상업/산업 |
| **mRo Pixracer** | mRobotics | 소형, RPi 조합 용이 | 소형 ROV |
| **Pixhawk Mini** | 3DR | 소형 | 소형 ROV |

**Pixhawk vs Cube**: 오픈소스 오토파일럿 보드의 양대산맥. Pixhawk가 더 보편적이고 저렴, Cube가 더 산업용/고신뢰성.

### 3.6 Companion PC (온보드 컴퓨터)

| 하드웨어 | AI 적합성 | BlueOS 지원 | 비고 |
|---|---|---|---|
| **Raspberry Pi 4/5** | ⭐⭐ (Coral TPU 추가 가능) | ✅ 공식 | 가장 보편적, 저비용 |
| **NVIDIA Jetson Nano/Orin** | ⭐⭐⭐⭐⭐ | ✅ 커뮤니티 | AI/엣지 컴퓨팅 표준 |
| **Intel NUC** | ⭐⭐⭐⭐ | ✅ (x86) | 고성능, OpenVINO |
| **Orange Pi** | ⭐⭐ | ✅ 커뮤니티 | 저비용 대안 |

**AI 관점**: 기존 업체/개발자들은 AI 기능을 위해 **NVIDIA Jetson**을 표준으로 고려합니다. Jetson은 CUDA 기반 GPU 가속으로 실시간 영상 분석, 객체 인식 등이 가능합니다. RPi는 Coral TPU나 Hailo 가속기를 추가하여 AI를 구현하기도 합니다.

### 3.7 Companion 이미지

| 이미지 | 상태 | 설명 |
|---|---|---|
| **BlueOS** | ✅ 현재 표준 | Docker 기반, 모듈형, Extension 지원, 웹 UI |
| **Companion (구버전)** | ❌ deprecated | BlueOS 이전. MAVLink 라우팅 + 영상. 웹 UI 없음 |
| **커스텀 Linux** | ✅ 가능 | Ubuntu/RPi OS + mavlink-router + GStreamer 직접 구성 |

**BlueOS와 Companion의 차이**: 기능적으로 유사 (MAVLink 라우팅, 영상 스트리밍)하지만, BlueOS는 Docker 기반 모듈형 구조로 Extension, OTA 업데이트, 웹 설정 UI 등을 제공합니다. Companion은 단순 라우팅 + 영상만 제공합니다.

### 3.8 영상 스트리밍: UDP vs WebRTC

| 방식 | 장점 | 단점 | 관제센터 적합성 |
|---|---|---|---|
| **UDP RTP** | 지연 낮음 | NAT/방화벽 통과 어려움, 브라우저 직접 수신 불가 | △ |
| **WebRTC** | 브라우저 직접 수신, NAT 통과 쉬움 | 지연 약간 더 높음 | ✅ |

관제센터가 웹 기반(Cockpit 포팅)이므로 **WebRTC가 유리**합니다. BlueOS는 GStreamer → WebRTC 변환을 자동으로 처리합니다.

### 3.9 ROS vs MAVLink

ROS(Robot Operating System)는 MAVLink와 **완전히 다른 생태계**입니다.

- ROS 기반 시스템은 MAVLink와 별개의 메시지 체계 사용
- ROS → MAVLink 브릿지(ros_mavlink, mavros)로 연동 가능하지만 복잡
- BlueOS 내에서 ROS 컨테이너 실행 가능 (Extension/Docker)
- **기존 ArduSub 시스템에서는 MAVLink 유지가 훨씬 효율적**
- ROS는 자율주행/AI 연구에서 주로 사용. 1단계(원격 수동 제어)에서는 불필요

---

## 4. 시스템 아키텍처

### 4.1 전체 구조

```
[관제센터 PC]
├── 통합관제 SW (커스텀 개발)
│   ├── ROV 레지스트리 (연결된 ROV 목록, IP, SYSID, 상태)
│   ├── 상태 모니터 (heartbeat, 배터리, 수심, 통신 품질)
│   ├── 지도 뷰 (모든 ROV 위치 마커)
│   ├── ROV 선택기 (클릭 → 선택)
│   ├── Cockpit 뷰 (선택된 ROV의 영상/조이스틱/텔레메트리)
│   └── 제어권 관리 (선택된 ROV에만 명령 전송)
├── MediaMTX (영상 RTSP → WebRTC 변환)
└── FastAPI + WebSocket (백엔드)

        │ LAN / 인터넷 / LTE / VPN

[ROV #1] (ArduSub + Pixhawk + BlueOS on RPi)
  └── mavlink-router → 관제센터 IP:포트

[ROV #2] (ArduSub + Pixhawk + BlueOS on RPi)
  └── mavlink-router → 관제센터 IP:포트

[ROV #N] ...
```

### 4.2 BlueOS MAVLink 라우팅 설정

BlueOS 웹 UI에서:

```
MAVLink Proxy → Add Endpoint
  Type: TCP Client (또는 UDP)
  Address: 관제센터_IP (또는 도메인)
  Port: 5760
```

**IP 대신 도메인 사용 가능** — 동적 IP 환경에서 유용합니다.

### 4.3 로컬 ↔ 원격 제어 전환

```
[모드 A: 로컬 제어]
ROV ↔ 노트북 (직접 tether/IP)
→ Cockpit이 ROV에 직접 연결

[모드 B: 원격 제어]
ROV ↔ 네트워크 ↔ 관제센터 PC
→ Cockpit이 관제센터에서 ROV에 연결

A와 B는 동시에 활성화되지 않음.
원격이면 로컬은 끊기고, 로컬이면 원격은 끊김.
```

이것은 **제어권 전환(Control Handoff)** 패턴입니다. ArduPilot에서 GCS 간 제어권 전환이 기본 지원됩니다 (MAVLink GCS ID).

### 4.4 관제센터 SW 기술 스택 (제안)

| 구성요소 | 기술 | 역할 |
|---|---|---|
| 백엔드 | Python FastAPI + WebSocket | MAVLink 수신, 상태 관리, 제어 명령 전달 |
| MAVLink 처리 | MAVSDK 또는 pymavlink | 다중 기체 접속, 텔레메트리, 명령 |
| 영상 | MediaMTX | RTSP → WebRTC 변환 |
| 프론트엔드 | Cockpit 포팅 버전 (Vue/TS) | 선택된 ROV의 제어 UI |
| 지도 | MapLibre / Leaflet | ROV 위치 표시 |
| DB | SQLite / TimescaleDB | 로그, 이벤트 |

---

## 5. 구현 로드맵

### Phase 1: 단일 ROV 원격 제어 검증 (1~2주)

```
1. ROV 1대 + BlueOS에서 mavlink-router 설정
   → 관제센터 IP:포트 엔드포인트 추가
2. 관제센터 PC에서 Cockpit 실행
   → ROV 원격 연결 확인
3. 조이스틱 제어 테스트
   → 로컬 노트북 연결 끊은 상태에서 원격 제어
4. 로컬 ↔ 원격 전환 테스트
   → 연결/해제 시 제어권 전환 확인
```

**검증 기준**: 관제센터 PC에서 ROV를 로컬처럼 제어 가능

### Phase 2: 다중 ROV 연결 (2~3주)

```
1. ROV 2~3대 각각 BlueOS mavlink-router 설정
2. 관제센터 백엔드 개발
   → MAVSDK로 다중 기체 접속
   → WebSocket으로 상태 브로드캐스트
3. ROV 선택 → Cockpit 뷰 전환 로직
4. 지도에 다중 ROV 위치 표시
```

**검증 기준**: 관제센터에서 ROV 선택 → 해당 ROV 제어 가능

### Phase 3: 통합관제 UI (3~4주)

```
1. ROV 레지스트리 + 상태 모니터 UI
2. 영상 스트림 선택적 표시 (MediaMTX)
3. 제어권 lock mechanism
4. 경보/이벤트 시스템
```

**검증 기준**: 교통관제센터처럼 다중 ROV 모니터/제어 가능

### Phase 4: 부표 + LTE 연동 (추후)

```
1. 부표 하드웨어 (배터리, LTE, 스위치, GPS)
2. mavlink-router를 부표에서 실행
3. 관제센터 ↔ 부표 ↔ ROV 전체 체인 검증
```

---

## 6. 난이도 평가

| 항목 | 난이도 | 비고 |
|---|---|---|
| MAVLink 라우팅 설정 (BlueOS) | ⭐ 쉬움 | 웹 UI, 10분 |
| 단일 ROV 원격 제어 | ⭐ 쉬움 | Cockpit 사용 |
| 다중 ROV 상태 모니터링 | ⭐⭐ 보통 | 백엔드 + WebSocket |
| ROV 선택 → 제어 전환 | ⭐⭐ 보통 | 프론트엔드 로직 |
| 통합관제 UI (지도+영상+상태) | ⭐⭐⭐ 도전적 | 커스텀 개발 |
| 제어권 lock / 충돌 방지 | ⭐⭐ 보통 | 백엔드 로직 |
| 부표 + LTE 연동 | ⭐⭐⭐⭐ 고난도 | 하드웨어 + 네트워크 |

---

## 7. 핵심 Q&A 요약

| 질문 | 답변 |
|---|---|
| Neptus 사용 시 기체 변경? | ✅ yes. IMC 프로토콜, DUNE 필요. ArduSub와 비호환 |
| Karshipta MAVLink = Pixhawk? | ❌ No. MAVLink 프로토콜. 하지만 실시간 수동 제어는 불명확 |
| WebGCS/QGC 다중 모니터링? | ✅ 모니터링은 됨. 하지만 동시 제어는 안 됨 |
| OpenMACE 구현 완료? | ❌ 프레임워크 수준. 제품 아님 |
| 1단계 요구사항 오픈소스 존재? | ❌ 없음. 자체 구축 필요 |
| Cockpit 포팅 활용? | ✅ yes. "선택된 ROV 뷰"로 활용 가능 |
| ArduSub vs PX4? | ArduSub 유지. PX4는 수중 기능 부족 |
| BlueOS vs Companion? | BlueOS가 표준. Docker 기반, Extension, OTA |
| Pixhawk 외 보드? | Cube, mRo 등 다양. 펌웨어만 같으면 동일 |
| Companion PC? | RPi 표준, Jetson (AI), NUC (고성능) |
| MAVLink Router 설치? | BlueOS 사용 시 설정만 변경 |
| IP 대신 도메인? | ✅ 가능 |
| ROS 사용? | 별개 생태계. 1단계에서 불필요. MAVLink 유지 |
| 통합관제 SW 개발 가능? | ✅ 가능. 통신은 쉽고, UI 개발이 핵심 공수 |
| 교통관제센터 비유? | ✅ 정확함. CCTV 연결은 쉬워도 통합 SW는 개발 필요 |

---

## 8. 새 프로젝트 시작 가이드

### 8.1 세션 시작 방법

이 문서를 기반으로 새 프로젝트 세션을 시작할 때:

```
1. 이 문서(통합관제시스템.md)를 Read로 읽기
2. Phase 1부터 시작: ROV 1대 + BlueOS mavlink-router 설정
3. 관제센터 PC에서 Cockpit으로 원격 제어 테스트
4. 결과에 따라 Phase 2로 진행
```

### 8.2 새 세션 첫 메시지 예시

```
"통합관제시스템 프로젝트를 시작합니다.
B:\OneDrive - 마르스\문서 - 마르스\검토\임요준\드론\수중드론(ROV)\통합관제시스템\통합관제시스템.md
문서를 읽고 Phase 1부터 진행해주세요.
현재 ROV 1대(ArduSub+Pixhawk+BlueOS), 관제센터 PC, LAN 환경이 준비되어 있습니다."
```

### 8.3 프로젝트 폴더 구조 (제안)

```
통합관제시스템/
├── 통합관제시스템.md          ← 이 문서 (기술 검토 + 계획)
├── README.md                   ← 프로젝트 개요
├── backend/                    ← FastAPI + MAVSDK 백엔드
├── frontend/                   ← Cockpit 포팅 + 통합관제 UI
├── config/                     ← BlueOS 설정, mavlink-router 설정
├── docs/                       ← 추가 문서
└── tests/                      ← SITL 테스트 시나리오
```

### 8.4 핵심 참고 자료

- BlueOS: https://github.com/bluerobotics/blueos
- Cockpit: https://github.com/bluerobotics/cockpit
- MAVSDK: https://mavsdk.mavlink.io/
- mavlink-router: https://github.com/mavlink-router/mavlink-router
- MediaMTX: https://github.com/bluenviron/mediamtx
- ArduSub: https://www.ardusub.com/
- Karshipta (참고): https://github.com/NIKX-Tech/karshipta

---

## 9. 부록: 하드웨어 구성 권장안

### 9.1 ROV (기체)

```
- 오토파일럿: Pixhawk 4/6C 또는 Cube Orange
- 펌웨어: ArduSub
- Companion PC: Raspberry Pi 4/5 (또는 Jetson Nano for AI)
- OS: BlueOS
- 센서: 카메라, 소나(선택), DVL(선택), CTD(선택), 누수센서
- 통신: Ethernet 테더 (100m~300m)
```

### 9.2 관제센터

```
- PC: 미니 PC 또는 노트북 (Windows/Linux)
- SW: Cockpit 포팅 + 커스텀 통합관제 SW
- 네트워크: LAN (사무실) → LTE/VPN (원격)
- 디스플레이: 듀얼 모니터 권장 (지도 + 영상)
```

### 9.3 부표 (추후)

```
- 본체: IP67/IP68 밀폐 케이스
- 전원: LiFePO4 배터리 + BMS + DC-DC
- 통신: LTE 모뎀 (주) + Wi-Fi/MESH (백업)
- 네트워크: 관리형 Ethernet 스위치
- 위치: GNSS/RTK
- 관제 노드: Raspberry Pi + BlueOS
- 안전: 누수센서, 내부 온도/습도, 비상 차단, 위치 비컨
```

---

> **이 문서는 2026-10-21 세션의 기술 검토 결과를 정리한 것입니다.**
> **새 프로젝트 세션에서는 이 문서를 먼저 읽고 Phase 1부터 진행하세요.**
