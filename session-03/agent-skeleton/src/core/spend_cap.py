"""일일·월 지출 상한. DynamoDB에 오늘/이번 달 누적 금액을 기록한다.

방식: 실행 전에 "이번 실행에서 쓸 수 있는 최대 금액"을 먼저 예약(조건부 쓰기)하고,
끝나면 실제 금액으로 정산(차액 반영)한다. 예약 단계에서 상한을 넘으면 거부한다.
동시에 여러 실행이 와도 조건부 쓰기가 원자적이라 상한을 넘겨 예약되지 않는다.

한계: 금액은 usage 필드와 설정된 단가로 계산한 값이라 실제 청구액과 다를 수 있다.
이 상한은 "봇이 스스로 멈추는 장치"이고, 계정 전체의 마지막 방어선은 AWS Budgets 알림이다.
금액은 정수(백만분의 1달러)로 저장해 부동소수점 오차를 피한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class CapExceeded(Exception):
    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope  # "daily" | "monthly" | "per_run"


@dataclass
class Reservation:
    micro: int
    day_key: str
    month_key: str


def _micro(usd: float) -> int:
    return round(usd * 1_000_000)


def _is_conditional_failure(exc: Exception) -> bool:
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class SpendCap:
    def __init__(self, client, table: str, daily_usd: float, monthly_usd: float, tz_offset_hours: int = 9, now=None):
        self.client, self.table = client, table
        self.daily_micro, self.monthly_micro = _micro(daily_usd), _micro(monthly_usd)
        self._tz = timezone(timedelta(hours=tz_offset_hours))  # 날짜 경계를 정하는 시간대 (기본 KST)
        self._now = now or (lambda: datetime.now(self._tz))

    def _keys(self) -> tuple[str, str]:
        d = self._now()
        return f"day#{d:%Y-%m-%d}", f"month#{d:%Y-%m}"

    def reserve(self, max_usd: float) -> Reservation:
        est = _micro(max_usd)
        day_key, month_key = self._keys()
        if est > self.daily_micro:
            raise CapExceeded("per_run")  # 한 번의 최대 금액이 하루 상한보다 크면 설정 오류로 본다
        self._reserve_one(day_key, est, self.daily_micro, "daily")
        try:
            self._reserve_one(month_key, est, self.monthly_micro, "monthly")
        except Exception:
            self._add(day_key, -est, runs=0)  # 월 예약 실패 시 일 예약을 되돌린다
            raise
        return Reservation(est, day_key, month_key)

    def settle(self, reservation: Reservation, actual_usd: float) -> None:
        """예약액과 실제 사용액의 차액을 반영한다. 실제가 더 작으면 돌려주고, 더 크면 더한다."""
        delta = _micro(actual_usd) - reservation.micro
        if delta:
            self._add(reservation.day_key, delta, runs=0)
            self._add(reservation.month_key, delta, runs=0)

    # ------------------------------------------------------------ 내부

    def _reserve_one(self, key: str, est: int, limit: int, scope: str) -> None:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"pk": {"S": key}},
                UpdateExpression="ADD spent_micro :d, runs :one",
                ConditionExpression="attribute_not_exists(spent_micro) OR spent_micro <= :room",
                ExpressionAttributeValues={
                    ":d": {"N": str(est)},
                    ":one": {"N": "1"},
                    ":room": {"N": str(limit - est)},
                },
            )
        except Exception as exc:
            if _is_conditional_failure(exc):
                raise CapExceeded(scope) from exc
            raise

    def _add(self, key: str, delta: int, runs: int) -> None:
        self.client.update_item(
            TableName=self.table,
            Key={"pk": {"S": key}},
            UpdateExpression="ADD spent_micro :d, runs :r",
            ExpressionAttributeValues={":d": {"N": str(delta)}, ":r": {"N": str(runs)}},
        )
