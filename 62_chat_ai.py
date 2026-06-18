import io
import os
import time
import uuid
import re
import base64
from xml.sax.saxutils import escape
from urllib.parse import parse_qs
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional

import pandas as pd
import requests
import resend
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from icalendar import Calendar

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


app = FastAPI(title="Janara Reception API")


# =========================
# CONFIGURAZIONE
# =========================

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

RESEND_FROM = os.environ.get("RESEND_FROM", "Janara <info@janara.net>")

INTERNAL_NOTIFICATION_EMAIL = os.environ.get(
    "INTERNAL_NOTIFICATION_EMAIL",
    "dario.guarriello@gmail.com"
)

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID",
    "1orw2D-Rxh2omVj_MOBIdIsiLf90UjsvSZrpQqKYeTys"
)

GID_CAMERE_CONFIG = os.environ.get("GID_CAMERE_CONFIG", "0")
GID_PREZZI = os.environ.get("GID_PREZZI", "415348027")

PRENOTAZIONI_SHEET_NAME = os.environ.get("PRENOTAZIONI_SHEET_NAME", "PRENOTAZIONI_AI")

WHATSAPP_LOG_SHEET_NAME = os.environ.get(
    "WHATSAPP_LOG_SHEET_NAME",
    "WHATSAPP_CHAT_LOG"
)

# WhatsApp: numero operatore per richieste urgenti / conferme.
WHATSAPP_OPERATOR_PHONE = os.environ.get(
    "WHATSAPP_OPERATOR_PHONE",
    "+39 375 562 6515"
)

# Email operativa dove inviare le foto dei documenti ricevuti via WhatsApp.
# Questa soluzione evita il problema quota Drive delle service account su Google Drive personale.
WHATSAPP_DOCUMENT_EMAIL = os.environ.get(
    "WHATSAPP_DOCUMENT_EMAIL",
    "infojanara@gmail.com"
)

# Cartella Google Drive mantenuta solo come compatibilità, ma non usata nel flusso attuale.
WHATSAPP_DOCUMENTS_FOLDER_ID = os.environ.get(
    "WHATSAPP_DOCUMENTS_FOLDER_ID",
    "1j68I_QeTgvbYlWn4POAU-6FpQ1WcFm1b"
)

# Twilio: necessari per scaricare allegati/foto WhatsApp da MediaUrl0.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "8"))

SHEET_CACHE_SECONDS = int(os.environ.get("SHEET_CACHE_SECONDS", "300"))
ICAL_CACHE_SECONDS = int(os.environ.get("ICAL_CACHE_SECONDS", "120"))

MAX_ICAL_WORKERS = int(os.environ.get("MAX_ICAL_WORKERS", "6"))

# SumUp
SUMUP_API_KEY = os.environ.get("SUMUP_API_KEY", "")
SUMUP_MERCHANT_CODE = os.environ.get("SUMUP_MERCHANT_CODE", "")
SUMUP_CURRENCY = os.environ.get("SUMUP_CURRENCY", "EUR")
SUMUP_REDIRECT_URL = os.environ.get(
    "SUMUP_REDIRECT_URL",
    "https://assistente-telefonico.onrender.com/sumup-webhook"
)

SUMUP_API_BASE_URL = os.environ.get(
    "SUMUP_API_BASE_URL",
    "https://api.sumup.com"
)

# Google API
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/google_calendar_credentials.json"
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# OpenAI opzionale. Il webhook WhatsApp funziona anche senza OpenAI,
# usando una logica conversazionale guidata e sicura.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

_openai_client = None
if OPENAI_API_KEY and OpenAI is not None:
    try:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"OpenAI non inizializzato: {str(e)}")

# Memoria conversazionale semplice per numero WhatsApp.
# Nota: su Render questa memoria è volatile e si resetta a ogni deploy/riavvio.
WHATSAPP_SESSIONS: Dict[str, dict] = {}


http = requests.Session()

_sheet_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
_ical_cache: Dict[str, Tuple[float, List[Tuple[date, date]]]] = {}

_google_sheets_service = None
_google_calendar_service = None
_google_drive_service = None


# =========================
# MODELLI REQUEST
# =========================

class AvailabilityRequest(BaseModel):
    structure: str
    check_in: str
    check_out: str
    guests: int


class BookingEmailRequest(BaseModel):
    nome: str
    email: str
    telefono: str
    struttura: str
    camera: str
    check_in: str
    check_out: str
    ospiti: int
    totale: float
    url_camera: Optional[str] = ""
    payment_link: Optional[str] = ""


class SumupCheckoutRequest(BaseModel):
    nome: str
    email: Optional[str] = None
    telefono: Optional[str] = ""
    struttura: str
    camera: str
    camera_key: Optional[str] = ""
    check_in: str
    check_out: str
    ospiti: int
    totale: float


class SumupCheckoutStatusRequest(BaseModel):
    checkout_id: str


# =========================
# FUNZIONI GOOGLE API
# =========================

def get_google_credentials():
    if not os.path.exists(GOOGLE_APPLICATION_CREDENTIALS):
        raise FileNotFoundError(
            f"File credenziali Google non trovato: {GOOGLE_APPLICATION_CREDENTIALS}"
        )

    return service_account.Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS,
        scopes=GOOGLE_SCOPES
    )


def get_sheets_service():
    global _google_sheets_service

    if _google_sheets_service is None:
        credentials = get_google_credentials()
        _google_sheets_service = build("sheets", "v4", credentials=credentials)

    return _google_sheets_service


def get_calendar_service():
    global _google_calendar_service

    if _google_calendar_service is None:
        credentials = get_google_credentials()
        _google_calendar_service = build("calendar", "v3", credentials=credentials)

    return _google_calendar_service


def get_drive_service():
    global _google_drive_service

    if _google_drive_service is None:
        credentials = get_google_credentials()
        _google_drive_service = build("drive", "v3", credentials=credentials)

    return _google_drive_service


def col_to_letter(col_index: int) -> str:
    result = ""

    while col_index > 0:
        col_index, remainder = divmod(col_index - 1, 26)
        result = chr(65 + remainder) + result

    return result


def get_prenotazioni_values() -> List[List[str]]:
    service = get_sheets_service()

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{PRENOTAZIONI_SHEET_NAME}!A1:R"
    ).execute()

    return result.get("values", [])


def append_prenotazione_ai(row: List):
    service = get_sheets_service()

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{PRENOTAZIONI_SHEET_NAME}!A:R",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={
            "values": [row]
        }
    ).execute()


def append_whatsapp_chat_log(row: List):
    service = get_sheets_service()

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WHATSAPP_LOG_SHEET_NAME}!A:K",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={
            "values": [row]
        }
    ).execute()


def find_prenotazione_by_checkout_id(checkout_id: str) -> Tuple[Optional[int], Optional[dict], List[str]]:
    values = get_prenotazioni_values()

    if not values:
        return None, None, []

    headers = [str(h).strip() for h in values[0]]

    for index, row in enumerate(values[1:], start=2):
        row_dict = {}

        for col_index, header in enumerate(headers):
            row_dict[header] = row[col_index] if col_index < len(row) else ""

        if str(row_dict.get("checkout_id", "")).strip() == str(checkout_id).strip():
            return index, row_dict, headers

    return None, None, headers


def update_prenotazione_fields(row_index: int, headers: List[str], updates: Dict[str, str]):
    service = get_sheets_service()

    data = []

    for field, value in updates.items():
        if field not in headers:
            continue

        col_index = headers.index(field) + 1
        col_letter = col_to_letter(col_index)

        data.append({
            "range": f"{PRENOTAZIONI_SHEET_NAME}!{col_letter}{row_index}",
            "values": [[value]]
        })

    if not data:
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "RAW",
            "data": data
        }
    ).execute()


def create_google_calendar_event_from_booking(booking: dict) -> dict:
    calendar_id = str(booking.get("google_calendar_id", "")).strip()

    if not calendar_id:
        raise ValueError("google_calendar_id mancante nella prenotazione.")

    check_in = str(booking.get("check_in", "")).strip()
    check_out = str(booking.get("check_out", "")).strip()
    nome = str(booking.get("nome", "")).strip()
    email = str(booking.get("email", "")).strip()
    telefono = str(booking.get("telefono", "")).strip()
    camera = str(booking.get("camera", "")).strip()
    struttura = str(booking.get("struttura", "")).strip()
    ospiti = str(booking.get("ospiti", "")).strip()
    totale = str(booking.get("totale", "")).strip()
    checkout_reference = str(booking.get("checkout_reference", "")).strip()
    checkout_id = str(booking.get("checkout_id", "")).strip()

    summary = f"Prenotazione Janara - {camera} - {nome}"

    description = f"""
Prenotazione confermata da pagamento SumUp.

Cliente: {nome}
Email: {email}
Telefono: {telefono}

Struttura: {struttura}
Camera: {camera}
Check-in: {check_in}
Check-out: {check_out}
Ospiti: {ospiti}
Totale: € {totale}

Checkout ID: {checkout_id}
Checkout reference: {checkout_reference}
""".strip()

    event = {
        "summary": summary,
        "description": description,
        "start": {
            "date": check_in
        },
        "end": {
            "date": check_out
        }
    }

    service = get_calendar_service()

    created_event = service.events().insert(
        calendarId=calendar_id,
        body=event
    ).execute()

    return created_event


# =========================
# FUNZIONI UTILI
# =========================

def google_sheet_csv_url(gid: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/"
        f"{SPREADSHEET_ID}/export?format=csv&gid={gid}"
    )


def normalize(value: str) -> str:
    return str(value).strip().lower()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def error_response(message: str) -> dict:
    return {
        "available": False,
        "message": message,
        "room_name": "",
        "camera_key": "",
        "total_price": 0,
        "url_camera": ""
    }


def load_sheet(gid: str, use_cache: bool = True) -> pd.DataFrame:
    now = time.time()

    if use_cache and gid in _sheet_cache:
        cached_at, cached_df = _sheet_cache[gid]
        if now - cached_at <= SHEET_CACHE_SECONDS:
            return cached_df.copy()

    response = http.get(
        google_sheet_csv_url(gid),
        timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()

    df = pd.read_csv(io.StringIO(response.text))
    df.columns = [str(c).strip() for c in df.columns]

    _sheet_cache[gid] = (now, df.copy())
    return df


def prepare_prices(prezzi: pd.DataFrame) -> pd.DataFrame:
    prezzi = prezzi.copy()
    prezzi.columns = [str(c).strip() for c in prezzi.columns]

    if "data" not in prezzi.columns:
        raise ValueError("Nel foglio PREZZI manca la colonna 'data'.")

    prezzi["data"] = pd.to_datetime(prezzi["data"], errors="coerce").dt.date
    prezzi = prezzi.dropna(subset=["data"])
    prezzi = prezzi.set_index("data", drop=False)

    return prezzi


def get_total_price_from_df(
    prezzi: pd.DataFrame,
    camera_key: str,
    check_in: date,
    check_out: date
) -> float:
    camera_key = str(camera_key).strip()

    if camera_key not in prezzi.columns:
        raise ValueError(f"Nel foglio PREZZI manca la colonna camera: {camera_key}")

    total = 0.0
    current = check_in

    while current < check_out:
        if current not in prezzi.index:
            raise ValueError(f"Prezzo mancante per {camera_key} in data {current}")

        price = prezzi.loc[current, camera_key]

        if isinstance(price, pd.Series):
            price = price.iloc[0]

        if pd.isna(price):
            raise ValueError(f"Prezzo vuoto per {camera_key} in data {current}")

        total += float(str(price).replace(",", "."))
        current += timedelta(days=1)

    return round(total, 2)


def get_booked_ranges(ical_url: str) -> List[Tuple[date, date]]:
    ical_url = str(ical_url).strip()
    now = time.time()

    if not ical_url:
        raise ValueError("URL iCal mancante")

    if ical_url in _ical_cache:
        cached_at, cached_ranges = _ical_cache[ical_url]
        if now - cached_at <= ICAL_CACHE_SECONDS:
            return cached_ranges

    response = http.get(ical_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    calendar = Calendar.from_ical(response.text)
    ranges: List[Tuple[date, date]] = []

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        start = component.get("dtstart")
        end = component.get("dtend")

        if not start or not end:
            continue

        start_value = start.dt
        end_value = end.dt

        if isinstance(start_value, datetime):
            start_value = start_value.date()

        if isinstance(end_value, datetime):
            end_value = end_value.date()

        ranges.append((start_value, end_value))

    _ical_cache[ical_url] = (now, ranges)
    return ranges


def is_available_from_ical(
    ical_url: str,
    check_in: date,
    check_out: date
) -> bool:
    for booked_start, booked_end in get_booked_ranges(ical_url):
        if check_in < booked_end and check_out > booked_start:
            return False

    return True


def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return default


def validate_camere_config(camere: pd.DataFrame) -> Optional[str]:
    required_cols = [
        "struttura_key",
        "nome_struttura",
        "citta",
        "camera_key",
        "nome_camera",
        "max_ospiti",
        "ical_url",
        "tipo_camera",
        "attiva"
    ]

    for col in required_cols:
        if col not in camere.columns:
            return f"Nel foglio CAMERE_CONFIG manca la colonna: {col}"

    return None


def find_camera_config(camera_key: str = "", camera_name: str = "", struttura: str = "") -> dict:
    camere = load_sheet(GID_CAMERE_CONFIG, use_cache=False)
    camere.columns = [str(c).strip() for c in camere.columns]

    if "google_calendar_id" not in camere.columns:
        raise ValueError("Nel foglio CAMERE_CONFIG manca la colonna google_calendar_id.")

    camera_key_norm = normalize(camera_key)
    camera_name_norm = normalize(camera_name)
    struttura_norm = normalize(struttura)

    if camera_key_norm:
        match = camere[
            camere["camera_key"].astype(str).str.strip().str.lower() == camera_key_norm
        ]

        if not match.empty:
            row = match.iloc[0].to_dict()
            return {
                "camera_key": str(row.get("camera_key", "")).strip(),
                "nome_camera": str(row.get("nome_camera", "")).strip(),
                "google_calendar_id": str(row.get("google_calendar_id", "")).strip()
            }

    match = camere[
        camere["nome_camera"].astype(str).str.lower().str.contains(camera_name_norm, na=False, regex=False)
        |
        camere["camera_key"].astype(str).str.lower().str.contains(camera_name_norm, na=False, regex=False)
    ]

    if struttura_norm and not match.empty:
        match2 = match[
            match["nome_struttura"].astype(str).str.lower().str.contains(struttura_norm, na=False, regex=False)
            |
            match["struttura_key"].astype(str).str.lower().str.contains(struttura_norm, na=False, regex=False)
            |
            match["citta"].astype(str).str.lower().str.contains(struttura_norm, na=False, regex=False)
        ]

        if not match2.empty:
            match = match2

    if match.empty:
        raise ValueError(f"Non riesco a trovare la camera nel CAMERE_CONFIG: {camera_name}")

    row = match.iloc[0].to_dict()

    return {
        "camera_key": str(row.get("camera_key", "")).strip(),
        "nome_camera": str(row.get("nome_camera", "")).strip(),
        "google_calendar_id": str(row.get("google_calendar_id", "")).strip()
    }


def check_single_room_availability(
    row,
    check_in: date,
    check_out: date
) -> Tuple[str, bool]:
    camera_key = str(row["camera_key"]).strip()
    ical_url = str(row["ical_url"]).strip()

    try:
        available = is_available_from_ical(ical_url, check_in, check_out)
    except Exception:
        available = False

    return camera_key, available


def sumup_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUMUP_API_KEY}",
        "Content-Type": "application/json"
    }


def extract_checkout_id_from_webhook(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    possible_keys = [
        "id",
        "checkout_id",
        "resource_id",
        "resourceId",
        "transaction_id"
    ]

    for key in possible_keys:
        value = payload.get(key)
        if value:
            return str(value)

    resource = payload.get("resource")

    if isinstance(resource, dict):
        for key in possible_keys:
            value = resource.get(key)
            if value:
                return str(value)

    event = payload.get("event")

    if isinstance(event, dict):
        for key in possible_keys:
            value = event.get(key)
            if value:
                return str(value)

    return ""


def send_payment_confirmed_internal_email(
    checkout_id: str,
    status: str,
    sumup_data: dict,
    booking: Optional[dict] = None,
    google_event_id: str = "",
    google_event_link: str = ""
) -> Tuple[bool, str]:
    if not RESEND_API_KEY:
        return False, "RESEND_API_KEY non configurata."

    checkout_reference = sumup_data.get("checkout_reference", "")
    amount = sumup_data.get("amount", "")
    currency = sumup_data.get("currency", "")
    description = sumup_data.get("description", "")
    payment_type = sumup_data.get("payment_type", "")
    transaction_id = sumup_data.get("transaction_id", "")

    booking_html = ""

    if booking:
        booking_html = f"""
        <hr>

        <h3>Dati prenotazione</h3>

        <p><b>Nome:</b> {booking.get("nome", "")}</p>
        <p><b>Email:</b> {booking.get("email", "")}</p>
        <p><b>Telefono:</b> {booking.get("telefono", "")}</p>
        <p><b>Struttura:</b> {booking.get("struttura", "")}</p>
        <p><b>Camera:</b> {booking.get("camera", "")}</p>
        <p><b>Camera key:</b> {booking.get("camera_key", "")}</p>
        <p><b>Check-in:</b> {booking.get("check_in", "")}</p>
        <p><b>Check-out:</b> {booking.get("check_out", "")}</p>
        <p><b>Ospiti:</b> {booking.get("ospiti", "")}</p>
        <p><b>Totale:</b> € {booking.get("totale", "")}</p>
        """

    calendar_html = ""

    if google_event_id:
        calendar_html = f"""
        <hr>

        <h3>Google Calendar</h3>

        <p><b>Evento creato:</b> Sì</p>
        <p><b>Google event ID:</b> {google_event_id}</p>
        <p><b>Link evento:</b><br>
        <a href="{google_event_link}">{google_event_link}</a>
        </p>
        """
    else:
        calendar_html = """
        <hr>

        <h3>Google Calendar</h3>

        <p><b>Evento creato:</b> No o non ancora disponibile</p>
        """

    html = f"""
    <h2>Pagamento SumUp confermato</h2>

    <p>
    SumUp ha notificato un pagamento con stato <b>{status}</b>.
    </p>

    <hr>

    <h3>Dati pagamento</h3>

    <p><b>Checkout ID:</b> {checkout_id}</p>
    <p><b>Checkout reference:</b> {checkout_reference}</p>
    <p><b>Importo:</b> {amount} {currency}</p>
    <p><b>Descrizione:</b> {description}</p>
    <p><b>Payment type:</b> {payment_type}</p>
    <p><b>Transaction ID:</b> {transaction_id}</p>

    {booking_html}

    {calendar_html}

    <hr>

    <p>
    Controlla che Airbnb/Beddy importino correttamente il calendario iCal della camera.
    </p>
    """

    try:
        response = resend.Emails.send({
            "from": RESEND_FROM,
            "to": [INTERNAL_NOTIFICATION_EMAIL],
            "subject": f"Pagamento SumUp confermato - {checkout_reference or checkout_id}",
            "html": html
        })

        print(response)
        return True, ""

    except Exception as e:
        return False, str(e)


def create_sumup_checkout_link(req: SumupCheckoutRequest) -> dict:
    if not SUMUP_API_KEY:
        return {
            "success": False,
            "message": "SUMUP_API_KEY non configurata nelle variabili ambiente.",
            "payment_link": "",
            "checkout_id": "",
            "checkout_reference": ""
        }

    if not SUMUP_MERCHANT_CODE:
        return {
            "success": False,
            "message": "SUMUP_MERCHANT_CODE non configurato nelle variabili ambiente.",
            "payment_link": "",
            "checkout_id": "",
            "checkout_reference": ""
        }

    if req.totale <= 0:
        return {
            "success": False,
            "message": "Il totale deve essere maggiore di zero.",
            "payment_link": "",
            "checkout_id": "",
            "checkout_reference": ""
        }

    try:
        camera_config = find_camera_config(
            camera_key=req.camera_key or "",
            camera_name=req.camera,
            struttura=req.struttura
        )
        resolved_camera_key = camera_config.get("camera_key", "")
        google_calendar_id = camera_config.get("google_calendar_id", "")
    except Exception as e:
        resolved_camera_key = req.camera_key or ""
        google_calendar_id = ""
        print(f"Errore lettura configurazione camera: {str(e)}")

    checkout_reference = (
        f"JANARA-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-"
        f"{uuid.uuid4().hex[:8]}"
    )

    description = (
        f"Prenotazione Janara - {req.camera} - "
        f"{req.check_in} / {req.check_out} - {req.nome}"
    )

    payload = {
        "checkout_reference": checkout_reference,
        "amount": round(float(req.totale), 2),
        "currency": SUMUP_CURRENCY,
        "merchant_code": SUMUP_MERCHANT_CODE,
        "description": description,
        "hosted_checkout": {
            "enabled": True
        }
    }

    if SUMUP_REDIRECT_URL:
        payload["return_url"] = SUMUP_REDIRECT_URL
        payload["redirect_url"] = SUMUP_REDIRECT_URL

    response = http.post(
        f"{SUMUP_API_BASE_URL}/v0.1/checkouts",
        headers=sumup_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "raw_response": response.text
        }

    if response.status_code not in [200, 201]:
        return {
            "success": False,
            "message": f"Errore SumUp: HTTP {response.status_code}",
            "sumup_response": data,
            "payment_link": "",
            "checkout_id": "",
            "checkout_reference": checkout_reference
        }

    payment_link = data.get("hosted_checkout_url", "")
    checkout_id = data.get("id", "")
    now_iso = datetime.utcnow().isoformat()

    prenotazione_row = [
        checkout_id,
        checkout_reference,
        req.nome,
        req.email or "",
        req.telefono or "",
        req.struttura,
        req.camera,
        resolved_camera_key,
        req.check_in,
        req.check_out,
        req.ospiti,
        req.totale,
        payment_link,
        "PENDING",
        google_calendar_id,
        "",
        now_iso,
        ""
    ]

    saved_to_sheet = False
    save_error = ""

    try:
        append_prenotazione_ai(prenotazione_row)
        saved_to_sheet = True
    except Exception as e:
        save_error = str(e)
        print(f"Errore salvataggio PRENOTAZIONI_AI: {save_error}")

    return {
        "success": True,
        "message": "Link pagamento SumUp creato correttamente.",
        "payment_link": payment_link,
        "checkout_id": checkout_id,
        "checkout_reference": checkout_reference,
        "camera_key": resolved_camera_key,
        "google_calendar_id": google_calendar_id,
        "saved_to_prenotazioni_ai": saved_to_sheet,
        "save_error": save_error,
        "status": data.get("status", ""),
        "sumup_response": data
    }


def handle_paid_checkout(checkout_id: str, sumup_data: dict) -> dict:
    row_index, booking, headers = find_prenotazione_by_checkout_id(checkout_id)

    google_event_id = ""
    google_event_link = ""
    calendar_created = False
    calendar_error = ""

    if not booking:
        email_sent, email_error = send_payment_confirmed_internal_email(
            checkout_id=checkout_id,
            status="PAID",
            sumup_data=sumup_data,
            booking=None
        )

        return {
            "booking_found": False,
            "calendar_created": False,
            "calendar_error": "Prenotazione non trovata in PRENOTAZIONI_AI.",
            "internal_email_sent": email_sent,
            "internal_email_error": email_error
        }

    existing_event_id = str(booking.get("google_event_id", "")).strip()

    if existing_event_id:
        google_event_id = existing_event_id
        calendar_created = False
        calendar_error = "Evento già presente, non duplicato."
    else:
        try:
            created_event = create_google_calendar_event_from_booking(booking)
            google_event_id = created_event.get("id", "")
            google_event_link = created_event.get("htmlLink", "")
            calendar_created = True
        except Exception as e:
            calendar_error = str(e)
            print(f"Errore creazione evento Google Calendar: {calendar_error}")

    updates = {
        "stato_pagamento": "PAID",
        "data_pagamento": datetime.utcnow().isoformat()
    }

    if google_event_id:
        updates["google_event_id"] = google_event_id

    try:
        update_prenotazione_fields(row_index, headers, updates)
    except Exception as e:
        print(f"Errore aggiornamento PRENOTAZIONI_AI: {str(e)}")

    if google_event_id:
        booking["google_event_id"] = google_event_id

    email_sent, email_error = send_payment_confirmed_internal_email(
        checkout_id=checkout_id,
        status="PAID",
        sumup_data=sumup_data,
        booking=booking,
        google_event_id=google_event_id,
        google_event_link=google_event_link
    )

    return {
        "booking_found": True,
        "calendar_created": calendar_created,
        "google_event_id": google_event_id,
        "google_event_link": google_event_link,
        "calendar_error": calendar_error,
        "internal_email_sent": email_sent,
        "internal_email_error": email_error
    }


# =========================
# ENDPOINTS
# =========================

@app.get("/")
def home():
    return {
        "status": "online",
        "message": "Janara Reception API attiva"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Janara Reception API",
        "resend_configured": bool(RESEND_API_KEY),
        "sumup_configured": bool(SUMUP_API_KEY and SUMUP_MERCHANT_CODE),
        "sumup_currency": SUMUP_CURRENCY,
        "sumup_redirect_url": SUMUP_REDIRECT_URL,
        "google_credentials_path": GOOGLE_APPLICATION_CREDENTIALS,
        "internal_notification_email": INTERNAL_NOTIFICATION_EMAIL,
        "prenotazioni_sheet_name": PRENOTAZIONI_SHEET_NAME,
        "whatsapp_document_email": WHATSAPP_DOCUMENT_EMAIL
    }


@app.post("/check-availability")
def check_availability(req: AvailabilityRequest):
    try:
        check_in = parse_date(req.check_in)
        check_out = parse_date(req.check_out)
    except Exception:
        return error_response("Le date devono essere nel formato YYYY-MM-DD.")

    if check_out <= check_in:
        return error_response("La data di check-out deve essere successiva al check-in.")

    if req.guests <= 0:
        return error_response("Il numero di ospiti deve essere maggiore di zero.")

    try:
        camere = load_sheet(GID_CAMERE_CONFIG)
        prezzi = prepare_prices(load_sheet(GID_PREZZI))
    except Exception as e:
        return error_response(
            f"Errore nel caricamento dei dati da Google Sheet: {str(e)}"
        )

    camere.columns = [str(c).strip() for c in camere.columns]

    config_error = validate_camere_config(camere)
    if config_error:
        return error_response(config_error)

    camere = camere[
        camere["attiva"].astype(str).str.upper().str.strip() == "SI"
    ].copy()

    struttura_request = normalize(req.structure)

    camere_struttura = camere[
        camere["nome_struttura"]
        .astype(str)
        .str.lower()
        .str.contains(struttura_request, na=False, regex=False)
        |
        camere["struttura_key"]
        .astype(str)
        .str.lower()
        .str.contains(struttura_request, na=False, regex=False)
        |
        camere["citta"]
        .astype(str)
        .str.lower()
        .str.contains(struttura_request, na=False, regex=False)
    ].copy()

    if camere_struttura.empty:
        return error_response(
            f"Non ho trovato la struttura richiesta: {req.structure}. Puoi ripetere il nome?"
        )

    disponibili = []
    disponibilita_per_camera_key: Dict[str, bool] = {}

    camere_standard = camere_struttura[
        camere_struttura["tipo_camera"]
        .astype(str)
        .str.lower()
        .str.strip() != "combinata"
    ].copy()

    camere_combinate = camere_struttura[
        camere_struttura["tipo_camera"]
        .astype(str)
        .str.lower()
        .str.strip() == "combinata"
    ].copy()

    camere_standard_ok = []

    for _, camera in camere_standard.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        max_ospiti = safe_int(camera["max_ospiti"])

        if req.guests > max_ospiti:
            disponibilita_per_camera_key[camera_key] = False
            continue

        camere_standard_ok.append(camera)

    if camere_standard_ok:
        workers = min(MAX_ICAL_WORKERS, len(camere_standard_ok))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    check_single_room_availability,
                    camera,
                    check_in,
                    check_out
                ): camera
                for camera in camere_standard_ok
            }

            for future in as_completed(future_map):
                camera = future_map[future]
                camera_key = str(camera["camera_key"]).strip()
                nome_camera = str(camera["nome_camera"]).strip()
                url_camera = str(camera.get("url_airbnb", "")).strip()

                try:
                    _, disponibile = future.result()
                except Exception:
                    disponibile = False

                disponibilita_per_camera_key[camera_key] = disponibile

                if not disponibile:
                    continue

                try:
                    total_price = get_total_price_from_df(
                        prezzi,
                        camera_key,
                        check_in,
                        check_out
                    )
                except Exception:
                    continue

                disponibili.append({
                    "room_name": nome_camera,
                    "camera_key": camera_key,
                    "total_price": total_price,
                    "url_camera": url_camera
                })

    for _, camera in camere_combinate.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        nome_camera = str(camera["nome_camera"]).strip()
        max_ospiti = safe_int(camera["max_ospiti"])
        url_camera = str(camera.get("url_airbnb", "")).strip()

        if req.guests > max_ospiti:
            continue

        dipendenze = ""

        if "camere_dipendenti" in camere.columns:
            dipendenze = str(camera.get("camere_dipendenti", "")).strip()

        dip_keys = [x.strip() for x in dipendenze.split(",") if x.strip()]

        if not dip_keys:
            continue

        combinata_disponibile = all(
            disponibilita_per_camera_key.get(dep, False)
            for dep in dip_keys
        )

        if not combinata_disponibile:
            continue

        try:
            total_price = get_total_price_from_df(
                prezzi,
                camera_key,
                check_in,
                check_out
            )
        except Exception:
            continue

        disponibili.append({
            "room_name": nome_camera,
            "camera_key": camera_key,
            "total_price": total_price,
            "url_camera": url_camera
        })

    if not disponibili:
        return error_response(
            f"Mi dispiace, non risultano disponibilità dal {req.check_in} "
            f"al {req.check_out} per {req.guests} ospiti."
        )

    disponibili_validi = [
        camera for camera in disponibili
        if camera["total_price"] > 0
    ]

    if not disponibili_validi:
        return error_response(
            "Ho trovato disponibilità, ma non riesco a calcolare correttamente il prezzo. "
            "Verifica il foglio PREZZI."
        )

    migliore = sorted(disponibili_validi, key=lambda x: x["total_price"])[0]

    return {
        "available": True,
        "message": (
            f"Sì, abbiamo disponibilità per {migliore['room_name']}. "
            f"Il prezzo totale dal {req.check_in} al {req.check_out} "
            f"è {migliore['total_price']} euro."
        ),
        "room_name": migliore["room_name"],
        "camera_key": migliore["camera_key"],
        "total_price": migliore["total_price"],
        "url_camera": migliore["url_camera"]
    }


@app.post("/create-sumup-checkout")
def create_sumup_checkout(req: SumupCheckoutRequest):
    try:
        return create_sumup_checkout_link(req)
    except Exception as e:
        return {
            "success": False,
            "message": f"Errore creazione link SumUp: {str(e)}",
            "payment_link": "",
            "checkout_id": "",
            "checkout_reference": ""
        }


@app.post("/sumup-checkout-status")
def sumup_checkout_status(req: SumupCheckoutStatusRequest):
    if not SUMUP_API_KEY:
        return {
            "success": False,
            "message": "SUMUP_API_KEY non configurata.",
            "status": ""
        }

    try:
        response = http.get(
            f"{SUMUP_API_BASE_URL}/v0.1/checkouts/{req.checkout_id}",
            headers=sumup_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text
            }

        if response.status_code not in [200, 201]:
            return {
                "success": False,
                "message": f"Errore SumUp: HTTP {response.status_code}",
                "sumup_response": data,
                "status": ""
            }

        status = str(data.get("status", "")).upper()
        paid_result = {}

        if status == "PAID":
            paid_result = handle_paid_checkout(
                checkout_id=req.checkout_id,
                sumup_data=data
            )

        return {
            "success": True,
            "message": "Stato checkout recuperato correttamente.",
            "checkout_id": data.get("id", ""),
            "status": status,
            "paid_result": paid_result,
            "sumup_response": data
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore controllo stato SumUp: {str(e)}",
            "status": ""
        }


@app.post("/sumup-webhook")
def sumup_webhook_post(payload: dict):
    print("Webhook SumUp POST ricevuto:", payload)

    checkout_id = extract_checkout_id_from_webhook(payload)

    if not checkout_id:
        return {
            "success": False,
            "message": "checkout_id non trovato nel webhook SumUp",
            "payload": payload
        }

    if not SUMUP_API_KEY:
        return {
            "success": False,
            "message": "SUMUP_API_KEY non configurata.",
            "checkout_id": checkout_id
        }

    try:
        response = http.get(
            f"{SUMUP_API_BASE_URL}/v0.1/checkouts/{checkout_id}",
            headers=sumup_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text
            }

        if response.status_code not in [200, 201]:
            return {
                "success": False,
                "message": f"Errore controllo checkout SumUp: HTTP {response.status_code}",
                "checkout_id": checkout_id,
                "sumup_response": data
            }

        status = str(data.get("status", "")).upper()
        paid_result = {}

        if status == "PAID":
            paid_result = handle_paid_checkout(
                checkout_id=checkout_id,
                sumup_data=data
            )

        return {
            "success": True,
            "message": "Webhook SumUp POST ricevuto correttamente.",
            "checkout_id": checkout_id,
            "status": status,
            "paid_result": paid_result,
            "sumup_response": data
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore gestione webhook SumUp: {str(e)}",
            "checkout_id": checkout_id
        }


@app.get("/sumup-webhook")
def sumup_webhook_get(checkout_id: Optional[str] = None, id: Optional[str] = None):
    resolved_checkout_id = checkout_id or id

    if not resolved_checkout_id:
        return {
            "success": True,
            "message": "Ritorno SumUp ricevuto, ma nessun checkout_id presente nei parametri.",
            "hint": "Il pagamento va verificato dal pannello SumUp o tramite /sumup-checkout-status."
        }

    if not SUMUP_API_KEY:
        return {
            "success": False,
            "message": "SUMUP_API_KEY non configurata.",
            "checkout_id": resolved_checkout_id
        }

    try:
        response = http.get(
            f"{SUMUP_API_BASE_URL}/v0.1/checkouts/{resolved_checkout_id}",
            headers=sumup_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text
            }

        status = str(data.get("status", "")).upper()
        paid_result = {}

        if response.status_code in [200, 201] and status == "PAID":
            paid_result = handle_paid_checkout(
                checkout_id=resolved_checkout_id,
                sumup_data=data
            )

        return {
            "success": response.status_code in [200, 201],
            "message": "Ritorno SumUp ricevuto.",
            "checkout_id": resolved_checkout_id,
            "status": status,
            "paid_result": paid_result,
            "sumup_response": data
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore gestione ritorno SumUp: {str(e)}",
            "checkout_id": resolved_checkout_id
        }


@app.post("/send-booking-email")
def send_booking_email(req: BookingEmailRequest):
    if not RESEND_API_KEY:
        return {
            "success": False,
            "message": "RESEND_API_KEY non configurata nelle variabili ambiente."
        }

    url_camera_html = ""

    if req.url_camera:
        url_camera_html = f"""
        <p>
            <b>Link camera / foto:</b><br>
            <a href="{req.url_camera}">{req.url_camera}</a>
        </p>
        """

    if req.payment_link:
        sumup_html = f"""
        <h3>Opzione 1 - Pagamento online con SumUp</h3>

        <p>
        Può confermare la prenotazione pagando online tramite il seguente link sicuro SumUp:
        </p>

        <p>
            <a href="{req.payment_link}"
               style="background-color:#111;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;display:inline-block;">
               Paga ora con SumUp
            </a>
        </p>

        <p>
        Se il pulsante non funziona, può copiare e aprire questo link:<br>
        <a href="{req.payment_link}">{req.payment_link}</a>
        </p>
        """
    else:
        sumup_html = """
        <h3>Opzione 1 - Pagamento online con SumUp</h3>

        <p>
        Il link SumUp non è disponibile in questa email. Può comunque confermare la prenotazione tramite bonifico bancario.
        </p>
        """

    payment_html = f"""
    <h3>Modalità di pagamento</h3>

    <p>
    Per confermare la prenotazione può scegliere una delle seguenti modalità di pagamento.
    </p>

    {sumup_html}

    <h3>Opzione 2 - Bonifico bancario</h3>

    <p>
    Può confermare la prenotazione anche tramite bonifico bancario utilizzando i seguenti dati:
    </p>

    <p>
    <b>Intestatario:</b> Gabriella Aucone<br>
    <b>IBAN:</b> IT20X0760115000001056847310<br>
    <b>Causale:</b> Soggiorno {req.nome}
    </p>

    <p>
    La prenotazione sarà confermata solo dopo la ricezione del pagamento.
    </p>
    """

    customer_html = f"""
    <h2>Riepilogo richiesta prenotazione Janara</h2>

    <p>Gentile {req.nome},</p>

    <p>
    grazie per aver contattato Janara.
    Di seguito trova il riepilogo della sua richiesta.
    </p>

    <hr>

    <p><b>Nome:</b> {req.nome}</p>
    <p><b>Telefono:</b> {req.telefono}</p>
    <p><b>Struttura:</b> {req.struttura}</p>
    <p><b>Camera:</b> {req.camera}</p>
    <p><b>Check-in:</b> {req.check_in}</p>
    <p><b>Check-out:</b> {req.check_out}</p>
    <p><b>Ospiti:</b> {req.ospiti}</p>

    <h3>Totale soggiorno: € {req.totale}</h3>

    {url_camera_html}

    <hr>

    {payment_html}

    <hr>

    <p>
    Grazie,<br>
    Janara Hospitality
    </p>
    """

    internal_payment_html = ""

    if req.payment_link:
        internal_payment_html = f"""
        <p><b>Link pagamento SumUp:</b><br>
        <a href="{req.payment_link}">{req.payment_link}</a>
        </p>
        """
    else:
        internal_payment_html = """
        <p><b>Link pagamento SumUp:</b> non disponibile</p>
        """

    internal_url_camera_html = ""

    if req.url_camera:
        internal_url_camera_html = f"""
        <p><b>Link camera / foto:</b><br>
        <a href="{req.url_camera}">{req.url_camera}</a>
        </p>
        """
    else:
        internal_url_camera_html = """
        <p><b>Link camera / foto:</b> non disponibile</p>
        """

    internal_html = f"""
    <h2>Nuova richiesta prenotazione Janara</h2>

    <p>
    È stata inviata una nuova email di riepilogo al cliente.
    </p>

    <hr>

    <h3>Dati cliente</h3>

    <p><b>Nome cliente:</b> {req.nome}</p>
    <p><b>Email cliente:</b> {req.email}</p>
    <p><b>Telefono:</b> {req.telefono}</p>

    <hr>

    <h3>Dati soggiorno</h3>

    <p><b>Struttura:</b> {req.struttura}</p>
    <p><b>Camera:</b> {req.camera}</p>
    <p><b>Check-in:</b> {req.check_in}</p>
    <p><b>Check-out:</b> {req.check_out}</p>
    <p><b>Ospiti:</b> {req.ospiti}</p>
    <p><b>Totale soggiorno:</b> € {req.totale}</p>

    <hr>

    <h3>Link utili</h3>

    {internal_url_camera_html}
    {internal_payment_html}

    <hr>

    <p>
    Questa è una notifica interna automatica generata dal centralino Janara.
    </p>
    """

    internal_email_sent = False
    internal_email_error = ""

    try:
        customer_response = resend.Emails.send({
            "from": RESEND_FROM,
            "to": [str(req.email)],
            "subject": "Riepilogo prenotazione Janara",
            "html": customer_html
        })

        print(customer_response)

        try:
            internal_response = resend.Emails.send({
                "from": RESEND_FROM,
                "to": [INTERNAL_NOTIFICATION_EMAIL],
                "subject": f"Nuova richiesta Janara - {req.nome}",
                "html": internal_html
            })

            print(internal_response)
            internal_email_sent = True

        except Exception as internal_error:
            internal_email_error = str(internal_error)
            print(f"Errore invio email interna: {internal_email_error}")

        return {
            "success": True,
            "message": f"Email inviata a {req.email}",
            "customer_email": str(req.email),
            "internal_email_sent": internal_email_sent,
            "internal_email": INTERNAL_NOTIFICATION_EMAIL,
            "internal_email_error": internal_email_error
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore invio email cliente: {str(e)}",
            "internal_email_sent": internal_email_sent,
            "internal_email": INTERNAL_NOTIFICATION_EMAIL,
            "internal_email_error": internal_email_error
        }



# =========================
# FUNZIONI WHATSAPP / TWILIO
# =========================

ITALIAN_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12
}


ZONE_CAMERA_MAP = {
    "via_annunziata": {
        "label": "Via Annunziata (Centro storico)",
        "allowed_camera_keywords": [
            "santa sofia",
            "rocca dei rettori",
            "arechi",
            "iside",
            "orsini",
            "arco di traiano"
        ]
    },
    "triggio": {
        "label": "Quartiere Triggio (nei pressi del Teatro Romano)",
        "allowed_camera_keywords": [
            "teatro romano",
            "duomo",
            "port arsa",
            "port’arsa",
            "mura longobarde"
        ]
    },
    "nessuna_preferenza": {
        "label": "Nessuna preferenza",
        "allowed_camera_keywords": []
    }
}


def twilio_xml_response(message: str) -> Response:
    escaped_reply = escape(str(message))
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        f'    <Message>{escaped_reply}</Message>\n'
        '</Response>'
    )
    return Response(content=twiml, media_type="application/xml")


def normalize_for_match(value: str) -> str:
    value = normalize(value)
    value = re.sub(r"[^a-z0-9àèéìòù\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def format_euro(value) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


def get_whatsapp_session(from_number: str) -> dict:
    key = str(from_number or "unknown").strip()

    if key not in WHATSAPP_SESSIONS:
        WHATSAPP_SESSIONS[key] = {
            "step": "ask_guests",
            "structure": "Benevento",
            "zona": "",
            "zona_label": "",
            "allowed_camera_keywords": [],
            "check_in": "",
            "check_out": "",
            "guests": 0,
            "last_availability": None,
            "urgent_request": False,
            "urgent_note": "",
            "contact_name": "",
            "contact_phone": "",
            "contact_email": "",
            "document_link": "",
            "document_status": "",
            "last_updated": datetime.utcnow().isoformat()
        }

    return WHATSAPP_SESSIONS[key]


def reset_whatsapp_session(from_number: str):
    if from_number in WHATSAPP_SESSIONS:
        del WHATSAPP_SESSIONS[from_number]


def parse_italian_date_parts(day: int, month_name: str, year: Optional[int] = None) -> Optional[str]:
    month = ITALIAN_MONTHS.get(str(month_name).lower().strip())

    if not month:
        return None

    if not year:
        year = date.today().year

    try:
        parsed = date(int(year), int(month), int(day))
        return parsed.isoformat()
    except Exception:
        return None


def extract_dates_from_message(text: str) -> Tuple[str, str]:
    text_l = text.lower()
    today = date.today()

    iso_dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text_l)

    if len(iso_dates) >= 2:
        return iso_dates[0], iso_dates[1]

    numeric_dates = re.findall(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text_l)

    if len(numeric_dates) >= 2:
        parsed_dates = []

        for d, m, y in numeric_dates[:2]:
            year = int(y)
            if year < 100:
                year += 2000

            try:
                parsed_dates.append(date(year, int(m), int(d)).isoformat())
            except Exception:
                pass

        if len(parsed_dates) >= 2:
            return parsed_dates[0], parsed_dates[1]

    m = re.search(
        r"dal\s+(\d{1,2})\s+al\s+(\d{1,2})\s+([a-zàèéìòù]+)(?:\s+(\d{4}))?",
        text_l
    )

    if m:
        day_in = int(m.group(1))
        day_out = int(m.group(2))
        month_name = m.group(3)
        year = int(m.group(4)) if m.group(4) else today.year

        check_in = parse_italian_date_parts(day_in, month_name, year)
        check_out = parse_italian_date_parts(day_out, month_name, year)

        if check_in and check_out:
            return check_in, check_out

    named_dates = re.findall(r"\b(\d{1,2})\s+([a-zàèéìòù]+)(?:\s+(\d{4}))?\b", text_l)
    parsed_named = []

    for d, month_name, y in named_dates:
        if month_name not in ITALIAN_MONTHS:
            continue

        year = int(y) if y else today.year
        parsed = parse_italian_date_parts(int(d), month_name, year)

        if parsed:
            parsed_named.append(parsed)

    if len(parsed_named) >= 2:
        return parsed_named[0], parsed_named[1]

    if "stasera" in text_l or "questa sera" in text_l:
        return today.isoformat(), (today + timedelta(days=1)).isoformat()

    if "domani" in text_l:
        tomorrow = today + timedelta(days=1)
        return tomorrow.isoformat(), (tomorrow + timedelta(days=1)).isoformat()

    return "", ""


def extract_guests_from_message(text: str) -> int:
    text_l = str(text or "").lower().strip()

    direct_map = {
        "a": 1,
        "1": 1,
        "b": 2,
        "2": 2,
        "c": 3,
        "3": 3,
        "d": 4,
        "4": 4
    }

    if text_l in direct_map:
        return direct_map[text_l]

    if text_l in ["e", "5", "più di 4", "piu di 4", "oltre 4"]:
        return 5

    m = re.search(r"\b(?:siamo|siamo in|per|in)\s+(\d+)\b", text_l)

    if m:
        return safe_int(m.group(1), 0)

    m = re.search(r"\b(\d+)\s*(?:persone|persona|ospiti|ospite|adulti|adulto|pax)\b", text_l)

    if m:
        return safe_int(m.group(1), 0)

    words = {
        "uno": 1,
        "una": 1,
        "due": 2,
        "tre": 3,
        "quattro": 4,
        "cinque": 5,
        "sei": 6,
        "sette": 7,
        "otto": 8
    }

    for word, value in words.items():
        if re.search(rf"\b(?:siamo\s+in\s+|siamo\s+|per\s+|in\s+)?{word}\b", text_l):
            return value

    return 0


def extract_contact_data(text: str) -> dict:
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    phone_match = re.search(r"(\+?\d[\d\s\.\-]{7,}\d)", text)

    email = email_match.group(0).strip() if email_match else ""
    telefono = phone_match.group(1).strip() if phone_match else ""

    cleaned = text

    if email:
        cleaned = cleaned.replace(email, " ")

    if telefono:
        cleaned = cleaned.replace(telefono, " ")

    cleaned = re.sub(
        r"\b(nome|mi chiamo|sono|email|mail|telefono|tel|cellulare|riepilogo|prenotazione|confermo|documento|carta|identità|identita)\b",
        " ",
        cleaned,
        flags=re.I
    )
    cleaned = re.sub(r"[^A-Za-zÀ-ÿ\s']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    nome = cleaned.title() if len(cleaned) >= 2 else ""

    return {
        "nome": nome,
        "email": email,
        "telefono": telefono
    }


def is_confirmation_intent(text: str) -> bool:
    text_l = text.lower().strip()

    return text_l in ["a", "si", "sì", "ok", "va bene", "confermo", "procedi", "prenota"]


def is_operator_intent(text: str) -> bool:
    text_l = str(text or "").lower().strip()

    return (
        text_l in ["operatore", "parlare con operatore", "voglio parlare con un operatore"]
        or "operatore" in text_l
    )


def build_guests_menu() -> str:
    return (
        "Ciao, sono l'assistente WhatsApp di Janara.\n"
        "Ti aiuto a trovare la soluzione più adatta per il tuo soggiorno.\n\n"
        "1. Quante persone siete?\n\n"
        "a) 1 persona\n"
        "b) 2 persone\n"
        "c) 3 persone\n"
        "d) 4 persone\n"
        "e) Più di 4 persone"
    )


def build_zone_menu() -> str:
    return (
        "Perfetto.\n\n"
        "2. In quale zona preferite soggiornare?\n\n"
        "a) Via Annunziata (Centro storico)\n"
        "b) Quartiere Triggio (nei pressi del Teatro Romano)\n"
        "c) Nessuna preferenza"
    )


def build_date_choice_menu() -> str:
    return (
        "3. Per quali date vuoi controllare la disponibilità?\n\n"
        "a) Questa sera\n"
        "b) Altra data"
    )


def build_operator_reply() -> str:
    return (
        "Va bene, puoi chiamarci o scriverci al numero:\n\n"
        f"{WHATSAPP_OPERATOR_PHONE}\n\n"
        "Un operatore potrà aiutarti con disponibilità, dettagli della struttura "
        "e conferma della prenotazione."
    )


def parse_zone_choice(message: str) -> Tuple[str, str, List[str]]:
    text_l = str(message or "").lower().strip()

    if text_l in ["a", "1", "via annunziata", "annunziata", "centro storico"]:
        zone_key = "via_annunziata"
    elif text_l in ["b", "2", "triggio", "quartiere triggio", "teatro romano"]:
        zone_key = "triggio"
    elif text_l in ["c", "3", "nessuna preferenza", "indifferente", "non ho preferenze", "nessuna"]:
        zone_key = "nessuna_preferenza"
    else:
        return "", "", []

    zone_data = ZONE_CAMERA_MAP[zone_key]
    return zone_key, zone_data["label"], zone_data["allowed_camera_keywords"]


def apply_room_filter_for_whatsapp(camere: pd.DataFrame, session: dict) -> pd.DataFrame:
    allowed_keywords = session.get("allowed_camera_keywords") or []

    if not allowed_keywords:
        return camere

    allowed_norm = [normalize_for_match(k) for k in allowed_keywords]

    def row_matches(row) -> bool:
        nome_camera = normalize_for_match(row.get("nome_camera", ""))
        camera_key = normalize_for_match(row.get("camera_key", ""))

        return any(
            keyword in nome_camera or keyword in camera_key
            for keyword in allowed_norm
        )

    filtered = camere[camere.apply(row_matches, axis=1)].copy()

    # Sicurezza: se i nomi sul foglio sono diversi e non troviamo nulla,
    # non blocchiamo il sistema. Proviamo comunque sul set completo.
    if filtered.empty:
        print("Filtro zona WhatsApp non ha trovato camere. Uso camere complete.")
        return camere

    return filtered


def check_whatsapp_availability_for_session(session: dict) -> dict:
    try:
        check_in = parse_date(str(session.get("check_in", "")))
        check_out = parse_date(str(session.get("check_out", "")))
    except Exception:
        return error_response("Le date non sono valide.")

    guests = int(session.get("guests", 0))

    if check_out <= check_in:
        return error_response("La data di check-out deve essere successiva al check-in.")

    if guests <= 0:
        return error_response("Il numero di ospiti deve essere maggiore di zero.")

    try:
        camere = load_sheet(GID_CAMERE_CONFIG)
        prezzi = prepare_prices(load_sheet(GID_PREZZI))
    except Exception as e:
        return error_response(
            f"Errore nel caricamento dei dati da Google Sheet: {str(e)}"
        )

    camere.columns = [str(c).strip() for c in camere.columns]

    config_error = validate_camere_config(camere)

    if config_error:
        return error_response(config_error)

    camere = camere[
        camere["attiva"].astype(str).str.upper().str.strip() == "SI"
    ].copy()

    struttura_request = normalize(str(session.get("structure", "Benevento") or "Benevento"))

    camere_struttura = camere[
        camere["nome_struttura"].astype(str).str.lower().str.contains(struttura_request, na=False, regex=False)
        |
        camere["struttura_key"].astype(str).str.lower().str.contains(struttura_request, na=False, regex=False)
        |
        camere["citta"].astype(str).str.lower().str.contains(struttura_request, na=False, regex=False)
    ].copy()

    if camere_struttura.empty:
        camere_struttura = camere.copy()

    camere_struttura = apply_room_filter_for_whatsapp(camere_struttura, session)

    disponibili = []
    disponibilita_per_camera_key: Dict[str, bool] = {}

    camere_standard = camere_struttura[
        camere_struttura["tipo_camera"].astype(str).str.lower().str.strip() != "combinata"
    ].copy()

    camere_combinate = camere_struttura[
        camere_struttura["tipo_camera"].astype(str).str.lower().str.strip() == "combinata"
    ].copy()

    camere_standard_ok = []

    for _, camera in camere_standard.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        max_ospiti = safe_int(camera["max_ospiti"])

        if guests > max_ospiti:
            disponibilita_per_camera_key[camera_key] = False
            continue

        camere_standard_ok.append(camera)

    if camere_standard_ok:
        workers = min(MAX_ICAL_WORKERS, len(camere_standard_ok))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(check_single_room_availability, camera, check_in, check_out): camera
                for camera in camere_standard_ok
            }

            for future in as_completed(future_map):
                camera = future_map[future]
                camera_key = str(camera["camera_key"]).strip()
                nome_camera = str(camera["nome_camera"]).strip()
                url_camera = str(camera.get("url_airbnb", "")).strip()
                zona_label = str(session.get("zona_label", "")).strip()

                try:
                    _, disponibile = future.result()
                except Exception:
                    disponibile = False

                disponibilita_per_camera_key[camera_key] = disponibile

                if not disponibile:
                    continue

                try:
                    total_price = get_total_price_from_df(prezzi, camera_key, check_in, check_out)
                except Exception:
                    continue

                disponibili.append({
                    "room_name": nome_camera,
                    "camera_key": camera_key,
                    "total_price": total_price,
                    "url_camera": url_camera,
                    "zona": zona_label
                })

    for _, camera in camere_combinate.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        nome_camera = str(camera["nome_camera"]).strip()
        max_ospiti = safe_int(camera["max_ospiti"])
        url_camera = str(camera.get("url_airbnb", "")).strip()
        zona_label = str(session.get("zona_label", "")).strip()

        if guests > max_ospiti:
            continue

        dipendenze = ""

        if "camere_dipendenti" in camere.columns:
            dipendenze = str(camera.get("camere_dipendenti", "")).strip()

        dip_keys = [x.strip() for x in dipendenze.split(",") if x.strip()]

        if not dip_keys:
            continue

        combinata_disponibile = all(
            disponibilita_per_camera_key.get(dep, False)
            for dep in dip_keys
        )

        if not combinata_disponibile:
            continue

        try:
            total_price = get_total_price_from_df(prezzi, camera_key, check_in, check_out)
        except Exception:
            continue

        disponibili.append({
            "room_name": nome_camera,
            "camera_key": camera_key,
            "total_price": total_price,
            "url_camera": url_camera,
            "zona": zona_label
        })

    if not disponibili:
        return error_response(
            f"Mi dispiace, non risultano disponibilità dal {session.get('check_in')} "
            f"al {session.get('check_out')} per {guests} ospiti."
        )

    disponibili_validi = [camera for camera in disponibili if camera["total_price"] > 0]

    if not disponibili_validi:
        return error_response(
            "Ho trovato disponibilità, ma non riesco a calcolare correttamente il prezzo. "
            "Verifica il foglio PREZZI."
        )

    migliore = sorted(disponibili_validi, key=lambda x: x["total_price"])[0]

    return {
        "available": True,
        "message": (
            f"Sì, abbiamo disponibilità per {migliore['room_name']}. "
            f"Il prezzo totale dal {session.get('check_in')} al {session.get('check_out')} "
            f"è {migliore['total_price']} euro."
        ),
        "room_name": migliore["room_name"],
        "camera_key": migliore["camera_key"],
        "total_price": migliore["total_price"],
        "url_camera": migliore["url_camera"],
        "zona": migliore.get("zona", "")
    }


def build_tonight_offer_reply(session: dict, result: dict) -> str:
    session["last_availability"] = result
    session["step"] = "urgent_wait_contact"

    if not result.get("available"):
        return (
            "Al momento non trovo disponibilità automatica per questa sera con i dati indicati.\n\n"
            "Essendo una richiesta urgente, ti consigliamo comunque di parlare direttamente "
            "con un operatore per verificare eventuali soluzioni alternative.\n\n"
            f"Puoi chiamarci ora al numero:\n{WHATSAPP_OPERATOR_PHONE}\n\n"
            "Ho registrato la tua richiesta come urgente."
        )

    url_camera = str(result.get("url_camera", "")).strip()
    url_line = f"\n\nPuoi vedere foto e dettagli della camera qui:\n{url_camera}" if url_camera else ""

    return (
        "Ho trovato una possibile soluzione disponibile per questa sera:\n\n"
        f"Struttura: Janara\n"
        f"Camera: {result.get('room_name', '')}\n"
        f"Zona: {session.get('zona_label') or result.get('zona') or 'Benevento'}\n"
        f"Periodo: questa sera\n"
        f"Ospiti: {session.get('guests')}\n"
        f"Totale soggiorno: € {format_euro(result.get('total_price', 0))}"
        f"{url_line}\n\n"
        "Per preparare il link di pagamento sicuro SumUp, scrivimi:\n\n"
        "Nome e cognome\n"
        "Telefono\n\n"
        "Dopo questi dati ti chiederò la foto del documento di identità, "
        "che sarà salvata in modo riservato e verificata da un operatore."
    )


def build_normal_offer_reply(session: dict, result: dict) -> str:
    session["last_availability"] = result
    session["step"] = "post_availability_choice"

    if not result.get("available"):
        return result.get("message", "Non risultano disponibilità per i dati indicati.")

    url_camera = str(result.get("url_camera", "")).strip()
    url_line = f"\n\nPuoi vedere foto e dettagli della camera qui:\n{url_camera}" if url_camera else ""

    return (
        "Ho trovato questa soluzione disponibile:\n\n"
        f"Struttura: Janara\n"
        f"Camera: {result.get('room_name', '')}\n"
        f"Zona: {session.get('zona_label') or result.get('zona') or 'Benevento'}\n"
        f"Periodo: {session.get('check_in')} - {session.get('check_out')}\n"
        f"Ospiti: {session.get('guests')}\n"
        f"Totale soggiorno: € {format_euro(result.get('total_price', 0))}"
        f"{url_line}\n\n"
        "Vuoi ricevere il riepilogo con il link di pagamento?\n\n"
        "a) Sì, voglio ricevere riepilogo e pagamento\n"
        "b) No\n"
        "c) Voglio parlare con un operatore"
    )


def send_whatsapp_document_to_operator_email(
    from_number: str,
    media_url: str,
    media_content_type: str = "",
    session: Optional[dict] = None
) -> Tuple[bool, str, str]:
    """
    Scarica il documento ricevuto da WhatsApp/Twilio e lo invia via email
    all'indirizzo operativo della struttura.

    Non salva il file su Google Drive, così evitiamo il problema:
    "Service Accounts do not have storage quota".
    """
    if not media_url:
        return False, "", "MediaUrl mancante."

    if not RESEND_API_KEY:
        return False, "", "RESEND_API_KEY non configurata."

    if not WHATSAPP_DOCUMENT_EMAIL:
        return False, "", "WHATSAPP_DOCUMENT_EMAIL non configurata."

    try:
        auth = None

        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            return False, "", "Credenziali Twilio mancanti: TWILIO_ACCOUNT_SID o TWILIO_AUTH_TOKEN non configurati."

response = http.get(
    media_url,
    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
    timeout=REQUEST_TIMEOUT_SECONDS
)
response.raise_for_status()

        content_type = media_content_type or response.headers.get(
            "Content-Type",
            "application/octet-stream"
        )

        extension = ".jpg"

        if "png" in content_type:
            extension = ".png"
        elif "pdf" in content_type:
            extension = ".pdf"
        elif "jpeg" in content_type or "jpg" in content_type:
            extension = ".jpg"
        elif "heic" in content_type:
            extension = ".heic"

        clean_number = re.sub(r"[^0-9]+", "", str(from_number))
        filename = (
            f"documento_whatsapp_{clean_number}_"
            f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{extension}"
        )

        encoded_content = base64.b64encode(response.content).decode("utf-8")

        session = session or {}
        nome = str(session.get("contact_name", "")).strip()
        telefono = str(session.get("contact_phone", "")).strip()
        email_cliente = str(session.get("contact_email", "")).strip()
        camera = ""
        totale = ""

        result = session.get("last_availability") or {}
        if isinstance(result, dict):
            camera = str(result.get("room_name", "")).strip()
            totale = str(result.get("total_price", "")).strip()

        html = f"""
        <h2>Documento ricevuto da WhatsApp</h2>

        <p>
        È stato ricevuto un documento da verificare per una richiesta Janara.
        Il file è allegato a questa email.
        </p>

        <hr>

        <h3>Dati cliente</h3>
        <p><b>Numero WhatsApp:</b> {from_number}</p>
        <p><b>Nome:</b> {nome}</p>
        <p><b>Telefono:</b> {telefono}</p>
        <p><b>Email:</b> {email_cliente}</p>

        <hr>

        <h3>Dati soggiorno</h3>
        <p><b>Struttura:</b> {session.get('structure', '')}</p>
        <p><b>Zona:</b> {session.get('zona_label', '')}</p>
        <p><b>Camera:</b> {camera}</p>
        <p><b>Check-in:</b> {session.get('check_in', '')}</p>
        <p><b>Check-out:</b> {session.get('check_out', '')}</p>
        <p><b>Ospiti:</b> {session.get('guests', '')}</p>
        <p><b>Totale:</b> € {totale}</p>

        <hr>

        <p><b>Stato:</b> DOCUMENTO RICEVUTO - INVIATO VIA EMAIL - DA VERIFICARE</p>
        <p>Verificare il documento manualmente prima di confermare definitivamente la prenotazione.</p>
        """

        resend_response = resend.Emails.send({
            "from": RESEND_FROM,
            "to": [WHATSAPP_DOCUMENT_EMAIL],
            "subject": f"Documento WhatsApp Janara - {nome or clean_number}",
            "html": html,
            "attachments": [
                {
                    "filename": filename,
                    "content": encoded_content
                }
            ]
        })

        print(resend_response)
        return True, f"INVIATO VIA EMAIL: {WHATSAPP_DOCUMENT_EMAIL}", ""

    except Exception as e:
        return False, "", str(e)


def create_urgent_sumup_and_reply(session: dict) -> str:
    result = session.get("last_availability") or {}

    if not result.get("available"):
        return build_operator_reply()

    nome = str(session.get("contact_name", "")).strip()
    telefono = str(session.get("contact_phone", "")).strip()
    email = str(session.get("contact_email", "")).strip()

    if not nome or not telefono:
        session["step"] = "urgent_wait_contact"
        return (
            "Per preparare il link di pagamento SumUp mi servono prima:\n\n"
            "Nome e cognome\n"
            "Telefono"
        )

    payment_link = ""
    checkout_error = ""

    try:
        checkout_result = create_sumup_checkout_link(
            SumupCheckoutRequest(
                nome=nome,
                email=email or "",
                telefono=telefono,
                struttura=str(session.get("structure", "Benevento")),
                camera=str(result.get("room_name", "")),
                camera_key=str(result.get("camera_key", "")),
                check_in=str(session.get("check_in", "")),
                check_out=str(session.get("check_out", "")),
                ospiti=int(session.get("guests", 0)),
                totale=float(result.get("total_price", 0))
            )
        )

        if checkout_result.get("success"):
            payment_link = str(checkout_result.get("payment_link", "")).strip()
        else:
            checkout_error = str(checkout_result.get("message", "Errore creazione link SumUp."))

    except Exception as e:
        checkout_error = str(e)

    session["step"] = "completed"

    if payment_link:
        return (
            f"Perfetto {nome}, abbiamo ricevuto il documento e preparato il link "
            "di pagamento sicuro SumUp per confermare la prenotazione:\n\n"
            f"{payment_link}\n\n"
            "Il documento sarà verificato da un operatore.\n\n"
            "Essendo una richiesta per questa sera, dopo il pagamento ti consigliamo "
            "comunque di confermare direttamente con un operatore.\n\n"
            f"Puoi chiamarci o scriverci al numero:\n{WHATSAPP_OPERATOR_PHONE}\n\n"
            "Ho registrato la tua richiesta come urgente."
        )

    return (
        f"Grazie {nome}, abbiamo ricevuto il documento ma non sono riuscito a generare "
        f"il link SumUp in automatico. Dettaglio tecnico: {checkout_error}\n\n"
        "La richiesta resta registrata come urgente.\n\n"
        f"Puoi chiamarci o scriverci al numero:\n{WHATSAPP_OPERATOR_PHONE}"
    )


def try_send_booking_summary_from_whatsapp(session: dict, contact: dict) -> str:
    result = session.get("last_availability") or {}

    if not result.get("available"):
        return (
            "Prima devo controllare una disponibilità valida. "
            "Indicami struttura, date e numero di ospiti."
        )

    nome = contact.get("nome", "").strip()
    email = contact.get("email", "").strip()
    telefono = contact.get("telefono", "").strip()

    missing = []

    if not nome:
        missing.append("nome")

    if not email:
        missing.append("email")

    if not telefono:
        missing.append("telefono")

    if missing:
        return (
            "Per inviarti il riepilogo mi manca: "
            + ", ".join(missing)
            + ". Puoi scriverli in un unico messaggio?"
        )

    payment_link = ""

    try:
        checkout_result = create_sumup_checkout_link(
            SumupCheckoutRequest(
                nome=nome,
                email=email,
                telefono=telefono,
                struttura=str(session.get("structure", "")),
                camera=str(result.get("room_name", "")),
                camera_key=str(result.get("camera_key", "")),
                check_in=str(session.get("check_in", "")),
                check_out=str(session.get("check_out", "")),
                ospiti=int(session.get("guests", 0)),
                totale=float(result.get("total_price", 0))
            )
        )

        if checkout_result.get("success"):
            payment_link = str(checkout_result.get("payment_link", "")).strip()

    except Exception as e:
        print(f"Errore creazione checkout SumUp da WhatsApp: {str(e)}")

    try:
        email_result = send_booking_email(
            BookingEmailRequest(
                nome=nome,
                email=email,
                telefono=telefono,
                struttura=str(session.get("structure", "")),
                camera=str(result.get("room_name", "")),
                check_in=str(session.get("check_in", "")),
                check_out=str(session.get("check_out", "")),
                ospiti=int(session.get("guests", 0)),
                totale=float(result.get("total_price", 0)),
                url_camera=str(result.get("url_camera", "")),
                payment_link=payment_link
            )
        )

        if email_result.get("success"):
            session["step"] = "completed"
            reply = f"Perfetto {nome}, ti ho inviato il riepilogo a {email}."

            if payment_link:
                reply += f"\n\nPuoi confermare anche da questo link SumUp:\n{payment_link}"
            else:
                reply += "\n\nNell'email trovi anche le istruzioni per il pagamento."

            reply += (
                "\n\nPrima di procedere, se vuoi verificare che sia tutto corretto "
                "o parlare con noi, puoi chiamare o scrivere al numero:\n"
                f"{WHATSAPP_OPERATOR_PHONE}"
            )

            return reply

        return (
            "Ho provato a inviare l'email, ma c'è stato un problema: "
            + str(email_result.get("message", "errore sconosciuto"))
        )

    except Exception as e:
        return f"Mi dispiace, non sono riuscito a inviare il riepilogo. Errore: {str(e)}"


def get_fallback_ai_reply(message: str, session: dict) -> str:
    if not _openai_client:
        return ""

    try:
        prompt = (
            "Sei l'assistente WhatsApp di Janara Hospitality. "
            "Rispondi in italiano, in modo breve e cortese. "
            "Non inventare disponibilità o prezzi. "
            "Se mancano dati, accompagna il cliente nel percorso guidato. "
            f"Stato conversazione: {session}. "
            f"Messaggio cliente: {message}"
        )

        completion = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Sei un assistente WhatsApp per prenotazioni hospitality."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            max_tokens=160,
            timeout=6
        )

        return completion.choices[0].message.content.strip()

    except Exception as e:
        print(f"Errore OpenAI WhatsApp: {str(e)}")
        return ""


def handle_whatsapp_message(
    from_number: str,
    incoming_message: str,
    media_url: str = "",
    media_content_type: str = ""
) -> str:
    message = str(incoming_message or "").strip()
    message_l = message.lower().strip()

    if message_l in ["reset", "ricomincia", "annulla"]:
        reset_whatsapp_session(from_number)
        return build_guests_menu()

    session = get_whatsapp_session(from_number)
    session["last_updated"] = datetime.utcnow().isoformat()

    if media_url:
        if session.get("step") == "urgent_wait_document":
            ok, document_marker, error = send_whatsapp_document_to_operator_email(
                from_number=from_number,
                media_url=media_url,
                media_content_type=media_content_type,
                session=session
            )

            if ok:
                session["document_link"] = document_marker
                session["document_status"] = "DOCUMENTO RICEVUTO - INVIATO VIA EMAIL - DA VERIFICARE"
                session["urgent_note"] = "RICHIESTA URGENTE - QUESTA SERA"
                return create_urgent_sumup_and_reply(session)

            return (
                "Ho ricevuto il file, ma non sono riuscito a salvarlo correttamente "
                f"su Google Drive. Errore: {error}\n\n"
                f"Puoi chiamarci o scriverci al numero:\n{WHATSAPP_OPERATOR_PHONE}"
            )

        return (
            "Ho ricevuto un allegato. Per completare la procedura, segui prima "
            "le domande guidate.\n\n"
            + build_guests_menu()
        )

    if not message:
        return build_guests_menu()

    if is_operator_intent(message):
        session["urgent_note"] = "RICHIESTA OPERATORE"
        return build_operator_reply()

    step = session.get("step", "ask_guests")

    if step == "ask_guests":
        guests = extract_guests_from_message(message)

        if guests <= 0:
            return (
                "Non ho capito il numero di persone.\n\n"
                + build_guests_menu()
            )

        session["guests"] = guests

        if guests > 4:
            session["urgent_note"] = "RICHIESTA OPERATORE - PIU DI 4 OSPITI"
            return (
                "Per più di 4 persone preferiamo verificare direttamente la soluzione migliore.\n\n"
                f"Puoi chiamarci o scriverci al numero:\n{WHATSAPP_OPERATOR_PHONE}"
            )

        session["step"] = "ask_zone"
        return build_zone_menu()

    if step == "ask_zone":
        zone_key, zone_label, allowed_keywords = parse_zone_choice(message)

        if not zone_key:
            return (
                "Non ho capito la zona scelta.\n\n"
                + build_zone_menu()
            )

        session["zona"] = zone_key
        session["zona_label"] = zone_label
        session["allowed_camera_keywords"] = allowed_keywords
        session["step"] = "ask_date_choice"

        return build_date_choice_menu()

    if step == "ask_date_choice":
        if message_l in ["a", "questa sera", "stasera", "1"]:
            today = date.today()
            session["check_in"] = today.isoformat()
            session["check_out"] = (today + timedelta(days=1)).isoformat()
            session["urgent_request"] = True
            session["urgent_note"] = "RICHIESTA URGENTE - QUESTA SERA"

            result = check_whatsapp_availability_for_session(session)
            return build_tonight_offer_reply(session, result)

        if message_l in ["b", "altra data", "2"]:
            session["step"] = "ask_dates_text"
            return (
                "Va bene. Scrivi le date del soggiorno.\n\n"
                "Esempio:\n"
                "dal 20 al 22 giugno 2026"
            )

        return (
            "Non ho capito la scelta.\n\n"
            + build_date_choice_menu()
        )

    if step == "ask_dates_text":
        check_in, check_out = extract_dates_from_message(message)

        if not check_in or not check_out:
            return (
                "Non ho capito le date.\n\n"
                "Scrivile così, ad esempio:\n"
                "dal 20 al 22 giugno 2026"
            )

        session["check_in"] = check_in
        session["check_out"] = check_out

        result = check_whatsapp_availability_for_session(session)
        return build_normal_offer_reply(session, result)

    if step == "post_availability_choice":
        if is_confirmation_intent(message):
            session["step"] = "wait_booking_contact"
            return (
                "Perfetto. Per preparare il riepilogo della prenotazione, scrivimi:\n\n"
                "Nome e cognome\n"
                "Email\n"
                "Telefono"
            )

        if message_l in ["b", "no", "2"]:
            session["step"] = "completed"
            return (
                "Va bene. Se vuoi, puoi scrivermi di nuovo per controllare altre date "
                "o altre zone."
            )

        if message_l in ["c", "3"] or is_operator_intent(message):
            session["urgent_note"] = "RICHIESTA OPERATORE"
            return build_operator_reply()

        return (
            "Non ho capito la scelta.\n\n"
            "a) Sì, voglio ricevere riepilogo e pagamento\n"
            "b) No\n"
            "c) Voglio parlare con un operatore"
        )

    if step == "wait_booking_contact":
        contact = extract_contact_data(message)
        return try_send_booking_summary_from_whatsapp(session, contact)

    if step == "urgent_wait_contact":
        contact = extract_contact_data(message)

        nome = contact.get("nome", "").strip()
        telefono = contact.get("telefono", "").strip()
        email = contact.get("email", "").strip()

        missing = []

        if not nome:
            missing.append("nome e cognome")

        if not telefono:
            missing.append("telefono")

        if missing:
            return (
                "Per preparare il link di pagamento SumUp mi manca: "
                + ", ".join(missing)
                + ".\n\nPuoi scriverli in un unico messaggio?"
            )

        session["contact_name"] = nome
        session["contact_phone"] = telefono
        session["contact_email"] = email
        session["step"] = "urgent_wait_document"

        return (
            f"Grazie {nome}.\n\n"
            "Ora invia qui su WhatsApp una foto leggibile del documento di identità.\n\n"
            "La foto sarà inviata in modo riservato all'email della struttura "
            "e sarà usata solo per la verifica della richiesta e per gli adempimenti "
            "previsti per la struttura ricettiva.\n\n"
            "Dopo la ricezione del documento ti invierò il link di pagamento sicuro SumUp."
        )

    if step == "urgent_wait_document":
        return (
            "Per proseguire devi inviare una foto leggibile del documento di identità "
            "come allegato WhatsApp.\n\n"
            f"Per assistenza puoi chiamare o scrivere al numero:\n{WHATSAPP_OPERATOR_PHONE}"
        )

    if step == "completed":
        if message_l in ["ciao", "buongiorno", "salve", "menu", "inizia"]:
            reset_whatsapp_session(from_number)
            return build_guests_menu()

        return (
            "La richiesta è già stata gestita. Se vuoi iniziare una nuova ricerca, "
            "scrivi: reset"
        )

    ai_reply = get_fallback_ai_reply(message, session)

    if ai_reply:
        return ai_reply

    return build_guests_menu()

# =========================
# ENDPOINT WHATSAPP / TWILIO
# =========================

@app.get("/whatsapp-webhook")
def whatsapp_webhook_test():
    return {
        "status": "ok",
        "message": "Webhook WhatsApp attivo. In Twilio usa questo endpoint con metodo POST.",
        "endpoint": "/whatsapp-webhook",
        "openai_configured": bool(OPENAI_API_KEY),
        "openai_available": bool(_openai_client),
        "document_email": WHATSAPP_DOCUMENT_EMAIL,
        "documents_folder_id_compatibility": WHATSAPP_DOCUMENTS_FOLDER_ID
    }


@app.post("/whatsapp-webhook")
async def whatsapp_webhook(request: Request):
    """
    Webhook per messaggi WhatsApp ricevuti da Twilio.

    Twilio invia normalmente i dati come application/x-www-form-urlencoded:
    - Body: testo del messaggio ricevuto
    - From: numero WhatsApp del cliente
    - NumMedia: numero allegati/foto/documenti
    - MediaUrl0: URL temporaneo del primo allegato
    - MediaContentType0: tipo file del primo allegato

    La risposta viene restituita in formato TwiML XML.
    """

    try:
        raw_body = await request.body()
        parsed_body = parse_qs(raw_body.decode("utf-8", errors="ignore"))

        incoming_message = str(parsed_body.get("Body", [""])[0]).strip()
        from_number = str(parsed_body.get("From", [""])[0]).strip()

        num_media = safe_int(str(parsed_body.get("NumMedia", ["0"])[0]), 0)

        media_url = ""
        media_content_type = ""

        if num_media > 0:
            media_url = str(parsed_body.get("MediaUrl0", [""])[0]).strip()
            media_content_type = str(parsed_body.get("MediaContentType0", [""])[0]).strip()

        print(
            f"Messaggio WhatsApp ricevuto da {from_number}: "
            f"{incoming_message if incoming_message else '[ALLEGATO]'} "
            f"- media: {num_media} "
            f"- content_type: {media_content_type}"
        )

        # Gestione messaggio WhatsApp.
        # Questa funzione deve accettare anche media_url e media_content_type.
        reply = handle_whatsapp_message(
            from_number=from_number,
            incoming_message=incoming_message,
            media_url=media_url,
            media_content_type=media_content_type
        )

        # Salvataggio log conversazione su Google Sheet
        try:
            session = WHATSAPP_SESSIONS.get(from_number, {})

            status = (
                session.get("document_status")
                or session.get("urgent_note")
                or "OK"
            )

            append_whatsapp_chat_log([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                from_number,
                incoming_message if incoming_message else "[ALLEGATO]",
                reply,
                session.get("structure", ""),
                session.get("check_in", ""),
                session.get("check_out", ""),
                session.get("guests", ""),
                status,
                session.get("zona_label", ""),
                session.get("document_link", "")
            ])

        except Exception as log_error:
            print(f"Errore salvataggio log WhatsApp: {str(log_error)}")

        return twilio_xml_response(reply)

    except Exception as e:
        print(f"Errore generale webhook WhatsApp: {str(e)}")

        return twilio_xml_response(
            "Mi dispiace, ho avuto un problema tecnico. "
            "Riprova tra qualche istante oppure contatta direttamente la struttura."
        )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        "62_chat_ai:app",
        host="0.0.0.0",
        port=port
    )
