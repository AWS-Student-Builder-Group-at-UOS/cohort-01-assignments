"""참가증 발급 함수: SQS 큐에서 메시지를 꺼내 참가증을 발급하고 결과를 DB에 기록한다.

이 함수는 같은 메시지를 두 번 이상 받을 수 있다 (큐는 '최소 한 번' 전달하기 때문).
그래서 여러 번 실행돼도 DB의 발급 결과는 한 번만 반영한다 (멱등성).
make_badge 자체는 여러 번 실행될 수 있다. 외부 메일 발송 등의 중복까지 막는 코드는 아니다.
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from common import ISSUE_DELAY_SECONDS, log, table

# true  : 조건부 쓰기로 발급 결과의 중복 기록을 막는다 (기본값)
# false : 중복 기록을 허용한다. 같은 작업이 두 번 오면 issueCount도 증가한다. (비교 실험용)
IDEMPOTENT = os.environ.get("IDEMPOTENT", "true").lower() == "true"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_badge_text(detail):
    """[실습용 오류] 선택 입력에 관한 오류를 로그와 test-exercise로 찾아 수정한다."""
    return f"{detail['name']} / {detail['organization']}"


def make_badge(detail):
    """느린 계산을 흉내 낸다. 외부 전송은 하지 않으며 DB에 기록할 값을 돌려준다."""
    time.sleep(ISSUE_DELAY_SECONDS)
    code = "BADGE-" + uuid.uuid4().hex[:6].upper()
    return code, build_badge_text(detail)


def save_badge(registration_id, badge_code, badge_text):
    """발급 결과를 DB에 기록한다. 기록했으면 True, 이미 발급되어 건너뛰었으면 False."""
    request = {
        "Key": {"registrationId": registration_id},
        "UpdateExpression": (
            "SET #s = :issued, badgeCode = :code, badgeText = :text, issuedAt = :now "
            "ADD issueCount :one"
        ),
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":issued": "ISSUED",
            ":code": badge_code,
            ":text": badge_text,
            ":now": now_iso(),
            ":one": 1,
        },
        # UpdateItem은 없는 항목도 만들 수 있으므로 신청 존재 조건은 항상 필요하다.
        "ConditionExpression": "attribute_exists(registrationId)",
    }
    if IDEMPOTENT:
        # 조건부 쓰기: 아직 참가증 코드가 없을 때만 기록한다.
        # 확인과 기록이 DynamoDB 안에서 한 번에 처리되므로, 동시에 두 개가 와도 하나만 성공한다.
        request["ConditionExpression"] += " AND attribute_not_exists(badgeCode)"

    try:
        table.update_item(**request)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            existing = table.get_item(Key={"registrationId": registration_id}, ConsistentRead=True).get("Item")
            if existing is None:
                raise LookupError(f"신청이 존재하지 않습니다: {registration_id}") from error
            return False
        raise
    return True


def handler(event, context):
    for record in event["Records"]:
        # EventBridge가 보낸 이벤트 전체가 SQS 메시지 본문(body)에 들어 있다.
        message = json.loads(record["body"])
        detail = message["detail"]
        registration_id = detail["registrationId"]
        log(
            "issue_started",
            registrationId=registration_id,
            messageId=record["messageId"],
            receiveCount=record["attributes"]["ApproximateReceiveCount"],
        )

        try:
            badge_code, badge_text = make_badge(detail)
            if save_badge(registration_id, badge_code, badge_text):
                log("badge_saved", registrationId=registration_id, badgeCode=badge_code)
            else:
                log("badge_already_issued_skip", registrationId=registration_id)
        except Exception as error:
            log("issue_failed", registrationId=registration_id, messageId=record["messageId"],
                errorType=type(error).__name__, error=str(error))
            raise  # 예외를 다시 올려야 Lambda가 실패로 판단하고 SQS에서 재시도한다.
        # 예외 없이 끝나면 Lambda가 메시지를 큐에서 지운다.
        # 예외가 나면 지우지 않아서, 가시성 제한 시간이 지난 뒤 다시 처리된다 (재시도).
