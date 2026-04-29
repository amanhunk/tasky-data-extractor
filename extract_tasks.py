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
    raise ValueError("Missing SHEET_URL or TASK_LIST_URL environment variables")

# ================= GOOGLE SHEETS =================
def init_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS")
    if not creds_json:
        raise ValueError("Missing GOOGLE_CREDS environment variable")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
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

def load_session_from_env():
    session_b64 = os.environ.get("SESSION_JSON_B64")
    if not session_b64:
        raise ValueError("Missing SESSION_JSON_B64 environment variable")
    print(f"✅ SESSION_JSON_B64 length: {len(session_b64)} characters")
    try:
        session_json = base64.b64decode(session_b64).decode('utf-8')
        print(f"✅ Decoding successful, decoded length: {len(session_json)} characters")
    except Exception as e:
        raise ValueError(f"Base64 decoding failed: {e}")
    if not session_json.strip().startswith('{'):
        raise ValueError("Decoded session JSON does not start with '{'")
    with open("session.json", "w") as f:
        f.write(session_json)
    if os.path.exists("session.json"):
        file_size = os.path.getsize("session.json")
        print(f"✅ session.json written successfully, size = {file_size} bytes")
    else:
        raise RuntimeError("Failed to write session.json")

# ================= SCRAPER =================
class TaskyScraper:
    def __init__(self, page):
        self.page = page

    async def get_all_task_urls(self):
        print(f"🔗 Extracting task links (limit {MAX_TASKS})...")
        all_urls = []
        page_num = 1
        # Wait for the table to appear – use a more generic selector
        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)

        while True:
            print(f"📄 Page {page_num}")
            # Get all task links on current page
            links = await self.page.eval_on_selector_all(
                'a[href*="/tasky/tasks/"]',
                'elements => elements.map(e => e.href)'
            )
            new_urls = []
            for link in links:
                match = re.search(r'/tasky/tasks/([^/?]+)', link)
                if match:
                    detail_url = f"https://hume.google.com/tasky/tasks/{match.group(1)}"
                    if detail_url not in all_urls:
                        new_urls.append(detail_url)
            all_urls.extend(new_urls)
            print(f"   Found {len(new_urls)} new (total: {len(all_urls)})")
            if len(all_urls) >= MAX_TASKS:
                all_urls = all_urls[:MAX_TASKS]
                break

            # Try to click "Next" button
            next_btn = await self.page.query_selector('button[aria-label="Next page"]:not([disabled])')
            if not next_btn:
                next_btn = await self.page.query_selector('.mat-mdc-paginator-navigation-next:not([disabled])')
            if not next_btn:
                break
            await next_btn.click()
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            page_num += 1
            if page_num > 10:  # safety limit
                break
        print(f"✅ Returning {len(all_urls)} task URLs")
        return all_urls

    async def extract_task_details(self, url, task_number):
        print(f"\n--- Task {task_number}: {url} ---")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Wait for the detail page to load – look for interpretation
        try:
            await self.page.wait_for_selector('p.interpretation', timeout=15000)
        except:
            print("   ⚠️ No p.interpretation found – page may be incomplete")
        await asyncio.sleep(2)
        # Debug info
        print(f"   Title: {await self.page.title()}")
        print(f"   URL: {self.page.url}")

        # ----- Prompt -----
        prompt = "Not found"
        try:
            elem = await self.page.query_selector('p.interpretation')
            if elem:
                text = await elem.inner_text()
                prompt = re.sub(r'^Interpretation\s*', '', text, flags=re.IGNORECASE).strip()
        except Exception as e:
            print(f"  Prompt error: {e}")

        # ----- Response -----
        response = "Not found"
        try:
            elem = await self.page.query_selector('p[data-test-id="magi-response"]')
            if elem:
                response = (await elem.inner_text()).strip()
                response = re.sub(r'<<!floatImage.*?>>', '', response)
        except Exception as e:
            print(f"  Response error: {e}")

        # ----- Feedback -----
        sentiment = "Not found"
        issue_type = "Not found"
        user_comment = "Not found"
        try:
            sent_elem = await self.page.query_selector('div.pill-container:has(span.pill-label:has-text("User Sentiment")) span.issue-type')
            if sent_elem:
                sentiment = (await sent_elem.inner_text()).strip()
            issue_elem = await self.page.query_selector('div.pill-container:has(span.pill-label:has-text("Issue Type")) span.issue-type')
            if issue_elem:
                issue_type = (await issue_elem.inner_text()).strip()
            comment_elem = await self.page.query_selector('div.pill-container.comment-container p.comment')
            if comment_elem:
                user_comment = (await comment_elem.inner_text()).strip()
        except Exception as e:
            print(f"  Feedback error: {e}")

        return (prompt, response, sentiment, issue_type, user_comment)

# ================= MAIN =================
async def main():
    print("🚀 Script started")
    load_session_from_env()
    sheet = init_sheet()
    print("✅ Google Sheets connected")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()
        scraper = TaskyScraper(page)

        print("🌐 Opening task list...")
        await page.goto(TASK_LIST_URL, timeout=60000, wait_until="domcontentloaded")
        # DEBUG: show page title and a screenshot
        print(f"📄 Page title: {await page.title()}")
        await page.screenshot(path="debug_list_page.png")
        print("📸 Screenshot saved: debug_list_page.png")
        # Also print first 500 chars of body
        body = await page.content()
        print("📄 Body snippet:", body[:500])

        task_urls = await scraper.get_all_task_urls()
        if not task_urls:
            print("❌ No tasks found – check filter or session")
            # Save full page source for debugging
            with open("debug_page_source.html", "w") as f:
                f.write(body)
            print("💾 Saved full page HTML as debug_page_source.html")
            return

        all_rows = []
        for idx, url in enumerate(task_urls, 1):
            try:
                data = await scraper.extract_task_details(url, idx)
                all_rows.append([url] + list(data))
            except Exception as e:
                print(f"❌ Task {idx} error: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        if not sheet.get_all_values():
            sheet.append_row(["Task URL", "Prompt", "Response", "Sentiment", "Issue Type", "User Comment"])
        safe_append_rows(sheet, all_rows)
        print("✅ Data uploaded")

if __name__ == "__main__":
    asyncio.run(main())
