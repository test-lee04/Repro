"""Reference evaluation fixture, not an Agent-generated test or Agent input.

Run against both versions. A correct regression assertion fails on buggy,
passes on fixed. Infrastructure exceptions remain errors, not bug detections.
"""
import argparse
from playwright.sync_api import sync_playwright, expect


def run(base_url: str, version: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/demo?version={version}")
            page.get_by_label("쿠폰 코드", exact=True).fill("SAVE10")
            page.get_by_role("button", name="쿠폰 적용", exact=True).click()
            expect(page.get_by_label("결제 합계", exact=True)).to_have_text("18000")
            page.get_by_label("수량", exact=True).fill("3")
            expect(page.get_by_label("결제 합계", exact=True)).to_have_text("27000")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--version", choices=["buggy", "fixed"], required=True)
    args = parser.parse_args()
    run(args.base_url, args.version)
