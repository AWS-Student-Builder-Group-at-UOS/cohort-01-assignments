"""AWS 없이 핸들러를 실행해 보기 위한 가짜(fake) boto3.

진짜 DynamoDB / EventBridge 대신 메모리에서 동작하는 간단한 흉내 구현이다.
이 프로젝트에서 쓰는 표현식(attribute_exists, attribute_not_exists, AND, SET, ADD)만 지원한다.
"""
import copy
import importlib
import itertools
import re
import sys
import types
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ClientError(Exception):
    def __init__(self, error_response, operation_name):
        super().__init__(f"{operation_name}: {error_response['Error']['Code']}")
        self.response = error_response
        self.operation_name = operation_name


class BotoCoreError(Exception):
    pass


def _conditional_failure(operation):
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, operation)


def _to_decimal(value):
    """진짜 DynamoDB처럼 읽을 때 숫자를 Decimal로 돌려준다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    return value


class FakeTable:
    def __init__(self):
        self.items = {}
        self.reads = []

    # -- 조건식: attribute_not_exists(x) / attribute_exists(x), AND만 지원 --
    def _check_condition(self, key, condition, operation):
        if condition is None:
            return
        for clause in condition.split(" AND "):
            match = re.fullmatch(r"(attribute_not_exists|attribute_exists)\((\w+)\)", clause.strip())
            if not match:
                raise NotImplementedError(f"fake가 지원하지 않는 조건식: {condition}")
            func, attr = match.groups()
            present = key in self.items and attr in self.items[key]
            if (func == "attribute_not_exists" and present) or (func == "attribute_exists" and not present):
                raise _conditional_failure(operation)

    def put_item(self, Item, ConditionExpression=None):
        key = Item["registrationId"]
        self._check_condition(key, ConditionExpression, "PutItem")
        self.items[key] = copy.deepcopy(Item)
        return {}

    def get_item(self, Key, ConsistentRead=False):
        self.reads.append({"Key": copy.deepcopy(Key), "ConsistentRead": ConsistentRead})
        item = self.items.get(Key["registrationId"])
        return {"Item": _to_decimal(copy.deepcopy(item))} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None):
        key = Key["registrationId"]
        self._check_condition(key, ConditionExpression, "UpdateItem")
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues

        def resolve(name):
            return names.get(name, name)

        match = re.fullmatch(r"\s*SET\s+(.*?)(?:\s+ADD\s+(.*))?\s*", UpdateExpression)
        if not match:
            raise NotImplementedError(f"fake가 지원하지 않는 UpdateExpression: {UpdateExpression}")
        set_part, add_part = match.groups()

        item = self.items.setdefault(key, {"registrationId": key})
        used_values = set()
        for assignment in set_part.split(","):
            attr, placeholder = [s.strip() for s in assignment.split("=")]
            item[resolve(attr)] = values[placeholder]
            used_values.add(placeholder)
        if add_part:
            for addition in add_part.split(","):
                attr, placeholder = addition.split()
                item[resolve(attr)] = item.get(resolve(attr), 0) + values[placeholder]
                used_values.add(placeholder)

        # 진짜 DynamoDB는 쓰지 않은 값이 있으면 오류를 낸다.
        unused = set(values) - used_values
        if unused:
            raise ClientError({"Error": {"Code": "ValidationException",
                                         "Message": f"unused values: {sorted(unused)}"}}, "UpdateItem")
        return {}


class FakeEvents:
    def __init__(self):
        self.entries = []
        self.attempts = []
        self.fail = False
        self.error = None

    def put_events(self, Entries):
        self.attempts.extend(copy.deepcopy(Entries))
        if self.error is not None:
            raise self.error
        if self.fail:
            return {"FailedEntryCount": len(Entries), "Entries": [{"ErrorCode": "InternalFailure"}] * len(Entries)}
        self.entries.extend(copy.deepcopy(Entries))
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "fake"}] * len(Entries)}


class World:
    """한 번의 테스트에서 쓰는 가짜 AWS 전체."""

    def __init__(self):
        self.table = FakeTable()
        self.events = FakeEvents()


def install_fake_boto3(world):
    boto3 = types.ModuleType("boto3")

    class _Resource:
        def Table(self, name):
            return world.table

    boto3.resource = lambda service, **kw: _Resource()
    boto3.client = lambda service, **kw: world.events

    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    exceptions.BotoCoreError = BotoCoreError
    botocore.exceptions = exceptions

    sys.modules["boto3"] = boto3
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exceptions


_MODULES = ("common", "register", "status", "resend", "issue")


def load_step(step, env, world):
    """step1 또는 step2의 src 폴더를 새로 불러온다. (두 폴더에 같은 이름의 파일이 있어서 매번 비운다.)"""
    import os

    for name in _MODULES:
        sys.modules.pop(name, None)
    src = str(ROOT / step / "src")
    sys.path[:] = [p for p in sys.path if not p.endswith(("/step1/src", "/step2/src"))]
    sys.path.insert(0, src)

    install_fake_boto3(world)
    base_env = {"TABLE_NAME": "test-table", "ISSUE_DELAY_SECONDS": "0", "EVENT_BUS_NAME": "test-bus", "IDEMPOTENT": "true"}
    os.environ.update({**base_env, **env})

    loaded = types.SimpleNamespace()
    for name in _MODULES:
        if (ROOT / step / "src" / f"{name}.py").exists():
            setattr(loaded, name, importlib.import_module(name))
    return loaded


_counter = itertools.count(1)


def http_event(method, path, body=None, path_params=None):
    """API Gateway(HTTP API)가 Lambda에 넘기는 이벤트 모양을 흉내 낸다."""
    import json

    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
        "isBase64Encoded": False,
        "pathParameters": path_params or {},
        "requestContext": {"requestId": f"req-{next(_counter)}"},
    }


def sqs_event_from_events(world, index=-1, receive_count=1):
    """EventBridge로 나간 이벤트를 SQS 메시지(Lambda 입력) 모양으로 바꾼다."""
    import json

    entry = world.events.entries[index]
    eventbridge_event = {
        "version": "0",
        "id": "evt-1",
        "detail-type": entry["DetailType"],
        "source": entry["Source"],
        "account": "123456789012",
        "time": "2026-11-03T10:00:00Z",
        "region": "ap-northeast-2",
        "resources": [],
        "detail": json.loads(entry["Detail"]),
    }
    return {
        "Records": [
            {
                "messageId": f"msg-{next(_counter)}",
                "receiptHandle": "handle",
                "body": json.dumps(eventbridge_event, ensure_ascii=False),
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
                "eventSource": "aws:sqs",
            }
        ]
    }
