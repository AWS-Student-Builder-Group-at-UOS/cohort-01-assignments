"""POST /registrations/{registrationId}/resend : (실습용) 같은 작업을 한 번 더 보낸다.

실제 서비스에서는 큐가 같은 메시지를 두 번 전달하는 일이 생길 수 있다 (최소 한 번 전달).
이 기능은 중복 전달을 재현하거나, 이벤트 발행 실패로 남은 PENDING 신청을 복구한다.
"""
from common import BadRequest, EventPublishError, log, publish_registration_event, response, table, validate_registration_id


def handler(event, context):
    try:
        registration_id = validate_registration_id((event.get("pathParameters") or {}).get("registrationId"))
    except BadRequest as error:
        return response(400, {"message": str(error)})

    item = table.get_item(Key={"registrationId": registration_id}, ConsistentRead=True).get("Item")
    if item is None:
        return response(404, {"message": "해당 신청 번호를 찾을 수 없습니다."})

    log("resend_requested", registrationId=registration_id)
    try:
        publish_registration_event(item)
    except EventPublishError:
        return response(
            503,
            {
                "message": "작업 전달을 확인하지 못했습니다. 잠시 뒤 다시 보내세요.",
                "registrationId": registration_id,
                "status": item["status"],
            },
        )
    return response(
        202,
        {
            "message": "같은 작업을 한 번 더 보냈습니다. (실습용)",
            "registrationId": registration_id,
        },
    )
