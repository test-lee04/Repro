"""브라우저 세션 접속·상태 격리·종료 검증."""

import asyncio

from playwright.async_api import expect

from app.browser.session import PlaywrightSessions


async def main():
    async with PlaywrightSessions() as sessions:
        # 첫 번째 세션에서 수량을 변경한다.
        first_id = await sessions.open(
            run_id="check-first",
            target_id="demo_cart",
        )
        first_page = sessions.get_page(first_id)

        await expect(
            first_page.get_by_label("결제 합계", exact=True)
        ).to_have_text("20000")

        await first_page.get_by_label(
            "수량", exact=True
        ).fill("3")

        await expect(
            first_page.get_by_label("결제 합계", exact=True)
        ).to_have_text("30000")

        print("PASS: 첫 번째 세션 접속 및 수량 변경")

        # 두 번째 세션은 초기 상태여야 한다.
        second_id = await sessions.open(
            run_id="check-second",
            target_id="demo_cart",
        )
        second_page = sessions.get_page(second_id)

        await expect(
            second_page.get_by_label("수량", exact=True)
        ).to_have_value("2")

        await expect(
            second_page.get_by_label("결제 합계", exact=True)
        ).to_have_text("20000")

        print("PASS: 두 번째 세션의 초기 상태 격리")

        # 한 세션을 닫아도 다른 세션은 유지돼야 한다.
        await sessions.close(first_id)

        assert first_page.is_closed()

        await expect(
            second_page.get_by_label("결제 합계", exact=True)
        ).to_have_text("20000")

        print("PASS: 개별 세션 종료")

    assert second_page.is_closed()
    assert not sessions.sessions

    print("PASS: 전체 세션 정리")


if __name__ == "__main__":
    asyncio.run(main())