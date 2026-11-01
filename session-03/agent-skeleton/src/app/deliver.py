"""결과 전달. 기본은 Slack의 response_url로 회신하고, 없으면(로컬 실행) 로그로 출력한다.

Slack 문서: response_url은 받은 뒤 30분 안에 최대 5번까지 쓸 수 있다.
그래서 한 번의 실행에서 보낼 수 있는 메시지는 5개까지다. 코멘트가 많은 에이전트는
한 메시지에 모아 보내거나, 봇 토큰으로 chat.postMessage를 쓰는 방식으로 바꿔야 한다.
"""

import json
import urllib.request
from urllib.parse import urlparse

CHUNK_CHARS = 3600  # Slack 문서의 권장(약 4,000자) 아래로 잡은 값. response_url 메시지의 정확한 한도는 확인하지 못했다
MAX_POSTS = 5       # response_url 사용 횟수 제한


def _is_slack_url(url: str) -> bool:
    # 방어용: 서명이 검증된 요청에서 온 값이지만, https이고 slack.com 계열 호스트일 때만 호출한다
    p = urlparse(url)
    host = p.hostname or ""
    return p.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


def post_to_response_url(url: str, text: str, response_type: str = "ephemeral") -> None:
    data = json.dumps({"response_type": response_type, "text": text}, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _blocks(text: str) -> list[str]:
    """빈 줄로 나누되, ``` 코드 블록 안의 빈 줄은 나누지 않는다 (붙여 넣을 코멘트가 중간에 끊기지 않게)."""
    blocks: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
        if line == "" and not in_fence and cur and cur[-1] == "":
            continue
        if line == "" and not in_fence:
            blocks.append("\n".join(cur))
            cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b != ""]


def _split_long(block: str) -> list[str]:
    """한 블록이 CHUNK_CHARS보다 길 때만 줄 단위로 자른다. 코드 블록이면 조각마다 펜스를 다시 닫고 연다."""
    fenced = block.startswith("```") and block.rstrip().endswith("```") and block.count("```") == 2
    inner = block.strip()[3:-3].strip("\n") if fenced else block
    limit = CHUNK_CHARS - (8 if fenced else 0)
    pieces, cur = [], ""
    for line in inner.split("\n"):
        while len(line) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:limit])
            line = line[limit:]
        merged = f"{cur}\n{line}" if cur else line
        if len(merged) <= limit:
            cur = merged
        else:
            pieces.append(cur)
            cur = line
    if cur:
        pieces.append(cur)
    return [f"```\n{p}\n```" for p in pieces] if fenced else pieces


def _chunks(text: str) -> list[str]:
    """블록(문단, 코드 블록) 경계를 지켜 CHUNK_CHARS 이하로 묶는다. 너무 긴 블록만 강제로 자른다."""
    parts: list[str] = []
    cur = ""
    for block in _blocks(text):
        for piece in (_split_long(block) if len(block) > CHUNK_CHARS else [block]):
            merged = f"{cur}\n\n{piece}" if cur else piece
            if len(merged) <= CHUNK_CHARS:
                cur = merged
            else:
                parts.append(cur)
                cur = piece
    if cur or not parts:
        parts.append(cur)
    if len(parts) > MAX_POSTS:
        parts = parts[:MAX_POSTS]
        parts[-1] = parts[-1][: CHUNK_CHARS - 40] + "\n...(길이 제한으로 잘렸어요)"
    return parts


def notify(request: dict, text: str) -> None:
    """요청자에게 텍스트를 보낸다. response_url이 없거나 이상하면 로그만 남긴다."""
    url = request.get("response_url", "")
    if not url:
        print(text)
        return
    if not _is_slack_url(url):
        print("skip: response_url is not a slack url")
        return
    for part in _chunks(text):
        post_to_response_url(url, part)


def deliver(payload: dict, request: dict) -> None:
    footer = (
        f"\n\n[종료={payload['stop_reason']} 호출={payload['turns']}회 "
        f"토큰 입력={payload['input_tokens']} 출력={payload['output_tokens']} "
        f"추정비용=${payload.get('cost_usd', 0):.3f}]"
    )
    notify(request, (payload["text"] or "(응답 없음)") + footer)
