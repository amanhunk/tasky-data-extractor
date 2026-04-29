import json
import os
import asyncio
import time
import re
from playwright.async_api import async_playwright
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1L1IhWoaMMIWjCW1v19KRc-8_ujdnM0Min2mw4mGVig8/edit?gid=776117815#gid=776117815"
TASK_LIST_URL = "https://hume.google.com/tasky/tasks?filter=job:aim_loss_pattern_labeling%20status:completed%20update_time%3E2026-04-01"

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
        """Paginate through all pages and collect direct review URLs."""
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
                    review_url = f"https://hume.google.com/datachangereview/{match.group(1)}"
                    if review_url not in all_urls:
                        new_urls.append(review_url)
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
        """Extract prompt, response, user comment, sentiment, issue type."""
        await self.page.goto(url)
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)   # allow dynamic content

        # ----- Prompt -----
        prompt = "Not found"
        try:
            # Look for p.interpretation
            elem = await self.page.query_selector('p.interpretation')
            if elem:
                text = await elem.inner_text()
                # Remove the "Interpretation" heading
                prompt = re.sub(r'^Interpretation\s*', '', text, flags=re.IGNORECASE).strip()
            else:
                # Fallback: any text inside .bubble that is not empty
                bubble = await self.page.query_selector('.bubble')
                if bubble:
                    text = await bubble.inner_text()
                    # If the bubble contains many lines, extract the first meaningful line
                    lines = [l.strip() for l in text.split('\n') if l.strip()]
                    if lines:
                        prompt = lines[0]
        except Exception as e:
            print(f"  Prompt error: {e}")

        # ----- Response -----
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

        # ----- User Comment, Sentiment, Issue Type -----
        user_comment = "Not found"
        sentiment = "Not found"
        issue_type = "Not found"
        try:
            # User Comment
            comment_elem = await self.page.query_selector('div.pill-container.comment-container p.comment')
            if comment_elem:
                user_comment = (await comment_elem.inner_text()).strip()

            # Sentiment
            sent_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("User Sentiment")) span.issue-type'
            )
            if sent_elem:
                sentiment = (await sent_elem.inner_text()).strip()

            # Issue Type
            issue_elem = await self.page.query_selector(
                'div.pill-container:has(span.pill-label:has-text("Issue Type")) span.issue-type'
            )
            if issue_elem:
                issue_type = (await issue_elem.inner_text()).strip()
        except Exception as e:
            print(f"  Feedback error: {e}")

        return prompt, response, sentiment, issue_type, user_comment

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

        # Go to the task list
        await page.goto(TASK_LIST_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # 1. Get all task detail URLs
        task_urls = await scraper.get_all_task_urls()
        if not task_urls:
            print("❌ No tasks found. Check your session or filter.")
            await browser.close()
            return

        # 2. Extract data from each task
        all_rows = []
        print(f"\n📊 Extracting data from {len(task_urls)} tasks...\n")
        for idx, url in enumerate(task_urls, 1):
            try:
                prompt, response, sentiment, issue_type, user_comment = await scraper.extract_task_details(url)
                print(f"{idx}/{len(task_urls)} {url}")
                print(f"   Prompt: {prompt[:80]}...")
                print(f"   Response: {response[:80]}...")
                print(f"   Sentiment: {sentiment}, Issue: {issue_type}")
                print(f"   User Comment: {user_comment[:80]}...")
                print()
                all_rows.append([url, prompt, response, sentiment, issue_type, user_comment])
            except Exception as e:
                print(f"❌ Error on {url}: {e}")
                all_rows.append([url, "ERROR", "ERROR", "ERROR", "ERROR", "ERROR"])

        # Append headers if sheet is empty
        if not sheet.get_all_values():
            sheet.append_row(["Task URL", "Prompt", "Response", "Sentiment", "Issue Type", "User Comment"])
        safe_append_rows(sheet, all_rows)
        print("\n✅ All data uploaded to Google Sheets!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
