"""핸들러 연동 테스트 (prepare/finish 연결, 지출 상한, 오래된 메시지, 한도 덮어쓰기)."""

import copy
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from core.agent import CACHE_POINT, Limits, Prices, Tool, run_agent  # noqa: E402
from core.app_api import Prepared, Refuse  # noqa: E402
from core.spend_cap import SpendCap  # noqa: E402
from test_guard import FakeClient, FakeDdb, resp, text_msg, tool_msg, ECHO, P  # noqa: E402

KST = timezone(timedelta(hours=9))


def make_cap(ddb, daily=1.0, monthly=10.0):
    return SpendCap(ddb, "t", daily, monthly, now=lambda: datetime(2026, 10, 28, 12, 0, tzinfo=KST))


class Ctx:
    def get_remaining_time_in_millis(self):
        return 120_000


class HandlerTests(unittest.TestCase):
    def setUp(self):
        import handler

        self.h = handler
        os.environ.update(MODEL_ID="m", CAP_TABLE="t", MAX_USD_PER_RUN="0.5")
        handler.MODEL_ID = "m"
        self._real_entry = handler.entry
        self.ddb = FakeDdb()
        handler._cap = make_cap(self.ddb)
        self.delivered, self.notified = [], []
        handler.deliver = lambda payload, request: self.delivered.append(payload)
        handler.notify = lambda request, text: self.notified.append(text)
        self.prepared = Prepared("SYS", "ping", [ECHO])
        self.install_fake_app(lambda body: self.prepared)

    def tearDown(self):
        self.h.entry = self._real_entry

    def install_fake_app(self, prepare, finish=None):
        """handler가 부르는 entry 모듈을 prepare/finish만 가진 가짜로 바꾼다(tearDown에서 되돌림)."""
        self.h.entry = types.SimpleNamespace(prepare=prepare, finish=finish or (lambda prepared, result: result.text))

    EVENT = {"Records": [{"body": json.dumps({"text": "ping"})}]}

    def test_success_settles_actual_cost_and_uses_finish(self):
        self.install_fake_app(lambda b: self.prepared, lambda p, r: "FINISHED:" + r.text)
        self.h._client = FakeClient([resp(text_msg("pong"), "end_turn", {"inputTokens": 1000, "outputTokens": 100})])
        self.h.lambda_handler(self.EVENT, Ctx())
        self.assertEqual(self.delivered[0]["text"], "FINISHED:pong")
        self.assertEqual(self.ddb.items["day#2026-10-28"]["spent_micro"], 1000 * 2 + 100 * 10)

    def test_refuse_skips_everything_and_costs_nothing(self):
        def refuse(body):
            raise Refuse("지금은 실행할 수 없어요")

        self.install_fake_app(refuse)
        self.h._client = FakeClient([])
        self.h.lambda_handler(self.EVENT, Ctx())
        self.assertEqual(self.notified, ["지금은 실행할 수 없어요"])
        self.assertEqual(self.h._client.calls, [])
        self.assertEqual(self.ddb.items, {})  # 예약조차 하지 않는다

    def test_prepare_error_notifies_and_reraises_without_reserving(self):
        def boom(body):
            raise KeyError("x")

        self.install_fake_app(boom)
        with self.assertRaises(KeyError):
            self.h.lambda_handler(self.EVENT, Ctx())
        self.assertTrue(any("준비 중 오류" in n for n in self.notified))
        self.assertEqual(self.ddb.items, {})

    def test_stale_message_is_skipped(self):
        self.h._now = lambda: 1_000_000.0
        self.h._client = FakeClient([])
        old = {"Records": [{"body": "{}", "attributes": {"SentTimestamp": str(int((1_000_000 - 1600) * 1000))}}]}
        self.h.lambda_handler(old, Ctx())
        self.assertEqual((self.h._client.calls, self.delivered, self.notified, self.ddb.items), ([], [], [], {}))

    def test_fresh_message_is_processed(self):
        self.h._now = lambda: 1_000_000.0
        self.h._client = FakeClient([resp(text_msg("ok"), "end_turn", {"inputTokens": 1, "outputTokens": 1})])
        fresh = {"Records": [{"body": json.dumps({"text": "x"}), "attributes": {"SentTimestamp": str(int((1_000_000 - 60) * 1000))}}]}
        self.h.lambda_handler(fresh, Ctx())
        self.assertEqual(len(self.delivered), 1)

    def test_limits_override_applies(self):
        self.prepared = Prepared("SYS", "ping", [ECHO], limits_override={"max_turns": 1})
        self.h._client = FakeClient([resp(tool_msg(), "tool_use", {"inputTokens": 1, "outputTokens": 1})] * 3)
        self.h.lambda_handler(self.EVENT, Ctx())
        self.assertEqual(self.delivered[0]["turns"], 1)
        self.assertEqual(self.delivered[0]["stop_reason"], "max_turns")

    def test_finish_failure_still_delivers_and_does_not_retry(self):
        def bad_finish(p, r):
            raise ValueError("render bug")

        self.install_fake_app(lambda b: self.prepared, bad_finish)
        self.h._client = FakeClient([resp(text_msg("raw answer"), "end_turn", {"inputTokens": 1, "outputTokens": 1})])
        self.h.lambda_handler(self.EVENT, Ctx())  # 예외 없이 끝난다
        self.assertIn("raw answer", self.delivered[0]["text"])
        self.assertIn("ValueError", self.delivered[0]["text"])

    def test_cap_exceeded_after_prepare_skips_model(self):
        self.h._cap.reserve(0.5)
        self.h._cap.reserve(0.5)  # 일일 상한(1.0) 소진
        self.h._client = FakeClient([])
        self.h.lambda_handler(self.EVENT, Ctx())
        self.assertEqual(self.h._client.calls, [])
        self.assertIn("한도", self.notified[0])

    def test_failure_settles_partial_cost_and_reraises(self):
        class Boom(FakeClient):
            def converse(self, **kw):
                if self.calls:
                    raise RuntimeError("throttled")
                return super().converse(**kw)

        self.h._client = Boom([resp(tool_msg(), "tool_use", {"inputTokens": 1000, "outputTokens": 100})])
        with self.assertRaises(RuntimeError):
            self.h.lambda_handler(self.EVENT, Ctx())
        self.assertEqual(self.ddb.items["day#2026-10-28"]["spent_micro"], 1000 * 2 + 100 * 10)
        self.assertTrue(any("오류" in n for n in self.notified))

    def test_missing_cap_table_refuses_to_run(self):
        os.environ["CAP_TABLE"] = ""
        with self.assertRaises(RuntimeError):
            self.h.lambda_handler(self.EVENT, Ctx())


class RollingCacheTests(unittest.TestCase):
    def test_tail_cache_point_moves_and_total_stays_within_four(self):
        u = {"inputTokens": 10, "outputTokens": 1}
        c = FakeClient([resp(tool_msg("a"), "tool_use", u), resp(tool_msg("b"), "tool_use", u), resp(text_msg("done"), "end_turn", u)])
        r = run_agent(c, "m", "SYS", "q", [ECHO], Limits(prices=P, cache=True, max_total_tokens=10**9))
        msgs = r.messages
        # 첫 메시지의 체크포인트는 유지, 마지막 user 메시지에만 꼬리 체크포인트가 있고 중간 것은 사라진다
        self.assertIn(CACHE_POINT, msgs[0]["content"])
        tails = [i for i, m in enumerate(msgs) if i > 0 and CACHE_POINT in m["content"]]
        self.assertEqual(tails, [len(msgs) - 2])  # 마지막 assistant 응답 바로 앞의 user(toolResult) 메시지
        total = 1 + 1 + sum(CACHE_POINT in m["content"] for m in msgs)  # 시스템 + 도구 + 메시지
        self.assertLessEqual(total, 4)

    def test_second_call_sees_cache_point_on_first_tool_result(self):
        u = {"inputTokens": 10, "outputTokens": 1}
        seen = []

        class Spy(FakeClient):
            def converse(self, **kw):
                seen.append([i for i, m in enumerate(kw["messages"]) if CACHE_POINT in m["content"]])
                return super().converse(**kw)

        c = Spy([resp(tool_msg("a"), "tool_use", u), resp(tool_msg("b"), "tool_use", u), resp(text_msg("done"), "end_turn", u)])
        run_agent(c, "m", "SYS", "q", [ECHO], Limits(prices=P, cache=True, max_total_tokens=10**9))
        self.assertEqual(seen, [[0], [0, 2], [0, 4]])  # 호출마다 꼬리 체크포인트가 가장 최근 toolResult로 옮겨 간다


class ContinuationTests(unittest.TestCase):
    U = {"inputTokens": 10, "outputTokens": 5}

    def run_it(self, responses, **kw):
        class Snap(FakeClient):  # 호출 시점의 messages를 복사해 둔다(루프가 같은 리스트를 계속 늘리기 때문)
            def converse(self, **k):
                k = dict(k, messages=copy.deepcopy(k["messages"]))
                return super().converse(**k)

        c = Snap(responses)
        r = run_agent(c, "m", "SYS", "q", [ECHO], Limits(prices=P, max_total_tokens=10**9, **kw))
        return c, r

    def test_truncated_final_answer_is_continued_and_joined(self):
        c, r = self.run_it(
            [resp(text_msg('{"a": "가나'), "max_tokens", self.U), resp(text_msg('다라"}'), "end_turn", self.U)],
            max_continuations=1,
        )
        self.assertEqual(r.text, '{"a": "가나다라"}')
        self.assertEqual((r.stop_reason, r.continuations, r.turns), ("end_turn", 1, 2))
        last = c.calls[1]["messages"]
        self.assertEqual([m["role"] for m in last], ["user", "assistant", "user"])  # 역할이 번갈아 가야 한다
        self.assertIn("이어서", last[-1]["content"][0]["text"])
        self.assertEqual(last[1]["content"][0]["text"], '{"a": "가나')

    def test_default_does_not_continue(self):
        c, r = self.run_it([resp(text_msg("앞부분"), "max_tokens", self.U)])
        self.assertEqual((r.stop_reason, r.text, r.continuations, len(c.calls)), ("max_tokens", "앞부분", 0, 1))

    def test_continuation_count_is_limited(self):
        c, r = self.run_it(
            [resp(text_msg("a"), "max_tokens", self.U), resp(text_msg("b"), "max_tokens", self.U), resp(text_msg("c"), "max_tokens", self.U)],
            max_continuations=1,
        )
        self.assertEqual((r.stop_reason, r.text, r.continuations, len(c.calls)), ("max_tokens", "ab", 1, 2))

    def test_truncated_tool_call_is_not_continued(self):
        c, r = self.run_it([resp(tool_msg(), "max_tokens", self.U)], max_continuations=2)
        self.assertEqual((r.stop_reason, r.continuations, len(c.calls)), ("max_tokens", 0, 1))

    def test_truncated_text_with_tool_call_is_not_continued(self):
        both = {"role": "assistant", "content": [{"text": "생각 중"}, {"toolUse": {"toolUseId": "t", "name": "echo", "input": {"x": "1"}}}]}
        c, r = self.run_it([resp(both, "max_tokens", self.U)], max_continuations=2)
        self.assertEqual((r.continuations, len(c.calls)), (0, 1))

    def test_empty_truncated_text_is_not_continued(self):
        c, r = self.run_it([resp(text_msg("  "), "max_tokens", self.U)], max_continuations=2)
        self.assertEqual((r.continuations, len(c.calls)), (0, 1))

    def test_carry_resets_after_tool_turns(self):
        c, r = self.run_it(
            [resp(text_msg("앞"), "max_tokens", self.U), resp(tool_msg(), "tool_use", self.U), resp(text_msg("끝"), "end_turn", self.U)],
            max_continuations=2,
        )
        self.assertEqual(r.text, "끝")  # 도구 호출 앞의 잘린 텍스트가 최종 답에 섞이지 않는다

    def test_cache_point_still_within_four_after_continuation(self):
        c, r = self.run_it(
            [resp(text_msg("a"), "max_tokens", self.U), resp(text_msg("b"), "end_turn", self.U)], max_continuations=1, cache=True
        )
        m = c.calls[1]["messages"]
        total = 1 + 1 + sum(CACHE_POINT in x["content"] for x in m)  # 시스템 + 도구 + 메시지
        self.assertLessEqual(total, 4)
        self.assertIn(CACHE_POINT, m[-1]["content"])  # 꼬리 체크포인트는 "이어서 써" 메시지로 옮겨 간다


class ChunkTests(unittest.TestCase):
    def test_chunks_prefer_paragraph_boundaries(self):
        from app import deliver as d

        text = "\n\n".join(f"문단{i} " + "가" * 900 for i in range(8))
        parts = d._chunks(text)
        self.assertTrue(all(len(p) <= d.CHUNK_CHARS for p in parts))
        self.assertTrue(all(p.startswith("문단") for p in parts))  # 문단 중간에서 시작하지 않는다
        self.assertEqual("\n\n".join(parts), text)

    def test_fenced_block_with_blank_lines_is_never_split_when_it_fits(self):
        from app import deliver as d

        block = "```\n첫 문단\n\n둘째 문단\n\n셋째 문단\n```"
        text = "가" * (d.CHUNK_CHARS - 20) + "\n\n" + block  # 앞 덩어리 끝에 블록의 앞부분만 들어갈 자리
        parts = d._chunks(text)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1], block)  # 블록은 통째로 다음 메시지로 넘어간다

    def test_oversize_fenced_block_is_rewrapped_in_every_piece(self):
        from app import deliver as d

        text = "```\n" + "\n".join("줄" + "가" * 200 for _ in range(60)) + "\n```"
        parts = d._chunks(text)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertTrue(p.startswith("```") and p.rstrip().endswith("```") and p.count("```") == 2)
            self.assertLessEqual(len(p), d.CHUNK_CHARS)

class TemplatePriceWiringTests(unittest.TestCase):
    def test_every_price_env_the_handler_reads_is_wired_in_the_template(self):
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parent.parent
        tpl = (root / "template.yaml").read_text(encoding="utf-8")
        code = (root / "src" / "handler.py").read_text(encoding="utf-8")
        names = set(re.findall(r'environ\.get\("(PRICE_[A-Z_]+)"', code))
        self.assertEqual(len(names), 4)
        worker_env = tpl[tpl.index("WorkerFunction:"):]
        for n in names:
            self.assertIn(f"{n}: !If", worker_env, n)  # 비우면 !Ref AWS::NoValue로 빠져 코드 기본값을 쓴다
        ack_block = tpl[tpl.index("  AckFunction:"):tpl.index("  AckFunction:") + 900]
        self.assertRegex(ack_block, r"MemorySize: (512|1024)")  # 콜드 스타트가 3초 응답 제한에 걸리지 않게 입구는 256MB보다 크게



if __name__ == "__main__":
    unittest.main()
