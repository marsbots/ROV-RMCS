> 이 문서는 레거시 프로젝트 '수중드론(ROV) 통합관제시스템'에서 이식되었습니다. RMCS 현재 아키텍처(브로커+차량 에이전트)와 일부 다를 수 있으니 참고용으로 사용하세요.

# BlueOS MAVLink Proxy 설정 가이드 (수정판)

## 개요
BlueOS는 Docker 기반 companion OS로, MAVLink 라우팅을 내장하고 있습니다.
관제센터가 **단일 게이트웨이** 역할을 하므로, ROV는 관제센터에만 연결합니다.

## 핵심 원칙
- ROV → 관제센터 백엔드 (TCP 5760) **단일 엔드포인트만** 추가
- 기존 로컬 엔드포인트 (UDP 14550) **제거** — 보안을 위해 ROV는 관제센터에만 연결
- 로컬 제어가 필요하면 관제센터 백엔드를 경유 (Cockpit → 백엔드 TCP 5761)

## 설정 경로
```
BlueOS Web UI → http://<ROV_IP>
  → 좌측 사이드바 "MAVLink" (또는 "MAVLink Endpoints" / "MAVLink Proxy")
  → "Endpoints" 탭
```

## 엔드포인트 설정 (Phase 1)

기존 엔드포인트를 모두 제거하고, 다음 하나만 추가:

| 필드 | 값 |
|---|---|
| Type | **TCP Client** |
| Address | `<관제센터_PC_IP>` |
| Port | `5760` |

## 다중 엔드포인트 (Phase 2+)

ROV는 여전히 관제센터에만 연결합니다. 다중 ROV는 각각 동일한 관제센터 IP:5760으로 연결:
```
ROV #1 (SYSID=1) → TCP Client → 관제센터:5760
ROV #2 (SYSID=2) → TCP Client → 관제센터:5760
ROV #3 (SYSID=3) → TCP Client → 관제센터:5760
```

백엔드가 SYSID로 각 ROV를 구분합니다.

## IP 대신 도메인 사용

동적 IP 환경 (LTE/VPN)에서는 도메인 사용:
```
Address: control-center.mars-rov.local   (mDNS)
Address: gcs.mars-rov.com               (DNS)
```

## SYSID 고유성 (중요)

여러 ROV가 동일 네트워크에 있을 때, 각 ROV의 `SYSID_THISMAV` (ArduSub 파라미터)가 고유해야 합니다:
- ROV #1: SYSID_THISMAV = 1
- ROV #2: SYSID_THISMAV = 2
- ROV #3: SYSID_THISMAV = 3

설정: BlueOS → Parameters → SYSID_THISMAV

## mavlink-router 설정 파일 (BlueOS 없이 커스텀 Linux 사용 시)

`/etc/mavlink-router/main.conf`:
```ini
[General]
TcpServerPort = 5760
ReportStats = false

[UartEndpoint pixhawk]
Device = /dev/ttyACM0
Baud = 115200

[TcpEndpoint control_center]
Mode = Client
Address = 192.168.1.100
Port = 5760
```

## 관제센터 백엔드 포트 정리

| 포트 | 용도 | 연결 주체 |
|---|---|---|
| 5760 | ROV MAVLink 수신 | ROV → 백엔드 |
| 5761 | Cockpit MAVLink 브릿지 | Cockpit → 백엔드 |
| 8000 | REST API + WebSocket | 모니터 → 백엔드 |

## 보안 참고

- ROV는 관제센터에만 연결하므로, 관제센터 PC의 방화벽/인증이 보안의 핵심입니다.
- 향후 Phase 3에서 파일럿 인증 (로그인) + 제어권 lock mechanism 추가 예정.
- LTE/VPN 환경에서는 WireGuard/OpenVPN으로 터널링 권장.