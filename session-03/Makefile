# source/ 폴더에서 실행하세요.
SHELL := /bin/sh
.DEFAULT_GOAL := help

STACK ?= event-signup
REGION ?= ap-northeast-2
DELAY ?= 1
IDEMPOTENT ?= true
MAX_RECEIVES ?= 3
F ?= RegisterFunction
PYTHON ?= python3

export AWS_PAGER :=
DEPLOY_FLAGS = --stack-name "$(STACK)" --region "$(REGION)" --resolve-s3 --s3-prefix "$(STACK)" --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset
# $(shell ...) 대신 실행 시점에 조회한다. 배포 직후에도 새 Outputs를 가져온다.
OUTPUT = sh scripts/output.sh "$(STACK)" "$(REGION)"

.PHONY: help doctor step1 step2 url outputs db logs dlq dlq-peek redrive redrive-status delete test test-exercise validate

help:
	@printf '%s\n' 'make doctor        도구·AWS 로그인 확인' 'make step1         동기 처리 배포 (DELAY=6 으로 지연 변경)' 'make step2         비동기 처리 배포 (IDEMPOTENT=true, MAX_RECEIVES=3)' 'make url           화면에 붙여 넣을 API 주소' 'make outputs       함수·테이블·큐 이름 확인' 'make logs F=IssueFunction  실시간 로그 (Ctrl+C로 종료)' 'make db / dlq      저장 결과 / 실패 메시지 수 확인' 'make redrive       수정·재배포 후 DLQ 재처리' 'make test          AWS 없는 기본 테스트' 'make test-exercise 오류 수정 후 확인 테스트' 'make validate      SAM 템플릿 검사 (SAM CLI 필요)' 'make delete        실습 스택과 로그 삭제'

doctor:
	@sh scripts/doctor.sh "$(REGION)" "$(PYTHON)"

# 별도 설치할 Python 라이브러리가 없어 sam build 없이 src/ 코드를 바로 배포한다.
# Lambda 런타임은 3.12지만 배포 컴퓨터에 Python 3.12나 Docker는 필요 없다.
step1:
	sam deploy --template-file step1/template.yaml $(DEPLOY_FLAGS) --parameter-overrides IssueDelaySeconds=$(DELAY)
	@$(MAKE) --no-print-directory url

step2:
	sam deploy --template-file step2/template.yaml $(DEPLOY_FLAGS) --parameter-overrides IssueDelaySeconds=$(DELAY) Idempotent=$(IDEMPOTENT) MaxReceiveCount=$(MAX_RECEIVES)
	@$(MAKE) --no-print-directory url

url:
	@$(OUTPUT) ApiUrl

outputs:
	aws cloudformation describe-stacks --stack-name "$(STACK)" --region "$(REGION)" --query 'Stacks[0].Outputs' --output table

db:
	@set -eu; table=$$($(OUTPUT) TableName); \
	aws dynamodb scan --table-name "$$table" --region "$(REGION)" \
	  --query 'Items[].{id:registrationId.S,status:status.S,badge:badgeCode.S,issueCount:issueCount.N,org:organization.S}' --output table

logs:
	@set -eu; group=$$($(OUTPUT) "$(F)LogGroup"); \
	aws logs tail "$$group" --region "$(REGION)" --since 10m --follow --format short

dlq:
	@set -eu; queue=$$($(OUTPUT) DeadLetterQueueUrl); \
	aws sqs get-queue-attributes --queue-url "$$queue" --region "$(REGION)" \
	  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --query Attributes --output table

# ReceiveMessage는 완전한 읽기 전용이 아니다. 수신 횟수가 늘며, 삭제하지는 않는다.
dlq-peek:
	@set -eu; queue=$$($(OUTPUT) DeadLetterQueueUrl); \
	aws sqs receive-message --queue-url "$$queue" --region "$(REGION)" \
	  --max-number-of-messages 5 --visibility-timeout 0 --query 'Messages[].Body' --output text

# EventBridge가 보낸 메시지도 확실히 원래 작업 큐로 돌아가도록 목적지를 지정한다.
redrive:
	@set -eu; source=$$($(OUTPUT) DeadLetterQueueArn); destination=$$($(OUTPUT) QueueArn); \
	aws sqs start-message-move-task --source-arn "$$source" --destination-arn "$$destination" --max-number-of-messages-per-second 1 --region "$(REGION)"

redrive-status:
	@set -eu; source=$$($(OUTPUT) DeadLetterQueueArn); \
	aws sqs list-message-move-tasks --source-arn "$$source" --region "$(REGION)" --output table

delete:
	sam delete --stack-name "$(STACK)" --region "$(REGION)" --no-prompts

test:
	$(PYTHON) -m unittest -b tests.test_handlers -v

test-exercise:
	$(PYTHON) -m unittest -b tests.test_exercise -v

validate:
	sam validate --lint --template-file step1/template.yaml --region "$(REGION)"
	sam validate --lint --template-file step2/template.yaml --region "$(REGION)"
