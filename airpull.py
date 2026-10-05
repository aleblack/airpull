#!/usr/bin/env python3
# Copyright (C) 2026 aleblack, GPL-3.0-or-later, see LICENSE
import argparse
import datetime
import math
import os
import struct
import sys
import tomllib
import unicodedata
import zoneinfo
import serial
from serial.tools import list_ports

VERSION = "0.1.0"
BAUD = 115200
FTDI_VID = 0x0403
FTDI_PID = 0x6015
PAGE_SIZE = 4096
PAGE_TAIL = 2
RECORD_SIZE = 23
NUM_PAGES = 256
GPS_EPOCH = 315964800
RETRIES = 3


def nmea_frame(body):
    cs = 0
    for c in body.encode():
        cs ^= c
    return f"${body}*{cs:02X}\r\n".encode()


def query_once(ser, body):
    ser.reset_input_buffer()
    ser.write(nmea_frame(body))
    line = ser.readline().decode(errors="replace").strip()
    if not line.startswith("$") or "*" not in line:
        return None
    payload, cs = line[1:].rsplit("*", 1)
    check = 0
    for c in payload.encode():
        check ^= c
    try:
        if check != int(cs, 16):
            return None
    except ValueError:
        return None
    return payload


def query(ser, body):
    for _ in range(RETRIES):
        payload = query_once(ser, body)
        if payload is not None:
            break
    else:
        raise RuntimeError(f"No valid answer to {body}, is the instrument on and in USB mode?")
    if payload.startswith("PDGFERR"):
        raise RuntimeError(f"Instrument refused {body}")
    return payload.split(",", 1)[1] if "," in payload else ""


def get_info(ser):
    fields = [f.strip() for f in query(ser, "PDGFVID").split(",")]
    return {"model": fields[0], "firmware": fields[1], "serial": fields[2], "pilot": fields[3]}


def get_flights(ser):
    # Offsets and lengths in this protocol are decimal, not hex
    answer = query(ser, "PDGFFLL")
    count, raw = answer.split(",", 1)
    raw = bytes.fromhex(raw)
    entries = []
    for i in range(int(count) * 2):
        e = raw[i * 7:(i + 1) * 7]
        ts = struct.unpack("<I", e[:4])[0] + GPS_EPOCH
        page = e[4]
        offset = (e[5] << 8) | e[6]
        entries.append((ts, page, offset))
    flights = []
    for i in range(0, len(entries), 2):
        start, end = entries[i], entries[i + 1]
        flights.append({"start_ts": start[0], "start_page": start[1], "end_ts": end[0], "end_page": end[1]})
    return flights


def read_page(ser, page):
    data = bytes.fromhex(query(ser, f"PDGFRMM,2,{page},0,{PAGE_SIZE}"))
    if len(data) != PAGE_SIZE:
        raise RuntimeError(f"Short read on page {page}: {len(data)} bytes")
    return data[:PAGE_SIZE - PAGE_TAIL]


def download_flight(ser, flight):
    page = flight["start_page"]
    total = (flight["end_page"] - page) % NUM_PAGES + 1
    stream = b""
    for done in range(1, total + 1):
        stream += read_page(ser, page)
        print(f"  page {done}/{total}", end="\r", flush=True)
        page = (page + 1) % NUM_PAGES
    print(" " * 20, end="\r")
    return parse_records(stream, flight)


def parse_records(stream, flight):
    points = []
    for pos in range(0, len(stream) - RECORD_SIZE + 1, RECORD_SIZE):
        rec = stream[pos:pos + RECORD_SIZE]
        sats = rec[0]
        ts, lat, lon = struct.unpack("<Iff", rec[1:13])
        press_alt, vario, speed, baro_alt, gps_alt = struct.unpack(">hhHhh", rec[13:23])
        if ts == 0xFFFFFFFF:
            break
        ts += GPS_EPOCH
        if ts > flight["end_ts"] + 60 or ts < flight["start_ts"] - 60:
            break
        if not (math.isfinite(lat) and math.isfinite(lon)):
            break
        points.append({
            "ts": ts, "lat": math.degrees(lat), "lon": math.degrees(lon), "sats": sats,
            "press_alt": press_alt, "baro_alt": baro_alt, "vario": vario, "speed": speed, "gps_alt": gps_alt,
        })
    return points


def fmt_lat(deg):
    hemi = "N" if deg >= 0 else "S"
    deg = abs(deg)
    d = int(deg)
    mmm = round((deg - d) * 60000)
    if mmm >= 60000:
        d += 1
        mmm -= 60000
    return f"{d:02d}{mmm // 1000:02d}{mmm % 1000:03d}{hemi}"


def fmt_lon(deg):
    hemi = "E" if deg >= 0 else "W"
    deg = abs(deg)
    d = int(deg)
    mmm = round((deg - d) * 60000)
    if mmm >= 60000:
        d += 1
        mmm -= 60000
    return f"{d:03d}{mmm // 1000:02d}{mmm % 1000:03d}{hemi}"


def fmt_alt(alt):
    return f"{alt:05d}" if alt >= 0 else f"-{-alt:04d}"


def fmt_vario(v):
    v = max(-999, min(999, v))
    return f"{'+' if v >= 0 else '-'}{abs(v):03d}"


def clean_text(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = "".join(c for c in text if c not in "$*!\\^~").strip()
    return text[:60] or "NIL"


def config_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        user_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "airpull")
    else:
        user_dir = os.path.expanduser("~/.config/airpull")
    return [os.path.join(here, "config.toml"), os.path.join(user_dir, "config.toml")]


def load_config():
    for path in config_paths():
        if os.path.exists(path):
            with open(path, "rb") as f:
                return tomllib.load(f)
    return {}


def build_igc(points, info, cfg):
    first = datetime.datetime.fromtimestamp(points[0]["ts"], datetime.UTC)
    alt_key = "baro_alt" if cfg["altitude"] == "qnh" else "press_alt"
    alt_ref = "MSL" if cfg["altitude"] == "qnh" else "ISA"
    lines = [
        f"AXDG{info['serial'][-3:]}",
        f"HFDTEDATE:{first:%d%m%y}",
        f"HFPLTPILOTINCHARGE:{cfg['pilot']}",
        "HFFXA100",
        "HFCM2CREW2:NIL",
        f"HFGTYGLIDERTYPE:{cfg['glider']}",
        f"HFGIDGLIDERID:{cfg['glider_id']}",
        "HFDTM100DATUM:WGS-1984",
        "HFALGALTGPS:GEO",
        f"HFALPALTPRESSURE:{alt_ref}",
        f"HFRFWFIRMWAREVERSION:{info['firmware']}",
        f"HFRHWHARDWAREVERSION:{info['model']}",
        f"HFFTYFRTYPE:{info['model']}",
        "I033637SIU3840GSP4144VAR",
    ]
    for p in points:
        t = datetime.datetime.fromtimestamp(p["ts"], datetime.UTC)
        valid = "A" if p["sats"] >= cfg["min_sats"] else "V"
        lines.append(
            f"B{t:%H%M%S}{fmt_lat(p['lat'])}{fmt_lon(p['lon'])}{valid}"
            f"{fmt_alt(p[alt_key])}{fmt_alt(p['gps_alt'])}"
            f"{min(p['sats'], 99):02d}{min(p['speed'], 999):03d}{fmt_vario(p['vario'])}"
        )
    return "\r\n".join(lines) + "\r\n"


def find_ports():
    ports = [p.device for p in list_ports.comports() if p.vid == FTDI_VID and p.pid == FTDI_PID]
    if sys.platform == "darwin":
        ports = [p for p in ports if p.startswith("/dev/cu.")]
    return ports


def show_flights(flights, tz, done=()):
    print("Flights on the instrument:")
    for i, f in enumerate(flights, 1):
        start = datetime.datetime.fromtimestamp(f["start_ts"], tz)
        minutes = (f["end_ts"] - f["start_ts"]) / 60
        mark = "  (already exported)" if i in done else ""
        print(f"  {i:2d}  {start:%Y-%m-%d %H:%M}  {minutes:5.1f} min{mark}")


def flight_path(out_dir, flight, tz):
    start = datetime.datetime.fromtimestamp(flight["start_ts"], tz)
    return os.path.join(out_dir, f"{start:%Y-%m-%d-%H%M}.igc")


def main():
    ap = argparse.ArgumentParser(description="Export flights from a Digifly Air as IGC files")
    ap.add_argument("--port", help="serial port (default: found by USB id)")
    ap.add_argument("--list", action="store_true", help="only list the flights")
    ap.add_argument("--all", action="store_true", help="export every flight, overwriting existing files")
    ap.add_argument("--flight", type=int, action="append", help="flight number from --list (repeatable)")
    ap.add_argument("--out", help="output directory (default: ./flights)")
    ap.add_argument("--pilot", help="pilot name for the header (default: name stored in the instrument)")
    ap.add_argument("--glider", help="glider type for the header")
    ap.add_argument("--glider-id", help="glider id for the header")
    ap.add_argument("--timezone", help="timezone for file names and --list, e.g. Europe/Rome (default: UTC)")
    ap.add_argument("--altitude", choices=["qne", "qnh"], help="barometric altitude to write (default: qne)")
    ap.add_argument("--version", action="version", version=f"airpull {VERSION}")
    args = ap.parse_args()
    print(f"airpull {VERSION}")

    conf = load_config()
    cfg = {
        "port": args.port or conf.get("port"),
        "out_dir": os.path.expanduser(args.out or conf.get("out_dir", "flights")),
        "timezone": args.timezone or conf.get("timezone", "UTC"),
        "altitude": args.altitude or conf.get("altitude", "qne"),
        "min_sats": conf.get("min_sats", 4),
    }
    try:
        tz = zoneinfo.ZoneInfo(cfg["timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        print(f"Error: unknown timezone {cfg['timezone']} (on Windows the tzdata package is needed)")
        return 1
    if cfg["altitude"] not in ("qne", "qnh"):
        print("Error: altitude must be qne or qnh")
        return 1

    port = cfg["port"]
    if not port:
        ports = find_ports()
        if not ports:
            print("Error: instrument not found, is it on and in USB mode? Use --port to set the port by hand.")
            return 1
        if len(ports) > 1:
            print("Error: more than one candidate, pick one with --port:")
            for p in ports:
                print(f"  • {p}")
            return 1
        port = ports[0]

    try:
        ser = serial.Serial(port, BAUD, timeout=3)
        info = get_info(ser)
        flights = get_flights(ser)
        print(f"{info['model']} fw {info['firmware']} sn {info['serial']} on {port}")
        print(f"Times shown in {cfg['timezone']}")
        existing = {n for n, f in enumerate(flights, 1) if os.path.exists(flight_path(cfg["out_dir"], f, tz))}
        show_flights(flights, tz, existing)
        if args.list:
            return 0

        if args.flight:
            wanted = args.flight
        elif args.all:
            wanted = range(1, len(flights) + 1)
        else:
            wanted = [n for n in range(1, len(flights) + 1) if n not in existing]
            if not wanted:
                print("Nothing new to export.")
                return 0

        os.makedirs(cfg["out_dir"], exist_ok=True)
        cfg["pilot"] = clean_text(args.pilot or conf.get("pilot") or info["pilot"])
        cfg["glider"] = clean_text(args.glider or conf.get("glider") or "")
        cfg["glider_id"] = clean_text(args.glider_id or conf.get("glider_id") or "")
        for n in wanted:
            if n < 1 or n > len(flights):
                print(f"Skipping flight {n}: not in the list")
                continue
            flight = flights[n - 1]
            print(f"Flight {n}: downloading...")
            points = download_flight(ser, flight)
            if not points:
                print(f"  No valid points found for flight {n}")
                continue
            path = flight_path(cfg["out_dir"], flight, tz)
            with open(path, "w", newline="") as f:
                f.write(build_igc(points, info, cfg))
            print(f"  {len(points)} points -> {path}")
        ser.close()
    except (serial.SerialException, RuntimeError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
