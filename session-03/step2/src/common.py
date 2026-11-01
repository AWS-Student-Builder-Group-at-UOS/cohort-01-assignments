"""공통 코드: DynamoDB 연결, 이벤트 발행, 로그 남기기, 요청 검증, HTTP 응답 만들기."""
import base64
import binascii
import json
import os
import re
from decimal import Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# 핸들러 '밖'에서 설정과 클라이언트를 만든다.
# Lambda 실행 환경이 재사용되면 이 부분은 다시 실행되지 않는다.
# 처음 실행될 때(콜드 스타트)만 한 번 실행되므로, 요청마다 연결을 새로 만들지 않아도 된다.
# ---------------------------------------------------------------------------
TABLE_NAME = os.environ["TABLE_NAME"]
ISSUE_DELAY_SECONDS = float(os.environ.get("ISSUE_DELAY_SECONDS", "1"))
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
table = boto3.resource("dynamodb").Table(TABLE_NAME)
events = boto3.client("events")

EVENT_SOURCE = "event-signup.registration"
EVENT_DETAIL_TYPE = "RegistrationReceived"


class BadRequest(Exception):
    """사용자가 잘못된 값을 보냈을 때 (HTTP 400)."""


class EventPublishError(Exception):
    """신청은 저장됐지만 이벤트 발행 성공을 확인하지 못한 상태 (HTTP 503)."""


def _json_default(value):
    # DynamoDB는 숫자를 Decimal로 돌려주는데, JSON으로 바꿀 때 일반 숫자로 변환한다.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"JSON으로 바꿀 수 없는 값: {value!r}")


def log(event_name, **fields):
    """한 줄짜리 JSON 로그. CloudWatch Logs에서 신청 번호로 검색하기 쉽다."""
    print(json.dumps({"event": event_name, **fields}, ensure_ascii=False, default=_json_default))


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def read_json_body(event):
    raw = event.get("body")
    if not isinstance(raw, str):
        raise BadRequest("요청 본문이 JSON 형식이 아닙니다.")
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, binascii.Error, UnicodeError, ValueError):
        raise BadRequest("요청 본문이 올바른 JSON 또는 base64 형식이 아닙니다.") from None
    if not isinstance(data, dict):
        raise BadRequest("요청 본문은 JSON 객체여야 합니다.")
    return data


_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def validate_registration_id(value):
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value.strip()):
        raise BadRequest("registrationId는 영문, 숫자, -, _ 로 1~64자여야 합니다.")
    return value.strip()


def validate_registration(data):
    """문자열 입력만 받는다. 소속은 생략하거나 빈 문자열로 보낼 수 있다."""
    registration_id = validate_registration_id(data.get("registrationId"))
    fields = {}
    for key in ("name", "email", "organization"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise BadRequest(f"{key}은 문자열이어야 합니다.")
        fields[key] = value.strip()
    name, email, organization = (fields[key] for key in ("name", "email", "organization"))

    if not name or len(name) > 50:
        raise BadRequest("name은 1~50자여야 합니다.")
    if len(email) > 100 or not _EMAIL_PATTERN.fullmatch(email):
        raise BadRequest("email 형식이 올바르지 않습니다.")
    if len(organization) > 50:
        raise BadRequest("organization은 50자 이하여야 합니다.")

    result = {"registrationId": registration_id, "name": name, "email": email}
    if organization:  # 비어 있으면 아예 저장하지 않는다.
        result["organization"] = organization
    return result


def publish_registration_event(registration):
    """'신청 접수됨' 이벤트를 EventBridge에 보낸다.

    이벤트를 받을 대상(SQS)은 여기서 모른다. 어디로 갈지는 template.yaml의 규칙(Rule)이 정한다.
    -> 보내는 쪽과 처리하는 쪽이 느슨하게 연결된다.
    """
    detail = {
        key: registration[key]
        for key in ("registrationId", "name", "email", "organization")
        if key in registration
    }
    try:
        result = events.put_events(
            Entries=[
                {
                    "EventBusName": EVENT_BUS_NAME,
                    "Source": EVENT_SOURCE,
                    "DetailType": EVENT_DETAIL_TYPE,
                    "Detail": json.dumps(detail, ensure_ascii=False),
                }
            ]
        )
    except (BotoCoreError, ClientError) as error:
        log("event_publish_failed", registrationId=registration["registrationId"], errorType=type(error).__name__)
        raise EventPublishError("이벤트 발행 성공을 확인하지 못했습니다.") from error
    # put_events는 일부가 실패해도 예외 없이 돌아오므로 FailedEntryCount를 꼭 확인한다.
    if result.get("FailedEntryCount", 0) > 0:
        log("event_publish_failed", registrationId=registration["registrationId"], errors=result.get("Entries"))
        raise EventPublishError("이벤트 발행에 실패했습니다.")
    log("event_published", registrationId=registration["registrationId"])
