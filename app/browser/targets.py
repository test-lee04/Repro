"""기존 데모 검증에서 사용하는 대상 주소 설정."""

from urllib.parse import urlencode


def legacy_demo_targets(
    base_url: str,
    version: str,
) -> dict[str, str]:
    """기존 검증 스크립트용 대상 주소를 구성한다.

    Args:
        base_url: 데모 서버의 기본 주소.
        version: 데모의 buggy 또는 fixed 버전.

    Returns:
        대상 식별자와 주소의 매핑.

    Raises:
        ValueError: 지원하지 않는 데모 버전인 경우.

    Notes:
        기존 검증 코드와의 전환용 설정이다.
        일반 URL 접속이나 버그 재현 규칙으로 사용하지 않는다.
    """
    if version not in {"buggy", "fixed"}:
        raise ValueError(
            "데모 버전은 buggy 또는 fixed여야 합니다."
        )

    query = urlencode({"version": version})

    return {
        "demo_cart": (
            f"{base_url.rstrip('/')}/demo?{query}"
        )
    }