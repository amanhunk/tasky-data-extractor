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

# ================= DEBUG FUNCTION =================
async def save_debug_info(page, url, task_number):
    """Saves a screenshot and page HTML for debugging (only first task)"""
    if task_number == 1:
        try:
            safe_url = url.replace('https://', '').replace('/', '_').replace(':', '_')
            screenshot_path = f"debug_screenshot_{safe_url}.png"
            html_path = f"debug_page_{safe_url}.html"
            
            await page.screenshot(path=screenshot_path)
            print(f"   📸 Screenshot saved: {screenshot_path}")
            
            html_content = await page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"   📄 Page HTML saved: {html_path}")
        except Exception as e:
            print(f"   ⚠️ Failed to save debug info: {e}")

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
        print(f"🔗 Starting pagination (limit {MAX_TASKS} tasks)...")
        all_urls = []
        page_num = 1
        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)

        while True:
            print(f"📄 Scraping page {page_num}...")
            links = await self.page.eval_on_selector_all(
                'a[href*="/tasky/tasks/"]',
                'elements => elements.map(e => e.href)'
            )
            new_urls = []
            for link in links:
                match = re.search(r'/tasky/tasks/([^/?]+)', link)
                if match:
                    # FIX: Use the correct detail page URL (not datachangereview)
                    detail_url = f"https://hume.google.com/tasky/tasks/{match.group(1)}"
                    if detail_url not in all_urls:
                        new_urls.append(detail_url)
            all_urls.extend(new_urls)
            print(f"   Found {len(new_urls)} new tasks (total: {len(all_urls)})")
            if len(all_urls) >= MAX_TASKS:
                print(f"🏁 Reached limit of {MAX_TASKS} tasks – stopping pagination.")
                all_urls = all_urls[:MAX_TASKS]
                break

            first_url = new_urls[0] if new_urls else None
            next_btn = await self.page.query_selector('.mat-mdc-paginator-navigation-next:not([disabled])')
            if not next_btn:
                next_btn = await self.page.query_selector('button[aria-label="Next page"]:not([disabled])')
            if not next_btn:
                print("🏁 No enabled Next button – last page reached.")
                break

            print("   Clicking Next...")
            await next_btn.click()
            try:
                await self.page.wait_for_function(
                    f'''() => {{
                        const first = document.querySelector('a[href*="/tasky/tasks/"]');
                        if (!first) return false;
                        return first.href !== '{first_url}';
                    }}''',
                    timeout=15000
                )
                print("   Page changed – new tasks loaded.")
            except:
                print("⚠️ First link did not change – assuming no more pages.")
                break

            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            page_num += 1
            if page_num > 500:
                print("⚠️ Stopped at page limit 500.")
                break

        print(f"✅ Returning {len(all_urls)} task URLs (limited to {MAX_TASKS})")
        return all_urls

    async def extract_task_details(self, url, task_number):
        print(f"\n--- Processing Task {task_number}: {url} ---")
        
        # Wait for navigation to finish
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)
        
        # Debug: print page title and URL
        title = await self.page.title()
        print(f"   🌐 Page title: '{title}'")
        print(f"   🔗 Current URL: '{self.page.url}'")
        
        # Save debug info for first task
        await save_debug_info(self.page, url, task_number)
        
        # Check for login page
        if "login" in self.page.url.lower() or "signin" in self.page.url.lower():
            print("   ❌❌❌ DETECTED LOGIN PAGE! Session expired. ❌❌❌")
        
        # ----- Prompt -----
        prompt = "Not found"
        try:
            prompt_elem = await self.page.query_selector('p.interpretation')
            if prompt_elem:
                full_text = await prompt_elem.inner_text()
                prompt = re.sub(r'^Interpretation\s*', '', full_text, flags=re.IGNORECASE).strip()
        except Exception as e:
            print(f"  Prompt error: {e}")
        
        # ----- Response -----
        response = "Not found"
        try:
            resp_elem = await self.page.query_selector('div.bubble.highlighted p[data-test-id="magi-response"]')
            if not resp_elem:
                resp_elem = await self.page.query_selector('p[data-test-id="magi-response"]')
            if resp_elem:
                response = (await resp_elem.inner_text()).strip()
                response = re.sub(r'\s*<<!floatImage\(.*?\)>>\s*$', '', response)
        except Exception as e:
            print(f"  Response error: {e}")
        
        # ----- Feedback -----
        sentiment = "Not found"
        issue_type = "Not found"
        user_comment = "Not found"
        try:
            sent_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("User Sentiment")) span.issue-type'
            )
            if sent_elem:
                sentiment = (await sent_elem.inner_text()).strip()
            
            issue_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("Issue Type")) span.issue-type'
            )
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
    print("🏁 Current working directory:", os.getcwd())
    print("📁 Directory contents:", os.listdir('.'))

    load_session_from_env()
    sheet = init_sheet()
    print("✅ Google Sheets connected.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()
        scraper = TaskyScraper(page)

        print("🌐 Navigating to task list...")
        await page.goto(TASK_LIST_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        detail_urls = await scraper.get_all_task_urls()
        if not detail_urls:
            print("❌ No tasks found.")
            await browser.close()
            return

        all_rows = []
        print(f"\n📊 Extracting data from {len(detail_urls)} tasks (max {MAX_TASKS})...\n")
        for idx, url in enumerate(detail_urls, 1):
            try:
                # Navigate to the task detail page
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                prompt, response, sentiment, issue_type, user_comment = await scraper.extract_task_details(url, idx)
                print(f"{idx}/{len(detail_urls)} {url}")
                print(f"   Prompt: {prompt[:80]}...")
                print(f"   Response: {response[:80]}...")
                print(f"   Sentiment: {sentiment}, Issue: {issue_type}")
                print(f"   User Comment: {user_comment[:80]}...\n")
                all_rows.append([url, prompt, response, sentiment, issue_type, user_comment])
            except Exception as e:
                print(f"❌ Error on {url}: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        if not sheet.get_all_values():
            sheet.append_row(["Task URL", "Prompt", "Response", "Sentiment", "Issue Type", "User Comment"])
        safe_append_rows(sheet, all_rows)
        print("\n✅ All data uploaded to Google Sheets!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
