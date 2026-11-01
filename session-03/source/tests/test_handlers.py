"""AWS 없이 내 컴퓨터에서 핸들러 동작을 확인하는 테스트.   실행:  make test

Lambda 핸들러는 '함수'일 뿐이라서, 이벤트(dict)를 직접 만들어 넣으면 로컬에서도 실행해 볼 수 있다.
"""
import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.fakes import BotoCoreError, ClientError, World, http_event, load_step, sqs_event_from_events

VALID = {"registrationId": "reg-1", "name": "홍길동", "email": "hong@example.com", "organization": "클라우드 동아리"}


def body(result):
    return json.loads(result["body"])


class InputContractTest(unittest.TestCase):
    """실습 단계가 바뀌어도 동일한 HTTP 입력 계약을 유지한다."""

    def test_null이나_잘못된_타입은_문자열로_저장하지_않는다(self):
        for step in ("step1", "step2"):
            for field in ("registrationId", "name", "email", "organization"):
                for value in (None, 123, True, [], {}):
                    with self.subTest(step=step, field=field, value=value):
                        world = World()
                        app = load_step(step, {}, world)
                        result = app.register.handler(http_event("POST", "/registrations", {**VALID, field: value}), None)
                        self.assertEqual(result["statusCode"], 400)
                        self.assertEqual(world.table.items, {})
                        self.assertEqual(world.events.entries, [])

    def test_본문은_JSON_객체여야_한다(self):
        for step in ("step1", "step2"):
            app = load_step(step, {}, World())
            for raw in (None, {}, "", "null", "[]", "123", '"text"', "not-json"):
                with self.subTest(step=step, raw=raw):
                    self.assertEqual(app.register.handler({"body": raw}, None)["statusCode"], 400)

    def test_base64_요청과_손상된_인코딩(self):
        for step, expected in (("step1", 201), ("step2", 202)):
            app = load_step(step, {}, World())
            encoded = base64.b64encode(json.dumps(VALID).encode()).decode()
            result = app.register.handler({"body": encoded, "isBase64Encoded": True}, None)
            self.assertEqual(result["statusCode"], expected)
            for raw in ("%%%", "a", "/w==", "한글"):
                with self.subTest(step=step, raw=raw):
                    result = app.register.handler({"body": raw, "isBase64Encoded": True}, None)
                    self.assertEqual(result["statusCode"], 400)

    def test_조회_경로의_잘못된_신청번호는_400(self):
        for step in ("step1", "step2"):
            app = load_step(step, {}, World())
            handlers = [app.status] + ([app.resend] if step == "step2" else [])
            for handler in handlers:
                for value in (None, "", "a b", 10):
                    with self.subTest(step=step, handler=handler.__name__, value=value):
                        result = handler.handler({"pathParameters": {"registrationId": value}}, None)
                        self.assertEqual(result["statusCode"], 400)


class Step1Test(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.app = load_step("step1", {}, self.world)

    def test_신청과_발급이_한_요청_안에서_끝난다(self):
        result = self.app.register.handler(http_event("POST", "/registrations", VALID), None)
        self.assertEqual(result["statusCode"], 201)
        self.assertEqual(body(result)["status"], "ISSUED")
        item = self.world.table.items["reg-1"]
        self.assertEqual(item["status"], "ISSUED")
        self.assertTrue(item["badgeCode"].startswith("BADGE-"))

    def test_잘못된_입력은_400(self):
        for bad in ({**VALID, "email": "nope"}, {**VALID, "name": ""}, {**VALID, "registrationId": "a b"}):
            result = self.app.register.handler(http_event("POST", "/registrations", bad), None)
            self.assertEqual(result["statusCode"], 400, bad)
        result = self.app.register.handler({"body": "이건 JSON이 아님"}, None)
        self.assertEqual(result["statusCode"], 400)

    def test_소속은_비워도_된다(self):
        result = self.app.register.handler(
            http_event("POST", "/registrations", {**VALID, "organization": ""}), None)
        self.assertEqual(result["statusCode"], 201)
        self.assertEqual(self.world.table.items["reg-1"]["badgeText"], "홍길동 / -")

    def test_같은_신청을_다시_보내면_참가증이_새로_발급된다_방어_없음(self):
        first = body(self.app.register.handler(http_event("POST", "/registrations", VALID), None))
        second = body(self.app.register.handler(http_event("POST", "/registrations", VALID), None))
        self.assertNotEqual(first["badgeCode"], second["badgeCode"])

    def test_상태_조회(self):
        self.app.register.handler(http_event("POST", "/registrations", VALID), None)
        found = self.app.status.handler(http_event("GET", "/registrations/reg-1", path_params={"registrationId": "reg-1"}), None)
        self.assertEqual(found["statusCode"], 200)
        self.assertEqual(body(found)["status"], "ISSUED")
        self.assertTrue(self.world.table.reads[-1]["ConsistentRead"])
        missing = self.app.status.handler(http_event("GET", "/registrations/x", path_params={"registrationId": "x"}), None)
        self.assertEqual(missing["statusCode"], 404)


class Step2Test(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.app = load_step("step2", {"IDEMPOTENT": "true"}, self.world)

    def post(self, payload=VALID):
        return self.app.register.handler(http_event("POST", "/registrations", payload), None)

    def run_issue(self, receive_count=1):
        return self.app.issue.handler(sqs_event_from_events(self.world, receive_count=receive_count), None)

    def test_접수는_바로_응답하고_이벤트를_발행한다(self):
        result = self.post()
        self.assertEqual(result["statusCode"], 202)
        self.assertEqual(body(result)["status"], "PENDING")
        self.assertEqual(self.world.table.items["reg-1"]["status"], "PENDING")
        self.assertNotIn("badgeCode", self.world.table.items["reg-1"])  # 아직 발급 전
        self.assertEqual(len(self.world.events.entries), 1)
        entry = self.world.events.entries[0]
        self.assertEqual(entry["Source"], "event-signup.registration")
        self.assertEqual(entry["DetailType"], "RegistrationReceived")
        self.assertEqual(entry["EventBusName"], "test-bus")
        self.assertEqual(json.loads(entry["Detail"])["registrationId"], "reg-1")

    def test_같은_신청을_다시_보내도_새_작업은_만들지_않는다(self):
        self.post()
        again = self.post()
        self.assertEqual(again["statusCode"], 200)
        self.assertEqual(len(self.world.events.entries), 1)
        self.assertTrue(self.world.table.reads[-1]["ConsistentRead"])

    def test_같은_번호로_입력을_바꾸면_409(self):
        self.post()
        for field, value in (("name", "다른이름"), ("email", "other@example.com"), ("organization", "")):
            with self.subTest(field=field):
                result = self.post({**VALID, field: value})
                self.assertEqual(result["statusCode"], 409)
        self.assertEqual(self.world.table.items["reg-1"]["name"], VALID["name"])
        self.assertEqual(len(self.world.events.entries), 1)

    def test_공백을_정리한_동일한_입력은_중복이다(self):
        self.post()
        result = self.post({key: f" {value} " for key, value in VALID.items()})
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(self.world.events.entries), 1)

    def test_이벤트_발행이_실패하면_503과_복구할_신청번호를_준다(self):
        self.world.events.fail = True
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.post()
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(body(result)["registrationId"], "reg-1")
        self.assertEqual(body(result)["status"], "PENDING")
        self.assertEqual(self.world.table.items["reg-1"]["status"], "PENDING")
        self.assertEqual(self.world.events.entries, [])
        self.assertIn('"event": "event_publish_failed"', output.getvalue())

        # 같은 POST는 이미 저장된 신청을 돌려주며, 복구 발행은 /resend로 수행한다.
        self.world.events.fail = False
        self.assertEqual(self.post()["statusCode"], 200)
        self.assertEqual(self.world.events.entries, [])
        resent = self.app.resend.handler(
            http_event("POST", "/registrations/reg-1/resend", path_params={"registrationId": "reg-1"}), None)
        self.assertEqual(resent["statusCode"], 202)
        self.run_issue()
        self.assertEqual(self.world.table.items["reg-1"]["status"], "ISSUED")

    def test_이벤트_API_예외도_503으로_알린다(self):
        for error in (BotoCoreError("network"), ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutEvents")):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.world.events.error = error
                result = self.post()
                self.assertEqual(result["statusCode"], 503)
                self.assertEqual(body(result)["status"], "PENDING")

    def test_발급_함수가_참가증을_기록한다(self):
        self.post()
        self.run_issue()
        item = self.world.table.items["reg-1"]
        self.assertEqual(item["status"], "ISSUED")
        self.assertEqual(item["issueCount"], 1)
        self.assertEqual(item["badgeText"], "홍길동 / 클라우드 동아리")

    def test_같은_메시지가_두_번_와도_DB_기록은_한_번만_된다_멱등성(self):
        self.post()
        self.run_issue()
        first_code = self.world.table.items["reg-1"]["badgeCode"]
        self.run_issue(receive_count=2)  # 큐가 같은 메시지를 다시 전달한 상황
        item = self.world.table.items["reg-1"]
        self.assertEqual(item["issueCount"], 1)
        self.assertEqual(item["badgeCode"], first_code)

    def test_resend로_보낸_작업도_중복_발급되지_않는다(self):
        self.post()
        self.run_issue()
        resent = self.app.resend.handler(
            http_event("POST", "/registrations/reg-1/resend", path_params={"registrationId": "reg-1"}), None)
        self.assertEqual(resent["statusCode"], 202)
        self.assertEqual(len(self.world.events.entries), 2)
        self.run_issue()
        self.assertEqual(self.world.table.items["reg-1"]["issueCount"], 1)

    def test_resend_없는_신청은_404(self):
        result = self.app.resend.handler(
            http_event("POST", "/registrations/none/resend", path_params={"registrationId": "none"}), None)
        self.assertEqual(result["statusCode"], 404)

    def test_resend도_이벤트_발행_실패를_503으로_알린다(self):
        self.post()
        self.world.events.fail = True
        result = self.app.resend.handler(
            http_event("POST", "/registrations/reg-1/resend", path_params={"registrationId": "reg-1"}), None)
        self.assertEqual(result["statusCode"], 503)
        self.assertEqual(body(result)["registrationId"], "reg-1")
        self.assertEqual(body(result)["status"], "PENDING")

    def test_없는_신청의_발급결과를_새로_만들지_않는다(self):
        for setting in ("true", "false"):
            with self.subTest(idempotent=setting):
                app = load_step("step2", {"IDEMPOTENT": setting}, self.world)
                with self.assertRaises(LookupError):
                    app.issue.save_badge("missing", "BADGE-123", "아무개 / ASBG")
                self.assertEqual(self.world.table.items, {})

    def test_발급_실패는_로그를_남기고_재시도를_위해_예외를_올린다(self):
        self.post()
        output = io.StringIO()
        with redirect_stdout(output), patch.object(self.app.issue, "make_badge", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                self.run_issue()
        self.assertIn('"event": "issue_failed"', output.getvalue())
        self.assertIn('"registrationId": "reg-1"', output.getvalue())
        self.assertEqual(self.world.table.items["reg-1"]["status"], "PENDING")

    def test_상태_조회는_발급_횟수도_JSON으로_돌려준다(self):
        self.post()
        self.run_issue()
        found = self.app.status.handler(
            http_event("GET", "/registrations/reg-1", path_params={"registrationId": "reg-1"}), None)
        self.assertEqual(body(found)["issueCount"], 1)
        self.assertTrue(self.world.table.reads[-1]["ConsistentRead"])


class Step2NonIdempotentTest(unittest.TestCase):
    """IDEMPOTENT=false 로 배포했을 때: 조건부 쓰기가 없으면 어떻게 되는지 보여준다."""

    def test_같은_메시지가_두_번_오면_두_번_발급된다(self):
        world = World()
        app = load_step("step2", {"IDEMPOTENT": "false"}, world)
        app.register.handler(http_event("POST", "/registrations", VALID), None)
        message = sqs_event_from_events(world)
        app.issue.handler(message, None)
        first_code = world.table.items["reg-1"]["badgeCode"]
        app.issue.handler(message, None)
        item = world.table.items["reg-1"]
        self.assertEqual(item["issueCount"], 2)
        self.assertNotEqual(item["badgeCode"], first_code)


if __name__ == "__main__":
    unittest.main()
