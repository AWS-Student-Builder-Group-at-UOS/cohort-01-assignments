# 수강신청 포털 소스 코드

AWS에 배포한 WEB·APP 애플리케이션과 설치·검증 스크립트다. 설계와 실험 결과는 [제출 보고서](../README.md), 리소스 생성부터 배포까지의 과정은 [구축·검증 기록](../IMPLEMENTATION.md)에 정리했다.

| 경로 | 내용 |
|---|---|
| `web/` | Nginx 설정, 로그인·강좌 조회·수강신청 화면 |
| `app/` | Node.js API, RDS 처리, Valkey 세션, systemd 서비스 |
| `userdata/` | EC2 시작 템플릿에 넣는 설치 스크립트 |
| `policies/` | WEB·APP 역할과 실습 사용자용 IAM 정책 |
| `scripts/watch.mjs` | 같은 로그인 쿠키로 요청하며 응답 APP·AZ·상태 코드를 확인하는 스크립트 |
| `pack.sh` | S3에 올릴 WEB·APP 배포 파일 생성 |

## 배포 파일 만들기

macOS와 Node.js 22 이상·npm이 있는 환경에서, 제출한 `이예나` 폴더를 기준으로 실행한다.

```bash
cd source
./pack.sh
```

`dist/web.tar.gz`와 `dist/app.tar.gz`가 생성된다. 두 파일을 본인의 S3 배포 버킷에 업로드한다. 생성되는 `dist/`는 배포용 결과물이며 제출 소스에 포함할 필요는 없다.

배포 전 [APP 사용자 데이터](userdata/app.sh)의 버킷·DB 주소·보안 암호 ARN·캐시 주소와 [WEB 사용자 데이터](userdata/web.sh)의 버킷·Internal ALB 주소를 본인 환경으로 바꾼다. [IAM 정책](policies/)의 계정 ID·ARN·리소스 이름도 함께 맞춘다. 현재 적힌 값은 실습 당시의 리소스 식별자이며 다른 계정에서 그대로 사용할 수 없다.

저장소의 프로젝트 폴더명은 `source`이다. EC2 안에서는 실제 실습에 사용한 `/srv/portal` 경로와 `portal-app` 서비스 이름을 유지한다. 압축 파일 안의 경로는 `app/`, `web/`이므로 저장소를 다른 위치로 옮겨도 배포 구조는 같다.

## 실행 확인

`source` 폴더에서 Public ALB DNS를 바꿔 실행한다.

```bash
node scripts/watch.mjs http://<Public-ALB-DNS>
```

실습용 계정은 `test01`~`test30`, 비밀번호는 `asbg1234`다. APP 초기화 과정에서 테이블과 계정을 준비한다. `watch.mjs`는 로그인 쿠키를 유지하며 `/api/me`와 `/api/courses`를 각각 요청한다. 로그의 APP ID는 `/api/me`에 응답한 서버를 뜻한다.

## API

| 요청 | 설명 |
|---|---|
| `GET /health` | 프로세스 상태 확인. DB·캐시는 조회하지 않음 |
| `POST /api/login` | `{ "id", "password" }`로 로그인 |
| `POST /api/logout` | 로그아웃 |
| `GET /api/me` | 로그인 사용자와 응답 인스턴스 ID·AZ |
| `GET /api/courses` | 개설 강좌 목록 |
| `POST /api/enrollments` | `{ "courseId" }` 신청. DB 조건부 UPDATE와 트랜잭션으로 정원 처리 |
| `DELETE /api/enrollments/:courseId` | 신청 취소 |
