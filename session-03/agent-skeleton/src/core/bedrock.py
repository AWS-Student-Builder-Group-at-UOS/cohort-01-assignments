"""Bedrock 클라이언트 생성. boto3는 Lambda Python 런타임에 들어 있다."""

import os


def make_client():
    import boto3
    from botocore.config import Config

    # 긴 응답을 기다릴 수 있도록 읽기 제한 시간을 늘린다. 값은 Lambda 제한 시간보다 작게 둔다.
    cfg = Config(read_timeout=int(os.environ.get("BEDROCK_READ_TIMEOUT", "120")), retries={"max_attempts": 2})
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "ap-northeast-2")),
        config=cfg,
    )
