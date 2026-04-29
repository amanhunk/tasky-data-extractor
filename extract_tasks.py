import json
import os
import asyncio
import time
import base64
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
SHEET_URL = os.environ.get("SHEET_URL")
TASK_LIST_URL = os.environ.get("TASK_LIST_URL")
MAX_TASKS = 10

# ================= GOOGLE SHEETS =================
def init_sheet():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDS"))
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_url(SHEET_URL).sheet1

def safe_append_rows(sheet, rows):
    for i in range(3):
        try:
            sheet.append_rows(rows)
            return
        except Exception as e:
            print(f"Retry {i+1}:", e)
            time.sleep(3)

# ================= SESSION =================
def load_session():
    session_b64 = os.environ.get("SESSION_JSON_B64")
    session_json = base64.b64decode(session_b64).decode("utf-8")
    with open("session.json", "w") as f:
        f.write(session_json)

# ================= SCRAPER =================
class Scraper:
    def __init__(self, page):
        self.page = page

    async def get_task_links(self):
        print("🔗 Extracting datachangereview links...")

        await self.page.wait_for_selector('a[href*="datachangereview"]', timeout=30000)

        links = await self.page.eval_on_selector_all(
            'a[href*="datachangereview"]',
            'els => els.map(e => e.href)'
        )

        # remove duplicates
        links = list(dict.fromkeys(links))

        print(f"✅ Found {len(links)} links")
        return links[:MAX_TASKS]

    async def extract(self, url, i):
        print(f"\n--- Task {i} ---")
        print("URL:", url)

        await self.page.goto(url, wait_until="networkidle")
        await self.page.wait_for_timeout(3000)

        print("🌐 Current URL:", self.page.url)

        if "login" in self.page.url.lower():
            print("❌ LOGIN PAGE DETECTED")
            return ["ERROR"] * 5

        # ---------- INTERPRETATION ----------
        prompt = "Not found"
        try:
            el = self.page.locator('text=Interpretation').locator('xpath=..//p').first
            if await el.count():
                prompt = (await el.inner_text()).strip()
        except Exception as e:
            print("Prompt error:", e)

        # ---------- RESPONSE ----------
        response = "Not found"
        try:
            el = self.page.locator('div.prose, div.markdown').first
            if await el.count():
                response = (await el.inner_text()).strip()
        except Exception as e:
            print("Response error:", e)

        # ---------- SENTIMENT ----------
        sentiment = "Not found"
        try:
            el = self.page.locator('text=User Sentiment').locator('xpath=..//span').last
            if await el.count():
                sentiment = (await el.inner_text()).strip()
        except:
            pass

        # ---------- ISSUE TYPE ----------
        issue_type = "Not found"
        try:
            el = self.page.locator('text=Issue Type').locator('xpath=..//span').last
            if await el.count():
                issue_type = (await el.inner_text()).strip()
        except:
            pass

        # ---------- USER COMMENT ----------
        user_comment = "Not found"
        try:
            el = self.page.locator('text=User Comment').locator('xpath=..//p').first
            if await el.count():
                user_comment = (await el.inner_text()).strip()
        except:
            pass

        print("Prompt:", prompt[:60])
        print("Response:", response[:60])
        print("Sentiment:", sentiment, "| Issue:", issue_type)

        return [url, prompt, response, sentiment, issue_type, user_comment]


# ================= MAIN =================
async def main():
    load_session()
    sheet = init_sheet()
    print("✅ Sheet connected")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox']
        )

        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()

        print("🌐 Opening task list...")
        await page.goto(TASK_LIST_URL, wait_until="networkidle")
        await page.wait_for_timeout(5000)

        scraper = Scraper(page)
        links = await scraper.get_task_links()

        if not links:
            print("❌ No links found")
            return

        rows = []

        for i, url in enumerate(links, 1):
            try:
                data = await scraper.extract(url, i)
                rows.append(data)
            except Exception as e:
                print("Error:", e)
                rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        # header
        if not sheet.get_all_values():
            sheet.append_row(["URL", "Prompt", "Response", "Sentiment", "Issue", "Comment"])

        safe_append_rows(sheet, rows)

        print("\n✅ DONE: Data uploaded")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
