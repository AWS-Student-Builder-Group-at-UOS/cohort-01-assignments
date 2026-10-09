# Session 02 · Assignment

제출자 장지원

## 구축

WEB·APP·DB 3-Tier 아키텍처 설계함
WEB 계층과 세션 저장소는 서울 리전에 구축함

VPC 구축함
서브넷 8개 구성함
2 AZ × 4계층임
IGW와 라우팅 테이블 구성함
보안 그룹 7개 구성함
Public ALB와 대상 그룹 구성함
시작 템플릿과 WEB ASG 구성함
EC2 2대 구성함
ElastiCache Redis 단일 노드 구성함
NAT Gateway는 검증 후 삭제함

Internal ALB와 APP ASG는 설계만 함
RDS Proxy와 RDS MySQL Multi-AZ는 비용 때문에 구축하지 않음

학생 → Internet Gateway → Public ALB → WEB EC2 → Internal ALB → APP EC2 → RDS Proxy → RDS MySQL 순서임
APP 세션은 Redis에 저장하도록 설계함

서비스 앱 코드는 구현하지 않음
WEB 인스턴스는 nginx로 호스트명과 `/health`만 응답함
로그인 유지는 검증하지 못함
두 서버의 Redis 공유만 확인함

![아키텍처](images/architecture.png)

실선은 구축한 리소스임
점선은 설계만 한 리소스임

## 설계

세션은 APP 메모리 대신 Redis에 저장함
APP 교체와 추가 및 제거 후에도 같은 세션을 읽는 구성임
ALB 고정 세션 sticky session은 서버 교체와 부하 집중 문제로 제외함
DB 세션 저장은 수강신청 시점의 DB 부하 때문에 제외함

ALB는 15초마다 `/health`를 확인함
2회 연속 실패 시 약 30초 뒤 요청에서 제외함
ASG 상태 확인은 EC2 + ELB임
최소 2대를 2a·2c에 나눠 배치함

수강신청 시작은 10:00로 가정함
09:30 증설과 12:00 축소를 예약함
평균 CPU 60% 대상 추적으로 변화에 대응하도록 설계함
워밍업은 120초임
예약 조정과 대상 추적은 미검증임

보안 그룹은 앞 계층만 소스로 허용함
`do-public-alb` → `do-web` → `do-internal-alb` → `do-app` → `do-redis` / `do-rds-proxy` → `do-rds` 순서임
보안 그룹 이름에 `sg-`를 사용할 수 없어 `do-`를 사용함
인터넷 진입 경로는 Public ALB 하나임
서버 접속은 SSM Session Manager를 사용함

APP 증가에 따른 DB 연결은 RDS Proxy로 재사용하도록 설계함
정원 초과는 트랜잭션과 행 잠금 `SELECT ... FOR UPDATE`로 막음
중복 신청은 `UNIQUE(student_id, course_id)`로 막음
Multi-AZ 대기 인스턴스는 장애 조치용임
읽기 분산용이 아님

## 설정

VPC는 10.0.0.0/16임
서브넷은 /24 × 8임
계층별 10.0.1x·2x·3x 대역임

public 라우팅은 IGW임
private 6개 라우팅은 NAT임
NAT Gateway는 public-subnet-a 1개임
운영 환경에서는 AZ별 1개 권장임

시작 템플릿은 Amazon Linux 2023임
인스턴스 유형은 t3.micro임
보안 그룹은 `do-web`임
SSM 역할을 사용함
키 페어 없음

상태 확인 경로는 `/health`임
간격은 15초임
제한은 5초임
임계값은 2/2임
성공 코드는 200-399임
nginx의 `/health` → `/health/` 301 리다이렉트를 포함한 값임

등록 취소 지연은 300초임
분산 방식은 라운드 로빈 기본값임
운영 환경에서는 30~60초 권장임

WEB ASG는 최소 2 / 원하는 2 / 최대 4임
서브넷은 web-subnet-a·c임
ASG 상태 확인은 EC2 + ELB임
유예 시간은 180초임

대상 추적은 CPU 60%임
워밍업은 120초임
예약 조정은 09:30 증설과 12:00 축소임

Redis 버전은 7.1임
노드 유형은 cache.t3.micro임
단일 노드임
저장 중 암호화함

## 트러블슈팅

### 대상 그룹 Unhealthy

04:22에 `tg-web`의 두 인스턴스가 `Health checks failed` 상태였음
성공 코드를 200-399로 넓혀도 해결되지 않음
EC2 시스템 로그에서 저장소와 SSM 연결 타임아웃을 확인함
`No match for argument: nginx`를 확인함
로그 경로는 `logs/ec2-system-log_public-subnet-instance.txt`임

NAT Gateway와 private 라우팅 테이블은 정상임
인스턴스가 `public-subnet-c`에 생성돼 있었음
IP는 10.0.2.216임
공인 IP 없음
ASG에 public 서브넷을 지정한 것이 원인임

ASG 서브넷을 `web-subnet-a·c`로 수정함
User Data 재실행을 위해 기존 인스턴스를 종료함
web-subnet-c의 10.0.12.23 인스턴스가 생성됨
web-subnet-a의 10.0.11.100 인스턴스가 생성됨
`tg-web`이 정상 2 / 비정상 0으로 복구됨
근거는 03-1~03-7과 02-4임

EC2 상태 확인만 사용할 때 Unhealthy 인스턴스가 04:37까지 교체되지 않음
ELB 상태 확인을 추가함
근거는 04-1임
성공 코드를 200으로만 설정한 경우는 확인하지 않음

### Redis 연결 실패

WEB 인스턴스에서 `redis6-cli ping` 실행 시 `Name or service not known`이 발생함
호스트가 `...amazonaws.com:6379:6379`로 표시됨
`-h`에 포트까지 입력한 것이 원인임
이름에 `-ro`가 붙은 리더 엔드포인트도 잘못 사용함
기본 엔드포인트 호스트는 `-h`에 입력함
포트는 `-p 6379`로 분리함

`PONG` 확인함
`SET → OK` 확인함
`GET → "hello-from-a"` 확인함
다른 인스턴스에서도 같은 값 확인함
근거는 05-3과 05-4임
앱 세션 저장 동작은 미검증임

### 장애 인스턴스 자동 교체

ASG 상태 확인을 EC2 + ELB로 변경함
실행 중인 WEB 인스턴스 하나를 종료함
04:56:32에 종료됨
04:56:34에 새 인스턴스가 시작됨
근거는 04-2임

`systemctl stop nginx` 시험은 미실시함
정상 판정까지 걸린 시간은 측정하지 않음
장애 중 요청 실패율은 측정하지 않음

## 검증

요청 분산 충족함
ALB DNS 반복 접속 시 2개 AZ 호스트명이 번갈아 응답함
근거는 02-3임

장애 감지와 제외는 부분 충족함
약 30초 안에 Unhealthy 상태 전환이 기준임
정상 인스턴스만 응답하고 5xx가 없어야 함
nginx 중지 후 1초 간격 `curl` 시험은 미실시함
근거는 03-2임

자동 복구 충족함
인스턴스 종료 후 대상 그룹이 정상 2로 복귀함
근거는 04-2와 03-7임

동적 확장은 미실시함
`stress-ng --cpu 2 --timeout 15m` 사용 계획임
Scale-out 후 최대 4 이내가 기준임
부하 중지 후 Scale-in이 기준임

예약 확장은 미실시함
지정 시각의 용량 변경이 기준임

세션 저장소 공유 충족함
두 인스턴스에서 같은 키를 SET/GET함
근거는 05-3과 05-4임

로그인 유지는 미실시함
같은 쿠키로 반복 요청하면서 APP 1대를 종료해야 함
재로그인 없음과 Redis 세션 키·TTL 존재가 기준임

## 화면

화면 시간은 KST 기준임
CloudWatch 그래프와 ASG 활동 로그 본문은 UTC 기준임

다중 AZ 배치와 계층별 접근 제어 충족함
Public 2개 서브넷은 IGW로 라우팅됨
Private 6개 서브넷은 NAT로 라우팅됨
WEB 인스턴스는 2a·2c에 하나씩 배치됨
보안 그룹 규칙은 설계 설명으로 대체함
별도 캡처 없음

![NAT Gateway](images/01_network/01-1_nat-gateway.png)
![라우팅 테이블](images/01_network/01-2_route-tables.png)

2a의 10.0.11.100과 2c의 10.0.12.23이 번갈아 응답함
접속 주소는 `http://course-public-alb-2132507007.ap-northeast-2.elb.amazonaws.com`임

![ALB 요청 분산](images/02_web-tier/02-3_alb-round-robin.png)
![대상 그룹 정상](images/02_web-tier/02-4_tg-healthy.png)

ASG가 2초 만에 대체 인스턴스를 시작함
nginx 프로세스 중지 시험과 장애 중 응답 측정은 미실시함

![ASG 상태 확인 유형](images/04_failover/04-1_asg-health-check-type.png)
![ASG 활동 기록](images/04_failover/04-2_asg-activity-replacement.png)
![대상 그룹 지표](images/03_troubleshooting/03-7_tg-health-graph.png)

![ASG 최초 생성](images/03_troubleshooting/03-1_asg-initial-ec2-healthy.png)
![대상 Unhealthy](images/03_troubleshooting/03-2_tg-unhealthy-targets.png)
![대상 그룹 요약](images/03_troubleshooting/03-3_tg-unhealthy-summary.png)
![Public 서브넷 인스턴스](images/03_troubleshooting/03-4_instance-in-public-subnet.png)
![교체 중](images/03_troubleshooting/03-5_tg-replacing.png)
![web 서브넷 인스턴스](images/03_troubleshooting/03-6_instance-in-web-subnet.png)

서버 수 조정은 부분 충족함
최소 2 / 원하는 2 / 최대 4는 구축함
조정 정책 화면과 부하 시험 결과 없음

![시작 템플릿](images/02_web-tier/02-1_launch-template.png)
![ASG 용량](images/02_web-tier/02-2_asg-capacity.png)

Redis 공유 저장소는 부분 충족함
인스턴스 A의 `session:test` 값을 인스턴스 B에서 읽음
앱 로그인 세션 연동은 구현하지 않음

![Redis 서브넷 그룹](images/05_session-redis/05-1_redis-subnet-group.png)
![Redis 구성](images/05_session-redis/05-2_redis-config.png)
![인스턴스 A](images/05_session-redis/05-3_redis-instance-a.png)
![인스턴스 B](images/05_session-redis/05-4_redis-instance-b.png)

