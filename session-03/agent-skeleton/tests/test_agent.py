"""AWS 자격 증명 없이 도는 테스트. 실행: python -m unittest discover -s tests -v"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.agent import Limits, Tool, run_agent  # noqa: E402


def tool_use_msg(*uses):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": i, "name": n, "input": a}} for i, n, a in uses]}


def text_msg(text):
    return {"role": "assistant", "content": [{"text": text}]}


def resp(message, stop, i=10, o=5):
    return {"output": {"message": message}, "stopReason": stop, "usage": {"inputTokens": i, "outputTokens": o}}


class FakeClient:
    """미리 정한 응답을 순서대로 돌려주고, 받은 요청을 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        # messages는 이후에도 늘어나므로 호출 시점의 길이만 기록한다
        self.calls.append({**kwargs, "n_messages": len(kwargs["messages"])})
        return self.responses.pop(0)


ECHO = Tool("echo", "echo", {"type": "object", "properties": {"x": {"type": "string"}}}, lambda x: f"got {x}")
BOOM = Tool("boom", "fail", {"type": "object", "properties": {}}, lambda: 1 / 0)
BIG = Tool("big", "big", {"type": "object", "properties": {}}, lambda: "a" * 100)


class AgentLoopTests(unittest.TestCase):
    def test_tool_then_final_answer(self):
        c = FakeClient([resp(tool_use_msg(("t1", "echo", {"x": "hi"})), "tool_use"), resp(text_msg("done"), "end_turn")])
        r = run_agent(c, "m", "sys", "q", [ECHO])
        self.assertTrue(r.completed)
        self.assertEqual((r.text, r.turns, r.input_tokens, r.output_tokens), ("done", 2, 20, 10))
        # 두 번째 호출에는 [user, assistant(toolUse), user(toolResult)] 세 개가 들어가야 한다
        self.assertEqual(c.calls[1]["n_messages"], 3)
        tr = r.messages[2]["content"][0]["toolResult"]
        self.assertEqual(tr["toolUseId"], "t1")
        self.assertEqual(tr["content"][0]["text"], "got hi")
        self.assertNotIn("status", tr)

    def test_tool_exception_becomes_error_result(self):
        c = FakeClient([resp(tool_use_msg(("t1", "boom", {})), "tool_use"), resp(text_msg("sorry"), "end_turn")])
        r = run_agent(c, "m", "sys", "q", [BOOM])
        tr = r.messages[2]["content"][0]["toolResult"]
        self.assertEqual(tr["status"], "error")
        self.assertIn("ZeroDivisionError", tr["content"][0]["text"])
        self.assertFalse(r.tool_calls[0]["ok"])

    def test_unknown_tool_is_error_not_crash(self):
        c = FakeClient([resp(tool_use_msg(("t1", "nope", {})), "tool_use"), resp(text_msg("ok"), "end_turn")])
        r = run_agent(c, "m", "sys", "q", [ECHO])
        self.assertEqual(r.messages[2]["content"][0]["toolResult"]["status"], "error")

    def test_multiple_tool_uses_answered_in_one_user_message(self):
        c = FakeClient([
            resp(tool_use_msg(("a", "echo", {"x": "1"}), ("b", "echo", {"x": "2"})), "tool_use"),
            resp(text_msg("ok"), "end_turn"),
        ])
        r = run_agent(c, "m", "sys", "q", [ECHO])
        ids = [b["toolResult"]["toolUseId"] for b in r.messages[2]["content"]]
        self.assertEqual(ids, ["a", "b"])

    def test_max_turns_stops_before_extra_call(self):
        loop = [resp(tool_use_msg(("t", "echo", {"x": "1"})), "tool_use") for _ in range(10)]
        c = FakeClient(loop)
        r = run_agent(c, "m", "sys", "q", [ECHO], Limits(max_turns=3))
        self.assertEqual((r.stop_reason, r.turns, len(c.calls)), ("max_turns", 3, 3))

    def test_token_budget_stops(self):
        loop = [resp(tool_use_msg(("t", "echo", {"x": "1"})), "tool_use", i=600, o=500) for _ in range(10)]
        c = FakeClient(loop)
        r = run_agent(c, "m", "sys", "q", [ECHO], Limits(max_total_tokens=2000))
        self.assertEqual(r.stop_reason, "token_budget")
        self.assertLessEqual(len(c.calls), 2)

    def test_tool_calls_limit_leaves_no_dangling_tool_use(self):
        c = FakeClient([resp(tool_use_msg(("t1", "echo", {"x": "1"})), "tool_use")])
        r = run_agent(c, "m", "sys", "q", [ECHO], Limits(max_tool_calls=0))
        self.assertEqual(r.stop_reason, "tool_calls_limit")
        self.assertEqual(len(r.messages), 1)  # 짝 없는 toolUse 메시지가 남지 않는다

    def test_deadline_prevents_any_call(self):
        c = FakeClient([])
        r = run_agent(c, "m", "sys", "q", [ECHO], Limits(deadline=time.monotonic() - 1))
        self.assertEqual((r.stop_reason, len(c.calls)), ("deadline", 0))

    def test_tool_result_is_truncated(self):
        c = FakeClient([resp(tool_use_msg(("t1", "big", {})), "tool_use"), resp(text_msg("ok"), "end_turn")])
        r = run_agent(c, "m", "sys", "q", [BIG], Limits(tool_result_max_chars=10))
        self.assertTrue(r.messages[2]["content"][0]["toolResult"]["content"][0]["text"].endswith("[truncated]"))

    def test_other_model_stop_reason_is_passed_through(self):
        c = FakeClient([resp(text_msg("cut"), "max_tokens")])
        r = run_agent(c, "m", "sys", "q", [ECHO])
        self.assertEqual((r.stop_reason, r.completed), ("max_tokens", False))

    def test_temperature_is_sent_only_when_set(self):
        c = FakeClient([resp(text_msg("hi"), "end_turn")])
        run_agent(c, "m", "sys", "q", [ECHO])
        self.assertEqual(c.calls[0]["inferenceConfig"], {"maxTokens": 2000})
        c = FakeClient([resp(text_msg("hi"), "end_turn")])
        run_agent(c, "m", "sys", "q", [ECHO], Limits(temperature=0.2))
        self.assertEqual(c.calls[0]["inferenceConfig"], {"maxTokens": 2000, "temperature": 0.2})
        c = FakeClient([resp(text_msg("hi"), "end_turn")])
        run_agent(c, "m", "sys", "q", [ECHO], Limits(temperature=0.0))
        self.assertEqual(c.calls[0]["inferenceConfig"]["temperature"], 0.0)

    def test_request_shape(self):
        c = FakeClient([resp(text_msg("hi"), "end_turn")])
        run_agent(c, "model-x", "SYS", "q", [ECHO], Limits(max_output_tokens=123))
        k = c.calls[0]
        self.assertEqual(k["modelId"], "model-x")
        self.assertEqual(k["system"], [{"text": "SYS"}])
        self.assertEqual(k["inferenceConfig"], {"maxTokens": 123})
        self.assertEqual(k["toolConfig"]["tools"][0]["toolSpec"]["name"], "echo")
        self.assertIn("json", k["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"])


if __name__ == "__main__":
    unittest.main()
