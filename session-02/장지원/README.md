# Session 02 · Assignment

**제출자** 장지원

수강신청 포털용 WEB·APP·DB 3-Tier 아키텍처 설계 · **WEB 계층과 세션 저장소(Redis)만 서울 리전에 실제 구축**

![아키텍처](images/architecture.png)

> 실선은 구축, 점선은 설계만 한 리소스

- **구축**: VPC(2 AZ × 4계층 서브넷 8개), Public ALB, WEB ASG(EC2 2대), ElastiCache Redis
- **설계만**: Internal ALB, APP ASG, RDS Proxy, RDS MySQL Multi-AZ

---

## 핵심 설계

### 1. 세션은 Redis에 저장 → 서버가 바뀌어도 같은 세션

- APP 메모리 대신 Redis에 세션 저장 → APP 교체·추가·제거 후에도 같은 세션을 읽음
- Sticky session은 서버 교체와 부하 집중 문제로 제외
- DB 세션 저장은 수강신청 시점의 DB 부하 때문에 제외

### 2. 장애 서버는 ALB가 빼고 ASG가 교체

- ALB가 15초마다 `/health` 확인 → 2회 연속 실패 시 약 30초 안에 요청에서 제외
- ASG 상태 확인을 **EC2 + ELB**로 설정 → ALB가 비정상으로 본 서버도 자동 교체
- 최소 2대를 2a·2c에 분산

### 3. 수강신청 시간에 맞춘 확장

- 10:00 시작 가정 → 09:30 예약 증설, 12:00 축소
- 그 밖의 변화는 평균 CPU 60% 대상 추적으로 대응

### 4. 보안과 DB

- 보안 그룹은 바로 앞 계층만 소스로 허용, 인터넷 진입점은 Public ALB 하나
  `do-public-alb` → `do-web` → `do-internal-alb` → `do-app` → `do-redis` / `do-rds-proxy` → `do-rds`
- 서버 접속은 SSM Session Manager (키 페어 X)
- DB(설계): RDS Proxy로 연결 재사용, 정원 초과는 `SELECT ... FOR UPDATE`, 중복 신청은 `UNIQUE(student_id, course_id)`로 방지

---

## 트러블슈팅: 대상 그룹 Unhealthy

**증상** — ALB를 붙인 뒤 `tg-web`의 두 대상이 모두 `Health checks failed`

![대상 그룹 정상 0 / 비정상 2](images/03_troubleshooting/03-3_tg-unhealthy-summary.png)

**추적**

1. 성공 코드를 `200-399`로 넓힘 (nginx의 `/health` → `/health/` 301 의심) → 그대로
2. EC2 시스템 로그 → 저장소·SSM 연결 타임아웃, `No match for argument: nginx` → **nginx 설치 자체가 실패**
3. NAT Gateway와 private 라우팅 테이블 → 정상
4. 인스턴스 IP 10.0.2.216 → **web 서브넷이 아니라 `public-subnet-c`에 공인 IP 없이 생성**

![public-subnet-c에 공인 IP 없이 생성된 인스턴스](images/03_troubleshooting/03-4_instance-in-public-subnet.png)

**원인** — ASG에 public 서브넷을 지정. 공인 IP가 없어 IGW로 나갈 수 없고 NAT 경로도 타지 않아 외부 통신이 불가

**해결** — ASG 서브넷을 `web-subnet-a·c`로 수정하고 기존 인스턴스 종료 (User Data 재실행을 위해 새로 생성)

![web-subnet-c에 새로 생성된 인스턴스](images/03_troubleshooting/03-6_instance-in-web-subnet.png)

![정상 호스트 0 → 2로 복구](images/03_troubleshooting/03-7_tg-health-graph.png)

**함께 발견한 문제** — ASG가 EC2 상태 확인만 쓰는 동안 Unhealthy 인스턴스가 04:22부터 04:37까지 교체되지 않음 → **ELB 상태 확인 추가**

![ASG 상태 확인 EC2 + ELB](images/04_failover/04-1_asg-health-check-type.png)

---

## 검증

충족: 다중 AZ 배치·계층별 접근 제어, 요청 분산, 장애 감지·제외, 장애 인스턴스 자동 교체, 세션 저장소 공유, 서버 수 조정, 동적 확장, 예약 확장, 로그인 유지

### 요청 분산

ALB 주소로 반복 접속 → 2a(10.0.11.100)와 2c(10.0.12.23)의 호스트명이 번갈아 응답

![ALB 요청 분산](images/02_web-tier/02-3_alb-round-robin.png)

### 장애 인스턴스 자동 교체

실행 중인 WEB 인스턴스 하나를 종료 → **2초 뒤** 대체 인스턴스 시작

![ASG 활동 기록](images/04_failover/04-2_asg-activity-replacement.png)

### 세션 저장소 공유

인스턴스 A에서 `session:test`를 SET → 인스턴스 B에서 같은 값을 GET

![인스턴스 A](images/05_session-redis/05-3_redis-instance-a.png)

![인스턴스 B](images/05_session-redis/05-4_redis-instance-b.png)

> 두 서버의 Redis 공유까지만 확인. **앱 로그인 세션 연동은 미검증**

---

## 한계 및 향후 개선

- **가용성 강화:** NAT Gateway를 AZ별로 구성해 특정 가용 영역의 장애가 전체 서비스에 미치는 영향 최소화
- **장애 대응 검증:** nginx 중지 등 간단한 장애 상황을 재현해 ALB의 Health Check 및 트래픽 전환 확인
- **Auto Scaling 검증:** CPU 부하에 따라 서버가 자동으로 확장·축소되는 과정 확인
- **DB 병목 개선:** 동시 접속 증가 시 APP 서버 확장만으로 해결하기 어려운 DB 병목과 데이터 정합성 문제 추가 학습

이번에는 인프라 구성 자체에 집중했다면 향후에는 **구축한 아키텍처가 실제 장애와 트래픽 증가 상황에서 어떻게 동작하는지 검증하는 것**을 다음 학습 목표로!

---

<details>
<summary>기타 화면</summary>

**01-1** NAT Gateway

![NAT Gateway](images/01_network/01-1_nat-gateway.png)

**01-2** 라우팅 테이블 — Public 2개는 IGW, Private 6개는 NAT

![라우팅 테이블](images/01_network/01-2_route-tables.png)

**02-1** 시작 템플릿

![시작 템플릿](images/02_web-tier/02-1_launch-template.png)

**02-2** ASG 용량

![ASG 용량](images/02_web-tier/02-2_asg-capacity.png)

**02-4** 대상 그룹 정상

![대상 그룹 정상](images/02_web-tier/02-4_tg-healthy.png)

**03-1** ASG 최초 생성

![ASG 최초 생성](images/03_troubleshooting/03-1_asg-initial-ec2-healthy.png)

**03-2** 대상 Unhealthy

![대상 Unhealthy](images/03_troubleshooting/03-2_tg-unhealthy-targets.png)

**03-5** 교체 중

![교체 중](images/03_troubleshooting/03-5_tg-replacing.png)

**05-1** Redis 서브넷 그룹

![Redis 서브넷 그룹](images/05_session-redis/05-1_redis-subnet-group.png)

**05-2** Redis 구성

![Redis 구성](images/05_session-redis/05-2_redis-config.png)

</details>