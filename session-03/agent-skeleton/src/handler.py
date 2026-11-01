"""워커 Lambda 진입점. SQS 메시지 한 건을 받아 에이전트를 한 번 실행한다.

SQS 메시지 본문 예: {"text": "지금 몇 시야?", "response_url": "https://hooks.slack.com/...", "user_id": "U..."}

흐름
  0) 너무 오래된 메시지는 건너뛴다 (Slack response_url은 30분 뒤 만료되어 회신할 수 없는데 비용만 든다)
  1) 앱의 prepare: 재료를 만든다. Refuse이면 안내만 보내고 끝낸다 (비용 없음)
  2) 일일·월 상한에 이번 실행의 최대 금액을 예약
  3) 달러 예산 안에서 에이전트 실행
  4) 앱의 finish로 결과 텍스트를 만들어 전달
  5) 실제 금액으로 정산
"""

import dataclasses
import json
import os
import time

from app import entry
from app.deliver import deliver, notify
from core.agent import Limits, Prices, run_agent
from core.app_api import Refuse
from core.bedrock import make_client
from core.spend_cap import CapExceeded, SpendCap

MODEL_ID = os.environ.get("MODEL_ID", "")  # global.로 시작하는 추론 프로파일 ID
DEADLINE_MARGIN_S = int(os.environ.get("DEADLINE_MARGIN_S", "20"))  # 남은 시간이 이보다 적으면 새 모델 호출을 시작하지 않는다. 한 호출의 읽기 제한(BEDROCK_READ_TIMEOUT)보다 길게 둔다
STALE_AFTER_S = int(os.environ.get("STALE_AFTER_S", "1500"))  # 25분. response_url은 받은 뒤 30분 안에만 쓸 수 있다

_client = None  # 실행 환경이 재사용되는 동안 클라이언트를 다시 만들지 않는다
_cap = None


def _now() -> float:
    return time.time()


def _get_client():
    global _client
    if _client is None:
        _client = make_client()
    return _client


def _get_cap():
    global _cap
    if _cap is None:
        import boto3

        _cap = SpendCap(
            boto3.client("dynamodb"),
            os.environ["CAP_TABLE"],
            daily_usd=float(os.environ.get("DAILY_CAP_USD", "2")),
            monthly_usd=float(os.environ.get("MONTHLY_CAP_USD", "10")),
        )
    return _cap


def _prices() -> Prices:
    d = Prices()
    return Prices(
        input=float(os.environ.get("PRICE_INPUT_PER_M", d.input)),
        output=float(os.environ.get("PRICE_OUTPUT_PER_M", d.output)),
        cache_read=float(os.environ.get("PRICE_CACHE_READ_PER_M", d.cache_read)),
        cache_write=float(os.environ.get("PRICE_CACHE_WRITE_PER_M", d.cache_write)),
    )


def lambda_handler(event, context):
    if not MODEL_ID:
        raise RuntimeError("환경변수 MODEL_ID가 설정되지 않았습니다.")
    if not os.environ.get("CAP_TABLE"):
        # 지출 상한 없이 돌지 않는다 (로컬 실행은 run_local.py를 쓴다)
        raise RuntimeError("환경변수 CAP_TABLE이 설정되지 않았습니다.")

    max_usd = float(os.environ.get("MAX_USD_PER_RUN", "0.5"))

    for record in event.get("Records", []):
        body = json.loads(record["body"])

        sent_ms = int(record.get("attributes", {}).get("SentTimestamp", 0) or 0)
        if sent_ms and _now() - sent_ms / 1000 > STALE_AFTER_S:
            print(f"skip stale message: age={int(_now() - sent_ms / 1000)}s")  # 회신할 수 없는 재시도는 비용을 쓰지 않는다
            continue

        try:
            prepared = entry.prepare(body)
        except Refuse as refusal:
            notify(body, refusal.text)
            continue
        except Exception as exc:
            _safe_notify(body, f"준비 중 오류가 났어요 ({type(exc).__name__}). 로그를 확인해 주세요.")
            raise

        try:
            reservation = _get_cap().reserve(max_usd)
        except CapExceeded as exc:
            label = {"daily": "오늘", "monthly": "이번 달", "per_run": "1회 실행"}[exc.scope]
            notify(body, f"{label} 사용 한도에 도달해서 실행하지 않았어요. 한도는 배포 설정에서 조정할 수 있어요.")
            continue  # 정상 종료: 메시지를 지운다 (재시도해도 같은 결과)

        actual = 0.0
        try:
            limits = Limits(
                max_turns=int(os.environ.get("MAX_TURNS", "8")),
                max_total_tokens=int(os.environ.get("MAX_TOTAL_TOKENS", "50000")),
                max_usd=max_usd,
                prices=_prices(),
                cache=os.environ.get("CACHE_ENABLED", "true").lower() == "true",
                deadline=time.monotonic() + context.get_remaining_time_in_millis() / 1000 - DEADLINE_MARGIN_S,
            )
            limits = dataclasses.replace(limits, **prepared.limits_override)
            result = run_agent(
                _get_client(),
                MODEL_ID,
                prepared.system_prompt,
                prepared.user_text,
                prepared.tools,
                limits,
                est_input_tokens=prepared.est_input_tokens,
            )
            actual = result.cost_usd
            try:
                text = entry.finish(prepared, result)
            except Exception as exc:  # 이미 비용을 썼으니 재시도하지 않고 원문을 보낸다
                print(f"finish failed: {type(exc).__name__}")
                text = f"(결과 정리 중 오류: {type(exc).__name__})\n{result.text}"
            deliver(
                {
                    "text": text,
                    "stop_reason": result.stop_reason,
                    "turns": result.turns,
                    "input_tokens": result.input_tokens + result.cache_read_tokens + result.cache_write_tokens,
                    "output_tokens": result.output_tokens,
                    "cost_usd": result.cost_usd,
                    "tool_calls": result.tool_calls,
                },
                body,
            )
        except Exception as exc:
            partial = getattr(exc, "agent_result", None)
            actual = partial.cost_usd if partial else actual
            # 사용자에게는 원인 종류만 알리고(내부 정보 제외), 예외는 다시 던져 SQS 재시도와 DLQ가 동작하게 한다
            _safe_notify(body, f"처리 중 오류가 났어요 ({type(exc).__name__}). 다시 시도하거나 로그를 확인해 주세요.")
            raise
        finally:
            try:
                _get_cap().settle(reservation, actual)
            except Exception as exc:  # 정산 실패는 로그로 남긴다 (예약액이 그대로 남아 보수적으로 집계된다)
                print(f"settle failed: {type(exc).__name__}")


def _safe_notify(body: dict, text: str) -> None:
    try:
        notify(body, text)
    except Exception:
        pass
