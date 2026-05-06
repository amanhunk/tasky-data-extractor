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

# ================= GOOGLE SHEETS =================
def init_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS")

    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        print("✅ Using credentials from environment variable")
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
    for i in range(3):
        try:
            sheet.append_rows(rows)
            return
        except Exception as e:
            print(f"Retry {i+1} (Google Sheets error):", e)
            time.sleep(3)


# ================= SCRAPER =================
class TaskyScraper:
    def __init__(self, page):
        self.page = page

    async def get_all_task_urls(self):
        """Paginate through all pages and collect review URLs."""
        print("🔗 Collecting task URLs...")
        all_urls = []
        page_num = 1

        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)

        while True:
            print(f"📄 Page {page_num}")

            links = await self.page.eval_on_selector_all(
                'a[href*="/tasky/tasks/"]',
                'elements => elements.map(e => e.href)'
            )

            new_urls = []
            for link in links:
                match = re.search(r'/tasky/tasks/([^/?]+)', link)
                if match:
                    review_url = f"https://hume.google.com/datachangereview/{match.group(1)}"
                    if review_url not in all_urls:
                        new_urls.append(review_url)

            all_urls.extend(new_urls)
            print(f"   ➜ {len(new_urls)} new (total: {len(all_urls)})")

            first_url = new_urls[0] if new_urls else None

            next_btn = await self.page.query_selector('.mat-mdc-paginator-navigation-next:not([disabled])')
            if not next_btn:
                next_btn = await self.page.query_selector('button[aria-label="Next page"]:not([disabled])')

            if not next_btn:
                print("🏁 No more pages")
                break

            await next_btn.click()

            try:
                await self.page.wait_for_function(
                    f'''() => {{
                        const first = document.querySelector('a[href*="/tasky/tasks/"]');
                        return first && first.href !== '{first_url}';
                    }}''',
                    timeout=15000
                )
            except:
                print("⚠️ Page did not change")
                break

            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

            page_num += 1
            if page_num > 200:
                print("⚠️ Page limit reached")
                break

        print(f"✅ Total URLs: {len(all_urls)}")
        return all_urls

    async def extract_task_data(self, url):
        """Extract Query, Interpretation, Response, User Comment"""
        await self.page.goto(url, timeout=60000)
        await self.page.wait_for_load_state("networkidle")

        try:
            await self.page.wait_for_selector('.bubble', timeout=20000)
        except:
            return "", "", "", ""

        # --- Query ---
        try:
            query = await self.page.locator('.bubble p:not(.interpretation)').first.inner_text()
        except:
            query = ""

        # --- Interpretation ---
        try:
            interpretation = await self.page.locator('.bubble .interpretation').inner_text()
        except:
            interpretation = ""

        # --- Response ---
        try:
            response = await self.page.locator('[data-test-id="magi-response"]').inner_text()
        except:
            response = ""

        # --- User Comment ---
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
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage'
            ]
        )

        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()

        scraper = TaskyScraper(page)

        print("🌐 Opening task list...")
        await page.goto(TASK_LIST_URL, timeout=60000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(5)

        # Step 1: Get all URLs
        review_urls = await scraper.get_all_task_urls()

        if not review_urls:
            print("❌ No tasks found")
            await browser.close()
            return

        # Step 2: Extract data
        print(f"\n📊 Extracting {len(review_urls)} tasks...\n")

        all_rows = []

        for idx, url in enumerate(review_urls, 1):
            try:
                query, interpretation, response, comment = await scraper.extract_task_data(url)

                print(f"{idx}/{len(review_urls)} ✅")

                all_rows.append([
                    url,
                    query,
                    interpretation,
                    response,
                    comment
                ])

            except Exception as e:
                print(f"❌ Error on {url}: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR"])

        # Step 3: Upload
        safe_append_rows(sheet, all_rows)

        print("\n✅ Data uploaded to Google Sheets!")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
