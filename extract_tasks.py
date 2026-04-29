import json
import os
import asyncio
import time
import re
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
SHEET_URL = os.environ.get("SHEET_URL")
TASK_LIST_URL = os.environ.get("TASK_LIST_URL")
MAX_TASKS = 10
HUME_EMAIL = os.environ.get("HUME_EMAIL")
HUME_PASSWORD = os.environ.get("HUME_PASSWORD")

if not SHEET_URL or not TASK_LIST_URL:
    raise ValueError("Missing SHEET_URL or TASK_LIST_URL environment variables")
if not HUME_EMAIL or not HUME_PASSWORD:
    raise ValueError("Missing HUME_EMAIL or HUME_PASSWORD environment variables")

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

# ================= SCRAPER =================
class TaskyScraper:
    def __init__(self, page):
        self.page = page

    async def login(self):
        print("🔐 Logging in dynamically...")
        await self.page.goto("https://hume.google.com/tasky/tasks", wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)

        if "login" not in self.page.url.lower() and "signin" not in self.page.url.lower():
            print("   Already logged in.")
            return

        await self.page.screenshot(path="debug_before_login.png")
        print("   📸 Saved debug_before_login.png")

        try:
            await self.page.click('button:has-text("Sign in with Google")', timeout=10000)
            print("   Clicked 'Sign in with Google'")
        except:
            try:
                await self.page.click('div[aria-label="Sign in with Google"]', timeout=5000)
            except:
                pass

        await self.page.wait_for_function(
            '() => window.location.href.includes("accounts.google.com")',
            timeout=15000
        )
        await self.page.wait_for_timeout(2000)
        await self.page.screenshot(path="debug_google_page.png")
        print("   📸 Saved debug_google_page.png")

        # Handle account chooser
        try:
            account_selector = f'div[data-email="{HUME_EMAIL}"]'
            account = await self.page.query_selector(account_selector, timeout=5000)
            if account:
                await account.click()
                print("   Selected existing account")
                await self.page.wait_for_timeout(2000)
        except:
            pass

        # Email step
        try:
            email_input = await self.page.wait_for_selector('input[type="email"]', timeout=8000)
            await email_input.fill(HUME_EMAIL)
            await self.page.click('button:has-text("Next")')
            print("   Entered email and clicked Next")
            await self.page.wait_for_timeout(3000)
        except:
            print("   Email field not found, maybe already at password step")

        # Password step
        try:
            password_input = await self.page.wait_for_selector('input[type="password"]', timeout=15000)
            await password_input.fill(HUME_PASSWORD)
            await self.page.click('button:has-text("Next")')
            print("   Entered password and clicked Next")
        except Exception as e:
            await self.page.screenshot(path="debug_password_failed.png")
            print(f"   ❌ Password field not found: {e}")
            raise

        # Wait for redirect
        try:
            await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=30000)
            print("   ✅ Login successful.")
        except:
            await self.page.screenshot(path="debug_post_login.png")
            raise Exception("Login succeeded but did not return to task list")

    async def get_all_task_urls(self):
        print(f"🔗 Extracting task links (limit {MAX_TASKS})...")
        all_urls = []
        page_num = 1
        await self.page.wait_for_selector('a[href*="/tasky/tasks/"]', timeout=60000)

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
                    detail_url = f"https://hume.google.com/tasky/tasks/{match.group(1)}"
                    if detail_url not in all_urls:
                        new_urls.append(detail_url)
            all_urls.extend(new_urls)
            print(f"   Found {len(new_urls)} new (total: {len(all_urls)})")
            if len(all_urls) >= MAX_TASKS:
                all_urls = all_urls[:MAX_TASKS]
                break

            next_btn = await self.page.query_selector('button[aria-label="Next page"]:not([disabled])')
            if not next_btn:
                next_btn = await self.page.query_selector('.mat-mdc-paginator-navigation-next:not([disabled])')
            if not next_btn:
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
        for attempt in range(3):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await self.page.wait_for_selector('p.interpretation, p[data-test-id="magi-response"]', timeout=45000)
                print("   ✅ Page content loaded")
                break
            except:
                print(f"⚠️ Timeout, retry {attempt+1}/3...")
                if attempt == 2:
                    print("   ❌ Failed after 3 retries.")
                    return ("ERROR_TIMEOUT",) * 5
                await asyncio.sleep(5)

        # Extraction (same as before)
        prompt = "Not found"
        try:
            elem = await self.page.query_selector('p.interpretation')
            if elem:
                text = await elem.inner_text()
                prompt = re.sub(r'^Interpretation\s*', '', text, flags=re.IGNORECASE).strip()
            else:
                page_text = await self.page.evaluate('document.body.innerText')
                match = re.search(r'Interpretation\s*\n\s*([^\n]+)', page_text)
                if match:
                    prompt = match.group(1).strip()
        except Exception as e:
            print(f"  Prompt error: {e}")

        response = "Not found"
        try:
            elem = await self.page.query_selector('div.bubble.highlighted p[data-test-id="magi-response"]')
            if not elem:
                elem = await self.page.query_selector('p[data-test-id="magi-response"]')
            if elem:
                response = (await elem.inner_text()).strip()
                response = re.sub(r'<<!floatImage.*?>>', '', response)
        except Exception as e:
            print(f"  Response error: {e}")

        sentiment = issue_type = user_comment = "Not found"
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
            else:
                page_text = await self.page.evaluate('document.body.innerText')
                match = re.search(r'User Comment:\s*(.+?)(?:\n|$)', page_text, re.IGNORECASE)
                if match:
                    user_comment = match.group(1).strip()
        except Exception as e:
            print(f"  Feedback error: {e}")

        print(f"   Prompt: {prompt[:60]}...")
        print(f"   Response: {response[:60]}...")
        print(f"   Sentiment: {sentiment}, Issue: {issue_type}")
        return (prompt, response, sentiment, issue_type, user_comment)

# ================= MAIN =================
async def main():
    print("🚀 Starting script")
    sheet = init_sheet()
    print("✅ Google Sheets connected")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--window-size=1920,1080',
                '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ]
        )
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_viewport_size({"width": 1920, "height": 1080})
        scraper = TaskyScraper(page)

        await scraper.login()

        print("🌐 Navigating to task list...")
        await page.goto(TASK_LIST_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        print(f"📄 Page title: {await page.title()}")

        task_urls = await scraper.get_all_task_urls()
        if not task_urls:
            print("❌ No task URLs extracted.")
            return

        all_rows = []
        for idx, url in enumerate(task_urls, 1):
            try:
                data = await scraper.extract_task_details(url, idx)
                all_rows.append([url] + list(data))
            except Exception as e:
                print(f"❌ Task {idx} error: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        if all_rows:
            if not sheet.get_all_values():
                sheet.append_row(["Task URL", "Prompt", "Response", "Sentiment", "Issue Type", "User Comment"])
            safe_append_rows(sheet, all_rows)
            print(f"✅ Uploaded {len(all_rows)} tasks to Google Sheets")
        else:
            print("⚠️ No data to upload")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
