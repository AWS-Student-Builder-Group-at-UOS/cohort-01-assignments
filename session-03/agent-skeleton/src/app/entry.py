"""샘플 앱의 진입점. 자기 에이전트를 만들 때는 이 파일과 tools.py, prompt.py, deliver.py를 바꾼다."""

from app.prompt import SYSTEM_PROMPT
from app.tools import TOOLS
from core.agent import AgentResult
from core.app_api import Prepared
from core.tokens import estimate_text_tokens


def prepare(body: dict) -> Prepared:
    text = body["text"]
    return Prepared(
        system_prompt=SYSTEM_PROMPT,
        user_text=text,
        tools=TOOLS,
        est_input_tokens=estimate_text_tokens(SYSTEM_PROMPT + text),
    )


def finish(prepared: Prepared, result: AgentResult) -> str:
    return result.text
