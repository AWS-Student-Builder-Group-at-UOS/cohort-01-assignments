"""예시 도구. 자기 에이전트를 만들 때 이 파일을 바꾼다.

도구 함수는 문자열이나 JSON으로 바꿀 수 있는 값을 돌려주면 된다.
오류가 나면 예외를 그대로 던져도 된다. 코어가 잡아서 모델에게 error 상태로 알려 준다.
"""

from datetime import datetime, timezone

from core.agent import Tool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def word_count(text: str) -> dict:
    return {"words": len(text.split()), "chars": len(text)}


TOOLS = [
    Tool(
        name="utc_now",
        description="현재 시각을 UTC ISO 8601 형식으로 돌려준다.",
        schema={"type": "object", "properties": {}},
        func=utc_now,
    ),
    Tool(
        name="word_count",
        description="주어진 텍스트의 단어 수와 글자 수를 센다.",
        schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "세려는 텍스트"}},
            "required": ["text"],
        },
        func=word_count,
    ),
]
