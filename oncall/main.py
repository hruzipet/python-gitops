import os
import logging
import requests
import json
import asyncio
import subprocess
import uuid
import urllib.parse
import time
from fastapi import FastAPI, Request, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2
from dotenv import load_dotenv
import edge_tts

# Load environment variables
load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip('/')
RESOURCE_ACCOUNT_ID = os.getenv("BOT_RESOURCE_ACCOUNT_OBJECT_ID")

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "database": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "port": os.getenv("DB_PORT", "5432")
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =================================================================
# ENTERPRISE MEMORY STORE
# Structure: call_id -> { ticket_id, phone, audio_id, answered_at, audio_played, audio_completed }
# =================================================================
ACTIVE_CALLS = {}

AUDIO_DIR = "./audio_files"
os.makedirs(AUDIO_DIR, exist_ok=True)

app = FastAPI()
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

class CallRequest(BaseModel):
    target_number: str
    ticket_id: str
    message: str
    voice: str = "en-US-AriaNeural"

def get_graph_token():
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    token_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }
    res = requests.post(token_url, data=token_data)
    res.raise_for_status()
    return res.json().get("access_token")

def db_log_event(ticket_id: str, call_id: str, status: str, phone: str, details: str = ""):
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        query = """
            INSERT INTO oncall_teams.call_logs (ticket_id, call_id, status, phone_number, details, created_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """
        cur.execute(query, (ticket_id, call_id, status, phone, details))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ [DB ERROR] Failed to write event to DB: {e}")
    finally:
        if conn:
            cur.close()
            conn.close()

def pregenerate_tts_wav(text: str, audio_id: str, voice: str):
    try:
        mp3_path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
        file_name = f"zprava_{audio_id}.wav"
        wav_path = os.path.join(AUDIO_DIR, file_name)

        communicate = edge_tts.Communicate(text, voice)
        asyncio.run(communicate.save(mp3_path))
        
        subprocess.run([
            "ffmpeg", "-y", "-i", mp3_path,
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            wav_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(mp3_path):
            os.remove(mp3_path)
            
        return file_name
    except Exception as e:
        logger.error(f"❌ [TTS ERROR] Detail: {e}")
        return None

def play_ready_audio(call_id: str, ticket_id: str, phone: str, audio_id: str):
    try:
        audio_uri = f"{APP_BASE_URL}/audio/zprava_{audio_id}.wav"
        db_log_event(ticket_id, call_id, "ACTION_PLAY_PROMPT", phone, f"Sending instant play prompt command: {audio_uri}")
        
        access_token = get_graph_token()
        play_url = f"https://graph.microsoft.com/v1.0/communications/calls/{call_id}/playPrompt"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        play_payload = {
            "clientContext": f"audio-{call_id}",
            "prompts": [
                {
                    "@odata.type": "#microsoft.graph.mediaPrompt",
                    "mediaInfo": {
                        "uri": audio_uri
                    },
                    "loop": 1
                }
            ]
        }
        
        requests.post(play_url, headers=headers, json=play_payload).raise_for_status()
        logger.info(f"✅ [AUDIO SUCCESS] Command accepted by Teams.")
    except Exception as e:
        logger.error(f"❌ [AUDIO ERROR] Failed to send play command: {e}")
        db_log_event(ticket_id, call_id, "ERROR_PLAY_PROMPT", phone, f"Error sending audio: {e}")

def hangup_call(call_id: str, ticket_id: str, phone: str):
    try:
        db_log_event(ticket_id, call_id, "ACTION_HANGUP", phone, "Sending MS Graph command to hang up.")
        access_token = get_graph_token()
        hangup_url = f"https://graph.microsoft.com/v1.0/communications/calls/{call_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        requests.delete(hangup_url, headers=headers).raise_for_status()
    except Exception as e:
        logger.error(f"❌ [HANGUP ERROR] {e}")

@app.post("/api/make_call")
def make_call_endpoint(req: CallRequest):
    if not RESOURCE_ACCOUNT_ID:
        return {"status": "error", "message": "BOT_RESOURCE_ACCOUNT_OBJECT_ID missing in .env"}
    
    audio_id = str(uuid.uuid4())
    file_name = pregenerate_tts_wav(req.message, audio_id, req.voice)
    
    if not file_name:
        return {"status": "error", "message": "Failed to generate TTS audio."}

    dynamic_callback_uri = f"{APP_BASE_URL}/api/callbacks"

    try:
        access_token = get_graph_token()
        graph_url = "https://graph.microsoft.com/v1.0/communications/calls"
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        
        call_payload = {
            "@odata.type": "#microsoft.graph.call",
            "callbackUri": dynamic_callback_uri,
            "tenantId": TENANT_ID,
            "source": {
                "@odata.type": "#microsoft.graph.participantInfo",
                "identity": {
                    "@odata.type": "#microsoft.graph.identitySet",
                    "applicationInstance": {"@odata.type": "#microsoft.graph.identity", "id": RESOURCE_ACCOUNT_ID}
                }
            },
            "targets": [
                {
                    "@odata.type": "#microsoft.graph.invitationParticipantInfo",
                    "identity": {
                        "@odata.type": "#microsoft.graph.identitySet",
                        "phone": {"@odata.type": "#microsoft.graph.identity", "id": req.target_number}
                    }
                }
            ],
            "requestedModalities": ["audio"],
            "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"}
        }

        call_res = requests.post(graph_url, headers=headers, json=call_payload)
        call_res.raise_for_status()
        
        call_id = call_res.json().get("id")
        
        ACTIVE_CALLS[call_id] = {
            "ticket_id": req.ticket_id,
            "phone": req.target_number,
            "audio_id": audio_id,
            "answered_at": None,
            "audio_played": False,
            "audio_completed": False # NEW FLAG FOR DETECTING INTERRUPTION
        }
        
        db_log_event(req.ticket_id, call_id, "API_INITIATED", req.target_number, f"Call triggered. Mapped to audio ID: {audio_id}")
        
        return {"status": "success", "call_id": call_id}
    
    except Exception as e:
        logger.error(f"❌ [INIT ERROR] Graph API failed: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/callbacks")
async def callback_handler(
    request: Request, 
    background_tasks: BackgroundTasks, 
    ticket_id_query: str = Query("UNKNOWN", alias="ticket_id"), 
    phone_query: str = Query("N/A", alias="phone")
):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error"}

    events = body.get("value", [])
    for event in events:
        resource_data = event.get("resourceData", {})
        resource_url = event.get("resourceUrl", "")
        odata_type = resource_data.get("@odata.type", "")
        
        if "/calls/" in resource_url:
            call_id = resource_url.split("/calls/")[-1].split("/")[0]
        else:
            call_id = resource_data.get("id", "UNKNOWN_CALL_ID")

        if call_id not in ACTIVE_CALLS:
            ACTIVE_CALLS[call_id] = {}

        session = ACTIVE_CALLS[call_id]
        ticket_id = session.get("ticket_id") or ticket_id_query
        phone = session.get("phone") or phone_query

        if odata_type == "#microsoft.graph.playPromptOperation":
            op_status = resource_data.get("status", "").lower()
            details_msg = f"Play prompt operation: {op_status}"
            
            if op_status == "completed":
                # Zpráva dohrála do konce!
                session["audio_completed"] = True
                details_msg = "Playback finished. Preparing hangup."
                background_tasks.add_task(hangup_call, call_id, ticket_id, phone)
                
            db_log_event(ticket_id, call_id, f"AUDIO_{op_status.upper()}", phone, details_msg)
            continue 

        state = resource_data.get("state", "UNKNOWN_STATE").upper()
        if state != "UNKNOWN_STATE":
            db_state = state 
            details_msg = f"State transition to {state}"
            
            if state == "ESTABLISHING":
                details_msg = "Dialing out to operator network. Waiting for pickup."
            
            elif state == "ESTABLISHED":
                details_msg = "Callee picked up. Call successfully established."
                if not session.get("answered_at"):
                    session["answered_at"] = time.time()
            
            elif state == "TERMINATED":
                res_info = resource_data.get("resultInfo", {})
                code = res_info.get("code", "N/A")
                subcode = res_info.get("subcode", "N/A")
                msg = res_info.get("message", "Unknown reason")
                
                ans_time = session.get("answered_at")
                audio_done = session.get("audio_completed")
                
                if ans_time:
                    duration = int(time.time() - ans_time)
                    if not audio_done:
                        # HOVOR BYL TÍPNUT UPROSTŘED ZPRÁVY
                        db_state = "INTERRUPTED"
                        details_msg = f"Call terminated EARLY by user DURING playback! Duration: {duration} seconds."
                    else:
                        # STANDARDNÍ ÚSPĚŠNÝ KONEC
                        db_state = "TERMINATED"
                        details_msg = f"Call terminated normally after FULL playback. Duration: {duration} seconds."
                else:
                    # HOVOR NEBYL VŮBEC ZVEDNUT / TÍPNUT
                    db_state = "NOT_ANSWERED"
                    details_msg = f"Call was rejected or not answered. MS Graph message: {msg} (Code: {code}, Subcode: {subcode})"

            # Zápis stavu do DB
            db_log_event(ticket_id, call_id, db_state, phone, details_msg)
            logger.info(f"🔄 [STATE] Call {call_id}: {db_state}")

            if state == "ESTABLISHED":
                if not session.get("audio_played"):
                    session["audio_played"] = True
                    audio_id = session.get("audio_id")
                    if audio_id:
                        background_tasks.add_task(play_ready_audio, call_id, ticket_id, phone, audio_id)

            if state == "TERMINATED":
                ACTIVE_CALLS.pop(call_id, None)

    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)