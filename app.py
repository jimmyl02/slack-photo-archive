import os
import sys
import json
import uuid
import requests
import urllib.parse

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

bot_token = os.environ.get("SLACK_BOT_TOKEN")
app_token = os.environ.get("SLACK_APP_TOKEN")

credentials_data = None
with open("credentials.json", "r") as f:
    credentials_data = json.loads(f.read())
    credentials_data = credentials_data["web"]

google_photos_auth_token = None
google_photos_refresh_token = None

google_photos_album_name = "[SP23] Slack Archives"
google_photos_album_id = None

# initialize app
app = App(
    token=bot_token,
    # signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

'''
google photos api
'''
def google_photos_api_oauth():
    # print the url to visit in order to authorize google photos api
    scopes = ["https://www.googleapis.com/auth/photoslibrary.sharing", "https://www.googleapis.com/auth/photoslibrary.appendonly", "https://www.googleapis.com/auth/photoslibrary.readonly"]

    oauth_query_params = "response_type=code&"
    oauth_query_params += "client_id=" + credentials_data["client_id"] + "&"
    oauth_query_params += "redirect_uri=" + urllib.parse.quote_plus("http://localhost:8080/callback") + "&"
    oauth_query_params += "scope=" + urllib.parse.quote_plus(" ".join(scopes)) + "&"
    oauth_query_params += "access_type=offline"

    oauth_url = credentials_data["auth_uri"] + "?" + oauth_query_params
    print(oauth_url)

def google_photos_api_oauth_token(code):
    global google_photos_auth_token, google_photos_refresh_token

    # convert user code into usable google photos api token
    token_request = {
        "grant_type": "authorization_code",
        "client_id": credentials_data["client_id"],
        "client_secret": credentials_data["client_secret"],
        "redirect_uri": "http://localhost:8080/callback",
        "code": code
    }

    r = requests.post(credentials_data["token_uri"], json=token_request)
    if r.status_code == 200:
        # successfully received auth token
        parsed_token_data = r.json()

        google_photos_auth_token = parsed_token_data["access_token"]
        
        if "refresh_token" in parsed_token_data:
            google_photos_refresh_token = parsed_token_data["refresh_token"]
        else:
            print("[!] WARNING: no refresh token, try removing the slack-photo-archive app from authorized google apps")

        print("[*] authentication successful")

        print("[*] saving credentials...")
        with open("savedcreds.json", "w") as creds:
            json.dump({
                "auth_token": google_photos_auth_token,
                "refresh_token": google_photos_refresh_token
            }, creds)
        print("[*] successfully saved credentials")
    else:
        print("[!] FATAL: something went wrong with getting authentication token")
        sys.exit("authentication error")

def google_photos_api_refresh_token():
    global google_photos_auth_token, google_photos_refresh_token

    # convert user code into usable google photos api token
    token_request = {
        "grant_type": "refresh_token",
        "client_id": credentials_data["client_id"],
        "client_secret": credentials_data["client_secret"],
        "refresh_token": google_photos_refresh_token
    }

    r = requests.post(credentials_data["token_uri"], json=token_request)
    if r.status_code == 200:
        # successfully received auth token
        parsed_token_data = r.json()

        google_photos_auth_token = parsed_token_data["access_token"]
        print("[*] token refresh successful")
    else:
        print("[!] FATAL: something went wrong with getting authentication token")
        print(r.content)
        sys.exit("authentication error")

def ensure_album_created():
    global google_photos_auth_token, google_photos_album_name, google_photos_album_id

    # get albums
    headers = {
        "Authorization": "Bearer " + google_photos_auth_token
    }
    r = requests.get("https://photoslibrary.googleapis.com/v1/albums", headers=headers)
    if r.status_code == 200:
        # received shared albums
        parsed_albums = r.json()
        albums = parsed_albums["albums"]

        found = False
        for album in albums:
            if "title" in album and "id" in album:
                if album["title"] == google_photos_album_name:
                    found = True
                    google_photos_album_id = album["id"]
                    break

        if not found:
            # album was not found, we must create it
            body = {
                "album": {
                    "title": google_photos_album_name
                }
            }
            r = requests.post("https://photoslibrary.googleapis.com/v1/albums", headers=headers, json=body)
            if r.status_code == 200:
                parsed_album = r.json()
                google_photos_album_id = parsed_album["id"]
            else:
                print("[!] FATAL: failed to create album")
                sys.exit("album creation error")

    else:
        print(r.content)
        print("[!] WARNING: something went wrong while retriving albums")

def upload_photo_to_album(photo_data, message, depth=0):
    global google_photos_refresh_token

    # set recursive limit
    if depth > 1:
        print("[!] FATAL: upload photo retry depth exceeded limit")
        return

    # upload photo bytes to google photos
    photo_token = ""

    headers = {
        "Content-type": "application/octet-stream",
        "Authorization": "Bearer " + google_photos_auth_token,
        "X-Goog-Upload-Protocol": "raw",
        # TODO: investigate X-Goog-Upload-Content-Type header
    }
    r = requests.post("https://photoslibrary.googleapis.com/v1/uploads", headers=headers, data=photo_data)
    if r.status_code == 200:
        photo_token = r.text
    else:
        # TODO: if this fails, use refresh token and try again
        print("[!] WARNING: something went wrong with uploading the photo, retrying with token refresh")
        google_photos_api_refresh_token()
        upload_photo_to_album(photo_data, message, depth=depth+1)

    # create media item
    headers = {
        "Content-type": "application/json",
        "Authorization": "Bearer " + google_photos_auth_token,
    }

    body = {
        "albumId": google_photos_album_id,
        "newMediaItems": [{
            "description": "funtimes: " + message,
            "simpleMediaItem": {
                "fileName": str(uuid.uuid4()),
                "uploadToken": photo_token
            }
        }]
    }
    r = requests.post("https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate", headers=headers, json=body)
    
    # TODO: debug
    print(r.content)

    if r.status_code != 200:
        print("[!] WARNING: failed add media to album")

'''
slack bot handlers
'''
@app.event("message")
def handle_message_events(event, say):
    print(event)
    if "files" in event:
        # this message event uploaded a file, we mark this and wish to save it
        for file in event["files"]:
            file_download_url = file["url_private_download"]
            
            headers = {"Authorization": f"Bearer {bot_token}"}
            r = requests.get(file_download_url, headers=headers)
            
            if r.status_code == 200:
                upload_photo_to_album(r.content, event["text"])
            else:
                print('[!] failed to get image from slack', r.content)

        say(text="saved files to google photos album :camera:", thread_ts=event["event_ts"])

# start the app
if __name__ == "__main__":
    # check if token is already saved
    saved_creds = None
    try:
        with open("savedcreds.json", "r") as creds:
            saved_creds = json.load(creds)
    except:
        print("[*] Failed to read saved credentials")

    if saved_creds:
        print("[*] Using previously saved user credentials")
        google_photos_auth_token = saved_creds["auth_token"]
        google_photos_refresh_token = saved_creds["refresh_token"]
    else:
        # authenticate user to google photos
        print("[*] Visit the following link and grant permissions")
        google_photos_api_oauth()

        print("[*] Copy the code parameter from the http request")
        code = input("Code: ")

        google_photos_api_oauth_token(code)

    # ensure album is created with name; NOTE: we can only add photos to album created via API
    ensure_album_created()
    print("[*] Images will be stored to album with name " + google_photos_album_name)

    SocketModeHandler(app, app_token).start()
    # app.start(port=int(os.environ.get("PORT", 3000)))
