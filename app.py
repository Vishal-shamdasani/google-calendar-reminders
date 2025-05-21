from flask import Flask, redirect, url_for, session, request, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials  # Add this import
from google.auth.transport.requests import Request
import uuid
import pytz 
import os
import json
import datetime
from dotenv import load_dotenv
import logging

# Configure logging

logging.basicConfig(
    level=logging.INFO,  # Set minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Console output
        # logging.FileHandler('app.log'),  # Uncomment to log to a file
    ]
)

logger = logging.getLogger(__name__)


app = Flask(__name__)


if os.path.exists(".env"):
    load_dotenv()
event_cache = []  # Temporary global list of events
# Setup OAuth flow
app.secret_key = os.getenv("FLASK_SECRET_KEY")
url = os.getenv("PUBLIC_URL")

credentials_data = json.loads(os.environ.get("CREDENTIALS_JSON"))
with open("credentials.json", "w") as f:
    json.dump(credentials_data, f)

flow = Flow.from_client_secrets_file(
    'credentials.json',
    scopes=['https://www.googleapis.com/auth/calendar.readonly'],
    redirect_uri=f'{url}/oauth2callback'
)

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
@app.route('/')
def index():
    global event_cache
    if not event_cache:
        if 'credentials' not in session:
            return redirect('/authorize')

        creds_dict = session['credentials']
        creds = Credentials(**creds_dict)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
    # Update session with refreshed credentials
            session['credentials'] = creds_to_dict(creds)
        service = build('calendar', 'v3', credentials=creds)

        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(
            calendarId='primary',
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        # Convert and format times
        event_cache = ist_time(events_result)

    return render_template('index.html', events=event_cache)

def ist_time(events_result):
    events = []
    ist = pytz.timezone('Asia/Kolkata')
    for event in events_result.get('items', []):
        start = event['start']
        end = event['end']

        start_time_str = start.get('dateTime')
        end_time_str = end.get('dateTime')
        start_date_str = start.get('date')
        end_date_str = end.get('date')

        if start_time_str and end_time_str:
            # Timed event
            start_time_utc = datetime.datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
            end_time_utc = datetime.datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))

            start_time_ist = start_time_utc.astimezone(ist)
            end_time_ist = end_time_utc.astimezone(ist)

            event['start_time_pretty'] = start_time_ist.strftime('%A, %d %B %Y — %I:%M %p')
            event['end_time_pretty'] = end_time_ist.strftime('%I:%M %p')
        elif start_date_str and end_date_str:
            # All-day event
            start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            
            event['start_time_pretty'] = start_date.strftime('%A, %d %B %Y (All Day)')
            event['end_time_pretty'] = end_date.strftime('%A, %d %B %Y')
        else:
            # Fallback
            event['start_time_pretty'] = 'Unknown'
            event['end_time_pretty'] = 'Unknown'

        events.append(event)
    return events


@app.route('/authorize')
def authorize():
    auth_url, _ = flow.authorization_url(prompt='consent')
    return redirect(auth_url)


@app.route('/oauth2callback')
def oauth2callback():
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    # Save creds to file
    with open('token.json', 'w') as token:
        token.write(creds.to_json())

    session['credentials'] = creds_to_dict(creds)
    service = build('calendar', 'v3', credentials=creds)
    start_watch(service)

    return redirect(url_for('index'))


def creds_to_dict(creds):
    return {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }

@app.route('/notifications', methods=['POST'])
def notifications():
    logger.info("🔔 Calendar changed!")

    try:
        with open('token.json', 'r') as token:
            creds_data = json.load(token)
            creds = Credentials.from_authorized_user_info(creds_data)
    except Exception as e:
        logger.error(f"⚠️ Failed to load credentials: {e}")
        return '', 401

    service = build('calendar', 'v3', credentials=creds)

    now = datetime.datetime.utcnow().isoformat() + 'Z'
    events_result = service.events().list(
        calendarId='primary',
        timeMin=now,
        maxResults=10,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    global event_cache
    event_cache = ist_time(events_result)
    logger.info("✅ Event cache updated")

    return '', 200



def start_watch(service):
    channel_id = str(uuid.uuid4())
    request_body = {
        'id': channel_id,
        'type': 'web_hook',
        'address': f'{url}/notifications',  # public HTTPS URL
    }

    response = service.events().watch(calendarId='primary', body=request_body).execute()
    logger.info("✅ Watch response:", response)


def check_for_upcoming_events():
    try:
        with open('token.json', 'r') as token:
            creds_data = json.load(token)
            creds = Credentials.from_authorized_user_info(creds_data)
    except Exception as e:
        logger.error(f"Failed to load credentials in scheduler: {e}")
        return
    
    service = build('calendar', 'v3', credentials=creds)

    now = datetime.datetime.utcnow()
    ten_min_later = now + datetime.timedelta(minutes=10)
    
    # ISO format with 'Z' for UTC
    now_iso = now.isoformat() + 'Z'
    ten_min_later_iso = ten_min_later.isoformat() + 'Z'

    events_result = service.events().list(
        calendarId='primary',
        timeMin=now_iso,
        timeMax=ten_min_later_iso,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    if events:
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No title')
            logger.info(f"🔔🔔🔔Upcoming event in 10 minutes: {summary} at {start}")

            # TODO: Send your notification here
            # For example, send_email(summary, start)
    else:
        logger.info("No events in the next 10 minutes.")


if __name__ == '__main__':
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_for_upcoming_events, 'interval', minutes=1)  # Check every minute
    scheduler.start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

