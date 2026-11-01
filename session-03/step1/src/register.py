"""실습 1: 신청 접수와 참가증 발급을 한 번의 요청 안에서 모두 처리한다 (동기 처리).

흐름:  POST /registrations
        1) 신청을 DynamoDB에 저장
        2) 참가증을 만든다 (느린 작업: ISSUE_DELAY_SECONDS 만큼 걸린다고 가정)
        3) 발급 결과를 DynamoDB에 기록
        4) 모든 것이 끝난 뒤에야 사용자에게 응답
"""
import time
import uuid
from datetime import datetime, timezone

from common import (
    ISSUE_DELAY_SECONDS,
    BadRequest,
    log,
    read_json_body,
    response,
    table,
    validate_registration,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_badge(registration):
    """참가증 만들기. 실제로는 PDF 생성이나 메일 발송처럼 오래 걸리는 일이라고 생각하자."""
    time.sleep(ISSUE_DELAY_SECONDS)
    code = "BADGE-" + uuid.uuid4().hex[:6].upper()
    text = f"{registration['name']} / {registration.get('organization', '-')}"
    return code, text


def handler(event, context):
    try:
        registration = validate_registration(read_json_body(event))
    except BadRequest as error:
        return response(400, {"message": str(error)})

    registration_id = registration["registrationId"]
    log("request_received", registrationId=registration_id)

    # 1) 신청 저장 (같은 신청 번호로 다시 오면 그냥 덮어쓴다 -> 중복 방어 없음)
    table.put_item(Item={**registration, "status": "RECEIVED", "createdAt": now_iso()})
    log("registration_saved", registrationId=registration_id)

    # 2) 참가증 발급 (여기서 시간이 오래 걸린다)
    badge_code, badge_text = make_badge(registration)
    log("badge_made", registrationId=registration_id, badgeCode=badge_code)

    # 3) 발급 결과 기록
    table.update_item(
        Key={"registrationId": registration_id},
        UpdateExpression="SET #s = :issued, badgeCode = :code, badgeText = :text, issuedAt = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":issued": "ISSUED",
            ":code": badge_code,
            ":text": badge_text,
            ":now": now_iso(),
        },
    )
    log("badge_saved", registrationId=registration_id)

    # 4) 이제서야 응답
    return response(
        201,
        {
            "message": "신청과 참가증 발급이 완료되었습니다.",
            "registrationId": registration_id,
            "status": "ISSUED",
            "badgeCode": badge_code,
        },
    )
