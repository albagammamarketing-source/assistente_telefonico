import io
import os
import time
import uuid
import re
import json
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional
from xml.sax.saxutils import escape

import pandas as pd
import requests
import resend
from openai import OpenAI
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from icalendar import Calendar

from google.oauth2 import service_account
from googleapiclient.discovery import build


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

# OpenAI per agente WhatsApp conversazionale
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Google API
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/google_calendar_credentials.json"
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets"
]


http = requests.Session()

_sheet_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
_ical_cache: Dict[str, Tuple[float, List[Tuple[date, date]]]] = {}

_google_sheets_service = None
_google_calendar_service = None

# Memoria conversazioni WhatsApp in RAM.
# Per la prima versione va bene. Se Render si riavvia, la memoria si resetta.
_whatsapp_sessions: Dict[str, dict] = {}


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
# AGENTE WHATSAPP TESTUALE
# =========================

def get_whatsapp_session(from_number: str) -> dict:
    session = _whatsapp_sessions.get(from_number)

    if not session:
        session = {
            "structure": "",
            "check_in": "",
            "check_out": "",
            "guests": 0,
            "last_available": False,
            "room_name": "",
            "camera_key": "",
            "total_price": 0,
            "url_camera": "",
            "nome": "",
            "email": "",
            "telefono": "",
            "history": []
        }
        _whatsapp_sessions[from_number] = session

    return session


def clean_json_text(value: str) -> str:
    value = str(value).strip()

    if value.startswith("```"):
        value = value.strip("`")
        value = value.replace("json", "", 1).strip()

    start = value.find("{")
    end = value.rfind("}")

    if start >= 0 and end >= start:
        return value[start:end + 1]

    return value


def extract_whatsapp_intent_with_ai(message: str, session: dict) -> dict:
    """
    Usa OpenAI per capire cosa vuole il cliente e trasformare il messaggio
    in dati strutturati. Se OPENAI_API_KEY non è configurata, ritorna un
    fallback semplice.
    """
    if not openai_client:
        return {
            "intent": "fallback",
            "reply": (
                "Ciao, sono l'assistente WhatsApp di Janara. "
                "Posso aiutarti con disponibilità, prezzi e richiesta di prenotazione. "
                "Scrivimi struttura o città, date di check-in/check-out e numero ospiti."
            ),
            "structure": "",
            "check_in": "",
            "check_out": "",
            "guests": 0,
            "nome": "",
            "email": "",
            "telefono": "",
            "wants_booking_email": False,
            "reset": False
        }

    system_prompt = """
Sei l'assistente WhatsApp di Janara Hospitality.
Parli sempre in italiano, in modo naturale, gentile e professionale.
Devi aiutare il cliente a controllare disponibilità e prezzi per soggiorni.

Strutture/città gestite:
- Benevento / Janara / Arco di Traiano
- Milano / Magenta
- Budapest / Kiraly

Devi estrarre dal messaggio questi dati, se presenti:
- struttura o città
- check_in in formato YYYY-MM-DD
- check_out in formato YYYY-MM-DD
- numero ospiti
- nome cliente
- email cliente
- telefono cliente
- se il cliente vuole ricevere riepilogo/prenotare

Regole:
- Se il cliente scrive date in formato italiano tipo 20/06/2026, convertile in YYYY-MM-DD.
- Se manca l'anno, usa 2026 solo se coerente con il contesto; altrimenti chiedi conferma.
- Non inventare prezzi o disponibilità.
- Se mancano dati necessari, fai una domanda breve.
- Rispondi SOLO con JSON valido, senza testo fuori dal JSON.

Schema JSON obbligatorio:
{
  "intent": "greeting | availability | booking_email | general | reset",
  "reply": "risposta naturale da inviare al cliente se non bisogna ancora chiamare il motore disponibilità",
  "structure": "",
  "check_in": "",
  "check_out": "",
  "guests": 0,
  "nome": "",
  "email": "",
  "telefono": "",
  "wants_booking_email": false,
  "reset": false
}
""".strip()

    compact_session = {
        "structure": session.get("structure", ""),
        "check_in": session.get("check_in", ""),
        "check_out": session.get("check_out", ""),
        "guests": session.get("guests", 0),
        "last_available": session.get("last_available", False),
        "room_name": session.get("room_name", ""),
        "total_price": session.get("total_price", 0),
        "nome": session.get("nome", ""),
        "email": session.get("email", ""),
        "telefono": session.get("telefono", "")
    }

    user_prompt = f"""
Stato conversazione precedente:
{json.dumps(compact_session, ensure_ascii=False)}

Ultimo messaggio cliente:
{message}
""".strip()

    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2
        )

        content = response.choices[0].message.content or "{}"
        return json.loads(clean_json_text(content))

    except Exception as e:
        print(f"Errore OpenAI WhatsApp agent: {str(e)}")
        return {
            "intent": "general",
            "reply": (
                "Mi scusi, ho avuto un piccolo problema nel capire il messaggio. "
                "Può scrivermi città o struttura, date di arrivo e partenza, e numero ospiti?"
            ),
            "structure": "",
            "check_in": "",
            "check_out": "",
            "guests": 0,
            "nome": "",
            "email": "",
            "telefono": "",
            "wants_booking_email": False,
            "reset": False
        }


def merge_ai_data_into_session(session: dict, data: dict):
    for key in ["structure", "check_in", "check_out", "nome", "email", "telefono"]:
        value = str(data.get(key, "") or "").strip()
        if value:
            session[key] = value

    guests = data.get("guests", 0)
    try:
        guests = int(guests)
    except Exception:
        guests = 0

    if guests > 0:
        session["guests"] = guests


def session_has_availability_data(session: dict) -> bool:
    return bool(
        session.get("structure")
        and session.get("check_in")
        and session.get("check_out")
        and int(session.get("guests") or 0) > 0
    )


def session_has_contact_data(session: dict) -> bool:
    return bool(
        session.get("nome")
        and session.get("email")
        and session.get("telefono")
    )


def try_check_availability_from_session(session: dict) -> str:
    result = check_availability(
        AvailabilityRequest(
            structure=session["structure"],
            check_in=session["check_in"],
            check_out=session["check_out"],
            guests=int(session["guests"])
        )
    )

    session["last_available"] = bool(result.get("available"))
    session["room_name"] = result.get("room_name", "")
    session["camera_key"] = result.get("camera_key", "")
    session["total_price"] = result.get("total_price", 0)
    session["url_camera"] = result.get("url_camera", "")

    reply = result.get("message", "Non sono riuscito a controllare la disponibilità.")

    if result.get("available"):
        if session.get("url_camera"):
            reply += f"\n\nPuò vedere la camera qui:\n{session['url_camera']}"

        reply += (
            "\n\nVuole ricevere il riepilogo della richiesta via email? "
            "Mi scriva nome, email e telefono."
        )

    return reply


def try_send_booking_email_from_session(session: dict) -> str:
    if not session.get("last_available"):
        return "Prima devo verificare una disponibilità valida. Mi indica struttura, date e numero ospiti?"

    if not session_has_contact_data(session):
        return "Per inviare il riepilogo mi servono nome, email e telefono."

    result = send_booking_email(
        BookingEmailRequest(
            nome=session["nome"],
            email=session["email"],
            telefono=session["telefono"],
            struttura=session["structure"],
            camera=session["room_name"],
            check_in=session["check_in"],
            check_out=session["check_out"],
            ospiti=int(session["guests"]),
            totale=float(session["total_price"] or 0),
            url_camera=session.get("url_camera", ""),
            payment_link=""
        )
    )

    if result.get("success"):
        return (
            f"Perfetto, ho inviato il riepilogo a {session['email']}. "
            "Controlli anche la cartella spam o promozioni."
        )

    return f"Mi dispiace, non sono riuscito a inviare l'email. {result.get('message', '')}"


def build_whatsapp_agent_reply(from_number: str, message: str) -> str:
    session = get_whatsapp_session(from_number)

    if message.strip().lower() in ["reset", "riavvia", "annulla", "cancella"]:
        _whatsapp_sessions.pop(from_number, None)
        return "Va bene, ho azzerato la conversazione. Come posso aiutarla?"

    data = extract_whatsapp_intent_with_ai(message, session)

    if data.get("reset"):
        _whatsapp_sessions.pop(from_number, None)
        return "Va bene, ripartiamo da capo. Come posso aiutarla?"

    merge_ai_data_into_session(session, data)

    session["history"].append({
        "at": datetime.utcnow().isoformat(),
        "user": message,
        "ai_data": data
    })
    session["history"] = session["history"][-10:]

    wants_email = bool(data.get("wants_booking_email")) or data.get("intent") == "booking_email"

    if wants_email and session.get("last_available"):
        return try_send_booking_email_from_session(session)

    if session_has_availability_data(session):
        # Se non è già stata controllata la disponibilità, oppure il cliente ha aggiornato i dati,
        # facciamo il controllo immediato.
        return try_check_availability_from_session(session)

    reply = str(data.get("reply", "") or "").strip()

    if reply:
        return reply

    return (
        "Certo, posso aiutarla. Mi indica città o struttura, date di check-in e check-out, "
        "e numero di ospiti?"
    )


def twilio_xml_response(message: str) -> Response:
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{escape(message)}</Message>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


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
        "prenotazioni_sheet_name": PRENOTAZIONI_SHEET_NAME
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


@app.get("/whatsapp-webhook")
def whatsapp_webhook_test():
    return {
        "status": "ok",
        "message": "Webhook WhatsApp attivo",
        "openai_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL
    }


@app.post("/whatsapp-webhook")
async def whatsapp_webhook(request: Request):
    form = await request.form()

    incoming_message = str(form.get("Body", "")).strip()
    from_number = str(form.get("From", "")).strip()

    print(f"Messaggio WhatsApp ricevuto da {from_number}: {incoming_message}")

    if not incoming_message:
        return twilio_xml_response(
            "Ciao, sono l'assistente WhatsApp di Janara. Come posso aiutarla?"
        )

    try:
        reply = build_whatsapp_agent_reply(from_number, incoming_message)
    except Exception as e:
        print(f"Errore webhook WhatsApp: {str(e)}")
        reply = (
            "Mi dispiace, si è verificato un problema tecnico. "
            "Può riprovare tra poco?"
        )

    return twilio_xml_response(reply)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        "62_chat_ai:app",
        host="0.0.0.0",
        port=port
    )