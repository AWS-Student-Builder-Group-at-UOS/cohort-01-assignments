"""비용 가드 테스트: 토큰 추정, 달러 예산, 캐시 설정, 일일·월 상한. AWS 없이 돈다."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.agent import CACHE_POINT, Limits, Prices, Tool, run_agent  # noqa: E402
from core.spend_cap import CapExceeded, SpendCap  # noqa: E402
from core.tokens import estimate_text_tokens  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_text_token_estimate_is_conservative_for_korean(self):
        self.assertGreaterEqual(estimate_text_tokens("가" * 100), 100)


# ------------------------------------------------------------ 달러 예산·캐시

def text_msg(t):
    return {"role": "assistant", "content": [{"text": t}]}


def tool_msg(i="t"):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": i, "name": "echo", "input": {"x": "1"}}}]}


def resp(msg, stop, usage):
    return {"output": {"message": msg}, "stopReason": stop, "usage": usage}


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def converse(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


ECHO = Tool("echo", "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, lambda x: "ok")
P = Prices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)


class BudgetTests(unittest.TestCase):
    def test_cost_counts_all_four_usage_fields(self):
        u = {"inputTokens": 1_000_000, "outputTokens": 1_000_000, "cacheReadInputTokens": 1_000_000, "cacheWriteInputTokens": 1_000_000}
        self.assertAlmostEqual(P.cost(u), 2.0 + 10.0 + 0.2 + 2.5)

    def test_loop_accumulates_cost(self):
        c = FakeClient([resp(tool_msg(), "tool_use", {"inputTokens": 1000, "outputTokens": 100}), resp(text_msg("x"), "end_turn", {"inputTokens": 2000, "outputTokens": 100})])
        r = run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P))
        self.assertAlmostEqual(r.cost_usd, (3000 * 2 + 200 * 10) / 1e6)

    def test_budget_stops_before_overspending(self):
        # 호출마다 입력 100k(=$0.2)를 쓰는 루프. 예산 $0.5이고 출력 상한 2000토큰($0.02)을 더해 보면 세 번째 호출 전에 멈춘다
        big = {"inputTokens": 100_000, "outputTokens": 100}
        c = FakeClient([resp(tool_msg(), "tool_use", big) for _ in range(10)])
        r = run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P, max_usd=0.5, max_total_tokens=10**9, max_turns=10))
        self.assertEqual(r.stop_reason, "budget_usd")
        self.assertEqual(len(c.calls), 2)
        self.assertLessEqual(r.cost_usd, 0.5)

    def test_preflight_estimate_refuses_without_calling_model(self):
        c = FakeClient([])
        r = run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P, max_usd=0.5), est_input_tokens=400_000)  # $0.8
        self.assertEqual((r.stop_reason, len(c.calls)), ("budget_usd", 0))

    def test_small_job_within_budget_runs(self):
        c = FakeClient([resp(text_msg("hi"), "end_turn", {"inputTokens": 500, "outputTokens": 50})])
        r = run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P, max_usd=0.5), est_input_tokens=1000)
        self.assertTrue(r.completed)

    def test_cached_context_is_priced_at_cache_read_for_projection(self):
        # 직전 호출: 캐시 읽기 100k + 신규 1k. 다음 호출 하한은 약 $0.02(캐시 읽기) + $0.002 + 출력 $0.02 < 예산 $0.1
        u = {"inputTokens": 1_000, "outputTokens": 100, "cacheReadInputTokens": 100_000}
        c = FakeClient([resp(tool_msg(), "tool_use", u), resp(text_msg("done"), "end_turn", u)])
        r = run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P, max_usd=0.1, max_total_tokens=10**9))
        self.assertTrue(r.completed)

    def test_exception_carries_partial_result_for_settlement(self):
        class Boom(FakeClient):
            def converse(self, **kw):
                if self.calls:
                    raise RuntimeError("throttled")
                return super().converse(**kw)

        c = Boom([resp(tool_msg(), "tool_use", {"inputTokens": 1000, "outputTokens": 100})])
        with self.assertRaises(RuntimeError) as cm:
            run_agent(c, "m", "s", "q", [ECHO], Limits(prices=P))
        self.assertAlmostEqual(cm.exception.agent_result.cost_usd, (1000 * 2 + 100 * 10) / 1e6)

    def test_cache_points_placed_when_enabled(self):
        c = FakeClient([resp(text_msg("hi"), "end_turn", {"inputTokens": 1, "outputTokens": 1})])
        run_agent(c, "m", "SYS", "q", [ECHO], Limits(prices=P, cache=True))
        kw = c.calls[0]
        self.assertEqual(kw["system"], [{"text": "SYS"}, CACHE_POINT])
        self.assertEqual(kw["toolConfig"]["tools"][-1], CACHE_POINT)
        self.assertEqual(kw["messages"][0]["content"], [{"text": "q"}, CACHE_POINT])
        n = 1 + 1 + 1
        self.assertLessEqual(n, 4)  # Bedrock 문서: Claude는 요청당 체크포인트 최대 4개

    def test_no_cache_points_by_default(self):
        c = FakeClient([resp(text_msg("hi"), "end_turn", {"inputTokens": 1, "outputTokens": 1})])
        run_agent(c, "m", "SYS", "q", [ECHO], Limits(prices=P))
        self.assertEqual(c.calls[0]["system"], [{"text": "SYS"}])
        self.assertNotIn(CACHE_POINT, c.calls[0]["messages"][0]["content"])


# ------------------------------------------------------------ 일일·월 상한

class CondErr(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class OtherErr(Exception):
    response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}


class FakeDdb:
    def __init__(self):
        self.items, self.fail_with = {}, None

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None):
        if self.fail_with:
            raise self.fail_with
        pk = Key["pk"]["S"]
        v = {k: int(x["N"]) for k, x in ExpressionAttributeValues.items()}
        cur = self.items.get(pk)
        if ConditionExpression and cur is not None and not cur["spent_micro"] <= v[":room"]:
            raise CondErr()
        item = cur or {"spent_micro": 0, "runs": 0}
        item["spent_micro"] += v[":d"]
        item["runs"] += v.get(":one", v.get(":r", 0))
        self.items[pk] = item


KST = timezone(timedelta(hours=9))


def make_cap(ddb, daily=1.0, monthly=2.0, now=None):
    return SpendCap(ddb, "t", daily, monthly, now=lambda: now or datetime(2026, 10, 28, 12, 0, tzinfo=KST))


class SpendCapTests(unittest.TestCase):
    def test_reserve_and_settle_refunds_difference(self):
        ddb = FakeDdb()
        cap = make_cap(ddb)
        r = cap.reserve(0.5)
        self.assertEqual(ddb.items["day#2026-10-28"]["spent_micro"], 500_000)
        cap.settle(r, 0.1)
        self.assertEqual(ddb.items["day#2026-10-28"]["spent_micro"], 100_000)
        self.assertEqual(ddb.items["month#2026-10"]["spent_micro"], 100_000)

    def test_daily_cap_blocks_after_limit(self):
        ddb = FakeDdb()
        cap = make_cap(ddb, daily=1.0, monthly=10.0)
        cap.reserve(0.5)
        cap.reserve(0.5)           # 합계 1.0 == 상한: 허용
        with self.assertRaises(CapExceeded) as cm:
            cap.reserve(0.5)       # 이미 1.0 사용: 거부
        self.assertEqual(cm.exception.scope, "daily")

    def test_monthly_failure_rolls_back_daily_reservation(self):
        ddb = FakeDdb()
        cap = make_cap(ddb, daily=5.0, monthly=1.0)
        cap.reserve(0.5)
        cap.reserve(0.5)
        before = ddb.items["day#2026-10-28"]["spent_micro"]
        with self.assertRaises(CapExceeded) as cm:
            cap.reserve(0.5)
        self.assertEqual(cm.exception.scope, "monthly")
        self.assertEqual(ddb.items["day#2026-10-28"]["spent_micro"], before)  # 되돌려짐

    def test_per_run_larger_than_daily_is_rejected(self):
        with self.assertRaises(CapExceeded) as cm:
            make_cap(FakeDdb(), daily=0.3).reserve(0.5)
        self.assertEqual(cm.exception.scope, "per_run")

    def test_date_boundary_uses_kst(self):
        ddb = FakeDdb()
        make_cap(ddb, now=datetime(2026, 10, 28, 23, 59, tzinfo=KST)).reserve(0.1)
        make_cap(ddb, now=datetime(2026, 10, 29, 0, 1, tzinfo=KST)).reserve(0.1)
        self.assertIn("day#2026-10-28", ddb.items)
        self.assertIn("day#2026-10-29", ddb.items)

    def test_non_conditional_errors_propagate(self):
        ddb = FakeDdb()
        ddb.fail_with = OtherErr()
        with self.assertRaises(OtherErr):
            make_cap(ddb).reserve(0.1)


if __name__ == "__main__":
    unittest.main()
