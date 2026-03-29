import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from MyMcdAPI import MyMcdAPI


# Assuming MyMcdAPI is imported or in the same file
# from mymcd_api import MyMcdAPI, Role

class McdShiftManager:
    def __init__(self, api: MyMcdAPI, db_path: str = "mymcd_shifts.sqlite"):
        self.api = api
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the SQLite database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS shifts
                       (
                           id
                           INTEGER
                           PRIMARY
                           KEY,
                           employee_id
                           INTEGER,
                           full_name
                           TEXT,
                           date
                           TEXT,
                           start_time
                           TEXT,
                           end_time
                           TEXT,
                           note
                           TEXT,
                           UNIQUE
                       (
                           employee_id,
                           date,
                           start_time
                       )
                           )
                       ''')
        conn.commit()
        conn.close()

    # ==========================================
    # FUNCTION 1: Sync all shifts to SQLite
    # ==========================================
    def sync_shifts(self, from_date: str, to_date: str):
        """
        Fetches shifts for all restaurant employees and saves them to the database.
        Uses the manager-level restaurant overview for efficiency.
        """
        print(f"Syncing shifts from {from_date} to {to_date}...")
        data = self.api.get_restaurant_shifts(from_date, to_date)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # [cite: 111-118] The restaurant shift endpoint returns 'internalEmployees'
        # which contains employeeInfo and shiftPlans
        employees = data.get("internalEmployees", [])

        for emp in employees:
            info = emp.get("employeeInfo", {})
            e_id = info.get("id")
            name = info.get("fullName")

            for shift in emp.get("shiftPlans", []):
                date = shift.get("date")
                note = shift.get("note")

                # [cite: 48-54] Shifts contain intervals with from/to timestamps
                for interval in shift.get("intervals", []):
                    start = interval.get("from")
                    end = interval.get("to")

                    cursor.execute('''
                        INSERT OR REPLACE INTO shifts (employee_id, full_name, date, start_time, end_time, note)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (e_id, name, date, start, end, note))

        conn.commit()
        conn.close()
        print("Sync complete.")

    # ==========================================
    # FUNCTION 2: Get Special Roles (RS, OS, NS)
    # ==========================================
    def get_special_roles(self, date: str) -> Dict[str, Optional[str]]:
        """
        Returns the names of the people assigned as RS, OS, and NS for a specific date.
        Searches within the shift notes.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        roles = {"RS": None, "OS": None, "NS": None}

        for role in roles.keys():
            #  Notes like "RS" are stored in the shift plan data
            cursor.execute('''
                           SELECT full_name
                           FROM shifts
                           WHERE date = ?
                             AND note LIKE ?
                               LIMIT 1
                           ''', (date, f"%{role}%"))
            result = cursor.fetchone()
            if result:
                roles[role] = result[0]

        conn.close()
        return roles

    # ==========================================
    # FUNCTION 3: Get Coworker Times
    # ==========================================
    def get_coworker_shift_times(self, user_id: int, date: str) -> List[Dict]:
        """
        Returns shifts of coworkers who are in the restaurant at the
        same time as the specified user on a specific date.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Get the user's own shift intervals for this day
        cursor.execute('''
                       SELECT start_time, end_time
                       FROM shifts
                       WHERE employee_id = ? AND date = ?
                       ''', (user_id, date))
        user_shifts = cursor.fetchall()

        if not user_shifts:
            conn.close()
            return []  # User isn't working, so they meet no one

        # 2. Get all other shifts for the day
        cursor.execute('''
                       SELECT employee_id, full_name, start_time, end_time, note
                       FROM shifts
                       WHERE date = ? AND employee_id != ?
                       ''', (date, user_id))
        all_other_shifts = cursor.fetchall()

        meeting_coworkers = []

        for coworker_shift in all_other_shifts:
            c_start = coworker_shift['start_time']
            c_end = coworker_shift['end_time']

            # Check if this coworker's shift overlaps ANY of the user's shift intervals
            overlaps = False
            for u_shift in user_shifts:
                u_start = u_shift['start_time']
                u_end = u_shift['end_time']

                # Mathematical overlap: Max(Starts) < Min(Ends)
                # Or simply: (StartA < EndB) and (EndA > StartB)
                if c_start < u_end and c_end > u_start:
                    overlaps = True
                    break

            if overlaps:
                meeting_coworkers.append(dict(coworker_shift))

        conn.close()
        # Sort by start time for a clean chronological list
        return sorted(meeting_coworkers, key=lambda x: x['start_time'])


# ==========================================
# EXAMPLE USAGE
# ==========================================
if __name__ == "__main__":
    my_api = MyMcdAPI("***REDACTED_EMAIL***", "***REDACTED_PASSWORD***")
    my_api.login()

    app = McdShiftManager(my_api)

    app.sync_shifts("2026-03-01", "2026-03-31")

    special_people = app.get_special_roles("2026-03-29")
    print(f"Special Roles for today: {special_people}")

    # 5. Find everyone else working today (excluding yourself)
    others = app.get_coworker_shift_times(user_id=89600, date="2026-03-29")
    for person in others:
        print(f"{person['full_name']}: {person['start_time']} - {person['end_time']} ({person['note']})")