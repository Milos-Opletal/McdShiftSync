import datetime
import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta

import dateutil.parser
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
from webapp.mcd_manager import get_shifts_from_website, get_name

TIMEZONE = 'Europe/Prague'


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


def are_shifts_equal(calendar_event, new_shift):
    """Compare if a calendar event matches a new shift."""
    try:
        # Get the interval from the new shift
        interval = new_shift['intervals'][0]
        new_start = dateutil.parser.parse(interval['from'])
        new_end = dateutil.parser.parse(interval['to'])

        # Make sure new times are timezone-aware
        timezone = pytz.timezone(TIMEZONE)
        new_start = timezone.localize(new_start) if new_start.tzinfo is None else new_start
        new_end = timezone.localize(new_end) if new_end.tzinfo is None else new_end

        # Get times from calendar event
        cal_start = dateutil.parser.parse(calendar_event['start']['dateTime'])
        cal_end = dateutil.parser.parse(calendar_event['end']['dateTime'])

        # Make sure calendar times are in the same timezone
        cal_start = cal_start.astimezone(timezone)
        cal_end = cal_end.astimezone(timezone)

        if new_start != cal_start or new_end != cal_end:
            return False

        # Compare summary (shift type)
        cal_summary = calendar_event['summary']
        new_summary = create_event_summary(new_shift)
        if new_summary != cal_summary:
            return False

        cal_description = calendar_event['description']
        new_description = create_event_description(new_shift)
        if new_description != cal_description:
            return False

        return True
    except Exception as e:
        logger.error(f"Error comparing shifts: {e}")
        return False


def batch_create_events(service, calendar_id, events_data):
    """Batch create events in the calendar."""
    if not events_data:
        logger.info("No events to create")
        return False

    logger.info(f"Attempting to create {len(events_data)} events in batch")
    batch = service.new_batch_http_request()
    for event_data in events_data:
        # Parse and reformat the dates to RFC 3339 format
        start_time = datetime.strptime(event_data['start']['dateTime'], "%Y-%m-%d %H:%M:%S")
        end_time = datetime.strptime(event_data['end']['dateTime'], "%Y-%m-%d %H:%M:%S")

        # Make dates timezone-aware
        timezone = pytz.timezone(TIMEZONE)
        start_time = timezone.localize(start_time)
        end_time = timezone.localize(end_time)

        # Create new event data with properly formatted dates
        formatted_event_data = {"summary": event_data['summary'], "description": event_data['description'],
                                "start": {"dateTime": start_time.isoformat(), "timeZone": TIMEZONE, },
                                "end": {"dateTime": end_time.isoformat(), "timeZone": TIMEZONE, }, }

        batch.add(service.events().insert(calendarId=calendar_id, body=formatted_event_data))

    try:
        batch.execute()
        logger.info(f"Successfully created {len(events_data)} events in batch")
        return True
    except Exception as e:
        logger.error(f"Error executing batch create: {e}")
        logger.debug(f"First event data: {events_data[0]}")
        return False


def batch_delete_events(service, calendar_id, event_ids):
    if not event_ids:
        return True

    batch = service.new_batch_http_request()
    for event_id in event_ids:
        batch.add(service.events().delete(calendarId=calendar_id, eventId=event_id), request_id=event_id)
    try:
        batch.execute()
        logger.info(f"Successfully deleted {len(event_ids)} events")
        return True
    except errors.HttpError as error:
        logger.error(f'An error occurred during batch delete: {error}')
        return False


def delete_upcoming_events(service, calendar_id, new_shifts):
    """Collects shift-related events from the calendar that should be deleted."""
    logger.info("Starting to process upcoming events for deletion")
    matched_shift_indices = set()
    events_to_delete = []

    # Calculate yesterday dynamically
    start_time_check = get_yesterday()

    try:
        # Get all events from the calendar
        events_result = service.events().list(calendarId=calendar_id, timeMin=start_time_check.isoformat(),
                                              singleEvents=True,
                                              orderBy='startTime').execute()
        upcoming_events = events_result.get('items', [])

        if not upcoming_events:
            logger.info("No upcoming events found in calendar.")
            return matched_shift_indices, events_to_delete

        # Process only shift-related events
        shift_events_skipped = 0

        for event in upcoming_events:
            try:
                # Check if this shift matches any in new_shifts
                should_skip = False
                if new_shifts:
                    for idx, new_shift in enumerate(new_shifts):
                        try:
                            if are_shifts_equal(event, new_shift):
                                should_skip = True
                                matched_shift_indices.add(idx)
                                shift_events_skipped += 1
                                break
                        except Exception as e:
                            logger.error(f"Error comparing shifts: {e}")
                            continue

                if should_skip:
                    continue

                events_to_delete.append(event['id'])
            except Exception as e:
                logger.error(f"Error processing event for deletion: {e}")
                continue

        logger.info(
            f"Found {len(events_to_delete)} events to delete and {shift_events_skipped} matching events to keep")
        return matched_shift_indices, events_to_delete

    except HttpError as error:
        logger.error(f"Error retrieving events: {error}")
        return matched_shift_indices, events_to_delete
    except Exception as e:
        logger.error(f"Error processing calendar: {e}")
        return matched_shift_indices, events_to_delete


def create_event_summary(data):
    event_summary = "Shift"
    if data.get('note'):
        event_summary += "+"
    return event_summary


def create_event_description(data):
    description_parts = []
    if data.get('hasBreak'):
        if data['hasBreak']:
            description_parts.append(f"Has 30 min break")
    if data.get('note') is not None:
        description_parts.append(f"note: {data['note']}")
    description_parts.append(":)")
    return "\n".join(description_parts)


def create_events_from_data(service, calendar_id, data_array, skip_indices=None):
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

            event_summary = create_event_summary(data)
            event_description = create_event_description(data)
            event_data = {"summary": event_summary, "description": event_description,
                          "start": {"dateTime": start_time_str, "timeZone": TIMEZONE, },
                          "end": {"dateTime": end_time_str, "timeZone": TIMEZONE, }, }
            events_to_create.append(event_data)
        except Exception as e:
            logger.error(f"Error processing shift {idx}: {e}")
            continue

    if events_to_create:
        if batch_create_events(service, calendar_id, events_to_create):
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
        service = build("calendar", "v3", credentials=credentials)
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
        data_array = get_shifts_from_website(current_user.mcd_email, current_user.mcd_password)
        if data_array is None:
            set_sync_status(user_id, success=False,
                            error_message="No shifts found for user")
            return False
    except Exception:
        set_sync_status(user_id, success=False,
                        error_message="Failed to get timetable data - check mcd website \n https://mymcd.eu/")
        return False

    matched_shift_indices, events_to_delete = delete_upcoming_events(service, calendar_id, data_array)
    batch_delete_events(service, calendar_id, events_to_delete)

    create_events_from_data(service, calendar_id, data_array, matched_shift_indices)

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
                service = build("calendar", "v3", credentials=creds)
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
                batch_delete_events(service, calendar_id, event_ids)
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

    # Initialize Flask app context here for each cycle to ensure clean DB connections
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(ROOT_DIR, "db", 'users.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        try:
            db.create_all()  # Ensure tables exist
            users = User.query.all()
            logger.info(f"Found {len(users)} users to sync")

            for user in users:
                if user.mcd_email is None or user.mcd_password is None:
                    continue

                if user.google_name == "temp":
                    try:
                        name = get_name(user.mcd_email, user.mcd_password)
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