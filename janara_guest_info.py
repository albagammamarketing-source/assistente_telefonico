"""Informazioni verificate per l'assistenza WhatsApp Janara.

Snapshot dai documenti Drive Janara 9_chatAI, consultati il 10/11/2026:
- infoCamere (regole generali check-in/out, indirizzi indicati)
- Janara_video_come_arrivare.xlsx (video pubblicati)
Non contiene password, serrature, codici di accesso o dati personali.
Quando cambia il materiale Drive, aggiornare questa mappatura.
"""
import re
import unicodedata
from urllib.parse import quote

STANDARD_CHECKIN = "16:00"
STANDARD_CHECKOUT = "10:30"

ROOMS = {
    "arechi": {"address": "Via Annunziata 71, Benevento", "videos": [
        ("entrance", "Video: arrivo dal civico 78", "https://drive.google.com/file/d/1-eb2AJTMj4T9OPBSQlbUzgbVxq4M91BI/view"),
        ("walking", "Video: percorso a piedi da Piazza Castello", "https://drive.google.com/file/d/100pXPGE4EHWYr6FPQ0EMA8cgmIWsUByg/view"),
    ]},
    "iside": {"address": "Via Annunziata 71, Benevento", "videos": [
        ("entrance", "Video: arrivo dal civico 78", "https://drive.google.com/file/d/1-eb2AJTMj4T9OPBSQlbUzgbVxq4M91BI/view"),
        ("walking", "Video: percorso a piedi da Piazza Castello", "https://drive.google.com/file/d/100pXPGE4EHWYr6FPQ0EMA8cgmIWsUByg/view"),
    ]},
    "santa sofia": {"address": "Via Annunziata 48, Benevento", "videos": [
        ("entrance", "Video: Casa parrocchiale", "https://drive.google.com/file/d/10G8tKYpISBLJ7dwG4Wrnz9cNfb9qlNqP/view"),
        ("walking", "Video: percorso Santa Rita–Santa Sofia", "https://drive.google.com/file/d/105o80nhMsVHWpLQNJt_jA1tlWRFOJywm/view"),
    ]},
    "traiano": {"address": "Via Annunziata 71, Benevento (Arco di Traiano)",
        "maps_url": "https://maps.app.goo.gl/SitT87r3nKhofLMc6", "videos": [
        ("driving", "Video: arrivo in auto", "https://drive.google.com/file/d/1jqZyPoybhiV-PzwsI-TtCT4NAYooJ9rH/view"),
        ("entrance", "Video: self check-in da civico 78", "https://drive.google.com/file/d/1myqre9R_zJUBArtt1LRcwtZ-jh3wExDj/view"),
        ("checkin", "Video: istruzioni self check-in", "https://drive.google.com/file/d/1whM4BxVUj6CjfzF6-5pzpVYyH3P9oCe5/view"),
    ]},
    "arco di traiano": {"alias": "traiano"},
    "port arsa": {"address": "Via Port'arsa 10, Benevento", "videos": [
        ("general", "Video: Teatro Romano – Port'Arsa", "https://drive.google.com/file/d/1nmMujPZiOhtr9Wy0zE_D8nEOR_CmfZh2/view"),
    ]},
    "duomo": {"address": "Vico 3 Triggio 10, Benevento", "videos": [
        ("general", "Video: Duomo e Teatro Romano", "https://drive.google.com/file/d/1u1mft-AJtdiifUll3Won-mlP5IiLPH-2/view"),
    ]},
    "teatro romano": {"address": "Vico 3 Triggio 10, Benevento", "videos": [
        ("general", "Video: Duomo e Teatro Romano", "https://drive.google.com/file/d/1u1mft-AJtdiifUll3Won-mlP5IiLPH-2/view"),
    ]},
    "mura longobarde": {"address": "Via Tommaso Rossi, Benevento", "videos": [
        ("general", "Video: come arrivare", "https://drive.google.com/file/d/15iO01mfCM9oCMF4AVQcdUq5bFMQPy7_l/view"),
    ]},
}

def _room_key(room):
    raw = unicodedata.normalize("NFKD", str(room or "").lower())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9 ]", " ", raw)
    raw = " ".join(raw.split())
    if "traiano" in raw:
        return "traiano"
    if "port" in raw and "arsa" in raw:
        return "port arsa"
    if "mura" in raw and "longobard" in raw:
        return "mura longobarde"
    for key in ROOMS:
        if key in raw:
            return key
    return ""

def guest_room_info(room):
    key = _room_key(room)
    value = ROOMS.get(key, {})
    if "alias" in value:
        value = ROOMS.get(value["alias"], {})
    return value

ARRIVAL_QUESTION = "Come arriverai? Rispondi «in auto» oppure «a piedi»."


def guest_room_directions(room):
    info = guest_room_info(room)
    if not info or not info.get("address"):
        return (
            "STEP 1 - Posizione: non ho una posizione verificata per questo appartamento. "
            "Contatta lo staff Janara prima di partire."
        )
    address = info["address"].split(" (")[0]
    maps_url = info.get("maps_url") or "https://www.google.com/maps/search/?api=1&query=" + quote(address)
    return (
        "📍 STEP 1 - Posizione Google Maps\n"
        f"Ecco l'indirizzo della tua struttura: {address}.\n"
        f"{maps_url}\n\n{ARRIVAL_QUESTION}"
    )


def guest_arrival_reply(room, message):
    """Risponde con il video pertinente alla richiesta dell'ospite verificato."""
    message = " ".join(re.sub(r"[^\w\s]", " ", message.lower()).split())
    info = guest_room_info(room)
    videos = info.get("videos", [])

    if re.search(r"\bself check\s*in\b", message):
        video = next(((label, url) for kind, label, url in videos if kind == "checkin"), None)
        if video:
            label, url = video
            return f"🎥 {label.removeprefix('Video: ')}\n{url}"
        return "Per le istruzioni di self check-in del tuo appartamento, contatta lo staff Janara."

    walking = bool(re.search(r"\ba piedi\b", message))
    driving = message in {"auto", "macchina", "automobile"} or bool(
        re.search(r"\bin (?:auto|macchina|automobile)\b", message)
    )
    if walking and (driving or re.search(r"\b(?:auto|macchina|automobile)\b", message)):
        return ARRIVAL_QUESTION
    if not walking and not driving:
        if re.search(r"\bvideo\b", message):
            return guest_room_directions(room)
        return ""

    mode = "walking" if walking else "driving"
    video = next(((label, url) for kind, label, url in videos if kind == mode), None)
    if video is None:
        video = next(((label, url) for kind, label, url in videos if kind == "general"), None)
    if video is None:
        video = next(((label, url) for kind, label, url in videos if kind == "entrance"), None)
    if video is None:
        return "Per un video adatto al tuo percorso, chiedi allo staff Janara."
    label, url = video
    reply = f"🎥 {label.removeprefix('Video: ')}\n{url}"
    if any(kind == "checkin" for kind, _, _ in videos):
        reply += "\n\nPer il video di accesso, scrivi «self check-in»."
    return reply
