"""이 계정이 어떤 Bedrock 모델을 실제로 호출할 수 있는지 아주 작은 호출로 확인한다 (호출당 수십 토큰, 비용은 사실상 0에 가깝다).
사용: python -I scripts/probe_models.py [모델 ID ...]
인자를 안 주면 기본 후보 3개를 시험한다. 결과는 OK 또는 오류 종류와 메시지 앞부분이다.
"""

import os
import sys

import boto3
from botocore.config import Config

DEFAULTS = [
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-haiku-5-5",
]
region = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "ap-northeast-2"))
client = boto3.client("bedrock-runtime", region_name=region, config=Config(read_timeout=60, retries={"max_attempts": 1}))

for model in sys.argv[1:] or DEFAULTS:
    try:
        r = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        print(f"OK    {model}  (입력 {r['usage']['inputTokens']} 출력 {r['usage']['outputTokens']} 토큰)")
    except Exception as exc:  # 오류 종류를 그대로 보여 주는 것이 목적이다
        msg = str(exc).replace("\n", " ")
        print(f"FAIL  {model}  {type(exc).__name__}: {msg[:230]}")
