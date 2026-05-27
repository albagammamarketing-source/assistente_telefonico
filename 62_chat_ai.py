import io
import os
import time
import uuid
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional

import pandas as pd
import requests
import resend
from fastapi import FastAPI
from pydantic import BaseModel, EmailStr
from icalendar import Calendar


app = FastAPI(title="Janara Reception API")


# =========================
# CONFIGURAZIONE
# =========================

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

RESEND_FROM = os.environ.get("RESEND_FROM", "Janara <info@janara.net>")

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID",
    "1orw2D-Rxh2omVj_MOBIdIsiLf90UjsvSZrpQqKYeTys"
)

GID_CAMERE_CONFIG = os.environ.get("GID_CAMERE_CONFIG", "0")
GID_PREZZI = os.environ.get("GID_PREZZI", "415348027")

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "8"))

SHEET_CACHE_SECONDS = int(os.environ.get("SHEET_CACHE_SECONDS", "300"))
ICAL_CACHE_SECONDS = int(os.environ.get("ICAL_CACHE_SECONDS", "120"))

MAX_ICAL_WORKERS = int(os.environ.get("MAX_ICAL_WORKERS", "6"))

# SumUp
SUMUP_API_KEY = os.environ.get("SUMUP_API_KEY", "")
SUMUP_MERCHANT_CODE = os.environ.get("SUMUP_MERCHANT_CODE", "")
SUMUP_CURRENCY = os.environ.get("SUMUP_CURRENCY", "EUR")
SUMUP_REDIRECT_URL = os.environ.get("SUMUP_REDIRECT_URL", "")

SUMUP_API_BASE_URL = os.environ.get(
    "SUMUP_API_BASE_URL",
    "https://api.sumup.com"
)


http = requests.Session()

_sheet_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
_ical_cache: Dict[str, Tuple[float, List[Tuple[date, date]]]] = {}


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
    email: EmailStr
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
    email: Optional[EmailStr] = None
    telefono: Optional[str] = ""
    struttura: str
    camera: str
    check_in: str
    check_out: str
    ospiti: int
    totale: float


class SumupCheckoutStatusRequest(BaseModel):
    checkout_id: str


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

    checkout_reference = f"JANARA-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

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

    return {
        "success": True,
        "message": "Link pagamento SumUp creato correttamente.",
        "payment_link": payment_link,
        "checkout_id": data.get("id", ""),
        "checkout_reference": checkout_reference,
        "status": data.get("status", ""),
        "sumup_response": data
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
        "sumup_currency": SUMUP_CURRENCY
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

        return {
            "success": True,
            "message": "Stato checkout recuperato correttamente.",
            "checkout_id": data.get("id", ""),
            "status": data.get("status", ""),
            "sumup_response": data
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore controllo stato SumUp: {str(e)}",
            "status": ""
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

    payment_html = ""

    if req.payment_link:
        payment_html = f"""
        <h3>Pagamento online</h3>

        <p>
        Per confermare la prenotazione può procedere al pagamento dal seguente link sicuro SumUp:
        </p>

        <p>
            <a href="{req.payment_link}" 
               style="background-color:#111;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;display:inline-block;">
               Paga ora con SumUp
            </a>
        </p>

        <p>
        Link pagamento:<br>
        <a href="{req.payment_link}">{req.payment_link}</a>
        </p>

        <p>
        La prenotazione sarà confermata solo dopo la ricezione del pagamento.
        </p>
        """
    else:
        payment_html = f"""
        <h3>Istruzioni pagamento</h3>

        <p>
        Per confermare la prenotazione è necessario effettuare il pagamento tramite bonifico bancario.
        </p>

        <p>
        <b>Intestatario:</b> Gabriella Aucone<br>
        <b>IBAN:</b> IT20X0760115000001056847310<br>
        <b>Causale:</b> Soggiorno {req.nome}
        </p>

        <p>
        Dopo il pagamento riceverà conferma definitiva della prenotazione.
        </p>
        """

    html = f"""
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

    try:
        email_response = resend.Emails.send({
            "from": RESEND_FROM,
            "to": [str(req.email)],
            "subject": "Riepilogo prenotazione Janara",
            "html": html
        })

        print(email_response)

        return {
            "success": True,
            "message": f"Email inviata a {req.email}"
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Errore invio email: {str(e)}"
        }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        "62_chat_ai:app",
        host="0.0.0.0",
        port=port
    )
