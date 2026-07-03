import requests, os
from dotenv import load_dotenv

load_dotenv()
ORG_ID = os.getenv("DEVIN_ORG_ID")
API_KEY = os.getenv("DEVIN_API_KEY")
BASE = "https://api.devin.ai/v3"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

async def create_session(payload):
    session = requests.post(
        f"{BASE}/organizations/{ORG_ID}/sessions",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=payload
    )
    if session.status_code not in (200, 201):
        print(f"Devin API Error: {session.status_code} - {session.text}")
        return None
    return session.json()

async def delete_session(devin_id):
    response = requests.delete(
        f"{BASE}/organizations/{ORG_ID}/sessions/{devin_id}", 
        headers=HEADERS
    )
    if response.status_code not in (200, 201):
        print(f"Devin API Error: {response.status_code} - {response.text}")
        return None
    return response.json()

async def get_session(devin_id):
    response = requests.get(
        f"https://api.devin.ai/v3/organizations/{ORG_ID}/sessions/{devin_id}", 
        headers=HEADERS
    )
    if response.status_code not in (200, 201):
        print(f"Devin API Error: {response.status_code} - {response.text}")
        return None
    return response.json()

async def send_message(devin_id, payload):
    response = requests.post(
        f"https://api.devin.ai/v3/organizations/{ORG_ID}/sessions/{devin_id}/messages", 
        headers={**HEADERS, "Content-Type": "application/json"},
        json=payload
    )
    if response.status_code not in (200, 201):
        print(f"Devin API Error: {response.status_code} - {response.text}")
        return None
    return response.json()