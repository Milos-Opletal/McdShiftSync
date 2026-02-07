import requests
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
import pytz

TIMEZONE = 'Europe/Prague'

# --- McDonald's account and shift management ---
def validate_mcd_credentials(email, password):
    """Validates McDonald's credentials by attempting to log in."""
    cookies = {
        'PHPSESSID': 'HiThisIsJustAnAPI',
    }
    data = {
        '_username': email,
        '_password': password,
    }
    session = requests.Session()
    session.max_redirects = 1
    try:
        session.post('https://mymcd.eu/user/login-check/', cookies=cookies, data=data)
        return False
    except requests.exceptions.TooManyRedirects:
        return True
    except:
        return False

def verify_mcd_account(email, password):
    """Verify if McDonald's account is currently valid."""
    try:
        return validate_mcd_credentials(email, password)
    except:
        return False

def get_name(email, password):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(30000)
                page.goto("https://mymcd.eu/login/")
                page.wait_for_selector("#username")
                page.get_by_placeholder("E-mail").fill(email)
                page.get_by_placeholder("Heslo").fill(password)
                page.click("#loginForm > form > div.l-group > input.btn")
                page.wait_for_selector("body > header > div.pull-right.newUserMenu > div > a > span.hidden-xs")
                return page.locator("body > header > div.pull-right.newUserMenu > div > a > span.hidden-xs").text_content().strip()
            finally:
                browser.close()
    except Exception as e:
        print(f"Error getting name: {e}")
        return "Unknown"

def get_mymcd2_token(email, password):
    """Get mymcd2 session token and user_id by logging in with Playwright."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(30000)
                page.goto("https://mymcd.eu/login/")
                page.wait_for_selector("#username")
                page.get_by_placeholder("E-mail").fill(email)
                page.get_by_placeholder("Heslo").fill(password)
                page.click("#loginForm > form > div.l-group > input.btn")
                try:
                    page.wait_for_selector("#dashboardWrapper > div.dashboard-container > div.row.mb-30 > div.col-sm-12.col-md-4 > div.white-box.mb-15 > a", timeout=5000)
                except TimeoutError:
                    page.wait_for_selector("#skip-wrapper > button", timeout=50)
                    raise Exception("Please complete E-learning")
                page.click("#dashboardWrapper > div.dashboard-container > div.row.mb-30 > div.col-sm-12.col-md-4 > div.white-box.mb-15 > a")
                user_id = page.url.split("/")[6]
                mymcd2_session = page.context.storage_state()["cookies"][2]["value"]
                return mymcd2_session, user_id
            finally:
                browser.close()
    except Exception as e:
        print(f"Error getting mymcd2 token: {e}")
        raise Exception("Error getting mymcd2 token")

def get_shifts_from_website(email, password):
    """Get shift data from the website using Playwright and requests."""
    mymcd2_session, user_id = get_mymcd2_token(email, password)
    if not mymcd2_session or not user_id:
        return None
    return get_shifts_json(mymcd2_session, user_id)

def get_shifts_json(mymcd2_session, user_id):
    """Get shift data from the API with optimized date range calculation."""
    cookies = {
        'mymcd2_session': mymcd2_session,
    }
    today = datetime.today()
    first_day_this_month = today.replace(day=1)
    last_day_this_month = (first_day_this_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    first_day_next_month = last_day_this_month + timedelta(days=1)
    last_day_next_month = (first_day_next_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    date_ranges = [
        (first_day_this_month.strftime("%Y-%m-%d"), last_day_this_month.strftime("%Y-%m-%d")),
        (first_day_next_month.strftime("%Y-%m-%d"), last_day_next_month.strftime("%Y-%m-%d"))
    ]
    shifts = []
    for start_date, end_date in date_ranges:
        try:
            response = requests.get(
                f'https://next.mymcd.eu/api/shifts-employee/{user_id}',
                params={'from': start_date, 'to': end_date},
                cookies=cookies,
                timeout=10
            )
            response.raise_for_status()
            shifts.extend(response.json()["shiftPlans"])
        except requests.exceptions.RequestException as e:
            print(f"Error fetching shifts for date range {start_date} to {end_date}: {e}")
            continue
    return shifts 