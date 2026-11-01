"""모델을 부르기 전에 입력 토큰을 어림하는 도구. 비용 상한을 미리 확인하는 데 쓴다."""


def estimate_text_tokens(text: str) -> int:
    """공식 토크나이저가 아니라 보수적 추정이다. 비ASCII(한글 등)는 글자당 1토큰, ASCII는 글자당 0.4토큰으로 본다."""
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return int(non_ascii + 0.4 * (len(text) - non_ascii)) + 1
