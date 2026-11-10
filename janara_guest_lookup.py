"""Riconoscimento prudente degli ospiti Janara da WhatsApp + ID prenotazione.

Non scrive sul foglio e non decide quali risposte inviare.
Un ID JNR da solo NON prova l'identita': il numero WhatsApp deve combaciare.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re
import unicodedata


def _norm_header(value):
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in text if not unicodedata.combining(ch)))


def normalize_phone(value):
    """Confronta numeri internazionali e cellulari italiani in modo conservativo."""
    raw = str(value or "").strip().lower()
    if raw.startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):]
    compact = re.sub(r"[\s()./\-]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    elif re.fullmatch(r"3\d{9}", compact):
        compact = "+39" + compact
    elif re.fullmatch(r"39\d{10}", compact):
        compact = "+" + compact
    return compact if re.fullmatch(r"\+[1-9]\d{7,14}", compact) else None


_MONTHS = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
}


def _date(value):
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    text = str(value or "").strip().lower()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})[- /]([a-zà-ù]{3})[- /](\d{4})", text)
    if match and match.group(2)[:3] in _MONTHS:
        try:
            return date(int(match.group(3)), _MONTHS[match.group(2)[:3]], int(match.group(1)))
        except ValueError:
            return None
    return None


def _headers(header):
    index = {_norm_header(value): i for i, value in enumerate(header)}
    aliases = {
        "phone": ("telefono", "cellulare", "whatsapp"),
        "id": ("idprenotazione", "numeroprenotazione"),
        "name": ("nomeospite", "nominativo"),
        "room": ("appartamento", "camera"),
        "arrival": ("datadiarrivo", "dataarrivo", "checkin"),
        "departure": ("datapartenza", "checkout"),
    }
    columns = {}
    for name, choices in aliases.items():
        columns[name] = next((index[a] for a in choices if a in index), None)
    if columns["id"] is None or columns["phone"] is None:
        raise ValueError("Colonne ID PRENOTAZIONE e telefono mancanti")
    return columns


def _get(row, ix):
    return str(row[ix]).strip() if ix is not None and ix < len(row) else ""


def extract_booking_id(text):
    match = re.search(r"\bJNR-[A-Z0-9-]{3,32}\b", str(text or "").upper())
    return match.group(0) if match else ""


def recognize_guest(values, whatsapp_number, message="", today=None):
    """Ritorna matched/multiple/not_found senza indovinare l'identita'.

    Richiede sempre corrispondenza del numero WhatsApp. Se piu' prenotazioni
    non scadute corrispondono, richiede un ID JNR senza mostrare dati altrui.
    """
    phone = normalize_phone(whatsapp_number)
    if not phone or not values:
        return {"status": "not_found"}
    columns = _headers(values[0])
    requested_id = extract_booking_id(message)
    today = today or date.today()
    candidates = []
    for row in values[1:]:
        if normalize_phone(_get(row, columns["phone"])) != phone:
            continue
        booking_id = _get(row, columns["id"]).upper()
        if not re.fullmatch(r"JNR-[A-Z0-9-]{3,32}", booking_id):
            continue
        arrival = _date(_get(row, columns["arrival"]))
        departure = _date(_get(row, columns["departure"]))
        # Evita il riconoscimento come ospite attivo per soggiorni storici.
        if departure and departure < today - timedelta(days=2):
            continue
        if not departure and (not arrival or arrival < today - timedelta(days=2)):
            continue
        if arrival and arrival > today + timedelta(days=365):
            continue
        candidates.append({
            "booking_id": booking_id,
            "room": _get(row, columns["room"]),
            "arrival": _get(row, columns["arrival"]),
            "departure": _get(row, columns["departure"]),
            "name": _get(row, columns["name"]),
        })
    if requested_id:
        candidates = [x for x in candidates if x["booking_id"] == requested_id]
    if len(candidates) == 1:
        return {"status": "matched", **candidates[0]}
    if len(candidates) > 1:
        return {"status": "multiple"}
    return {"status": "not_found"}
