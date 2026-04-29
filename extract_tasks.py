import json
import os
import asyncio
import time
import re
import base64
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
SHEET_URL = os.environ.get("SHEET_URL")
TASK_LIST_URL = os.environ.get("TASK_LIST_URL")
MAX_TASKS = 10

if not SHEET_URL or not TASK_LIST_URL:
    raise ValueError("Missing SHEET_URL or TASK_LIST_URL")

# ================= DEBUG =================
async def save_debug(page):
    await page.screenshot(path="debug.png")
    with open("debug.html", "w", encoding="utf-8") as f:
        f.write(await page.content())
    print("📸 Debug files saved")

# ================= SAFE NAVIGATION =================
async def safe_goto(page, url):
    for i in range(3):
        try:
            print(f"🌐 Loading page (attempt {i+1})...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(7000)
            print("✅ Page loaded")
            return
        except Exception as e:
            print(f"⚠️ Retry {i+1} failed:", e)
            await asyncio.sleep(3)
    raise Exception("❌ Failed to load page after retries")

# ================= GOOGLE SHEETS =================
def init_sheet():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDS"))
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_url(SHEET_URL).sheet1

def safe_append(sheet, rows):
    for _ in range(3):
        try:
            sheet.append_rows(rows)
            return
        except Exception as e:
            print("Retrying sheet write:", e)
            time.sleep(3)

# ================= SESSION =================
def load_session():
    session_b64 = os.environ.get("SESSION_JSON_B64")

    if not session_b64:
        raise Exception("❌ SESSION_JSON_B64 is missing")

    try:
        session_json = base64.b64decode(session_b64).decode("utf-8")
    except Exception as e:
        raise Exception(f"❌ Base64 decode failed: {e}")

    with open("session.json", "w") as f:
        f.write(session_json)

    print("✅ Session loaded")

# ================= SCRAPER =================
class TaskyScraper:
    def __init__(self, page):
        self.page = page

    async def get_task_links(self):
        print("🔗 Extracting task links...")

        await self.page.wait_for_selector("body", timeout=30000)

        links = await self.page.eval_on_selector_all(
            "a",
            "els => els.map(e => e.href).filter(h => h.includes('datachangereview'))"
        )

        links = list(dict.fromkeys(links))
        print(f"✅ Found {len(links)} links")

        if not links:
            await save_debug(self.page)
            raise Exception("❌ No links found (likely login issue)")

        return links[:MAX_TASKS]

    async def extract(self, url, i):
        print(f"\n--- Task {i} ---")
        print("URL:", url)

        await safe_goto(self.page, url)

        print("🌐 Current URL:", self.page.url)

        # LOGIN CHECK
        if "login" in self.page.url.lower():
            raise Exception("❌ Session expired - login page detected")

        # WAIT FOR PAGE CONTENT
        await self.page.wait_for_selector("text=Interpretation", timeout=30000)

        # -------- PROMPT --------
        prompt = "Not found"
        try:
            el = await self.page.query_selector("p.interpretation")
            if el:
                text = await el.inner_text()
                prompt = re.sub(r'^Interpretation\s*', '', text, flags=re.I).strip()
        except Exception as e:
            print("Prompt error:", e)

        # -------- RESPONSE --------
        response = "Not found"
        try:
            el = await self.page.query_selector('[data-test-id="magi-response"]')
            if el:
                response = await el.evaluate("el => el.innerText")
                response = re.sub(r'\s*<<!floatImage\(.*?\)>>\s*$', '', response)
        except Exception as e:
            print("Response error:", e)

        # -------- SENTIMENT --------
        sentiment = "Not found"
        try:
            el = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("User Sentiment")) span.issue-type'
            )
            if el:
                sentiment = (await el.inner_text()).strip()
        except:
            pass

        # -------- ISSUE TYPE --------
        issue_type = "Not found"
        try:
            el = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("Issue Type")) span.issue-type'
            )
            if el:
                issue_type = (await el.inner_text()).strip()
        except:
            pass

        # -------- USER COMMENT --------
        user_comment = "Not found"
        try:
            el = await self.page.query_selector(
                'div.pill-container.comment-container p.comment'
            )
            if el:
                user_comment = (await el.inner_text()).strip()
        except:
            pass

        print("Prompt:", prompt[:60])
        print("Response:", response[:60])
        print("Sentiment:", sentiment, "| Issue:", issue_type)

        return [url, prompt, response, sentiment, issue_type, user_comment]

# ================= MAIN =================
print("🚀 Script started")

print("TASK_LIST_URL:", os.environ.get("TASK_LIST_URL"))
print("SHEET_URL:", os.environ.get("SHEET_URL"))

print("SESSION_JSON_B64 exists:", bool(os.environ.get("SESSION_JSON_B64")))
print("GOOGLE_CREDS exists:", bool(os.environ.get("GOOGLE_CREDS")))
async def main():
    print("🚀 Starting script...")

    load_session()
    sheet = init_sheet()
    print("✅ Google Sheet connected")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )

        context = await browser.new_context(
            storage_state="session.json",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )

        page = await context.new_page()

        print("🌐 Opening task list...")
        try:
            await safe_goto(page, TASK_LIST_URL)
        except:
            await save_debug(page)
            raise

        print("🌐 Final URL:", page.url)

        # LOGIN CHECK
        if "login" in page.url.lower():
            await save_debug(page)
            raise Exception("❌ Login required on task list")

        scraper = TaskyScraper(page)

        links = await scraper.get_task_links()

        rows = []

        for i, url in enumerate(links, 1):
            try:
                data = await scraper.extract(url, i)
                rows.append(data)
            except Exception as e:
                print("❌ Error:", e)
                rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        # HEADER
        if not sheet.get_all_values():
            sheet.append_row(["URL", "Prompt", "Response", "Sentiment", "Issue", "Comment"])

        safe_append(sheet, rows)

        print("\n✅ DONE: Data uploaded")
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("\n❌❌❌ SCRIPT FAILED ❌❌❌")
        print("ERROR:", str(e))

        import traceback
        traceback.print_exc()

        import sys
        sys.stdout.flush()

        raise
