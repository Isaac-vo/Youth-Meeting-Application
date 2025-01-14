import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, redirect, url_for, render_template, jsonify
from googleapiclient.discovery import build
from google.oauth2 import service_account
import pandas as pd
import sqlite3
import atexit

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Set up logging
logging.basicConfig(level=logging.INFO)

# Path to your service account key file (service_account.json as example)
SERVICE_ACCOUNT_FILE = '/path/to/your/service/account/service_account.json'

# Print the current working directory to verify the path
print(f"Current working directory: {os.getcwd()}")

# Check if the file exists
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    raise FileNotFoundError(f"Service account file not found: {SERVICE_ACCOUNT_FILE}")

SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Create the credentials object
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)

# The ID of your Google Sheet
SPREADSHEET_ID = {
    'Spreadsheet1': {
        'id': 'Spreadsheet1 id',
        'db_file': 'database1.db'
    },
    'Spreadsheet2': {
        'id': 'Spreadsheet2 id',
        'db_file': 'database2.db'
    }
}

app = Flask(__name__)


def get_sheet_data(spreadsheet_id):
    try:
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=spreadsheet_id,
                                    range='Responses!A1:Z1000').execute()
        values = result.get('values', [])
        if not values:
            logging.warning(f"No data found for {spreadsheet_id}")
            return []

        # Ensure all rows have the correct number of columns
        num_columns = len(values[0])
        for row in values:
            if len(row) < num_columns:
                row.extend([''] * (num_columns - len(row)))

        logging.info(f"Google Sheets Data for {spreadsheet_id}:", values)
        return values
    except Exception as e:
        logging.error(f"Error fetching data from Google Sheets {spreadsheet_id}:", e)
        return []


def update_database(data, db_file):
    try:
        if not data:
            logging.info("No data to update")
            return
        columns = data[0]
        df = pd.DataFrame(data[1:], columns=columns)
        logging.info(f"Columns from Google Sheet: {columns}")
        logging.info(f"DataFrame for {db_file}:\n{df}")

        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()

        # Create table with dynamic column names
        columns_sql = ', '.join([f'"{col}" TEXT' for col in columns])
        cursor.execute(f'CREATE TABLE IF NOT EXISTS form_responses ({columns_sql});')

        # Insert DataFrame into SQLite
        df.to_sql('form_responses', conn, if_exists='replace', index=False)
        logging.info(f"Database {db_file} updated successfully")

        # Confirm table and data insertion
        cursor.execute("SELECT * FROM form_responses LIMIT 5;")
        rows = cursor.fetchall()
        logging.info(f"Data in {db_file}:\n{rows}")

        conn.close()
    except sqlite3.DatabaseError as db_err:
        logging.error(f"SQLite Error while updating the database {db_file}: {db_err}")
    except Exception as e:
        logging.error(f"Error updating the database {db_file}: {e}")


def match_attendees_to_hosters():
    try:
        logging.info("Connecting to database.")
        conn1 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet1']['db_file'])
        attendees_df = pd.read_sql_query("SELECT * FROM form_responses", conn1)
        conn2 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet2']['db_file'])
        hosters_df = pd.read_sql_query("SELECT * FROM form_responses", conn2)

        logging.info(f"Attendees DataFrame loaded: {len(attendees_df)} records.")
        logging.info(f"Hosters DataFrame loaded: {len(hosters_df)} records.")

        # Map Gender Values
        gender_map = {'Boy': 'Boy', 'Girl': 'Girl'}
        attendees_df['Gender'] = attendees_df['Gender'].map(gender_map)
        hosters_df['Preferred Gender'] = hosters_df['Whom can you host?'].map(gender_map)

        # Print unique values for Gender
        logging.info(f"Unique Gender Values in Attendees: {attendees_df['Gender'].unique()}")
        logging.info(f"Unique Gender Values in Hosters: {hosters_df['Preferred Gender'].unique()}")

        # Filter attendees based on "Where will you be staying?" and exclude "Maryland" origin
        brethren_attendees = attendees_df[
            (attendees_df['Where will you be staying?'] == 'Brethren household') &
            (attendees_df['Church of origin'] != 'Maryland')
        ]
        logging.info(f"Filtered Attendees DataFrame: {len(brethren_attendees)} records.")

        # Filter hosters based on "Would you like to host or donate a hotel?"
        hosters_df = hosters_df[hosters_df['Would you like to host or donate a hotel?']
                                == 'I can host (posso receber jovens)']
        logging.info(f"Filtered Hosters DataFrame: {len(hosters_df)} records.")

        matches = []
        hotel_overflow = []

        # Ensure attendees are grouped by Church of origin
        brethren_attendees = brethren_attendees.sort_values(by=['Church of origin'])

        # Ensure the column name matches exactly
        preferred_capacity_column = 'Quantos jovens você pode receber?'
        preferred_gender_column = 'Preferred Gender'
        address_column = 'Address (Endereço)'
        contact_column = 'Cell phone #'

        for index, hoster in hosters_df.iterrows():
            logging.info(f"Processing hoster {index}: {hoster['First name']}")
            try:
                spots_left = int(hoster[preferred_capacity_column])
                preferred_gender = hoster[preferred_gender_column]
                address = hoster[address_column]
                contact = hoster[contact_column]

                initial_spots = spots_left

                # Initialize church of origin to None
                church_of_origin = None

                while spots_left > 0 and not brethren_attendees.empty:
                    logging.info(
                        f"Trying to match attendees for {hoster['First name']}, spots left: {spots_left}")
                    # Try to match attendees from the same church first
                    same_church_attendees = brethren_attendees[
                        (brethren_attendees['Gender'] == preferred_gender) &
                        (brethren_attendees['Church of origin'] == church_of_origin)
                    ]

                    if not same_church_attendees.empty:
                        attendee = same_church_attendees.iloc[0]
                        church_of_origin = attendee['Church of origin']
                    else:
                        other_church_attendees = brethren_attendees[
                            (brethren_attendees['Gender'] == preferred_gender) &
                            (brethren_attendees['Church of origin'] != church_of_origin)
                        ]

                        if other_church_attendees.empty:
                            # No more attendees to match for this hoster
                            logging.info(
                                f"No other church attendees found for {hoster['First name']}.")
                            break

                        attendee = other_church_attendees.iloc[0]
                        church_of_origin = None

                    matches.append({
                        'Hoster': hoster['First name'],
                        'Attendee': f"{attendee['First name']} {attendee['Last name']}",
                        'Gender': attendee['Gender'],
                        'Church of origin': attendee['Church of origin'],
                        'Address': address,
                        'Contact': contact,
                        'Initial spots': initial_spots,
                        'Remaining spots': spots_left - 1
                    })
                    spots_left -= 1

                    brethren_attendees = brethren_attendees.drop(attendee.name)
                    logging.info(
                        f"Matched {attendee['First name']} {attendee['Last name']} with {hoster['First name']}")

            except Exception as e:
                logging.error(f"Error processing hoster {hoster['First name']}: {e}")

        # Any remaining attendees without spots should be moved to hotel_overflow
        for index, attendee in brethren_attendees.iterrows():
            hotel_overflow.append({
                'First name': attendee['First name'],
                'Last name': attendee['Last name'],
                'Age': attendee['Age'],
                'Cellphone': attendee['Cell phone #'],
                'Additional Notes': attendee['Please to inform additional notes, if needed:'],
                'Gender': attendee['Gender'],
                'Church of origin': attendee['Church of origin'],
                'Preferred stay': 'Hotel'
            })
            logging.info(
                f"Moved {attendee['First name']} {attendee['Last name']} to Hotel overflow")

        logging.info(f"Total matches found: {len(matches)}")
        logging.info(f"Total overflow to Hotels: {len(hotel_overflow)}")
        conn1.close()
        conn2.close()

        return matches, hotel_overflow
    except Exception as e:
        logging.error(f"Error matching attendees to hosters: {e}")
        return [], []

# Helper function to normalize attendee data


def normalize_attendee_data(attendees):
    normalized_attendees = []
    for attendee in attendees:
        normalized_attendees.append({
            'First name': attendee['First name'],
            'Last name': attendee['Last name'],
            'Age': attendee.get('Age', None),
            'Cellphone': attendee.get('Cell phone #', attendee.get('Cellphone', None)),
            'Additional Notes': attendee.get('Please to inform additional notes, if needed:', attendee.get('Additional Notes', None)),
            'Gender': attendee['Gender'],
            'Church of origin': attendee['Church of origin'],
            'Preferred stay': attendee.get('Preferred stay', 'Hotel')
        })
    return normalized_attendees

# Make sure get_hotel_attendees returns hotel_overflow data correctly


def get_hotel_attendees():
    try:
        conn1 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet1']['db_file'])
        attendees_df = pd.read_sql_query("SELECT * FROM form_responses", conn1)
        conn1.close()

        # Filter attendees based on "Where will you be staying"
        hotel_attendees = attendees_df[
            (attendees_df['Where will you be staying?'] == 'Hotel') &
            (attendees_df['Church of origin'] != 'Maryland')
        ]
        logging.info(f"Filtered Hotel Attendees DataFrame: {len(hotel_attendees)} records.")

        # Convert DataFrame to a list of fictionaries
        hotel_attendees = hotel_attendees.to_dict('records')

        # Normalize the data
        hotel_attendees = normalize_attendee_data(hotel_attendees)

        # Get overflow attendees from the match_attendees_to_hosters function
        _, hotel_overflow = match_attendees_to_hosters()
        logging.info(
            f"Total overflow to Hotels from match_attendees_to_hosters: {len(hotel_overflow)} records.")

        # Normalize the hotel_overflow
        hotel_overflow = normalize_attendee_data(hotel_overflow)

        # Combine regular hotel attendees with overflow attendees
        combined_hotel_attendees = hotel_attendees + hotel_overflow
        logging.info(f"Combined Hotel Attendees List: {len(combined_hotel_attendees)} records.")

        return combined_hotel_attendees
    except Exception as e:
        logging.error(f"Error retrieving hotel attendees: {e}")
        return []

# Define the hotel donors


def get_hotel_donors():
    try:
        conn2 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet2']['db_file'])
        hosters_df = pd.read_sql_query("SELECT * FROM form_responses", conn2)
        conn2.close()

        # Filter hosters based on "Would you like to host or donate a hotel?"
        hotel_donors = hosters_df[
            hosters_df[
                "Would you like to host or donate a hotel?"] == 'I would rather donate a hotel (prefiro doar um quarto de hotel)'
        ]
        logging.info(f"Filtered Hotel Donors DataFrame: {len(hotel_donors)} records.")

        return hotel_donors.to_dict('records')
    except Exception as e:
        logging.error(f"Error retrieving hotel donors: {e}")
        return []

# Define Attendees who come by plane


def get_drive_attendees():
    try:
        conn1 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet1']['db_file'])
        attendees_df = pd.read_sql_query("SELECT * FROM form_responses", conn1)
        conn1.close()

        # Filter attendees based on "Airport of arrival"
        drive_attendees = attendees_df[
            attendees_df["Airport of arrival (if arriving by car, please select Car)"].isin([
                'Plane (BWI)', 'Plane (DCA)'])
        ]
        logging.info(f"Filtered Drive Attendees DataFrame: {len(drive_attendees)} records.")

        return drive_attendees.to_dict('records')
    except Exception as e:
        logging.error(f"Error retrieving drive attendees: {e}")
        return []


# Define attendees' count to the event
def get_attendee_count():
    try:
        conn1 = sqlite3.connect(SPREADSHEET_ID['Spreadsheet1']['db_file'])
        attendees_df = pd.read_sql_query("SELECT * FROM form_responses", conn1)
        conn1.close()

        # Coun attendees excluding those from Maryland
        non_maryland_count = len(attendees_df[attendees_df['Church of origin'] != 'Maryland'])

        # Count attendees from Maryland
        maryland_count = len(attendees_df[attendees_df['Church of origin'] == 'Maryland'])

        # Total count
        total_count = non_maryland_count + maryland_count

        return non_maryland_count, maryland_count, total_count
    except Exception as e:
        logging.error(f"Error calculating attendee counts: {e}")
        return 0, 0, 0


# Define the periodic update function
def periodic_update():
    for key, value in SPREADSHEET_ID.items():
        data = get_sheet_data(value['id'])
        update_database(data, value['db_file'])


# Set up the scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(periodic_update, 'interval', minutes=0.2)
scheduler.start()

# Ensure the scheduler is stopped when the program exits
atexit.register(lambda: scheduler.shutdown(wait=False))


@app.route('/')
def index():
    try:
        non_maryland_count, maryland_count, total_count = get_attendee_count()
        return render_template('index.html', non_maryland_count=non_maryland_count, maryland_count=maryland_count, total_count=total_count)
    except Exception as e:
        logging.error(f"Error rendering index page: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/data')
def get_data():
    return redirect(url_for('index'))


@app.route('/hotels')
def hotels():
    try:
        hotel_attendees = get_hotel_attendees()
        return render_template('hotel.html', attendees=hotel_attendees)
    except Exception as e:
        logging.error(f"Error rendering hotels page: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/drive')
def drive():
    try:
        drive_attendees = get_drive_attendees()
        return render_template('pickup.html', attendees=drive_attendees)
    except Exception as e:
        logging.error(f"Error rendering drive page: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/donations')
def donations():
    try:
        hotel_donors = get_hotel_donors()
        return render_template('donations.html', donors=hotel_donors)
    except Exception as e:
        logging.error(f"Error rendering donations page: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/accommodations')
def accommodations():
    try:
        logging.info("Starting match_attendees_to_hosters function")
        matches, hotel_overflow = match_attendees_to_hosters()
        logging.info(f"Matches to be rendered: {matches}")
        logging.info(f"Hotel overflow to be rendered: {hotel_overflow}")

        if not matches:
            logging.warning("NO matches found.")

        return render_template('accommodations.html', matches=matches)
    except Exception as e:
        logging.error(f"Error renderinf accommodations: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


if __name__ == '__main__':
    app.run(debug=True)
