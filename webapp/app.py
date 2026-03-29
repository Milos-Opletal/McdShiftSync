import sys
from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, jsonify
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import os
import secrets
from functools import wraps
from datetime import datetime, timedelta
import re
import json
import requests

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from webapp.db_manager import db, init_db, User, get_user_by_google_id, create_or_update_user, delete_user, update_last_sync, get_sync_status
from sync import sync_user_data, get_calendar_name, create_calendar
from MyMcdAPI import MyMcdAPI
from webapp.translations import translations

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # Required for session

# SQLite configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(ROOT_DIR,"db", 'users.sqlite')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Cache control configuration
CACHE_CONTROL_STATIC = 'public, max-age=86400'  # 1 day in seconds
CACHE_CONTROL_DYNAMIC = 'no-cache, no-store, must-revalidate'

@app.after_request
def add_cache_headers(response):
    """Add appropriate cache headers based on the request path."""
    if request.path.startswith('/static/'):
        # Static content (CSS, JS, images, etc.)
        response.headers['Cache-Control'] = CACHE_CONTROL_STATIC
        response.headers['Expires'] = (datetime.now() + timedelta(days=1)).strftime('%a, %d %b %Y %H:%M:%S GMT')
    else:
        # Dynamic content (HTML pages, API endpoints)
        response.headers['Cache-Control'] = CACHE_CONTROL_DYNAMIC
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

init_db(app)


# OAuth 2.0 configuration
SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "openid"
]
CLIENT_SECRETS_FILE = os.path.join(ROOT_DIR, "credentials.json")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    """Home page with login button or redirect to dashboard if logged in."""
    if 'user_id' in session:
        return redirect(url_for('dashboard', lang=get_user_language()))
    return render_template('index.html')

@app.route('/login')
def login():
    """Initiate Google OAuth flow."""
    # Get the current language from query parameter or default to 'en'
    current_lang = request.args.get('lang', 'en')
    
    # Clear any existing session
    session.clear()
    
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True, _scheme='https')
    )

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    session['state'] = state
    # Store the language in the state parameter to preserve it through the OAuth flow
    session['language'] = current_lang
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    """Handle Google OAuth callback and check for existing McDonald's account."""
    # Get the language from session
    current_lang = session.get('language', 'en')
    state = session['state']

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True, _scheme='https')
    )
    
    
    
    # Get authorization code from callback
    authorization_response = request.url.replace('http://', 'https://')
    flow.fetch_token(authorization_response=authorization_response)

    credentials = flow.credentials
    
    
    # Get user info from Google
    service = build('oauth2', 'v2', credentials=credentials)
    user_info = service.userinfo().get().execute()

    # Clear and reset session
    session.clear()
    session['user_id'] = user_info['id']
    session['email'] = user_info['email']
    # Store credentials and user info in the database
    token_json = credentials.to_json()
    create_or_update_user(
        google_id=user_info['id'],
        google_email=user_info['email'],
        google_name="temp",
        google_token=token_json
    )
    # If the user has not linked their McDonald's account, redirect to /link_mcd
    if not get_user_by_google_id(user_info['id']).mcd_email or not get_user_by_google_id(user_info['id']).mcd_password:
        return redirect(url_for('link_mcd', lang=current_lang))

    return redirect(url_for('dashboard', lang=current_lang))

@app.route('/sync_calendar', methods=['GET', 'POST'])
@login_required
def sync_calendar():
    """Handle calendar sync."""
    user_id = session['user_id']
    
    try:
        if sync_user_data(user_id):
            update_last_sync(user_id)
            return jsonify({
                'success': True,
                'message': 'Calendar synced successfully!',
                'timestamp': datetime.now().isoformat()
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to sync calendar. Please check your account credentials.',
                'timestamp': datetime.now().isoformat()
            }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error syncing calendar: {str(e)}',
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/dashboard')
@login_required
def dashboard():
    """Show dashboard with user info and sync status."""
    user_id = session['user_id']
    user = get_user_by_google_id(user_id)
    # Get user's Google email from session or database
    google_email = session.get('email')
    token_error = None
    if not google_email and user:
        google_email = user.google_email if hasattr(user, 'google_email') else None
    if not google_email:
        token_error = "You need to log in with Google."

    # Get McDonald's account info
    mcd_email = user.mcd_email if user else None
    
    # Get sync status
    sync_status = get_sync_status(user_id)

    return render_template('dashboard.html',
                         google_email=google_email,
                         mcd_email=mcd_email,
                         sync_status=sync_status,
                         token_error=token_error)

@app.route('/link_mcd', methods=['GET', 'POST'])
@login_required
def link_mcd():
    if request.method == 'POST':
        mcd_email = request.form['email']
        mcd_password = request.form['password']

        # Prevent linking the same mymcd email to multiple Google accounts
        existing_user = User.query.filter_by(mcd_email=mcd_email).first()
        if existing_user and existing_user.google_id != session['user_id']:
            return jsonify({
                'success': False,
                'message': 'This McDonald\'s email is already linked to another Google account.'
            }), 400

        # Validate McDonald's credentials before saving
        try:
            api = MyMcdAPI(mcd_email, mcd_password)
            api.login()
            mcd_id = api.user_id
            create_or_update_user(google_id=session['user_id'], google_email=session['email'], mcd_email=mcd_email, mcd_password=mcd_password, mcd_id=mcd_id)
            return jsonify({
                'success': True,
                'message': 'McDonald\'s account linked successfully!'
            })
        except Exception:
            return jsonify({
                'success': False,
                'message': 'Invalid McDonald\'s credentials. Please check your email and password.'
            }), 400

    return render_template('link_mcd.html',
                          google_email=session['email'])

@app.route('/delete_data', methods=['POST'])
@login_required
def delete_data():
    """Delete user's configuration files and revoke OAuth token."""
    user_id = session['user_id']
    user = get_user_by_google_id(user_id)
    current_lang = get_user_language()  # Get language before clearing session
    
    if user and user.google_token:
        try:
            # Build credentials from stored token
            credentials = Credentials.from_authorized_user_info(json.loads(user.google_token))
            
            # Build the service
            service = build('oauth2', 'v2', credentials=credentials)
            
            # Revoke the token
            requests.post('https://oauth2.googleapis.com/revoke',
                        params={'token': credentials.token},
                        headers={'content-type': 'application/x-www-form-urlencoded'})
        except Exception as e:
            # Log the error but continue with deletion
            print(f"Error revoking token: {str(e)}")
    
    # Delete user data
    delete_user(user_id)
    flash('Your data has been deleted successfully.', 'success')
    session.clear()
    return redirect(url_for('index', lang=current_lang))

@app.route('/logout')
def logout():
    """Clear user session and redirect to home."""
    current_lang = get_user_language()  # Get language before clearing session
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('index', lang=current_lang))

@app.template_filter('datetime')
def format_datetime(value):
    """Format a datetime string."""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime('%H:%M, %b %d')
    except:
        return value

@app.route('/api/verify_mcd_account')
@login_required
def api_verify_mcd_account():
    user_id = session['user_id']
    user = get_user_by_google_id(user_id)
    if not user:
        return jsonify({'valid': False, 'error': 'No account config found.'})
    mcd_email = user.mcd_email
    mcd_password = user.mcd_password
    try:
        api = MyMcdAPI(mcd_email, mcd_password)
        api.login()
        valid = True
    except Exception:
        valid = False
    return jsonify({'valid': valid})



@app.context_processor
def inject_year():
    return {'year': datetime.now().year}

def get_user_language():
    """Get the user's preferred language.
    
    Priority:
    1. Query string parameter
    2. Browser's Accept-Language header
    3. Default to English
    """
    # Check query string
    if request.args.get('lang'):
        lang = request.args.get('lang')
        if lang in ['en', 'cs', 'uk']:
            return lang
    
    # Check browser's Accept-Language header
    if request.accept_languages:
        # Get the best match from our supported languages
        supported_languages = {'en', 'cs', 'uk'}
        for lang in request.accept_languages.values():
            if lang[:2] in supported_languages:
                return lang[:2]
    
    # Default to English
    return 'en'

@app.context_processor
def inject_translations():
    lang = get_user_language()
    return {'t': translations[lang], 'current_lang': lang}

@app.route('/set_language/<lang>')
def set_language(lang):
    if lang not in ['en', 'cs', 'uk']:
        lang = 'en'
    
    next_page = request.args.get('next') or request.referrer or url_for('index')
    # Add or replace the lang parameter in the URL
    if '?' in next_page:
        if 'lang=' in next_page:
            # Replace existing lang parameter
            next_page = re.sub(r'lang=[^&]+', f'lang={lang}', next_page)
        else:
            # Add lang parameter to existing query string
            next_page = f"{next_page}&lang={lang}"
    else:
        # Add new query string
        next_page = f"{next_page}?lang={lang}"
    
    return redirect(next_page)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4330, debug=False)
