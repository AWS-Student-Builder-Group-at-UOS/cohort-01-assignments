"""Slack 슬래시 명령의 입구 Lambda (Function URL로 호출).

하는 일은 세 가지뿐이다. 서명 검증 -> 허용된 사용자인지 확인 -> SQS에 넣고 바로 응답.
Slack은 요청 후 3초 안에 HTTP 200을 받아야 하므로, 모델 호출 같은 무거운 일은 여기서 하지 않는다.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

MAX_SKEW_S = 300  # Slack 문서: 요청 시각이 5분 넘게 어긋나면 거부 (재전송 공격 방지)

def _init_client():
    """SQS 클라이언트를 함수 시작(init) 단계에서 만든다. 첫 요청 처리 중에 boto3를 불러오면 콜드 스타트에서
    1~2초가 더 걸려 Slack의 3초 제한을 넘길 수 있다(첫 실제 배포에서 처리 시간 2.7초가 찍혔다). 로컬 테스트처럼 boto3가 없으면 None."""
    try:
        import boto3

        return boto3.client("sqs")
    except Exception:
        return None


_sqs = _init_client()


def _sqs_client():
    global _sqs
    if _sqs is None:
        import boto3

        _sqs = boto3.client("sqs")
    return _sqs


def verify_signature(secret: str, timestamp: str, signature: str, raw_body: bytes, now: float | None = None) -> bool:
    """Slack 서명 검증. basestring은 v0:<timestamp>:<원본 본문>, HMAC SHA-256, 결과 앞에 v0= 를 붙인다."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - ts) > MAX_SKEW_S:
        return False
    base = b"v0:" + str(ts).encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")  # 상수 시간 비교


def _json(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _ephemeral(text: str) -> dict:
    # 기본값이 ephemeral(명령을 친 사람에게만 보임)이지만 의도를 드러내려고 명시한다
    return _json(200, {"response_type": "ephemeral", "text": text})


def lambda_handler(event, context):
    # 헤더 이름은 대소문자를 가정하지 않는다 (Slack 문서 권고)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        print("reject: SLACK_SIGNING_SECRET is empty")
        return _json(401, {"error": "invalid signature"})
    if not verify_signature(secret, headers.get("x-slack-request-timestamp", ""), headers.get("x-slack-signature", ""), raw_bytes):
        # 값(서명, 비밀)은 남기지 않고 이유만 남긴다. 비밀을 잘못 붙여 넣은 경우와 시각이 어긋난 경우를 구분하는 데 쓴다
        print(f"reject: signature mismatch (has_timestamp={bool(headers.get('x-slack-request-timestamp'))}, has_signature={bool(headers.get('x-slack-signature'))}, body_bytes={len(raw_bytes)})")
        return _json(401, {"error": "invalid signature"})

    form = parse_qs(raw_bytes.decode("utf-8"), keep_blank_values=True)

    def field(name: str) -> str:
        return form.get(name, [""])[0]

    # 허용 목록이 비어 있으면 아무도 쓰지 못하게 한다 (실수로 열어 두지 않기 위함)
    allowed = {u.strip() for u in os.environ.get("ALLOWED_SLACK_USER_IDS", "").split(",") if u.strip()}
    user_id = field("user_id")
    if user_id not in allowed:
        print(f"reject: user not allowed (allowed_count={len(allowed)})")
        return _ephemeral("이 명령을 사용할 권한이 없어요.")

    text = field("text").strip()
    if not text:
        return _ephemeral("사용법: 명령 뒤에 요청 내용을 적어 주세요.")

    message = {"text": text, "response_url": field("response_url"), "user_id": user_id}
    t0 = time.monotonic()
    try:
        _sqs_client().send_message(
            QueueUrl=os.environ["QUEUE_URL"],
            MessageBody=json.dumps(message, ensure_ascii=False),
        )
    except Exception as exc:
        print(f"enqueue failed: {type(exc).__name__}")
        return _ephemeral("접수에 실패했어요. 잠시 후 다시 시도해 주세요.")

    print(f"enqueued in {int((time.monotonic() - t0) * 1000)} ms")
    return _ephemeral("접수했어요. 끝나면 이 대화로 알려 드릴게요.")
