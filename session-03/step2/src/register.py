"""실습 2: 신청은 '접수'만 하고 바로 응답한다. 참가증 발급은 이벤트로 넘긴다 (비동기 처리).

흐름:  POST /registrations
        1) 신청을 DynamoDB에 저장 (상태: PENDING). 같은 신청 번호면 저장하지 않는다 (조건부 쓰기).
        2) "신청 접수됨" 이벤트를 EventBridge로 발행
        3) 바로 응답 (202 Accepted: 접수했고, 처리는 나중에 된다는 뜻)

        이후 EventBridge -> SQS -> issue.py 가 참가증을 발급한다.

알려진 한계: 1)은 성공했는데 2)의 성공을 확인하지 못하면 HTTP 503으로 알린다.
신청은 PENDING 상태로 보존한다. 조회 후 /resend로 다시 발행할 수 있다.
두 작업의 원자성은 보장하지 않는다. DynamoDB Streams와 아웃박스는 심화 과제다.
"""
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from common import (
    BadRequest,
    EventPublishError,
    log,
    publish_registration_event,
    read_json_body,
    response,
    table,
    validate_registration,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def handler(event, context):
    try:
        registration = validate_registration(read_json_body(event))
    except BadRequest as error:
        return response(400, {"message": str(error)})

    registration_id = registration["registrationId"]
    log("request_received", registrationId=registration_id)

    # 1) 신청 저장: 같은 신청 번호가 이미 있으면 저장하지 않는다.
    try:
        table.put_item(
            Item={**registration, "status": "PENDING", "createdAt": now_iso()},
            ConditionExpression="attribute_not_exists(registrationId)",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"registrationId": registration_id}, ConsistentRead=True).get("Item", {})
        original = {key: existing[key] for key in ("registrationId", "name", "email", "organization") if key in existing}
        if original != registration:
            log("registration_conflict", registrationId=registration_id)
            return response(
                409,
                {"message": "같은 신청 번호로 다른 내용이 접수되어 있습니다. 새 번호를 사용하세요.",
                 "registrationId": registration_id},
            )
        log("duplicate_request", registrationId=registration_id, status=existing.get("status"))
        return response(
            200,
            {
                "message": "이미 접수된 신청입니다. 새 작업은 만들지 않았습니다.",
                "registrationId": registration_id,
                "status": existing.get("status"),
            },
        )
    log("registration_saved", registrationId=registration_id)

    # 2) 후속 작업(참가증 발급)을 이벤트로 넘긴다.
    try:
        publish_registration_event(registration)
    except EventPublishError:
        return response(
            503,
            {
                "message": "신청은 저장했지만 작업 전달을 확인하지 못했습니다. 상태 조회 후 작업을 다시 보내세요.",
                "registrationId": registration_id,
                "status": "PENDING",
            },
        )

    # 3) 발급이 끝나기를 기다리지 않고 바로 응답한다.
    return response(
        202,
        {
            "message": "신청이 접수되었습니다. 참가증은 잠시 뒤 발급됩니다.",
            "registrationId": registration_id,
            "status": "PENDING",
        },
    )
