#!/usr/bin/env python3
"""Снимает скриншоты интерфейса (нужен playwright + chromium).

  pip install playwright && playwright install chromium
  python screenshots.py [--url http://127.0.0.1:8000] [--out docs/screenshots]
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

PAGES = [
    ("dashboard", "#/dashboard", "01-dashboard.png", True),
    ("campaigns", "#/campaigns", "02-campaigns.png", True),
    ("campaign", "#/campaign/1", "03-campaign.png", True),
    ("results", "#/results/1", "04-results.png", True),
    ("equipment", "#/equipment", "05-equipment.png", True),
    ("settings", "#/settings", "06-settings.png", True),
    ("dnc", "#/do-not-call", "07-stop-list.png", True),
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()

    from playwright.async_api import async_playwright

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": args.width, "height": args.height},
                                      device_scale_factor=1.5, locale="ru-RU")
        for name, route, filename, full in PAGES:
            await page.goto(f"{args.url}/{route}", wait_until="networkidle")
            await page.wait_for_timeout(1200)
            await page.screenshot(path=str(out_dir / filename), full_page=full)
            print("saved", out_dir / filename)
        # модальное окно «Редактировать кампанию»
        await page.goto(f"{args.url}/#/campaigns", wait_until="networkidle")
        await page.wait_for_timeout(600)
        await page.click("#new-campaign")
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(out_dir / "08-campaign-modal.png"))
        print("saved modal")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
