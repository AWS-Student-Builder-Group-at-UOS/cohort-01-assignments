"""GET /registrations/{registrationId} : 저장된 신청 상태를 조회한다."""
from common import BadRequest, log, response, table, validate_registration_id


def handler(event, context):
    try:
        registration_id = validate_registration_id((event.get("pathParameters") or {}).get("registrationId"))
    except BadRequest as error:
        return response(400, {"message": str(error)})

    # 방금 저장한 신청도 즉시 조회할 수 있도록 강력한 일관성으로 읽는다.
    result = table.get_item(Key={"registrationId": registration_id}, ConsistentRead=True)
    item = result.get("Item")
    log("status_checked", registrationId=registration_id, found=item is not None)
    if item is None:
        return response(404, {"message": "해당 신청 번호를 찾을 수 없습니다."})
    return response(200, item)
