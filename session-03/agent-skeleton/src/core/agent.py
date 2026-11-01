"""에이전트 루프 코어. 주제와 무관하게 재사용하는 부분이다.

흐름 (Bedrock Converse 도구 호출 방식):
  1) messages와 toolConfig로 converse를 호출한다.
  2) stopReason이 "tool_use"이면 응답의 toolUse 블록마다 도구를 실행한다.
  3) assistant 메시지를 그대로 messages에 붙이고, 이어서 toolResult를 담은 user 메시지를 붙인다.
  4) 다시 converse를 호출한다. stopReason이 "end_turn" 등 다른 값이면 끝낸다.

비용 안전장치: 호출 횟수, 도구 호출 수, 누적 토큰, 달러 예산, 마감 시각 중 하나라도 넘으면
모델을 더 부르지 않고 멈춘다. 달러 예산은 usage 필드로 실제 비용을 누적해 계산한다.

이 파일은 boto3를 직접 import하지 않는다. client는 converse()를 가진 객체면 되므로
테스트에서는 가짜 client를 넣고, Lambda에서는 boto3 bedrock-runtime client를 넣는다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------- 도구 정의

@dataclass
class Tool:
    """모델에게 보여 줄 명세(name, description, schema)와 실제로 실행할 함수."""

    name: str
    description: str
    schema: dict  # JSON Schema (type: object ...)
    func: Callable[..., Any]

    def spec(self) -> dict:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.schema},
            }
        }


# ---------------------------------------------------------------- 요금

@dataclass
class Prices:
    """100만 토큰당 USD. 기본값은 2026-10-28에 확인한 Bedrock 서울 요금표의
    Claude Sonnet 5 Global 값이다. 모델이나 요금이 바뀌면 환경변수로 덮어쓴다."""

    input: float = 2.00
    output: float = 10.00
    cache_read: float = 0.20
    cache_write: float = 2.50  # 5분 캐시 쓰기

    def cost(self, usage: dict) -> float:
        # Bedrock 문서: 캐시를 쓰면 inputTokens에는 캐시되지 않은 입력만 들어 있다.
        # 그래서 네 항목을 따로 계산해 더한다.
        return (
            usage.get("inputTokens", 0) * self.input
            + usage.get("outputTokens", 0) * self.output
            + usage.get("cacheReadInputTokens", 0) * self.cache_read
            + usage.get("cacheWriteInputTokens", 0) * self.cache_write
        ) / 1e6


# ---------------------------------------------------------------- 상한(비용·시간 안전장치)

@dataclass
class Limits:
    max_turns: int = 8              # converse 호출 횟수 상한
    max_tool_calls: int = 12        # 도구 실행 횟수 상한
    max_total_tokens: int = 50_000  # 입력+출력 누적 토큰 상한 (캐시 읽기·쓰기 포함)
    tool_result_max_chars: int = 8_000  # 도구 결과를 모델에 돌려줄 때 자르는 길이
    max_output_tokens: int = 2_000  # 호출 한 번의 응답 토큰 상한 (inferenceConfig.maxTokens)
    max_usd: float | None = None    # 한 번의 실행에 쓸 최대 금액 (USD). None이면 달러 상한 없음
    prices: Prices = field(default_factory=Prices)
    cache: bool = False             # True면 도구·시스템·첫 user 메시지 뒤에 cachePoint를 둔다
    deadline: float | None = None   # time.monotonic() 기준 마감 시각. Lambda 남은 시간에서 계산해 넣는다
    temperature: float | None = None  # None이면 모델 기본값. 낮추면 실행마다 결과가 덜 흔들린다(모델이 지원하는 범위에서)
    max_continuations: int = 0      # 최종 답이 max_tokens로 끊겼을 때 "이어서 써" 요청을 보낼 최대 횟수. 0이면 이어 받지 않는다
    continue_prompt: str = (
        "직전 응답이 길이 제한으로 중간에 끊겼어요. 끊긴 바로 다음 글자부터 이어서 출력하세요. "
        "이미 쓴 부분을 반복하지 말고, 설명이나 사과를 붙이지 마세요."
    )


# ---------------------------------------------------------------- 결과

@dataclass
class AgentResult:
    text: str                       # 마지막 assistant 응답의 텍스트
    stop_reason: str                # end_turn | max_turns | tool_calls_limit | token_budget | budget_usd | deadline | 그 외 모델 stopReason
    turns: int = 0
    continuations: int = 0          # max_tokens로 끊겨 이어 받은 횟수
    input_tokens: int = 0           # 캐시되지 않은 입력
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0           # usage로 계산한 누적 비용
    last_input_total: int = 0       # 직전 호출의 전체 입력 토큰(캐시 포함)
    last_uncached_input: int = 0    # 직전 호출의 캐시되지 않은 입력
    tool_calls: list[dict] = field(default_factory=list)  # {name, input, ok, chars}
    messages: list[dict] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.stop_reason == "end_turn"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


CACHE_POINT = {"cachePoint": {"type": "default"}}


# ---------------------------------------------------------------- 루프

def run_agent(
    client: Any,
    model_id: str,
    system_prompt: str,
    user_text: str,
    tools: list[Tool],
    limits: Limits | None = None,
    est_input_tokens: int = 0,
) -> AgentResult:
    """est_input_tokens: 첫 호출 입력 토큰 추정치.

    예외가 나면 그 시점까지의 결과를 예외의 agent_result 속성에 붙여 다시 던진다.
    호출자가 이미 쓴 비용을 정산할 수 있게 하기 위해서다.
    """
    limits = limits or Limits()
    result = AgentResult(text="", stop_reason="")
    try:
        _loop(client, model_id, system_prompt, user_text, tools, limits, est_input_tokens, result)
    except Exception as exc:
        exc.agent_result = result  # type: ignore[attr-defined]
        raise
    return result


def _next_call_floor_usd(result: AgentResult, limits: Limits, est_input_tokens: int) -> float:
    """다음 호출이 최소한 얼마를 쓸지 보수적으로 어림한다. 출력은 최대치(max_output_tokens)를 가정한다.

    첫 호출은 호출자가 준 추정 입력 토큰을, 이후 호출은 직전 호출의 사용량을 쓴다.
    이후 호출의 입력은 직전 전체 입력을 다시 보내므로, 캐시된 분량은 캐시 읽기 단가로,
    나머지(직전의 캐시되지 않은 입력)는 정가로 본다.
    """
    p = limits.prices
    if result.turns == 0:
        input_usd = est_input_tokens * p.input / 1e6
    else:
        cached = max(result.last_input_total - result.last_uncached_input, 0)
        input_usd = (result.last_uncached_input * p.input + cached * p.cache_read) / 1e6
    return input_usd + limits.max_output_tokens * p.output / 1e6


def _loop(client, model_id, system_prompt, user_text, tools, limits, est_input_tokens, result):
    registry = {t.name: t for t in tools}

    system = [{"text": system_prompt}]
    first_content = [{"text": user_text}]
    tool_specs = [t.spec() for t in tools]
    if limits.cache:
        system.append(CACHE_POINT)
        first_content.append(CACHE_POINT)
        if tool_specs:
            tool_specs.append(CACHE_POINT)
    tool_config = {"tools": tool_specs} if tools else None

    messages: list[dict] = [{"role": "user", "content": first_content}]
    result.messages = messages
    carry = ""  # max_tokens로 끊긴 답의 앞부분. 이어 받은 뒤 한 덩어리로 합친다

    while True:
        # 호출 전에 상한부터 본다. 상한에 걸리면 모델을 더 부르지 않는다.
        if result.turns >= limits.max_turns:
            result.stop_reason = "max_turns"
            return
        if result.total_tokens >= limits.max_total_tokens:
            result.stop_reason = "token_budget"
            return
        if limits.deadline is not None and time.monotonic() >= limits.deadline:
            result.stop_reason = "deadline"
            return
        if limits.max_usd is not None and (
            result.cost_usd + _next_call_floor_usd(result, limits, est_input_tokens) > limits.max_usd
        ):
            result.stop_reason = "budget_usd"
            return

        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": messages,
            "system": system,
            "inferenceConfig": {"maxTokens": limits.max_output_tokens},
        }
        if limits.temperature is not None:
            kwargs["inferenceConfig"]["temperature"] = limits.temperature
        if tool_config:
            kwargs["toolConfig"] = tool_config

        response = client.converse(**kwargs)
        result.turns += 1

        usage = response.get("usage", {})
        result.input_tokens += usage.get("inputTokens", 0)
        result.output_tokens += usage.get("outputTokens", 0)
        result.cache_read_tokens += usage.get("cacheReadInputTokens", 0)
        result.cache_write_tokens += usage.get("cacheWriteInputTokens", 0)
        result.cost_usd += limits.prices.cost(usage)
        result.last_uncached_input = usage.get("inputTokens", 0)
        result.last_input_total = (
            usage.get("inputTokens", 0) + usage.get("cacheReadInputTokens", 0) + usage.get("cacheWriteInputTokens", 0)
        )

        message = response["output"]["message"]
        messages.append(message)  # toolUse 블록이 들어 있는 assistant 메시지를 그대로 붙인다
        result.text = carry + _text_of(message)
        stop_reason = response.get("stopReason", "")

        has_tool_use = any("toolUse" in b for b in message.get("content", []))
        if (
            stop_reason == "max_tokens"
            and not has_tool_use
            and result.text.strip()
            and result.continuations < limits.max_continuations
        ):
            # 최종 답이 길이 제한에서 끊겼다. 잘린 부분을 버리지 않고 이어서 쓰게 한다.
            # 이전 호출의 입력은 캐시에서 읽으므로 비용은 다음 호출의 출력이 대부분이다.
            result.continuations += 1
            carry = result.text
            messages.append({"role": "user", "content": [{"text": limits.continue_prompt}]})
            if limits.cache:
                _move_tail_cache_point(messages)
            continue
        carry = ""

        if stop_reason != "tool_use":
            result.stop_reason = stop_reason or "end_turn"
            return

        # 도구 실행. 한 응답에 toolUse가 여러 개일 수 있어 모두 처리한 뒤 한 메시지로 돌려보낸다.
        tool_results = []
        for block in message.get("content", []):
            use = block.get("toolUse")
            if not use:
                continue
            if len(result.tool_calls) >= limits.max_tool_calls:
                result.stop_reason = "tool_calls_limit"
                # 짝이 맞지 않는 toolUse를 남긴 채 끝내지 않도록 마지막 assistant 메시지를 제거한다
                messages.pop()
                return
            tool_results.append(_execute(use, registry, limits, result))

        messages.append({"role": "user", "content": [{"toolResult": r} for r in tool_results]})
        if limits.cache:
            _move_tail_cache_point(messages)


# ---------------------------------------------------------------- 내부 함수

def _move_tail_cache_point(messages: list[dict]) -> None:
    """꼬리 체크포인트를 마지막 메시지 끝으로 옮긴다.

    도구 결과가 쌓이는 멀티턴에서는 매 호출마다 앞부분 전체가 다시 전송된다. 마지막 메시지에 체크포인트를 두면
    그 앞의 내용은 다음 호출에서 캐시 읽기 단가로 계산된다. 첫 user 메시지(0번)의 체크포인트는 그대로 두므로
    전체 체크포인트는 도구 1 + 시스템 1 + 첫 메시지 1 + 꼬리 1 = 최대 4개이고,
    Bedrock 문서의 Claude 한도(요청당 4개)와 같다.
    """
    for m in messages[1:]:
        m["content"] = [b for b in m["content"] if b != CACHE_POINT]
    messages[-1]["content"].append(CACHE_POINT)


def _text_of(message: dict) -> str:
    return "".join(b["text"] for b in message.get("content", []) if "text" in b)


def _execute(use: dict, registry: dict[str, Tool], limits: Limits, result: AgentResult) -> dict:
    """toolUse 하나를 실행해 toolResult 블록을 만든다. 실패해도 예외를 밖으로 내보내지 않는다."""
    tool_use_id = use["toolUseId"]  # 모델마다 형식이 다를 수 있으므로 값을 가공하지 않고 그대로 되돌려 준다
    name = use.get("name", "")
    tool_input = use.get("input", {})
    tool = registry.get(name)

    if tool is None:
        ok, text = False, f"Unknown tool: {name}"
    else:
        try:
            output = tool.func(**tool_input)
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            ok = True
        except Exception as exc:  # 도구 오류는 모델에게 알려 주고 스스로 복구하게 한다
            ok, text = False, f"{type(exc).__name__}: {exc}"

    if len(text) > limits.tool_result_max_chars:
        text = text[: limits.tool_result_max_chars] + "\n...[truncated]"

    result.tool_calls.append({"name": name, "input": tool_input, "ok": ok, "chars": len(text)})

    block: dict[str, Any] = {"toolUseId": tool_use_id, "content": [{"text": text}]}
    if not ok:
        block["status"] = "error"
    return block
