import importlib.util
import math
import os
import struct
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("airpull", os.path.join(HERE, "..", "airpull.py"))
ef = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ef)

START_UNIX = 1790000000
INFO = {"model": "DIGIFLY AIR BT", "firmware": "706a-019", "serial": "12345", "pilot": "Test"}
CFG = {"pilot": "Test", "glider": "Glider", "glider_id": "NIL", "altitude": "qne", "min_sats": 4}


def make_record(i):
    ts = START_UNIX + i * 2
    lat = math.radians(45.0 + i * 0.0001)
    lon = math.radians(7.0 + i * 0.0001)
    return (bytes([9]) + struct.pack("<Iff", ts - ef.GPS_EPOCH, lat, lon)
            + struct.pack(">hhHhh", 400 + i, i % 20 - 10, 30, 475 + i, 480 + i))


def make_flash(records, first_page):
    stream = b"".join(records)
    usable = ef.PAGE_SIZE - ef.PAGE_TAIL
    flash = {}
    page = first_page
    for pos in range(0, len(stream), usable):
        chunk = stream[pos:pos + usable]
        flash[page] = chunk + b"\xff" * (ef.PAGE_SIZE - len(chunk))
        page = (page + 1) % ef.NUM_PAGES
    return flash, (page - 1) % ef.NUM_PAGES


class FakeSerial:
    def __init__(self, flash, flights=b"", count=0, bad_first=False):
        self.flash = flash
        self.flights = flights
        self.count = count
        self.bad_first = bad_first
        self.reply = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        body = data.decode().strip()[1:].rsplit("*", 1)[0]
        parts = body.split(",")
        if parts[0] == "PDGFVID":
            out = "PDGFVID,DIGIFLY AIR BT,706a-019,12345,Pilot  "
        elif parts[0] == "PDGFFLL":
            out = f"PDGFFLL,{self.count},{self.flights.hex().upper()}"
        elif parts[0] == "PDGFRMM":
            page, offset, length = int(parts[2]), int(parts[3]), int(parts[4])
            out = "PDGFRMM," + self.flash[page][offset:offset + length].hex().upper()
        else:
            out = "PDGFERR"
        frame = ef.nmea_frame(out)
        if self.bad_first:
            self.bad_first = False
            frame = frame.replace(b"*", b"*0")
        self.reply = frame

    def readline(self):
        return self.reply


class Formatting(unittest.TestCase):
    def test_frame_checksum(self):
        self.assertEqual(ef.nmea_frame("PDGFVID"), b"$PDGFVID*4E\r\n")

    def test_lat_lon(self):
        self.assertEqual(ef.fmt_lat(41.591231), "4135474N")
        self.assertEqual(ef.fmt_lat(-33.5), "3330000S")
        self.assertEqual(ef.fmt_lon(12.957416), "01257445E")
        self.assertEqual(ef.fmt_lon(-0.5), "00030000W")

    def test_minutes_rollover(self):
        self.assertEqual(ef.fmt_lat(10.999999999), "1100000N")

    def test_altitude(self):
        self.assertEqual(ef.fmt_alt(5), "00005")
        self.assertEqual(ef.fmt_alt(-9), "-0009")
        self.assertEqual(ef.fmt_alt(1690), "01690")

    def test_vario(self):
        self.assertEqual(ef.fmt_vario(-1), "-001")
        self.assertEqual(ef.fmt_vario(12), "+012")
        self.assertEqual(ef.fmt_vario(5000), "+999")

    def test_clean_text(self):
        self.assertEqual(ef.clean_text("Zanù $Test*"), "Zanu Test")
        self.assertEqual(ef.clean_text(""), "NIL")
        self.assertEqual(len(ef.clean_text("x" * 100)), 60)


class Instrument(unittest.TestCase):
    def setUp(self):
        self.records = [make_record(i) for i in range(400)]
        # starts on page 254 so the flight wraps around the 256 pages
        self.flash, self.end_page = make_flash(self.records, 254)
        entry = lambda ts, page, off: struct.pack("<I", ts - ef.GPS_EPOCH) + bytes([page]) + struct.pack(">H", off)
        flights = entry(START_UNIX, 254, 0) + entry(START_UNIX + 798, self.end_page, 0)
        self.ser = FakeSerial(self.flash, flights, 1)

    def test_info(self):
        info = ef.get_info(self.ser)
        self.assertEqual(info["serial"], "12345")
        self.assertEqual(info["pilot"], "Pilot")

    def test_flight_list_uses_gps_epoch(self):
        flights = ef.get_flights(self.ser)
        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0]["start_ts"], START_UNIX)
        self.assertEqual(flights[0]["start_page"], 254)
        self.assertEqual(flights[0]["end_page"], self.end_page)

    def test_download_wraps_and_drops_page_tail(self):
        flight = ef.get_flights(self.ser)[0]
        points = ef.download_flight(self.ser, flight)
        self.assertEqual(self.end_page, 0)
        self.assertEqual(len(points), 400)
        self.assertEqual(points[0]["ts"], START_UNIX)
        self.assertEqual(points[399]["ts"], START_UNIX + 798)
        self.assertAlmostEqual(points[250]["lat"], 45.025, places=4)
        self.assertEqual(points[250]["press_alt"], 650)

    def test_retry_on_bad_checksum(self):
        self.ser.bad_first = True
        self.assertEqual(ef.get_info(self.ser)["model"], "DIGIFLY AIR BT")

    def test_gives_up_after_retries(self):
        class Silent(FakeSerial):
            def write(self, data):
                self.reply = b""
        with self.assertRaises(RuntimeError):
            ef.get_info(Silent({}))

    def test_igc_output(self):
        flight = ef.get_flights(self.ser)[0]
        points = ef.download_flight(self.ser, flight)
        igc = ef.build_igc(points, INFO, CFG)
        lines = igc.split("\r\n")
        self.assertEqual(lines[0], "AXDG345")
        self.assertIn("HFPLTPILOTINCHARGE:Test", lines)
        self.assertIn("HFALPALTPRESSURE:ISA", lines)
        b_lines = [l for l in lines if l.startswith("B")]
        self.assertEqual(len(b_lines), 400)
        self.assertTrue(all(len(l) == 44 for l in b_lines))
        self.assertEqual(b_lines[0][:7], "B141320")

    def test_qnh_option(self):
        flight = ef.get_flights(self.ser)[0]
        points = ef.download_flight(self.ser, flight)
        igc = ef.build_igc(points, INFO, dict(CFG, altitude="qnh"))
        self.assertIn("HFALPALTPRESSURE:MSL", igc)
        first_b = [l for l in igc.split("\r\n") if l.startswith("B")][0]
        self.assertEqual(first_b[25:30], "00475")


if __name__ == "__main__":
    unittest.main()
