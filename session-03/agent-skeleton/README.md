# 내 에이전트 뼈대 (Slack + Lambda + SQS + Bedrock)

## 개요

Slack 슬래시 명령으로 에이전트를 시작하고, Lambda가 Bedrock의 Claude를 호출해 판단한 결과를 Slack으로 돌려주는 최소 뼈대이다. 처음에는 시각과 글자 수를 도구로 확인하는 샘플 에이전트가 들어 있고, 프롬프트와 도구만 바꾸면 내 에이전트가 된다. 세션 3 과제의 "도전 · 나만의 에이전트로 바꿔 보기"를 Slack과 LLM까지 넓힌 추가 도전용이며, 과제 C1–C4는 이 코드 없이도 할 수 있다.

```
Slack /명령 ──▶ 입구 Lambda ──▶ SQS 큐 ──▶ 워커 Lambda ──▶ Bedrock (Claude)
                (서명 검증,        │ 5회 실패하면       │
                 3초 안에 응답)    ▼ DLQ              └─▶ 결과를 Slack response_url로 회신
```

### 진행 순서

1. 테스트를 돌려 처음 상태가 정상인지 확인한다.
2. 쓸 모델을 정한다.
3. 샘플을 내 컴퓨터에서 한 번 돌린다.
4. `src/app/`을 내 에이전트로 바꾼다.
5. Slack 앱을 만든다.
6. 배포한다.
7. Slack에 연결하고 실행한다.
8. 다 쓰고 스택을 지운다.

### 한 번 실행의 흐름

번호는 실행 순서이다. 입구 Lambda는 접수까지만 하고, 모델을 부르는 일은 큐 뒤의 워커 Lambda가 한다.

| 순서 | 하는 곳 | 하는 일 |
|---|---|---|
| 1 | Slack | 사용자가 `/명령 <글>`을 실행한다 |
| 2 | 입구 Lambda | 서명과 허용 사용자를 확인하고 SQS에 넣은 뒤 "접수했어요"를 바로 응답한다 (Slack은 3초 안에 응답을 요구한다) |
| 3 | 워커 Lambda | 큐에서 메시지 한 건을 받아 재료를 만든다 (`prepare`) |
| 4 | 워커 Lambda | 모델을 부르기 전에 DynamoDB에 최대 비용을 예약한다. 하루·한 달 한도를 넘으면 모델을 부르지 않는다 |
| 5 | 워커 Lambda | 모델과 도구를 오가며 판단한다. 한도(턴, 토큰, 금액, 남은 시간)에 닿으면 멈춘다 |
| 6 | 워커 Lambda | 결과를 Slack `response_url`로 회신한다 |
| 7 | 워커 Lambda | 실제 비용으로 DynamoDB를 정산한다 |

워커가 오류를 던지면 큐가 같은 메시지를 다시 전달하고, 5회 수신된 뒤에는 DLQ로 보낸다. 25분이 지난 메시지는 회신 주소가 곧 만료되므로 모델을 부르지 않고 건너뛴다.

## 사용 환경

| 구성 요소 | 용도 | 비고 |
|---|---|---|
| AWS 계정 + AWS CLI | 배포와 로그 확인 | `aws configure` 완료 |
| SAM CLI | `sam build`, `sam deploy`, `sam delete` | |
| Python 3.13 이상 | 테스트와 로컬 실행 | 템플릿 런타임이 `python3.13` |
| Amazon Bedrock | Claude 호출 | 서울 리전(`ap-northeast-2`), Global 추론 프로파일 |
| Slack 앱 | 슬래시 명령과 회신 | 연습용 워크스페이스를 권한다 |

## 지켜야 할 제약

| 제약 | 처리 |
|---|---|
| 허용한 사람만 쓴다 | 입구 Lambda가 Slack 서명을 검증하고 `AllowedSlackUserIds`에 있는 사용자만 통과시킨다. 목록이 비면 전원 거부한다 |
| 비밀값을 남기지 않는다 | Signing Secret과 키는 코드, 스크린샷, README, 채팅에 두지 않는다. 배포 명령의 입력칸에만 넣는다 |
| 비용이 한도를 넘지 않는다 | 실행 1회, 하루, 한 달 한도를 둔다 (아래 "한도" 절) |
| 되돌릴 수 없는 행동은 사람이 한다 | 처음에는 초안을 만들어 보여 주는 데까지만 시킨다 |
| 샘플 앱은 외부에 쓰지 않는다 | 도구가 시각과 글자 수만 다룬다 |

## 폴더 구성

| 파일 | 역할 | 바꾸는가 |
|---|---|---|
| `src/app/prompt.py` | 에이전트의 역할과 규칙 | 바꾼다 |
| `src/app/tools.py` | 도구 (함수 + 설명 + 입력 스키마) | 바꾼다 |
| `src/app/entry.py` | 요청을 받아 재료를 만들고(`prepare`) 결과를 다듬는다(`finish`) | 바꾼다 |
| `src/app/deliver.py` | 결과 전달 (기본: Slack 회신, 3,600자 단위로 나눠 보냄) | 필요하면 바꾼다 |
| `src/ack_handler.py` | 입구 Lambda: 서명 검증, 허용 사용자 확인, 큐에 넣고 바로 응답 | 두고 쓴다 |
| `src/handler.py` | 워커 Lambda: 지출 예약 → 에이전트 실행 → 전달 → 정산, 재시도와 오래된 메시지 건너뛰기 | 두고 쓴다 |
| `src/core/` | 에이전트 루프, 한도, 지출 상한(DynamoDB), 프롬프트 캐시 | 두고 쓴다 |
| `template.yaml` | SAM 템플릿 (큐, DLQ, 두 Lambda, 권한, 로그, 지출 상한 표) | 한도를 바꿀 때만 |
| `run_local.py` | 내 컴퓨터에서 실제 Bedrock으로 한 번 실행 | 두고 쓴다 |
| `scripts/probe_models.py` | 이 계정에서 부를 수 있는 모델 확인 | 두고 쓴다 |
| `tests/` | AWS와 Slack 없이 도는 테스트 | 도구를 추가하면 같이 고친다 |

## 진행 절차

### 1단계. 테스트

AWS 없이 돌아가며 비용이 들지 않는다. 코드를 바꾼 뒤에도 같은 명령으로 다시 확인한다.

```
pip install boto3
python -I -m unittest discover -s tests
```

| 확인 항목 | 이렇게 보이면 |
|---|---|
| 단위 테스트 | 마지막에 `OK` |
| 템플릿 점검 | `OK: 참조, 수치 관계, 핸들러 경로가 모두 맞음` |

### 2단계. 모델 선정

아주 작은 호출로 이 계정에서 모델을 부를 수 있는지 확인한다. 호출당 비용은 작지만 호출은 호출이다.

```
python -I scripts/probe_models.py
python -I scripts/probe_models.py global.<모델 ID>
aws bedrock list-inference-profiles --region ap-northeast-2
```

`OK`가 나온 `global.` 프로파일 ID를 고른다. 그 ID에서 `global.`만 뗀 값이 배포 때 쓰는 `BaseModelId`이다.

### 3단계. 로컬 실행

실제 모델을 부르므로 비용이 든다. Windows 명령 프롬프트에서는 `set MODEL_ID=global.<모델 ID>`를 먼저 실행한 뒤 `python run_local.py "지금 몇 시야?"`를 실행한다.

```
MODEL_ID=global.<모델 ID> python run_local.py "지금 몇 시야?"
```

마지막 줄이 한 번 실행의 기록이다. 이 값으로 한도를 정한다.

```
[종료 사유=end_turn 호출=2회 입력토큰=… 캐시읽기=… 캐시쓰기=… 출력토큰=… 추정비용=$…]
```

모델이 Sonnet 5 Global이 아니면 단가 환경변수 네 개(`PRICE_INPUT_PER_M`, `PRICE_OUTPUT_PER_M`, `PRICE_CACHE_READ_PER_M`, `PRICE_CACHE_WRITE_PER_M`)를 같이 설정해야 추정비용이 맞다. 단가는 Bedrock 요금 페이지에서 직접 확인한다.

### 4단계. 에이전트 변경

`src/app/`의 파일만 고친다. 고친 뒤 1단계 테스트와 3단계 로컬 실행을 다시 해서 로컬에서 먼저 확인한다. 바꾸는 방법은 아래 "에이전트 수정 위치"에 있다.

### 5단계. Slack 앱 생성

Request URL은 7단계에서 채운다. Request URL을 쓰는 슬래시 명령은 Socket Mode가 꺼져 있어야 하고, 화면 이름은 바뀔 수 있으니 [Slack 슬래시 명령 문서](https://docs.slack.dev/interactivity/implementing-slash-commands/)를 같이 본다.

| 순서 | 할 일 |
|---|---|
| 1 | 새 앱을 만들고 Basic Information의 Signing Secret을 확인한다. 어디에도 붙여 넣지 않는다 |
| 2 | Slash Commands에서 명령을 만든다 (예: `/agent`) |
| 3 | 앱을 워크스페이스에 설치한다 |
| 4 | 본인의 Slack 사용자 ID를 확인한다 (프로필 메뉴의 멤버 ID 복사) |

### 6단계. 배포

`--guided`가 파라미터를 하나씩 물어본다.

```
sam build
sam deploy --guided --region ap-northeast-2
```

| 파라미터 | 넣을 값 |
|---|---|
| Stack Name | 예: `my-agent` |
| `BaseModelId` | 2단계 ID에서 `global.`을 뗀 값 |
| `SlackSigningSecret` | 5단계의 Signing Secret. 물어볼 때만 입력한다 (화면에 보이지 않는다) |
| `AllowedSlackUserIds` | 본인 Slack 사용자 ID. 쉼표로 여러 명. 비우면 아무도 쓰지 못한다 |
| `PriceInputPerM` 등 네 개 | 쓰는 모델의 단가. 비우면 코드 기본값(Sonnet 5 Global) |
| `BudgetAlertEmail` | 계정 월 비용 알림을 받을 이메일. 비우면 알림을 만들지 않는다. 알림만 보내고 자동으로 막지는 않는다 |
| 나머지 | 엔터로 기본값 |

물어보는 질문에는 이렇게 답한다.

| 질문 | 답 | 이유 |
|---|---|---|
| IAM 역할 생성 허용 | `y` | 템플릿이 Lambda 실행 역할을 만든다 |
| Function URL에 인증이 없다 | `y` | 인증은 코드 안의 Slack 서명 검증이 맡는다 |
| Save arguments to configuration file | `n` | 비밀값이 `samconfig.toml`에 남지 않게 한다 |

스택이 `ROLLBACK_COMPLETE`로 끝나면 같은 이름으로 다시 만들 수 없다. 스택을 삭제한 뒤 다시 배포한다.

### 7단계. Slack 연결과 실행

배포 출력의 `SlackRequestUrl`을 5단계 명령의 Request URL에 넣고 저장한 뒤 Slack에서 명령을 실행한다.

```
/agent 지금 몇 시야?
```

| 확인 항목 | 이렇게 보이면 |
|---|---|
| 입구 응답 | 곧바로 "접수했어요. 끝나면 이 대화로 알려 드릴게요." |
| 결과 | 잠시 뒤 같은 대화로 답이 오고, 마지막 줄에 호출 수와 추정비용이 붙는다 |

### 8단계. 정리

연습이 끝나면 스택을 지운다. 테스트용으로 만든 액세스 키와 임시로 붙인 권한도 같이 지운다.

```
sam delete --stack-name <스택 이름> --region ap-northeast-2
```

## 에이전트 수정 위치

먼저 입력, 판단, 행동, 기록 네 칸을 한 줄씩 적고, 아래 표대로 고칠 곳에 대응시킨다.

| 칸 | 질문 | 고칠 곳 |
|---|---|---|
| 입력 | 어떤 요청을 받나 | `app/entry.py`의 `prepare`. Slack 명령 뒤의 글이 `body["text"]`로 들어온다 |
| 판단 | 무엇을 기준으로 정하나 | `app/prompt.py` (역할과 규칙) |
| 행동 | 무엇을 확인하거나 실행하나 | `app/tools.py` (도구) |
| 기록 | 결과를 어디에 남기나 | `app/entry.py`의 `finish`, `app/deliver.py` |

도구 하나는 이런 모양이다. 도구 함수는 문자열이나 JSON으로 바꿀 수 있는 값을 돌려주고, 오류가 나면 예외를 던져도 된다. 코어가 잡아서 모델에게 오류로 알려 준다.

```python
def word_count(text: str) -> dict:
    return {"words": len(text.split()), "chars": len(text)}

Tool(
    name="word_count",
    description="주어진 텍스트의 단어 수와 글자 수를 센다.",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "세려는 텍스트"}},
        "required": ["text"],
    },
    func=word_count,
)
```

| 원칙 | 이유 |
|---|---|
| 규칙으로 되는 일(형식 검사, 숫자 계산)은 코드나 도구로 처리한다 | 모델 호출 비용이 없고 결과가 항상 같다 |
| 모델의 답은 코드가 검사할 수 있는 모양(JSON, 정해진 항목)으로 받는다 | 확인할 수 있는 부분은 코드가 확인하고, 나머지는 "사람이 직접 확인할 곳"으로 결과에 적을 수 있다 |
| 꼭 지켜야 하는 규칙은 프롬프트가 아니라 코드로 막는다 | 프롬프트에 금지 규칙을 써도 모델이 어길 수 있다 |
| `ack_handler.py`, `handler.py`, `core/`는 고치지 않는다 | 서명 검증, 재시도, 한도, 정산이 거기 있다 |
| 앱 코드는 `src/app/entry.py`(`prepare`, `finish`)에서만 바꾼다 | 서명 검증, 재시도, 한도, 정산을 건드리지 않고 앱만 바꿀 수 있다 |

## 한도

모델을 부르는 에이전트는 비용이 예상보다 크게 나올 수 있다. 배포 전에 한도를 정하고 첫 실행의 `추정비용`으로 보정한다. 금액 기본값은 요금표 값이 아니라 이 뼈대의 가정이다.

| 한도 | 기본값(샘플) | 바꾸는 곳 | 막는 것 |
|---|---|---|---|
| 한 번의 실행 | $0.5 | `MaxUsdPerRun` | 다음 호출의 최소 비용이 한도를 넘을 것 같으면 모델을 부르지 않는다 |
| 하루(KST) | $2 | `DailyCapUsd` | 여러 번 눌러도, 재시도가 쌓여도 합계가 넘지 않는다. 실행 전에 예약하고 끝나면 실제액으로 정산한다 |
| 한 달 | $10 | `MonthlyCapUsd` | 위와 같다 |
| 도구 호출 / 턴 | 12회 / 8턴 | `core/agent.py`의 `Limits` | 도구 호출이 끝없이 반복되는 루프 |
| 워커 제한 시간 | 300초(템플릿에 고정) | `template.yaml`의 `Timeout`과 큐 `VisibilityTimeout` | 가시성 제한 시간은 1800초로, 워커 제한 시간의 6배이다. 워커 제한 시간을 바꾸면 이 값도 같이 바꾼다 |
| 동시 실행 | 2 | 템플릿의 `MaximumConcurrency` | 요청이 몰려도 모델 호출이 늘지 않는다 |

일일·월 상한은 뼈대가 DynamoDB에 기록한 금액 기준이고, `BudgetAlertEmail`의 계정 예산 알림은 알려 줄 뿐 막지 않는다. 도구 결과로 입력이 갑자기 커지면 마지막 호출에서 한도를 조금 넘길 수 있다. 크레딧이 Bedrock의 Claude에 적용되는지는 크레딧 약관마다 달라서, 확인하기 전에는 카드로 청구될 수 있다고 보고 한도를 낮게 시작한다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 스택이 `ROLLBACK_COMPLETE`로 끝남 | CloudFormation 콘솔의 Events에서 첫 실패 이유를 읽고, 스택을 삭제한 뒤 다시 배포한다. 템플릿을 고쳤다면 `!Equals`에 숫자를 쓰지 않았는지 본다 (문자열로 써야 한다) |
| Slack에서 403이나 "실패했습니다" | 입구 Lambda 로그의 거부 이유(서명 불일치, 허용 사용자 아님)를 본다. `SlackSigningSecret`과 `AllowedSlackUserIds`를 확인한다. 첫 요청이 3초에 가까우면 콜드 스타트일 수 있다 (입구 메모리 512MB가 기본) |
| "접수했어요"는 오는데 결과가 없음 | 워커 Lambda 로그와 DLQ를 확인한다. 같은 메시지는 5회 실패하면 DLQ로 가고, 25분 지난 메시지는 건너뛰므로 명령을 다시 실행한다 |
| 모델 호출이 `AccessDenied` 등으로 실패 | `probe_models.py`로 이 계정·리전에서 그 모델을 부를 수 있는지 먼저 확인한다 |
| 결과가 잘림 | Slack `response_url`은 30분 안에 5번까지만 쓸 수 있어서 3,600자 단위로 나눠 보낸다. 결과가 더 길면 줄인다 |
| 비용이 예상보다 큼 | 마지막 줄의 `추정비용`과 `호출` 횟수를 본다. 도구 결과가 커서 입력이 늘었는지 확인한다 |

로그는 CloudWatch Logs에서 보고, 비밀값이 로그에 찍히지 않았는지 확인한다. 로그 보존 기간은 기본 14일이다(`LogRetentionDays`).

## 주의

배포 전에 한 번 읽는다. 에이전트는 비밀값, 비용, 부작용 세 가지가 사고가 되는 자리이다.

| 항목 | 내용 |
|---|---|
| 비밀값 | 이 뼈대는 Signing Secret을 Lambda 환경변수에 넣어서 콘솔에서 보인다. 오래 쓸 에이전트라면 Parameter Store나 Secrets Manager로 옮긴다 |
| 부작용 | 같은 메시지가 큐에서 다시 올 수 있고 이 뼈대에는 조건부 쓰기가 없다. 메일 발송, 결제, 글 게시는 사람이 마지막에 한다 |
| 프롬프트 주입 | 사용자 입력과 도구 결과는 데이터로 다루라고 프롬프트에 적지만 주입을 막는다고 보장하지는 않는다. 행동을 사람이 확인하는 구조가 마지막 방어선이다 |
| 허용 사용자 | 목록이 비면 전원 거부한다. 설정을 빠뜨렸을 때 모두에게 열리는 쪽이 더 위험하기 때문이다 |

## 참고한 공식 문서

| 주제 | 문서 |
|---|---|
| Converse 도구 호출 | [Client-side tool use](https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use-client-side.html) |
| Global 추론 프로파일 | [Global cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html) |
| 가시성 제한 시간은 함수 제한 시간의 6배 이상, `maxReceiveCount`는 5 이상 권장 | [Lambda SQS 설정](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html) |
| Slack 요청 서명 검증, 3초 응답 | [Verifying requests from Slack](https://docs.slack.dev/authentication/verifying-requests-from-slack/) |
| 슬래시 명령 | [Implementing slash commands](https://docs.slack.dev/interactivity/implementing-slash-commands/) |
