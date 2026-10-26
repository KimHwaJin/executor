from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name(value: str) -> str:
    """앞뒤 공백과 연속 공백을 정리합니다."""
    return " ".join(value.strip().split())


def make_session_name(message_text: str, limit: int = 100) -> str:
    """첫 메시지에서 Session 제목을 생성합니다."""
    normalized = " ".join(message_text.strip().split())
    return normalized[:limit] or "새 대화"

