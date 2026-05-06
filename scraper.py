import json
import os
import asyncio
import time
import re
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1L1IhWoaMMIWjCW1v19KRc-8_ujdnM0Min2mw4mGVig8/edit"
TASK_LIST_URL = "https://hume.google.com/tasky/tasks?filter=job:aim_loss_pattern_labeling%20status:completed"

MAX_TASKS = 10  # 🔥 LIMIT


# ================= GOOGLE SHEETS =================
def init_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS")

    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        print("✅ Using credentials from environment")
    else:
        creds = Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        print("✅ Using credentials from file")

    client = gspread.authorize(creds)
    sheet = client.open_by_url(SHEET_URL).sheet1
    return sheet


def safe_append_rows(sheet, rows):
    if not rows:
        print("⚠️ No rows to upload")
        return

    for i in range(3):
        try:
            sheet.append_rows(rows)
            print(f"✅ Uploaded {len(rows)} rows to Google Sheets")
            return
        except Exception as e:
            print(f"Retry {i+1}:", e)
            time.sleep(3)


# ================= SCRAPER =================
class TaskyScraper:
    def __init__(self, page):
        self.page = page

    async def get_all_task_urls(self):
        print("🔗 Collecting task URLs...")

        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)

        links = await self.page.eval_on_selector_all(
            'a[href*="/tasky/tasks/"]',
            'elements => elements.map(e => e.href)'
        )

        urls = []
        for link in links:
            match = re.search(r'/tasky/tasks/([^/?]+)', link)
            if match:
                review_url = f"https://hume.google.com/datachangereview/{match.group(1)}"
                urls.append(review_url)

        # 🔥 LIMIT HERE
        urls = list(dict.fromkeys(urls))[:MAX_TASKS]

        print(f"✅ Found {len(urls)} task URLs (limited to {MAX_TASKS})")
        return urls

    async def extract_task_data(self, url):
        print(f"🌐 Opening: {url}")

        try:
            await self.page.goto(url, timeout=60000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
        except Exception as e:
            print("❌ Page load error:", e)
            return "", "", "", ""

        # Wait for content
        try:
            await self.page.wait_for_selector('.bubble', timeout=15000)
        except:
            print("⚠️ No content found")
            return "", "", "", ""

        # --- Extract ---
        try:
            query = await self.page.locator('.bubble p').first.inner_text()
        except:
            query = ""

        try:
            interpretation = await self.page.locator('.interpretation').inner_text()
        except:
            interpretation = ""

        try:
            response = await self.page.locator('[data-test-id="magi-response"]').inner_text()
        except:
            response = ""

        try:
            comment = await self.page.locator('.feedback-content .comment').inner_text()
        except:
            comment = ""

        return query.strip(), interpretation.strip(), response.strip(), comment.strip()


# ================= MAIN =================
async def main():
    sheet = init_sheet()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )

        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()

        print("🌐 Opening task list...")
        await page.goto(TASK_LIST_URL, timeout=60000)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)

        scraper = TaskyScraper(page)

        # ✅ GET URLS
        review_urls = await scraper.get_all_task_urls()

        print(f"DEBUG: Found URLs = {len(review_urls)}")

        if not review_urls:
            print("❌ No tasks found - possible login/session issue")

            # 🔥 Debug screenshot
            await page.screenshot(path="debug.png")

            await browser.close()
            return

        # ✅ EXTRACT
        all_rows = []

        for idx, url in enumerate(review_urls, 1):
            try:
                query, interpretation, response, comment = await scraper.extract_task_data(url)

                print(f"{idx}/{len(review_urls)} ✅ Extracted")

                all_rows.append([
                    url,
                    query,
                    interpretation,
                    response,
                    comment
                ])

            except Exception as e:
                print(f"❌ Error: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR"])

        # ✅ UPLOAD
        safe_append_rows(sheet, all_rows)

        print("\n🎉 DONE")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
