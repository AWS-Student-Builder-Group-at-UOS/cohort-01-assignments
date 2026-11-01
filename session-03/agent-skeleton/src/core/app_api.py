"""앱(에이전트 용도)과 핸들러 사이의 약속.

앱 모듈 `app/entry.py`에 아래 두 함수를 둔다.
  prepare(body: dict) -> Prepared      요청을 받아 모델에 보낼 재료를 만든다. 모델을 부르지 않아야 할 때는 Refuse를 던진다.
  finish(prepared, result) -> str      모델의 최종 결과를 사용자에게 보낼 텍스트로 만든다. 예외를 던지지 않는 것이 좋다.
핸들러는 `src/app/entry.py`를 불러 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Refuse(Exception):
    """모델을 호출하지 않고 사용자에게 안내만 보낼 때 던진다. 비용이 들지 않고 재시도하지 않는다."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


@dataclass
class Prepared:
    system_prompt: str
    user_text: str
    tools: list
    est_input_tokens: int = 0                          # 첫 호출 입력 토큰 추정치
    limits_override: dict = field(default_factory=dict)  # core.agent.Limits 필드를 덮어쓴다
    context: Any = None                                # finish로 넘길 앱 전용 자료
