import datetime
import json
import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta
import pytz
from flask import Flask
from google.oauth2.credentials import Credentials
from googleapiclient import errors
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Logging Configuration ---
# This sets up logging to print to the console with timestamps.
# In Docker, this output will be captured automatically.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(ROOT_DIR)

from webapp.db_manager import db, User, get_user_by_google_id, update_last_sync, set_sync_status, set_calendar_id, \
    delete_user, create_or_update_user, get_sync_status
from MyMcdAPI import MyMcdAPI

TIMEZONE = 'Europe/Prague'
MANAGER_EMAIL = '***REDACTED_EMAIL***'
MANAGER_PASSWORD = '***REDACTED_PASSWORD***'


def get_yesterday():
    """Calculates 'yesterday' dynamically to ensure it is always current."""
    return datetime.combine(datetime.today() - timedelta(days=1), datetime.min.time(), tzinfo=pytz.timezone(TIMEZONE))


def get_calendar_name(username):
    """Generate a personalized calendar name for the user."""
    return f"McDonald's - {username}"


def get_calendar_service(user_data):
    """
    Shows basic usage of the Google Calendar API.
    Handles user authentication and token management.
    """
    logger.info(f"Getting calendar service for user {user_data.id}")
    creds = user_data.get_google_token()
    if not creds:
        logger.warning(f"No credentials found for user {user_data.id}")
        return None

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info(f"Successfully built calendar service for user {user_data.id}")
        return service
    except Exception as e:
        logger.error(f"Error building calendar service for user {user_data.id}: {e}")
        return None


def create_calendar(service, calendar_name, user):
    """Creates a calendar with the given name if it does not exist."""
    logger.info(f"Checking/creating calendar: {calendar_name}")
    try:
        # First check if we have a stored calendar ID
        if user.google_calendar_id:
            try:
                # Try to get the calendar to verify it exists
                service.calendars().get(calendarId=user.google_calendar_id).execute()

                # Check if user is still subscribed to the calendar
                calendar_list = service.calendarList().list().execute()
                for calendar in calendar_list.get('items', []):
                    if calendar['id'] == user.google_calendar_id:
                        logger.info(f"Using existing calendar with ID: {user.google_calendar_id}")
                        return user.google_calendar_id

                logger.info("User is no longer subscribed to the calendar, deleting old calendar and creating new one")
                try:
                    # Try to delete the old calendar
                    service.calendars().delete(calendarId=user.google_calendar_id).execute()
                    logger.info(f"Successfully deleted old calendar with ID: {user.google_calendar_id}")
                except Exception as del_error:
                    logger.warning(f"Could not delete old calendar: {del_error}")

            except Exception as e:
                logger.warning(f"Stored calendar is inaccessible: {e}, will create new calendar")

        # Create new calendar
        calendar = {"summary": calendar_name}
        created_calendar = service.calendars().insert(body=calendar).execute()
        calendar_id = created_calendar['id']

        # Store the calendar ID in the database
        set_calendar_id(user.google_id, calendar_id)

        logger.info(f"Created new calendar '{calendar_name}' with ID: {calendar_id}")
        return calendar_id
    except HttpError as error:
        logger.error(f"HTTP error occurred creating calendar: {error}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error creating calendar: {e}")
        return None




def execute_batch_async(creds, calendar_id, chunks, action):
    """Executes batched requests in a background thread."""
    try:
        # Build thread-safe service connection
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        for chunk in chunks:
            batch = service.new_batch_http_request()
            for item in chunk:
                if action == "create":
                    batch.add(service.events().insert(calendarId=calendar_id, body=item))
                elif action == "delete":
                    batch.add(service.events().delete(calendarId=calendar_id, eventId=item))
            try:
                batch.execute()
                logger.info(f"Background {action} batch successfully executed.")
            except Exception as e:
                logger.error(f"Error executing background {action} batch: {e}")
    except Exception as e:
        logger.error(f"Background thread connection error: {e}")

def batch_create_events(creds, calendar_id, events_data):
    """Queues events into 50-item chunks and processes them via async background thread."""
    if not events_data:
        return False

    logger.info(f"Queuing {len(events_data)} events for background batch creation")
    chunk_size = 50
    chunks = [events_data[i:i + chunk_size] for i in range(0, len(events_data), chunk_size)]
    
    # Dispatch async thread
    thread = threading.Thread(target=execute_batch_async, args=(creds, calendar_id, chunks, "create"), daemon=True)
    thread.start()
    return True


def batch_delete_events(creds, calendar_id, event_ids):
    """Queues events into 50-item chunks and processes them via async background thread."""
    if not event_ids:
        return True

    logger.info(f"Queuing {len(event_ids)} events for background batch deletion")
    chunk_size = 50
    chunks = [event_ids[i:i + chunk_size] for i in range(0, len(event_ids), chunk_size)]
    
    # Dispatch async thread
    thread = threading.Thread(target=execute_batch_async, args=(creds, calendar_id, chunks, "delete"), daemon=True)
    thread.start()
    return True


def delete_upcoming_events(service, calendar_id, new_shifts, premium, user_id):
    """Collects shift-related events from the calendar that should be deleted."""
    logger.info("Starting to process upcoming events for deletion")
    matched_shift_indices = set()
    events_to_delete = []

    tz = pytz.timezone(TIMEZONE)
    start_time_check = get_yesterday()
    
    # Pre-compute fingerprints for incoming shifts (O(1) memory map)
    new_shifts_fingerprints = {}
    if new_shifts:
        for idx, new_shift in enumerate(new_shifts):
            try:
                summary = create_event_summary(new_shift)
                start_str = new_shift['intervals'][0]['from']
                end_str = new_shift['intervals'][0]['to']
                date_str = start_str.split(" ")[0]  # Extract just the date part
                description = create_event_description(new_shift, premium, date_str, user_id)
                new_start = tz.localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
                new_end = tz.localize(datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S"))
                key = (new_start.timestamp(), new_end.timestamp(), summary, description)
                new_shifts_fingerprints[key] = idx
            except Exception as e:
                logger.error(f"Error fingerprinting shift: {e}")

    try:
        events_result = service.events().list(calendarId=calendar_id, timeMin=start_time_check.isoformat(),
                                              singleEvents=True,
                                              orderBy='startTime').execute()
        upcoming_events = events_result.get('items', [])

        if not upcoming_events:
            logger.info("No upcoming events found in calendar.")
            return matched_shift_indices, events_to_delete

        shift_events_skipped = 0

        for event in upcoming_events:
            try:
                cal_summary = event.get('summary', '')
                cal_description = event.get('description', '')
                cal_start_str = event['start'].get('dateTime', event['start'].get('date', '')).replace('Z', '+00:00')
                cal_end_str = event['end'].get('dateTime', event['end'].get('date', '')).replace('Z', '+00:00')

                # Ensure valid start strings internally mapping to YYYY-MM-DD
                if len(cal_start_str) <= 10 or len(cal_end_str) <= 10:
                    events_to_delete.append(event['id'])
                    continue

                cal_start = datetime.fromisoformat(cal_start_str).astimezone(tz)
                cal_end = datetime.fromisoformat(cal_end_str).astimezone(tz)
                
                key = (cal_start.timestamp(), cal_end.timestamp(), cal_summary, cal_description)

                if key in new_shifts_fingerprints:
                    matched_shift_indices.add(new_shifts_fingerprints[key])
                    shift_events_skipped += 1
                else:
                    events_to_delete.append(event['id'])
            except Exception as e:
                logger.error(f"Error processing event footprint: {e}")
                events_to_delete.append(event['id'])

        logger.info(f"Found {len(events_to_delete)} events to delete and {shift_events_skipped} matching events to keep")
        return matched_shift_indices, events_to_delete

    except HttpError as error:
        logger.error(f"Error retrieving events: {error}")
        return matched_shift_indices, events_to_delete


def create_event_summary(data):
    event_summary = "Shift"
    if data.get('note'):
        event_summary += "+"
    return event_summary


def create_event_description(data, premium, date, user_id):
    description_parts = []
    if data.get('hasBreak'):
        if data['hasBreak']:
            description_parts.append(f"Has 30 min break")
    if data.get('note') is not None:
        description_parts.append(f"note: {data['note']}")

    if premium:
        try:
            from McdShiftManager import McdShiftManager
            from webapp.db_manager import PersonOfInterest
            manager = McdShiftManager(None, db_path=os.path.join(ROOT_DIR, "db", "mymcd_shifts.sqlite"))
            
            # Special Roles
            special_roles = manager.get_special_roles(date)
            roles_text = []
            for role, name in special_roles.items():
                if name:
                    roles_text.append(f"{role}: {name}")
            if roles_text:
                description_parts.append("\nShift Managers:")
                description_parts.extend(roles_text)
            
            # Coworkers filtering
            poi_records = PersonOfInterest.query.all()
            poi_ids = {str(r.mcd_id) for r in poi_records}
            
            if poi_ids and user_id is not None:
                coworkers = manager.get_coworker_shift_times(user_id, date)
                filtered_coworkers = [c for c in coworkers if str(c['employee_id']) in poi_ids]
                if filtered_coworkers:
                    description_parts.append("\nSpecial Coworkers:")
                    for c in filtered_coworkers:
                        note_str = f" ({c['note']})" if c['note'] else ""
                        # Format "YYYY-MM-DD HH:MM:SS" into "HH:MM"
                        start_time = c['start_time'].split(" ")[1][:5] if " " in c['start_time'] else c['start_time']
                        end_time = c['end_time'].split(" ")[1][:5] if " " in c['end_time'] else c['end_time']
                        description_parts.append(f"{c['full_name']}: {start_time} - {end_time}{note_str}")
        except Exception as e:
            logger.error(f"Error appending premium data: {e}")

    description_parts.append(":)")
    return "\n".join(description_parts)


def create_events_from_data(creds, calendar_id, data_array, skip_indices=None, premium=False, user_id=None):
    if skip_indices is None:
        skip_indices = set()
    events_to_create = []
    events_skipped = 0
    past_shifts = 0

    # Calculate yesterday dynamically
    check_date_limit = get_yesterday()

    for idx, data in enumerate(data_array):
        if idx in skip_indices:
            events_skipped += 1
            continue
        try:
            event_intervals = data['intervals']
            for interval in event_intervals:
                start_time_str = interval['from']
                end_time_str = interval['to']

            date_to_check = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
            timezone = pytz.timezone(TIMEZONE)
            date_to_check = timezone.localize(date_to_check)

            if date_to_check < check_date_limit:
                past_shifts += 1
                continue

            date_str = start_time_str.split(" ")[0]
            event_summary = create_event_summary(data)
            event_description = create_event_description(data, premium, date_str, user_id)
            iso_start = start_time_str.replace(" ", "T")
            iso_end = end_time_str.replace(" ", "T")

            event_data = {
                "summary": event_summary, 
                "description": event_description,
                "start": {"dateTime": iso_start, "timeZone": TIMEZONE},
                "end": {"dateTime": iso_end, "timeZone": TIMEZONE}
            }
            events_to_create.append(event_data)
        except Exception as e:
            logger.error(f"Error processing shift {idx}: {e}")
            continue

    if events_to_create:
        if batch_create_events(creds, calendar_id, events_to_create):
            events_created = len(events_to_create)
        else:
            events_created = 0
    else:
        logger.info("No events to create")
        events_created = 0
    return events_created, events_skipped


def sync_user_data(user_id):
    """Sync user's McDonald's data with their calendar using the database."""
    current_user = get_user_by_google_id(user_id)
    if not current_user or not current_user.mcd_email or not current_user.mcd_password or not current_user.google_token:
        if current_user:
            set_sync_status(user_id, success=False, error_message='No configuration found for user')
        return False

    try:
        credentials = Credentials.from_authorized_user_info(json.loads(current_user.google_token))
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except Exception as e:
        error_message = str(e)
        if "invalid_grant: Token has been expired or revoked." in error_message:
            logger.warning(f"Token revoked for user {user_id}, deleting user from database")
            delete_user(user_id)
            return False
        set_sync_status(user_id, success=False, error_message='Failed to get calendar service')
        return False

    try:
        calendar_name = get_calendar_name(current_user.google_name)
        calendar_id = create_calendar(service, calendar_name, current_user)
        if calendar_id is None:
            set_sync_status(user_id, success=False, error_message='Failed to create or find calendar')
            return False
    except Exception:
        set_sync_status(user_id, success=False, error_message='Failed to create or find calendar')
        return False

    try:
        api = MyMcdAPI(current_user.mcd_email, current_user.mcd_password)
        api.login()
        
        today = datetime.today()
        first_day_this_month = today.replace(day=1)
        last_day_this_month = (first_day_this_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        first_day_next_month = last_day_this_month + timedelta(days=1)
        last_day_next_month = (first_day_next_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        
        from_str = first_day_this_month.strftime("%Y-%m-%d")
        to_str = last_day_next_month.strftime("%Y-%m-%d")
        
        shifts_data = api.get_employee_shifts(from_str, to_str)
        data_array = shifts_data.get("shiftPlans", [])
        employee_id = current_user.mcd_id or api.user_id
    except Exception as e:
        logger.error(f"Failed to get timetable data: {e}")
        set_sync_status(user_id, success=False,
                        error_message="Failed to get timetable data - check mcd website \n https://mymcd.eu/")
        return False

    logger.info(f"Processing calendar synchronization logic for user {user_id}...")

    matched_shift_indices, events_to_delete = delete_upcoming_events(service, calendar_id, data_array, current_user.premium, employee_id)
    batch_delete_events(credentials, calendar_id, events_to_delete)

    create_events_from_data(credentials, calendar_id, data_array, matched_shift_indices, current_user.premium, employee_id)

    update_last_sync(user_id)
    set_sync_status(user_id, success=True, error_message=None)
    
    return True


def create_error_event(user_id, service=None, calendar_id=None):
    """Creates an error event in the user's calendar and deletes all future events."""
    try:
        user = get_user_by_google_id(user_id)
        if not user:
            logger.error(f"create_error_event: User {user_id} not found")
            return False

        # Build service if not provided
        if service is None:
            try:
                if not user.google_token:
                    logger.error(f"create_error_event: No Google token for user {user_id}")
                    return False
                creds = Credentials.from_authorized_user_info(json.loads(user.google_token))
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            except Exception as e:
                logger.error(f"create_error_event: Failed to build calendar service for user {user_id}: {e}")
                return False

        # Ensure we have a calendar ID
        if not calendar_id:
            calendar_id = getattr(user, "google_calendar_id", None)
        if not calendar_id:
            try:
                calendar_name = get_calendar_name(
                    user.google_name if getattr(user, "google_name", None) else user.google_email)
                calendar_id = create_calendar(service, calendar_name, user)
            except Exception as e:
                logger.error(f"create_error_event: Failed to get or create calendar for user {user_id}: {e}")
                return False

        if not calendar_id:
            logger.error(f"create_error_event: No calendar ID available for user {user_id}")
            return False

        # Fetch error message from DB
        error_message = "Unknown synchronization error."
        try:
            status = get_sync_status(user_id)
            if status is not None:
                error_message = status["error"]
        except Exception as e:
            logger.error(f"create_error_event: Failed to fetch sync status for user {user_id}: {e}")

        # Delete all future events
        try:
            tz = pytz.timezone(TIMEZONE)
            time_min = datetime.now(tz).isoformat()
            event_ids = []
            page_token = None
            while True:
                events_result = service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    singleEvents=True,
                    orderBy='startTime',
                    pageToken=page_token
                ).execute()
                items = events_result.get('items', [])
                event_ids.extend([item['id'] for item in items if 'id' in item])
                page_token = events_result.get('nextPageToken')
                if not page_token:
                    break

            if event_ids:
                batch_delete_events(creds, calendar_id, event_ids)
        except Exception as e:
            logger.error(f"create_error_event: Failed deleting future events for user {user_id}: {e}")

        # Create the error event
        try:
            tz = pytz.timezone(TIMEZONE)
            start_dt = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            end_dt = start_dt + timedelta(days=1)
            event_body = {
                "summary": "Sync Error",
                "description": error_message,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
            }
            service.events().insert(calendarId=calendar_id, body=event_body).execute()
            logger.info(f"create_error_event: Created error event for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"create_error_event: Failed to create error event for user {user_id}: {e}")
            return False

    except Exception as e:
        logger.error(f"create_error_event: Unexpected error for user {user_id}: {e}")
        return False


def run_sync_cycle():
    """Runs one complete synchronization cycle for all users."""
    logger.info("Starting sync cycle...")
    start_time = time.time()

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(ROOT_DIR, "db", 'users.sqlite')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        try:
            db.create_all()  # Ensure tables exist
            
            try:
                logger.info("Running global manager shift sync...")
                manager_api = MyMcdAPI(MANAGER_EMAIL, MANAGER_PASSWORD)
                manager_api.login()
                from McdShiftManager import McdShiftManager
                manager = McdShiftManager(manager_api, db_path=os.path.join(ROOT_DIR, "db", "mymcd_shifts.sqlite"))
                
                today = datetime.today()
                first_day_this_month = today.replace(day=1)
                last_day_this_month = (first_day_this_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                first_day_next_month = last_day_this_month + timedelta(days=1)
                last_day_next_month = (first_day_next_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                
                from_str = first_day_this_month.strftime("%Y-%m-%d")
                to_str = last_day_next_month.strftime("%Y-%m-%d")
                
                manager.sync_shifts(from_str, to_str)
            except Exception as e:
                logger.error(f"Global manager sync failed: {e}")

            users = User.query.all()
            logger.info(f"Found {len(users)} users to sync")

            for user in users:
                if user.mcd_email is None or user.mcd_password is None:
                    continue

                if user.google_name == "temp":
                    try:
                        api = MyMcdAPI(user.mcd_email, user.mcd_password)
                        api.login()
                        me = api.get_me()
                        name = me.get("fullname", "Unknown")
                        create_or_update_user(user.google_id, user.google_email, google_name=name)
                        user.google_name = name
                    except Exception as e:
                        logger.error(f"Failed to update temp name for user {user.google_id}: {e}")

                logger.info(f"Syncing data for user {user.google_id}...")

                if sync_user_data(user.google_id):
                    logger.info(f"Syncing data for user {user.google_id}... success")
                else:
                    logger.error(f"Syncing data for user {user.google_id}... failed")
                    create_error_event(user.google_id)
        except Exception as e:
            logger.critical(f"Critical error during sync cycle: {e}")

    logger.info("Sync cycle complete")
    logger.info(f"Total cycle time: {time.time() - start_time:.2f} seconds")


if __name__ == "__main__":
    logger.info("Worker process initializing...")

    # Run once immediately upon startup
    try:
        run_sync_cycle()
    except Exception as e:
        logger.error(f"Initial sync failed: {e}")

    # Enter the infinite loop
    while True:
        logger.info("Sleeping for 2 hours...")
        try:
            time.sleep(7200)  # 2 hours
            run_sync_cycle()
        except KeyboardInterrupt:
            logger.info("Worker stopping...")
            break
        except Exception as e:
            logger.error(f"Crash in main loop: {e}. Retrying in 60 seconds...")
            time.sleep(60)