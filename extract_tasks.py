import json
import os
import asyncio
import time
import re
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
# Load from config.json if exists, otherwise use environment variables
CONFIG_FILE = "config.json"
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    SHEET_URL = config.get("sheet_url", os.environ.get("SHEET_URL"))
    TASK_LIST_URL = config.get("task_list_url", os.environ.get("TASK_LIST_URL"))
else:
    SHEET_URL = os.environ.get("SHEET_URL")
    TASK_LIST_URL = os.environ.get("TASK_LIST_URL")

if not SHEET_URL or not TASK_LIST_URL:
    raise ValueError("Missing SHEET_URL or TASK_LIST_URL. Set via config.json or environment variables.")

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
    elif os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        print("✅ Using credentials from file (credentials.json)")
    else:
        raise FileNotFoundError("No Google credentials found. Set GOOGLE_CREDS env or place credentials.json")

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
        """Paginate through all pages and collect direct task detail URLs."""
        print("🔗 Starting pagination...")
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
                    detail_url = f"https://hume.google.com/datachangereview/{match.group(1)}"
                    if detail_url not in all_urls:
                        new_urls.append(detail_url)
            all_urls.extend(new_urls)
            print(f"   Found {len(new_urls)} new tasks (total: {len(all_urls)})")

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

        print(f"✅ Total unique task URLs: {len(all_urls)}")
        return all_urls

    async def extract_task_details(self, url):
        """Extract prompt, response, sentiment, issue type from task detail page."""
        await self.page.goto(url)
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        prompt = ""
        response = ""
        sentiment = ""
        issue_type = ""

        # Extract Prompt (Interpretation)
        try:
            page_text = await self.page.evaluate('document.body.innerText')
            match = re.search(r'Interpretation\s*\n\s*([^\n]+)', page_text)
            if match:
                prompt = match.group(1).strip()
        except Exception as e:
            print(f"  Prompt extraction error: {e}")

        # Extract Response (modelResponse)
        try:
            page_text = await self.page.evaluate('document.body.innerText')
            match = re.search(r'"modelResponse":\s*"([^"\\]*(?:\\.[^"\\]*)*)"', page_text)
            if match:
                response = match.group(1).replace('\\"', '"').replace('\\n', ' ')
        except Exception as e:
            print(f"  Response extraction error: {e}")

        # Extract Sentiment & Issue Type
        try:
            page_text = await self.page.evaluate('document.body.innerText')
            sent_match = re.search(r'User Sentiment:\s*(\w+)', page_text, re.IGNORECASE)
            if sent_match:
                sentiment = sent_match.group(1).strip()
            issue_match = re.search(r'Issue Type:\s*([^\n]+)', page_text, re.IGNORECASE)
            if issue_match:
                issue_type = issue_match.group(1).strip()
        except Exception as e:
            print(f"  Feedback extraction error: {e}")

        return (prompt or "Not found",
                response or "Not found",
                sentiment or "Not found",
                issue_type or "Not found")

# ================= MAIN =================
async def main():
    sheet = init_sheet()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(storage_state="session.json")
        page = await context.new_page()
        scraper = TaskyScraper(page)

        await page.goto(TASK_LIST_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        detail_urls = await scraper.get_all_task_urls()
        if not detail_urls:
            print("❌ No tasks found.")
            await browser.close()
            return

        all_rows = []
        print(f"\n📊 Extracting data from {len(detail_urls)} tasks...\n")
        for idx, url in enumerate(detail_urls, 1):
            try:
                prompt, response, sentiment, issue_type = await scraper.extract_task_details(url)
                print(f"{idx}/{len(detail_urls)} {url}")
                print(f"   Prompt: {prompt[:80]}...")
                print(f"   Response: {response[:80]}...")
                print(f"   Sentiment: {sentiment}, Issue: {issue_type}\n")
                all_rows.append([url, prompt, response, sentiment, issue_type])
            except Exception as e:
                print(f"❌ Error on {url}: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR"])

        if not sheet.get_all_values():
            sheet.append_row(["Task URL", "Prompt", "Response", "Sentiment", "Issue Type"])
        safe_append_rows(sheet, all_rows)
        print("\n✅ All data uploaded to Google Sheets!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
