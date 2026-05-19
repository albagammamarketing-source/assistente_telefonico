import io
import os
from datetime import datetime, date, timedelta

import pandas as pd
import requests
import resend
from fastapi import FastAPI
from pydantic import BaseModel
from icalendar import Calendar


app = FastAPI(title="Janara Reception API")

resend.api_key = os.environ["RESEND_API_KEY"]

SPREADSHEET_ID = "1orw2D-Rxh2omVj_MOBIdIsiLf90UjsvSZrpQqKYeTys"

GID_CAMERE_CONFIG = "0"
GID_PREZZI = "415348027"


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
    url_camera: str | None = ""


def google_sheet_csv_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={gid}"


def load_sheet(gid: str) -> pd.DataFrame:
    response = requests.get(google_sheet_csv_url(gid), timeout=20)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text))


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def normalize(value: str) -> str:
    return str(value).strip().lower()


def get_booked_ranges(ical_url: str):
    response = requests.get(ical_url, timeout=20)
    response.raise_for_status()

    calendar = Calendar.from_ical(response.text)
    ranges = []

    for component in calendar.walk():
        if component.name == "VEVENT":
            start = component.get("dtstart").dt
            end = component.get("dtend").dt

            if isinstance(start, datetime):
                start = start.date()
            if isinstance(end, datetime):
                end = end.date()

            ranges.append((start, end))

    return ranges


def is_available_from_ical(ical_url: str, check_in: date, check_out: date) -> bool:
    for booked_start, booked_end in get_booked_ranges(ical_url):
        if check_in < booked_end and check_out > booked_start:
            return False
    return True


def get_total_price(camera_key: str, check_in: date, check_out: date) -> float:
    prezzi = load_sheet(GID_PREZZI)
    prezzi.columns = [str(c).strip() for c in prezzi.columns]

    if "data" not in prezzi.columns:
        raise ValueError("Nel foglio PREZZI manca la colonna 'data'.")

    if camera_key not in prezzi.columns:
        raise ValueError(f"Nel foglio PREZZI manca la colonna camera: {camera_key}")

    prezzi["data"] = pd.to_datetime(prezzi["data"], errors="coerce").dt.date

    total = 0
    current = check_in

    while current < check_out:
        row = prezzi[prezzi["data"] == current]

        if row.empty:
            raise ValueError(f"Prezzo mancante per {camera_key} in data {current}")

        price = row.iloc[0][camera_key]

        if pd.isna(price):
            raise ValueError(f"Prezzo vuoto per {camera_key} in data {current}")

        total += float(str(price).replace(",", "."))
        current += timedelta(days=1)

    return round(total, 2)


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "Janara Reception API attiva"
    }


@app.post("/check-availability")
def check_availability(req: AvailabilityRequest):
    try:
        check_in = parse_date(req.check_in)
        check_out = parse_date(req.check_out)
    except Exception:
        return {
            "available": False,
            "message": "Le date devono essere nel formato YYYY-MM-DD.",
            "room_name": "",
            "total_price": 0
        }

    if check_out <= check_in:
        return {
            "available": False,
            "message": "La data di check-out deve essere successiva al check-in.",
            "room_name": "",
            "total_price": 0
        }

    try:
        camere = load_sheet(GID_CAMERE_CONFIG)
    except Exception as e:
        return {
            "available": False,
            "message": f"Errore nel caricamento del foglio CAMERE_CONFIG: {str(e)}",
            "room_name": "",
            "total_price": 0
        }

    camere.columns = [str(c).strip() for c in camere.columns]

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
            return {
                "available": False,
                "message": f"Nel foglio CAMERE_CONFIG manca la colonna: {col}",
                "room_name": "",
                "total_price": 0
            }

    camere = camere[
        camere["attiva"].astype(str).str.upper().str.strip() == "SI"
    ]

    struttura_request = normalize(req.structure)

    camere_struttura = camere[
        camere["nome_struttura"].astype(str).str.lower().str.contains(struttura_request, na=False)
        | camere["struttura_key"].astype(str).str.lower().str.contains(struttura_request, na=False)
        | camere["citta"].astype(str).str.lower().str.contains(struttura_request, na=False)
    ]

    if camere_struttura.empty:
        return {
            "available": False,
            "message": f"Non ho trovato la struttura richiesta: {req.structure}. Puoi ripetere il nome?",
            "room_name": "",
            "total_price": 0
        }

    disponibili = []
    disponibilita_per_camera_key = {}

    for _, camera in camere_struttura.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        nome_camera = str(camera["nome_camera"]).strip()
        max_ospiti = int(camera["max_ospiti"])
        ical_url = str(camera["ical_url"]).strip()
        tipo_camera = str(camera.get("tipo_camera", "")).strip().lower()

        if req.guests > max_ospiti:
            disponibilita_per_camera_key[camera_key] = False
            continue

        if tipo_camera == "combinata":
            continue

        try:
            disponibile = is_available_from_ical(ical_url, check_in, check_out)
        except Exception:
            disponibile = False

        disponibilita_per_camera_key[camera_key] = disponibile

        if disponibile:
            try:
                total_price = get_total_price(camera_key, check_in, check_out)
            except Exception:
                total_price = 0

            disponibili.append({
                "room_name": nome_camera,
                "camera_key": camera_key,
                "total_price": total_price
            })

    camere_combinate = camere_struttura[
        camere_struttura["tipo_camera"].astype(str).str.lower().str.strip() == "combinata"
    ]

    for _, camera in camere_combinate.iterrows():
        camera_key = str(camera["camera_key"]).strip()
        nome_camera = str(camera["nome_camera"]).strip()
        max_ospiti = int(camera["max_ospiti"])

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

        if combinata_disponibile:
            try:
                total_price = get_total_price(camera_key, check_in, check_out)
            except Exception:
                total_price = 0

            disponibili.append({
                "room_name": nome_camera,
                "camera_key": camera_key,
                "total_price": total_price
            })

    if not disponibili:
        return {
            "available": False,
            "message": f"Mi dispiace, non risultano disponibilità dal {req.check_in} al {req.check_out} per {req.guests} ospiti.",
            "room_name": "",
            "total_price": 0
        }

    migliore = sorted(disponibili, key=lambda x: x["total_price"])[0]

    return {
        "available": True,
        "message": f"Sì, abbiamo disponibilità per {migliore['room_name']}. Il prezzo totale dal {req.check_in} al {req.check_out} è {migliore['total_price']} euro.",
        "room_name": migliore["room_name"],
        "camera_key": migliore["camera_key"],
        "total_price": migliore["total_price"]
    }


@app.post("/send-booking-email")
def send_booking_email(req: BookingEmailRequest):
    url_camera_html = ""

    if req.url_camera:
        url_camera_html = f"""
        <p>
            <b>Link camera / foto:</b><br>
            <a href="{req.url_camera}">{req.url_camera}</a>
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

    <p>
    Grazie,<br>
    Janara Hospitality
    </p>
    """

    resend.Emails.send({
        "from": "Janara <onboarding@resend.dev>",
        "to": req.email,
        "subject": "Riepilogo prenotazione Janara",
        "html": html
    })

    return {
        "success": True,
        "message": f"Email inviata a {req.email}"
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        "62_chat_ai:app",
        host="0.0.0.0",
        port=port
    )
