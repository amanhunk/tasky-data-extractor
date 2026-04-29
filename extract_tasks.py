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
        raise ValueError("Decoded session JSON does not start with '{' – maybe not valid?")
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

        # Wait for the table rows or any link containing '/tasky/tasks/'
        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)

        while True:
            print(f"📄 Page {page_num}")
            # Get all task links – using a more robust evaluation
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

            # Try to find and click the "Next" button
            next_btn = await self.page.query_selector('button[aria-label="Next page"]:not([disabled])')
            if not next_btn:
                next_btn = await self.page.query_selector('.mat-mdc-paginator-navigation-next:not([disabled])')
            if not next_btn:
                print("🏁 No enabled Next button – last page reached.")
                break
            await next_btn.click()
            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            page_num += 1
            if page_num > 50:
                break

        print(f"✅ Returning {len(all_urls)} task URLs")
        return all_urls

    async def extract_task_details(self, url, task_number):
        print(f"\n--- Task {task_number}: {url} ---")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait for either interpretation or response element (timeout 20s)
        try:
            await self.page.wait_for_selector('p.interpretation, p[data-test-id="magi-response"]', timeout=20000)
            print("   ✅ Page content loaded")
        except:
            print("   ⚠️ Timeout waiting for content – page may be incomplete")
            # Take screenshot for debugging
            await self.page.screenshot(path=f"debug_timeout_{task_number}.png")
            print(f"   📸 Saved screenshot: debug_timeout_{task_number}.png")
        await asyncio.sleep(2)

        # ----- Prompt (from p.interpretation) -----
        prompt = "Not found"
        try:
            elem = await self.page.query_selector('p.interpretation')
            if elem:
                text = await elem.inner_text()
                # Remove the "Interpretation" heading if present
                prompt = re.sub(r'^Interpretation\s*', '', text, flags=re.IGNORECASE).strip()
        except Exception as e:
            print(f"  Prompt error: {e}")

        # ----- Response (from div.bubble.highlighted p[data-test-id="magi-response"]) -----
        response = "Not found"
        try:
            elem = await self.page.query_selector('div.bubble.highlighted p[data-test-id="magi-response"]')
            if not elem:
                elem = await self.page.query_selector('p[data-test-id="magi-response"]')
            if elem:
                response = (await elem.inner_text()).strip()
                # Remove any trailing <<!floatImage...>>
                response = re.sub(r'\s*<<!floatImage\(.*?\)>>\s*$', '', response)
        except Exception as e:
            print(f"  Response error: {e}")

        # ----- Feedback -----
        sentiment = "Not found"
        issue_type = "Not found"
        user_comment = "Not found"
        try:
            # Sentiment
            sent_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("User Sentiment")) span.issue-type'
            )
            if sent_elem:
                sentiment = (await sent_elem.inner_text()).strip()
            else:
                # Fallback: look at the text directly
                page_text = await self.page.evaluate('document.body.innerText')
                sent_match = re.search(r'User Sentiment:\s*(\w+)', page_text, re.IGNORECASE)
                if sent_match:
                    sentiment = sent_match.group(1)

            # Issue Type
            issue_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("Issue Type")) span.issue-type'
            )
            if issue_elem:
                issue_type = (await issue_elem.inner_text()).strip()
            else:
                page_text = await self.page.evaluate('document.body.innerText')
                issue_match = re.search(r'Issue Type:\s*([^\n]+)', page_text, re.IGNORECASE)
                if issue_match:
                    issue_type = issue_match.group(1).strip()

            # User Comment
            comment_elem = await self.page.query_selector('div.pill-container.comment-container p.comment')
            if comment_elem:
                user_comment = (await comment_elem.inner_text()).strip()
            else:
                page_text = await self.page.evaluate('document.body.innerText')
                comment_match = re.search(r'User Comment:\s*(.+?)(?:\n|$)', page_text, re.IGNORECASE)
                if comment_match:
                    user_comment = comment_match.group(1).strip()
        except Exception as e:
            print(f"  Feedback error: {e}")

        # Print extracted data summary
        print(f"   Prompt: {prompt[:60]}...")
        print(f"   Response: {response[:60]}...")
        print(f"   Sentiment: {sentiment}, Issue: {issue_type}")
        print(f"   Comment: {user_comment[:60]}...")
        return (prompt, response, sentiment, issue_type, user_comment)

# ================= MAIN =================
async def main():
    print("🚀 Starting script")
    load_session_from_env()
    sheet = init_sheet()
    print("✅ Google Sheets connected")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()
        scraper = TaskyScraper(page)

        print("🌐 Opening task list...")
        await page.goto(TASK_LIST_URL, timeout=60000, wait_until="domcontentloaded")
        print(f"📄 Page title: {await page.title()}")

        # Wait a bit for the table to render
        await page.wait_for_timeout(5000)

        # Debug: take a screenshot of the list page
        await page.screenshot(path="debug_list_page.png")
        print("📸 Saved screenshot of list page: debug_list_page.png")

        task_urls = await scraper.get_all_task_urls()
        if not task_urls:
            print("❌ No task URLs extracted. Check that the list page contains task links.")
            # Save page content for inspection
            with open("debug_list.html", "w") as f:
                f.write(await page.content())
            print("💾 Saved HTML of list page: debug_list.html")
            await browser.close()
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
        if all_rows:
            safe_append_rows(sheet, all_rows)
            print(f"✅ Uploaded {len(all_rows)} tasks to Google Sheets")
        else:
            print("⚠️ No data to upload")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
