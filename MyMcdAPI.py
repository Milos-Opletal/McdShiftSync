import requests
import json
import urllib.parse
from bs4 import BeautifulSoup
from enum import IntEnum
from functools import wraps
from typing import Optional, List, Dict, Any


class Role(IntEnum):
    CREW = 1
    CT = 2
    MANAGER = 3


class PermissionDeniedError(Exception):
    """Raised when a user tries to access an endpoint above their permission level."""
    pass


def requires_role(minimum_role: Role):
    """Decorator to enforce base permission levels on API methods."""

    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.role < minimum_role:
                raise PermissionDeniedError(
                    f"Access denied. '{func.__name__}' requires at least {minimum_role.name} level. "
                    f"Your current level is {self.role.name}."
                )
            return func(self, *args, **kwargs)

        return wrapper

    return decorator


class MyMcdAPI:
    # Role ID mappings based on the /api/default-codes endpoint
    MANAGER_POSITION_IDS = {8, 9, 10, 11, 13}
    CT_POSITION_IDS = {5, 6}

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.mymcd2_session = None
        self.phpsessid = None
        self.req_session = requests.Session()

        # Context variables established after login
        self.role = Role.CREW
        self.user_id = None
        self.restaurant_id = None
        self.restaurant_code = None

    import requests

    def login(self):
        login_page_url = "https://mymcd.eu/login/"
        login_post_url = "https://mymcd.eu/user/login-check/"
        dashboard_a_tag_url = "https://next.mymcd.eu/app/shifts/"

        session = requests.Session()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Referer": login_page_url,
            "Origin": "https://mymcd.eu"
        }

        try:
            session.get(login_page_url, headers=headers)

            payload = {
                "_username": self.email,
                "_password": self.password,
                "redir": ""
            }

            response = session.post(login_post_url, data=payload, headers=headers, allow_redirects=True)
            if "skip-wrapper" in response.text:
                raise Exception("Please complete E-learning before proceeding.")

            self.phpsessid = session.cookies.get("PHPSESSID")
            self.mymcd2_session = session.cookies.get("mymcd2_session")
            if not self.mymcd2_session:
                headers["Referer"] = response.url
                session.get(dashboard_a_tag_url, headers=headers)
                self.mymcd2_session = session.cookies.get("mymcd2_session")

            if not self.phpsessid or not self.mymcd2_session:
                raise Exception("Failed to locate required cookies. Authentication may have failed.")

            self._establish_context()

        except Exception as e:
            print(f"Error getting mymcd tokens: {e}")
            raise

    def _get_headers_json(self) -> dict:
        return {
            "Cookie": f"mymcd2_session={self.mymcd2_session}",
            "User-Agent": "Mozilla/5.0 (MyMcd-Wrapper/1.0)",
            "Accept": "application/json"
        }

    def _get_headers_html(self) -> dict:
        return {
            "Cookie": f"PHPSESSID={self.phpsessid}",
            "User-Agent": "Mozilla/5.0 (MyMcd-Wrapper/1.0)",
            "Accept": "text/html"
        }

    def _request_json(self, method: str, url: str, **kwargs) -> Any:
        headers = self._get_headers_json()
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        response = self.req_session.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def _request_html(self, method: str, url: str, **kwargs) -> str:
        headers = self._get_headers_html()
        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        response = self.req_session.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.text

    def _establish_context(self):
        """Fetches /api/user/me to set the user_id, restaurant_id, and dynamically set the role."""
        me_data = self.get_me()
        self.user_id = me_data.get("id")

        primary_restaurant = me_data.get("primaryRestaurant", {})
        self.restaurant_id = primary_restaurant.get("id")
        self.restaurant_code = primary_restaurant.get("code")

        position_id = me_data.get("position", {}).get("id")
        if position_id in self.MANAGER_POSITION_IDS:
            self.role = Role.MANAGER
        elif position_id in self.CT_POSITION_IDS:
            self.role = Role.CT
        else:
            self.role = Role.CREW

        print(f"Logged in as User ID: {self.user_id} | Role: {self.role.name} | Restaurant ID: {self.restaurant_id}")

    # ==========================================
    # CREW LEVEL ENDPOINTS (Base Access)
    # ==========================================

    @requires_role(Role.CREW)
    def get_me(self) -> dict:
        """Fetch the current authenticated user's profile and access data."""
        return self._request_json("GET", "https://next.mymcd.eu/api/user/me")

    @requires_role(Role.CREW)
    def get_default_codes(self) -> dict:
        """Fetch global application definitions (contract types, positions, restaurants, verifications, etc.)."""
        return self._request_json("GET", "https://next.mymcd.eu/api/default-codes")

    @requires_role(Role.CREW)
    def get_events(self, from_date: str, to_date: str, restaurant_id: Optional[int] = None) -> List[dict]:
        """Fetch events (like state holidays / 2x pay days). Dates should be formatted YYYY-MM-DD."""
        target = restaurant_id or self.restaurant_id
        url = f"https://next.mymcd.eu/api/events/{target}?from={from_date}&to={to_date}"
        return self._request_json("GET", url)

    @requires_role(Role.CREW)
    def get_employee_details(self, employee_id: int) -> dict:
        """Fetch basic details for a specific employee. (Phone/Email redacted unless caller is Manager)."""
        url = f"https://next.mymcd.eu/api/employees/single/{employee_id}?simple=false"
        return self._request_json("GET", url)

    @requires_role(Role.CREW)
    def get_employees_data_list(self, limit: int = 100, skip: int = 0, order_by: str = "surname",
                                order_dir: str = "asc", search: str = "") -> dict:
        """Fetch the paginated list of employees for the restaurant."""
        params_dict = {
            "orderBy": order_by,
            "orderByDir": order_dir,
            "skip": skip,
            "limit": limit,
            "search": search,
            "filter": '{"state":null,"positionId":null}'
        }
        encoded_params = urllib.parse.quote(json.dumps(params_dict))
        url = f"https://mymcd.eu/api/employees/{self.restaurant_id}/getData?params={encoded_params}"
        return self._request_json("GET", url)

    # ==========================================
    # MIXED PERMISSION ENDPOINTS (Self vs Others)
    # ==========================================

    @requires_role(Role.CREW)
    def get_employee_shifts(self, from_date: str, to_date: str, employee_id: Optional[int] = None) -> dict:
        """Fetch shift plans. Requires MANAGER role if querying an employee_id other than your own."""
        target = employee_id or self.user_id
        if target != self.user_id and self.role < Role.MANAGER:
            raise PermissionDeniedError("MANAGER role required to view shifts of other employees.")
        url = f"https://next.mymcd.eu/api/shifts-employee/{target}?from={from_date}&to={to_date}"
        return self._request_json("GET", url)

    @requires_role(Role.CREW)
    def get_profile_verifications(self, employee_id: Optional[int] = None) -> dict:
        """
        Fetch training plans and all possible verifications for an employee.
        Requires CT (Crew Trainer) role if querying an employee_id other than your own.
        Splits dates and maps verification IDs from default-codes.
        """
        target = employee_id or self.user_id
        if target != self.user_id and self.role < Role.CT:
            raise PermissionDeniedError("CT role required to view verifications of other employees.")

        # 1. Fetch the master list of all possible verifications from default_codes
        default_codes = self.get_default_codes()
        master_verifications = default_codes.get("verifications", [])

        # Build a map seeded with every possible verification (defaulting to unverified/not assigned)
        verifications_map = {}
        name_to_id_lookup = {}

        for v in master_verifications:
            v_id = v["id"]
            # Prioritize Czech name, fallback to Slovak
            name_cs = v.get("name", {}).get("cs", "").strip()
            name_sk = v.get("name", {}).get("sk", "").strip()
            display_name = name_cs or name_sk

            verifications_map[v_id] = {
                "id": v_id,
                "name": display_name,
                "plan": None,
                "last_verification_date": None,
                "next_verification_date": None,
                "status": "Nepřiřazeno",  # Default to not assigned
                "is_verified": False
            }

            # Populate lookup table with lowercased names for matching HTML
            if name_cs: name_to_id_lookup[name_cs.lower()] = v_id
            if name_sk: name_to_id_lookup[name_sk.lower()] = v_id

        # 2. Fetch the HTML profile
        url = f"https://mymcd.eu/api/profile/refresh/{target}/{self.restaurant_code}/verification/"
        html_content = self._request_html("GET", url)
        soup = BeautifulSoup(html_content, "html.parser")

        tables = soup.find_all("table", class_="courses-table")
        training_plans = []

        # 3. Parse Training Plans (Table 1)
        if len(tables) > 0:
            tp_tbody = tables[0].find("tbody")
            if tp_tbody:
                for row in tp_tbody.find_all("tr"):
                    cols = row.find_all("td")
                    if len(cols) >= 4:
                        name = cols[0].get_text(strip=True)
                        period_str = cols[1].get_text(strip=True)

                        # Split "DD.MM.YYYY - DD.MM.YYYY"
                        start_date, end_date = None, None
                        if "-" in period_str:
                            parts = period_str.split("-")
                            start_date = parts[0].strip()
                            end_date = parts[1].strip()
                        else:
                            start_date = period_str  # Fallback

                        progress_bar = cols[2].find("div", class_="progress-bar")
                        progress = progress_bar.text.strip() if progress_bar else "0%"
                        status = cols[3].get_text(strip=True)

                        training_plans.append({
                            "name": name,
                            "start_date": start_date,
                            "end_date": end_date,
                            "progress": progress,
                            "status": status
                        })

        # 4. Parse Verifications (Table 2) and update the master map
        if len(tables) > 1:
            verif_tbody = tables[1].find("tbody")
            if verif_tbody:
                for row in verif_tbody.find_all("tr"):
                    cols = row.find_all("td")
                    if len(cols) >= 5:
                        raw_name = cols[0].get_text(strip=True)
                        plan = cols[1].get_text(strip=True)

                        last_date = cols[2].get_text(strip=True)
                        if last_date == "N/A": last_date = None

                        next_date = cols[3].get_text(strip=True)
                        status = cols[4].get_text(strip=True)

                        # Determine truthy verified state
                        is_verified = "Verifikován" in status or "Verifikovaný" in status

                        # Find the ID from our lookup table
                        match_id = name_to_id_lookup.get(raw_name.lower())

                        if match_id and match_id in verifications_map:
                            verifications_map[match_id].update({
                                "plan": plan,
                                "last_verification_date": last_date,
                                "next_verification_date": next_date,
                                "status": status,
                                "is_verified": is_verified
                            })
                        else:
                            # Edge case: Verification is in HTML but not in default-codes
                            verifications_map[f"unknown_{raw_name}"] = {
                                "id": None,
                                "name": raw_name,
                                "plan": plan,
                                "last_verification_date": last_date,
                                "next_verification_date": next_date,
                                "status": status,
                                "is_verified": is_verified
                            }

        return {
            "training_plans": training_plans,
            "verifications": list(verifications_map.values())
        }

    # ==========================================
    # MANAGER LEVEL ENDPOINTS (Strict Access)
    # ==========================================

    @requires_role(Role.MANAGER)
    def get_restaurant_shifts(self, from_date: str, to_date: str) -> dict:
        url = f"https://next.mymcd.eu/api/shifts-restaurant/{self.restaurant_id}?from={from_date}&to={to_date}"
        return self._request_json("GET", url)

    @requires_role(Role.MANAGER)
    def get_restaurant_floorplan(self, date: str) -> dict:
        url = f"https://next.mymcd.eu/api/shifts-restaurant/{self.restaurant_id}/floorplan/{date}"
        return self._request_json("GET", url)

    @requires_role(Role.MANAGER)
    def get_restaurant_availability(self, from_date: str, to_date: str) -> List[dict]:
        url = f"https://next.mymcd.eu/api/availability-restaurant/{self.restaurant_id}?from={from_date}&to={to_date}"
        return self._request_json("GET", url)

    @requires_role(Role.MANAGER)
    def get_employee_shift_stats(self, year: int, month: int, employee_ids: List[int]) -> List[dict]:
        """Fetch shift statistics for a list of specific employees."""
        url = f"https://next.mymcd.eu/api/shifts-employees-stats?year={year}&month={month}"
        for emp_id in employee_ids:
            url += f"&employees%5B%5D={emp_id}"
        return self._request_json("GET", url)

    @requires_role(Role.MANAGER)
    def get_expiring_verifications(self, from_date: str, to_date: str, employee_ids: List[int]) -> dict:
        url = f"https://next.mymcd.eu/api/training/expiring-verifications/{self.restaurant_id}?from={from_date}&to={to_date}"
        for emp_id in employee_ids:
            url += f"&employees%5B%5D={emp_id}"
        return self._request_json("GET", url)