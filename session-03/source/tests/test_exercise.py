"""실습 2의 오류 실험을 위한 테스트.   실행:  make test-exercise

처음에는 이 테스트가 '실패'하는 게 정상이다.
클라우드에서 로그로 원인을 찾아 step2/src/issue.py 를 고친 뒤, 이 테스트가 통과하는지 확인해 보자.
(테스트로 먼저 재현하고 → 고치고 → 통과를 확인한 다음 재배포하는 것이 개발자의 일반적인 순서다.)
"""
import unittest

from tests.fakes import World, http_event, load_step, sqs_event_from_events


class OrganizationOptionalTest(unittest.TestCase):
    def test_소속을_비우고_신청해도_참가증이_발급되어야_한다(self):
        for optional in ({"organization": ""}, {}):
            with self.subTest(optional=optional):
                world = World()
                app = load_step("step2", {"IDEMPOTENT": "true"}, world)
                payload = {"registrationId": "reg-9", "name": "김개발", "email": "dev@example.com", **optional}
                accepted = app.register.handler(http_event("POST", "/registrations", payload), None)
                self.assertEqual(accepted["statusCode"], 202)

                app.issue.handler(sqs_event_from_events(world), None)  # 오류가 나면 여기서 예외가 발생한다.

                item = world.table.items["reg-9"]
                self.assertEqual(item["status"], "ISSUED")
                self.assertEqual(item["badgeText"], "김개발 / -")
                self.assertEqual(item["issueCount"], 1)


if __name__ == "__main__":
    unittest.main()
