"""Read the Janara guest workbook without executing workbook formulas or macros."""
from io import BytesIO
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from openpyxl import load_workbook

from janara_guest_info import guest_room_info

APPROVED = {"approvato", "approvata", "verificato", "verificata", "operativo", "operativa", "operativo regola"}
_CACHE = None
_REFRESH_AT = 0
_LOCK = Lock()


def normalize(value):
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def room_key(value):
    key = normalize(str(value or "").split(" (", 1)[0])
    return {"arco di traiano": "traiano", "portarsa": "port arsa"}.get(key, key)


def sheet_rows(book, name, headers):
    if name not in book.sheetnames:
        return
    rows = book[name].iter_rows(max_row=500, max_col=24, values_only=True)
    for row in rows:
        index = {normalize(value): i for i, value in enumerate(row) if value}
        if all(header in index for header in headers):
            break
    else:
        return
    for row in rows:
        yield tuple(str(row[index[header]] or "").strip() for header in headers)


def trusted_links(value, hosts):
    for candidate in re.findall(r'https://[^\s<>"\]]+', value):
        candidate = candidate.rstrip(").,")
        parsed = urlsplit(candidate)
        if parsed.netloc in hosts and (
            parsed.netloc not in {"www.google.com", "google.com"} or parsed.path.startswith("/maps")
        ):
            yield candidate


def video_kind(description, room, url):
    label = normalize(description)
    if re.search(r"\b(?:in auto|in macchina)\b", label):
        return "driving"
    if "a piedi" in label or (room == "santa sofia" and "percorso santa rita" in label):
        return "walking"
    if "civico" in label or "casa parrocchiale" in label:
        return "entrance"
    if "self check" in label:
        return "checkin"
    for kind, _, existing in guest_room_info(room).get("videos", ()):
        if url.split("?", 1)[0] == existing.split("?", 1)[0]:
            return kind
    if label in {"come arrivare", "video breve"} or room == "port arsa":
        return "general"
    return ""


def parse_guest_workbook(content):
    if len(content) > 5_000_000:
        raise ValueError("Janara workbook exceeds size limit")
    book = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
    try:
        if "FAQ" not in book.sheetnames or "come arrivare video" not in book.sheetnames:
            raise ValueError("Janara workbook sheets missing")
        faq = []
        for identifier, question, answer, state, scope in sheet_rows(book, "FAQ", (
            "id", "domanda ospite", "risposta prudenziale bozza", "stato", "ambito"
        )):
            if (
                re.fullmatch(r"FAQ-\d+", identifier, re.IGNORECASE)
                and normalize(state) in APPROVED and question and answer and scope
            ):
                faq.append((identifier.lower(), question[:240], answer[:950], room_key(scope)))

        wifi = {}
        for room, network, password in sheet_rows(book, "wifi", (
            "stanza ambiente", "nome rete ssid", "password wi fi"
        )):
            if room and network and password and all(value.isprintable() for value in (network, password)):
                wifi[room_key(room)] = (network, password)

        routes = {}
        single_room_maps = {}
        for apartment, description, video, maps in sheet_rows(book, "come arrivare video", (
            "appartamento", "descrizione video", "link video", "posizione maps"
        )):
            rooms = [room_key(part) for part in re.split(r"\s+e\s+", apartment, flags=re.IGNORECASE)]
            rooms = [room for room in rooms if room]
            video_url = next(trusted_links(video, {"drive.google.com"}), "")
            for room in rooms:
                route = routes.setdefault(room, {"videos": {}})
                for line in maps.splitlines():
                    url = next(trusted_links(line, {"maps.app.goo.gl", "maps.google.com", "www.google.com", "google.com"}), "")
                    if not url:
                        continue
                    label = room_key(line.split("https://", 1)[0])
                    if label == room or (not label and len(rooms) == 1):
                        route.setdefault("maps_url", url)
                        if len(rooms) == 1 and not label:
                            single_room_maps.setdefault(room, url)
                if video_url:
                    kind = video_kind(description, room, video_url)
                    if kind:
                        route["videos"].setdefault(kind, ("Video: " + (description or "indicazioni di arrivo"), video_url))

        for room, url in single_room_maps.items():
            routes[room]["maps_url"] = url
        return {"faq": faq, "wifi": wifi, "routes": routes}
    finally:
        book.close()


def get_guest_knowledge(download):
    global _CACHE, _REFRESH_AT
    with _LOCK:
        if monotonic() < _REFRESH_AT:
            return _CACHE
        try:
            _CACHE = parse_guest_workbook(download())
            _REFRESH_AT = monotonic() + 120
        except Exception as error:
            _CACHE = None
            _REFRESH_AT = monotonic() + 30
            print(f"Workbook Janara non disponibile: {type(error).__name__}")
        return _CACHE
