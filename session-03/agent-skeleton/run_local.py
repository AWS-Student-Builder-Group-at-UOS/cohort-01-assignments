"""내 컴퓨터에서 Lambda 없이 에이전트를 실행해 본다. 지출 상한 표(DynamoDB)는 쓰지 않고 1회 금액 상한만 적용된다.

사전 준비: pip install boto3, AWS 자격 증명 설정(aws configure 등), Bedrock에서 모델 사용 가능 상태.
사용 (모델이 Sonnet 5 Global이 아니면 PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M, PRICE_CACHE_READ_PER_M, PRICE_CACHE_WRITE_PER_M도 설정):
  MODEL_ID=<모델 ID> python run_local.py "지금 몇 시야?"
"""

import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from core.agent import Limits, Prices, run_agent  # noqa: E402
from core.app_api import Refuse  # noqa: E402
from app import entry  # noqa: E402
from core.bedrock import make_client  # noqa: E402

model_id = os.environ["MODEL_ID"]
question = " ".join(sys.argv[1:]) or "지금 UTC 시각을 알려 주고, 'hello serverless world'의 단어 수도 세어 줘."

try:
    prepared = entry.prepare({"text": question})
except Refuse as r:
    print(r.text)
    sys.exit(0)

d = Prices()  # 기본값은 Sonnet 5 Global 단가다. 다른 모델이면 PRICE_*_PER_M 환경변수로 덮어쓴다 (100만 토큰당 USD)
prices = Prices(
    input=float(os.environ.get("PRICE_INPUT_PER_M", d.input)),
    output=float(os.environ.get("PRICE_OUTPUT_PER_M", d.output)),
    cache_read=float(os.environ.get("PRICE_CACHE_READ_PER_M", d.cache_read)),
    cache_write=float(os.environ.get("PRICE_CACHE_WRITE_PER_M", d.cache_write)),
)
limits = Limits(max_usd=float(os.environ.get("MAX_USD_PER_RUN", "1.0")), cache=True, prices=prices)
limits = dataclasses.replace(limits, **prepared.limits_override)
result = run_agent(
    make_client(), model_id, prepared.system_prompt, prepared.user_text, prepared.tools, limits,
    est_input_tokens=prepared.est_input_tokens,
)
print(entry.finish(prepared, result))
print(
    f"\n[종료 사유={result.stop_reason} 호출={result.turns}회 입력토큰={result.input_tokens} "
    f"캐시읽기={result.cache_read_tokens} 캐시쓰기={result.cache_write_tokens} 출력토큰={result.output_tokens} "
    f"추정비용=${result.cost_usd:.4f}]"
)
for call in result.tool_calls:
    print(f"  도구 {call['name']} 입력={call['input']} 성공={call['ok']}")
