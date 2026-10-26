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

- APP 메모리 대신 Redis에 세션 저장 → APP 교체·추가·제거 후에도 같은 세션을 읽도록 설계
- Sticky Session은 서버 교체와 부하 집중 문제로 제외
- DB 세션 저장은 수강신청 시점의 DB 부하를 고려해 제외

#### Redis 자체의 장애 대응

APP 서버끼리 세션을 공유하더라도 정작 Redis가 중단되면 모든 서버에서 로그인 정보를 확인할 수 없다는 문제

초기에는 단일 노드로 구성했으나 Redis도 장애에 대비해야 한다고 판단해 다른 AZ에 Replica를 추가하고 Multi-AZ 자동 장애 조치 활성화

- **Primary** — 로그인 정보를 저장하고 변경하는 주 노드
- **Replica** — Primary의 데이터를 복제해 두는 예비 노드
- **자동 장애 조치** — Primary 장애 시 Replica가 역할을 이어받도록 설정

변경 후 설정 화면은 캡처하지 못해 초기 단일 노드 구성 화면만 첨부

실제 Failover 테스트는 진행하지 않았으며 Redis의 비동기 복제로 인해 장애 직전 일부 데이터가 유실될 가능성 존재

### 2. 장애 서버는 ALB가 빼고 ASG가 교체

- ALB가 15초마다 `/health` 확인 → 2회 연속 실패 시 약 30초 안에 요청에서 제외
- ASG 상태 확인을 **EC2 + ELB**로 설정 → ALB가 비정상으로 본 서버도 자동 교체
- 최소 2대를 2a·2c에 분산

### 3. 수강신청 시간에 맞춘 확장

- 10:00 시작 가정 → 09:30 예약 증설, 12:00 축소
- 그 밖의 변화는 평균 CPU 60% 대상 추적으로 대응

### 4. 보안과 DB

- 보안 그룹은 바로 앞 계층만 소스로 허용, 인터넷 진입점은 Public ALB 하나
  - 요청 경로: `do-public-alb` → `do-web` → `do-internal-alb` → `do-app`
  - 세션 관리: `do-app` → `do-redis`
  - DB 연결: `do-app` → `do-rds-proxy` → `do-rds`
- 서버 접속은 SSM Session Manager (키 페어 X)
- DB(설계): RDS Proxy로 연결 재사용, 정원 초과는 `SELECT ... FOR UPDATE`, 중복 신청은 `UNIQUE(student_id, course_id)`로 방지

#### Redis 접근 경로와 실습 환경의 차이

실습에서는 두 WEB EC2가 같은 Redis 데이터를 조회하는지 확인하기 위해 Redis CLI로 직접 접속

반면 최종 설계에서는 APP 서버만 Redis에 접근하도록 제한하고 WEB 서버는 APP으로 요청을 전달하는 역할만 담당

- **실습** — `WEB → Redis` 직접 연결로 데이터 공유 확인
- **최종 설계** — `APP → Redis` 연결만 허용하도록 Redis의 TCP 6379 인바운드 규칙 제한

따라서 최종 보안 정책을 적용하면 실습에서 사용했던 WEB 서버의 Redis 직접 접근은 차단되는 구조

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

#### 네트워크 관점에서의 원인 분석

처음에는 NAT Gateway와 라우팅 테이블이 정상이라 네트워크 문제가 아닐 거라고 생각했으나 실제로는 EC2가 잘못된 서브넷에 생성된 것이 원인

Public Subnet에 생성된 EC2는 공인 IP가 없어 IGW로 직접 나갈 수 없었고 Private Subnet에 설정한 NAT Gateway 경로도 사용하지 못한 상태

**정상적인 외부 통신 경로**

`WEB EC2 → Private Route Table → NAT Gateway → Internet Gateway → 외부 패키지 저장소`

**해결** — ASG 서브넷을 `web-subnet-a·c`로 수정하고 기존 인스턴스 종료 (User Data 재실행을 위해 새로 생성)

![web-subnet-c에 새로 생성된 인스턴스](images/03_troubleshooting/03-6_instance-in-web-subnet.png)

![정상 호스트 0 → 2로 복구](images/03_troubleshooting/03-7_tg-health-graph.png)

**함께 발견한 문제** — ASG가 EC2 상태 확인만 쓰는 동안 Unhealthy 인스턴스가 04:22부터 04:37까지 교체되지 않음 → **ELB 상태 확인 추가**

![ASG 상태 확인 EC2 + ELB](images/04_failover/04-1_asg-health-check-type.png)


#### 추가 학습: ALB Health Check Reason Code

이번에는 `Health checks failed`를 확인한 뒤 EC2 로그와 네트워크 설정을 하나씩 살펴보며 원인 추적

리뷰를 통해 ALB가 Health Check 실패 이유를 구분하는 `Reason Code`를 제공한다는 점을 알게 됨

```bash
aws elbv2 describe-target-health \
  --target-group-arn <TARGET_GROUP_ARN>
```

- `Target.ResponseCodeMismatch` — 예상한 HTTP 응답 코드와 실제 응답이 다를 때
- `Target.Timeout` — 서버가 정해진 시간 안에 응답하지 않을 때

이번에는 직접 사용하지 못했지만 다음에 비슷한 문제가 발생한다면 Reason Code부터 확인해 볼 계획

---

## 검증

충족: 다중 AZ WEB 서버 배치·ALB 요청 분산·장애 인스턴스 자동 교체·Redis 데이터 공유

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


#### 검증 범위

- **검증 완료** — 서로 다른 WEB EC2에서 동일한 Redis 데이터 조회
- **구성 완료** — Redis Replica 추가 및 Multi-AZ 자동 장애 조치 활성화
- **미검증** — Primary 장애 발생 시 Replica 승격 및 실제 Failover 과정
- **미검증** — APP 로그인 세션 연동 및 장애 발생 중 로그인 유지

---

## 한계 및 향후 개선

- **고가용성 검증** — Redis Multi-AZ 자동 장애 조치와 WEB 서버 장애 상황에서의 실제 서비스 복구 과정 확인
- **트래픽 대응 검증** — CPU 부하에 따른 Auto Scaling 동작과 요청 분산 효과 확인
- **DB 병목 및 정합성** — APP 서버 확장에 따른 DB 연결 증가와 동시 수강신청 시 데이터 정확성 검증

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

**05-2** Redis 초기 단일 노드 구성

![Redis 초기 구성](images/05_session-redis/05-2_redis-config.png)

</details>