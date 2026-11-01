"""Slack 입구(ack_handler)와 전달(deliver) 테스트. AWS와 Slack 없이 돈다."""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ack_handler  # noqa: E402
from app import deliver as deliver_mod  # noqa: E402

SECRET = "test-secret"


def sign(body: bytes, ts: int | None = None, secret: str = SECRET):
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return ts, sig


def make_event(fields: dict, ts=None, secret=SECRET, b64=False, header_case="lower"):
    body = urlencode(fields).encode()
    t, s = sign(body, ts, secret)
    names = ("x-slack-request-timestamp", "x-slack-signature")
    if header_case == "title":
        names = ("X-Slack-Request-Timestamp", "X-Slack-Signature")
    return {
        "headers": {names[0]: t, names[1]: s},
        "body": base64.b64encode(body).decode() if b64 else body.decode(),
        "isBase64Encoded": b64,
    }


class FakeSqs:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, **kw):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(kw)


class AckTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(SLACK_SIGNING_SECRET=SECRET, ALLOWED_SLACK_USER_IDS="U1, U2", QUEUE_URL="https://sqs/q")
        self.sqs = FakeSqs()
        ack_handler._sqs = self.sqs

    FIELDS = {"user_id": "U1", "text": "  hello  ", "response_url": "https://hooks.slack.com/commands/x"}

    def call(self, event):
        r = ack_handler.lambda_handler(event, None)
        return r["statusCode"], json.loads(r["body"])

    def test_valid_request_is_enqueued_and_acked(self):
        status, body = self.call(make_event(self.FIELDS))
        self.assertEqual(status, 200)
        self.assertEqual(body["response_type"], "ephemeral")
        sent = json.loads(self.sqs.sent[0]["MessageBody"])
        self.assertEqual(sent, {"text": "hello", "response_url": self.FIELDS["response_url"], "user_id": "U1"})
        self.assertEqual(self.sqs.sent[0]["QueueUrl"], "https://sqs/q")

    def test_bad_signature_rejected_and_not_enqueued(self):
        status, _ = self.call(make_event(self.FIELDS, secret="wrong"))
        self.assertEqual(status, 401)
        self.assertEqual(self.sqs.sent, [])

    def test_stale_timestamp_rejected(self):
        status, _ = self.call(make_event(self.FIELDS, ts=int(time.time()) - 301))
        self.assertEqual(status, 401)

    def test_missing_headers_rejected(self):
        status, _ = self.call({"headers": {}, "body": "x=1"})
        self.assertEqual(status, 401)

    def test_missing_secret_config_rejects_everything(self):
        os.environ["SLACK_SIGNING_SECRET"] = ""
        status, _ = self.call(make_event(self.FIELDS))
        self.assertEqual(status, 401)

    def test_unlisted_user_gets_message_and_nothing_is_enqueued(self):
        status, body = self.call(make_event({**self.FIELDS, "user_id": "U9"}))
        self.assertEqual(status, 200)
        self.assertIn("권한", body["text"])
        self.assertEqual(self.sqs.sent, [])

    def test_empty_allowlist_denies_everyone(self):
        os.environ["ALLOWED_SLACK_USER_IDS"] = ""
        status, body = self.call(make_event(self.FIELDS))
        self.assertIn("권한", body["text"])
        self.assertEqual(self.sqs.sent, [])

    def test_empty_text_returns_usage(self):
        _, body = self.call(make_event({**self.FIELDS, "text": "   "}))
        self.assertIn("사용법", body["text"])
        self.assertEqual(self.sqs.sent, [])

    def test_base64_body_and_title_case_headers(self):
        status, _ = self.call(make_event(self.FIELDS, b64=True, header_case="title"))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sqs.sent), 1)

    def test_enqueue_failure_still_acks_with_message(self):
        ack_handler._sqs = FakeSqs(fail=True)
        status, body = self.call(make_event(self.FIELDS))
        self.assertEqual(status, 200)
        self.assertIn("실패", body["text"])

    def test_rejections_log_the_reason_without_secrets(self):
        import contextlib, io
        buf = io.StringIO()
        ev = make_event(self.FIELDS)
        ev["headers"]["x-slack-signature"] = "v0=" + "0" * 64
        with contextlib.redirect_stdout(buf):
            status, _ = self.call(ev)
        self.assertEqual(status, 401)
        out = buf.getvalue()
        self.assertIn("reject: signature mismatch", out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("0" * 64, out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.call(make_event({**self.FIELDS, "user_id": "U9"}))
        self.assertIn("reject: user not allowed", buf.getvalue())
        os.environ["SLACK_SIGNING_SECRET"] = ""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status, _ = self.call(make_event(self.FIELDS))
        self.assertEqual(status, 401)
        self.assertIn("SLACK_SIGNING_SECRET is empty", buf.getvalue())

    def test_enqueue_is_logged_with_timing(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.call(make_event(self.FIELDS))
        self.assertRegex(buf.getvalue(), r"enqueued in \d+ ms")

    def test_client_is_created_at_import_time_when_boto3_exists(self):
        import importlib, types
        fake = types.ModuleType("boto3")
        made = []
        fake.client = lambda name: made.append(name) or ("client", name)
        saved = sys.modules.get("boto3")
        sys.modules["boto3"] = fake
        try:
            mod = importlib.reload(ack_handler)
            self.assertEqual(made, ["sqs"])                   # 함수 시작(import) 때 한 번 만든다
            self.assertEqual(mod._sqs, ("client", "sqs"))
        finally:
            if saved is None:
                sys.modules.pop("boto3", None)
            else:
                sys.modules["boto3"] = saved
            importlib.reload(ack_handler)
        self.assertIsNone(ack_handler._sqs)                   # boto3가 없는 환경에서는 None으로 두고 첫 호출 때 시도한다

    def test_korean_text_roundtrip(self):
        self.call(make_event({**self.FIELDS, "text": "한글 요청"}))
        self.assertEqual(json.loads(self.sqs.sent[0]["MessageBody"])["text"], "한글 요청")


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self._orig = deliver_mod.post_to_response_url
        deliver_mod.post_to_response_url = lambda url, text, response_type="ephemeral": self.posts.append((url, text))

    def tearDown(self):
        deliver_mod.post_to_response_url = self._orig

    PAYLOAD = {"text": "hi", "stop_reason": "end_turn", "turns": 2, "input_tokens": 10, "output_tokens": 5}

    def test_posts_to_slack_url(self):
        deliver_mod.deliver(self.PAYLOAD, {"response_url": "https://hooks.slack.com/commands/a"})
        self.assertEqual(len(self.posts), 1)
        self.assertIn("hi", self.posts[0][1])
        self.assertIn("종료=end_turn", self.posts[0][1])

    def test_non_slack_url_is_not_called(self):
        for bad in ("http://hooks.slack.com/x", "https://evil.example.com/x", "https://slack.com.evil.io/x"):
            deliver_mod.deliver(self.PAYLOAD, {"response_url": bad})
        self.assertEqual(self.posts, [])

    def test_no_url_prints_instead(self):
        deliver_mod.deliver(self.PAYLOAD, {})
        self.assertEqual(self.posts, [])

    def test_long_text_is_chunked_and_capped_at_five_posts(self):
        deliver_mod.notify({"response_url": "https://hooks.slack.com/c"}, "가" * 20_000)
        self.assertEqual(len(self.posts), 5)
        self.assertTrue(self.posts[-1][1].endswith("잘렸어요)"))
        self.assertTrue(all(len(t) <= deliver_mod.CHUNK_CHARS for _, t in self.posts))


if __name__ == "__main__":
    unittest.main()
