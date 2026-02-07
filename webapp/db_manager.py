import os
from flask_sqlalchemy import SQLAlchemy
from flask import Flask
from datetime import datetime
import json

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(128), unique=True, nullable=False)
    google_email = db.Column(db.String(256), nullable=False)
    google_name = db.Column(db.String(256))  # Add field for user's name
    google_calendar_id = db.Column(db.String(255), nullable=True)
    mcd_email = db.Column(db.String(256), unique=True)
    mcd_password = db.Column(db.String(256))
    sync_status = db.Column(db.String(32))
    google_token = db.Column(db.Text)  # Store serialized Google credentials

# --- DB management and user utility functions ---
def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()

# User CRUD and sync helpers
def get_user_by_google_id(google_id):
    return User.query.filter_by(google_id=google_id).first()

def create_or_update_user(google_id, google_email=None, google_name=None, mcd_email=None, mcd_password=None, google_token=None):
    user = get_user_by_google_id(google_id)
    if user:
        if google_email:
            user.google_email = google_email
        if google_name:
            user.google_name = google_name
        if mcd_email:
            user.mcd_email = mcd_email
        if mcd_password:
            user.mcd_password = mcd_password
        if google_token:
            user.google_token = google_token
    else:
        user = User(
            google_id=google_id,
            google_email=google_email,
            google_name=google_name,
            mcd_email=mcd_email,
            mcd_password=mcd_password,
            google_token=google_token
        )
        db.session.add(user)
    db.session.commit()
    return user

def delete_user(google_id):
    user = get_user_by_google_id(google_id)
    if user:
        db.session.delete(user)
        db.session.commit()

def update_last_sync(google_id):
    user = get_user_by_google_id(google_id)
    if user:
        user.last_sync = datetime.now().isoformat()
        db.session.commit()

def set_sync_status(google_id, success, error_message=None):
    user = get_user_by_google_id(google_id)
    if user:
        user.sync_status = json.dumps({
            'timestamp': datetime.now().isoformat(),
            'success': success,
            'error': error_message
        })
        db.session.commit()

def get_sync_status(google_id):
    user = get_user_by_google_id(google_id)
    if user and user.sync_status:
        try:
            status = json.loads(user.sync_status)
            # Ensure required keys exist
            if 'timestamp' not in status:
                status['timestamp'] = None
            if 'success' not in status:
                status['success'] = None
            if 'error' not in status:
                status['error'] = None
            return status
        except Exception:
            return {'timestamp': None, 'success': None, 'error': None}
    return {'timestamp': None, 'success': None, 'error': None}

def set_sync_error(google_id, error_message):
    user = get_user_by_google_id(google_id)
    if user:
        user.sync_error = error_message
        db.session.commit()

def clear_sync_error(google_id):
    user = get_user_by_google_id(google_id)
    if user:
        user.sync_error = None
        db.session.commit()

def get_calendar_id(google_id):
    user = get_user_by_google_id(google_id)
    if user:
        return user.google_calendar_id
    return None

def set_calendar_id(google_id, calendar_id):
    user = get_user_by_google_id(google_id)
    if user:
        user.google_calendar_id = calendar_id
        db.session.commit()
        return True
    return False 