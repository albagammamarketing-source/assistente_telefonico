"""Test offline: nessuna chiamata WhatsApp, Google o Twilio."""
import unittest
from datetime import date
from janara_guest_lookup import normalize_phone, extract_booking_id, recognize_guest

HEAD = ["APPARTAMENTO", "NOME OSPITE", "n. Ospiti", "DATA  DI ARRIVO",
        "", "dettagli arrivo", "DATA PARTENZA", "dettagli partenza",
        "telefono", "ID PRENOTAZIONE"]


class GuestIdentityTests(unittest.TestCase):
    def test_phone_formats(self):
        self.assertEqual(normalize_phone("whatsapp:+393351404756"), "+393351404756")
        self.assertEqual(normalize_phone("0039 335 1404756"), "+393351404756")
        self.assertEqual(normalize_phone("3351404756"), "+393351404756")
        self.assertIsNone(normalize_phone("non disponibile"))

    def test_id_hint(self):
        self.assertEqual(extract_booking_id("la mia prenotazione è jnr-000125"), "JNR-000125")

    def test_unique_guest(self):
        rows = [HEAD, ["Arechi", "Mario Rossi", "2", "15/11/2026", "", "",
                       "18/11/2026", "", "0039 335 1404756", "JNR-000125"]]
        got = recognize_guest(rows, "whatsapp:+393351404756", today=date(2026, 11, 10))
        self.assertEqual(got["status"], "matched")
        self.assertEqual(got["booking_id"], "JNR-000125")

    def test_old_booking_not_active(self):
        rows = [HEAD, ["Arechi", "Mario Rossi", "2", "15/11/2024", "", "",
                       "18/11/2024", "", "3351404756", "JNR-000125"]]
        got = recognize_guest(rows, "whatsapp:+393351404756", today=date(2026, 11, 10))
        self.assertEqual(got["status"], "not_found")

    def test_multiple_bookings_require_id(self):
        rows = [HEAD,
                ["Arechi", "A", "2", "15/11/2026", "", "", "18/11/2026", "", "3351404756", "JNR-000125"],
                ["Iside", "B", "2", "20/11/2026", "", "", "21/11/2026", "", "3351404756", "JNR-000126"]]
        self.assertEqual(recognize_guest(rows, "whatsapp:+393351404756", today=date(2026, 11, 10))["status"], "multiple")
        got = recognize_guest(rows, "whatsapp:+393351404756", "La prenotazione JNR-000126", date(2026, 11, 10))
        self.assertEqual(got["booking_id"], "JNR-000126")

    def test_id_alone_not_enough(self):
        rows = [HEAD, ["Arechi", "Mario Rossi", "2", "15/11/2026", "", "",
                       "18/11/2026", "", "3351404756", "JNR-000125"]]
        got = recognize_guest(rows, "whatsapp:+393399999999", "JNR-000125", date(2026, 11, 10))
        self.assertEqual(got["status"], "not_found")

    def test_reordered_rows_not_affect_identity(self):
        rows = [HEAD, ["Arechi", "Mario Rossi", "2", "15/11/2026", "", "",
                       "18/11/2026", "", "3351404756", "JNR-000125"]]
        rows.append(["Iside", "Other", "1", "15/11/2026", "", "", "17/11/2026", "", "3450000000", "JNR-000126"])
        self.assertEqual(recognize_guest([HEAD, rows[2], rows[1]], "3351404756", today=date(2026, 11, 10))["booking_id"], "JNR-000125")


if __name__ == "__main__":
    unittest.main()
