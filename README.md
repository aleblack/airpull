# AirPull: Digifly flight downloader

Downloads flight tracks from Digifly variometers over USB and saves them as IGC files. An open source alternative to the flight download of Digifly AirTools, which doesn't start on current macOS (Apple Silicon included) because it's a 32 bit app.

Unofficial, read the [disclaimer](#disclaimer) below before using it. The instrument must be in USB mode while it runs.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Needs Python 3.11 or newer.

## Usage

The instrument has to be in USB mode and plugged in, then:

```bash
./airpull.py                              # export the flights not exported yet
./airpull.py --list                       # what's on the instrument
./airpull.py --all                        # everything, overwriting existing files
./airpull.py --flight 3 --flight 5        # only these (numbers from --list)
./airpull.py --all --out ~/flights --pilot "Name" --glider "Ozone Rush 5"
```

On Windows run it as `python airpull.py`.

A flight counts as already exported when its file is in the output folder, so running it after every flying day only downloads the new ones.

The instrument is found by its USB id (FTDI FT230X). If that fails, or you have more than one, use `--port` (`/dev/ttyUSB0`, `COM3`, ...). On Linux your user needs to be in the `dialout` group. On Windows the FTDI driver is needed, normally Windows installs it by itself.

## Configuration

Copy `config.example.toml` to `config.toml` (next to the script, or in `~/.config/airpull/`, `%APPDATA%\airpull\` on Windows) and edit it. It's in `.gitignore`, so your name and glider stay local. Everything is optional and command line options win over the file:

- `pilot`, `glider`, `glider_id`: header fields. Accents and `$ * ! \ ^ ~` are stripped, IGC wants plain ASCII
- `out_dir`: where the files go, default `flights`
- `timezone`: used for file names and `--list`, e.g. `Europe/Rome`. Times inside the IGC are always UTC
- `altitude`: `qne` (pressure altitude, default) or `qnh` (the altitude shown on the instrument)
- `min_sats`: below this the fix is marked V
- `port`: fixed serial port

## Output

One file per flight, named after the first fix in the configured timezone (UTC by default), e.g. `2026-10-03-1242.igc`. GPS altitude is the plain GPS one. Pilot name comes from the instrument unless set in the config or with `--pilot`.

The files are not signed, there is no G record, so they are fine for logbooks and viewers but not for anything that needs a certified track. If you need one, the instrument can write a certified IGC itself on its microSD (see the manual: Logbook, long press OK on the flight), if your model has the slot.

## Tests

```bash
python -m unittest discover -s tests
```

They run against a fake instrument, no device needed.

## Tested on

Only a Digifly AirPro (it reports itself as `DIGIFLY AIR BT`) on macOS, with firmware 706a-019 and 801b-019. It should work with other Digifly models that speak the same protocol, but I haven't tried: other models and firmware versions may store flights differently. Linux and Windows are untested too. If you try it, a note on what worked or didn't is welcome.

## Disclaimer

This is a personal project. It is not affiliated with, endorsed or approved by Digifly. Digifly, AirPro, AirTools and the other product names belong to their owners and are only used to say what this works with. No code or files from the Digifly software are included here, the protocol was figured out by looking at how the instrument talks over USB.

It comes as is, without any warranty and without support. In particular:

- it only sends read commands to the instrument, but you use it at your own risk. I'm not responsible for lost data, a bricked instrument, or anything else
- a firmware update can change the protocol and break it. I don't promise to fix it, to answer issues or to keep it working. You're free to fix it yourself
- the IGC files are unsigned and not checked against any standard validator, don't use them for official claims, records or competitions

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Use it, change it and share it as you like. If you distribute a modified version you have to share its source under the same license.

Copyright (C) 2026 aleblack (https://github.com/aleblack)

## Protocol notes

Serial 115200 8N1, NMEA style frames (`$...*XX`, XOR checksum).

- `$PDGFVID` model, firmware, serial, pilot
- `$PDGFFLL` flight list: count, then start/end entries of 7 bytes each (seconds since 1980-01-06 LE, page, offset BE)
- `$PDGFRMM,2,<page>,<offset>,<length>` reads flash, pages are 4096 bytes

Numbers in the command are decimal. The firmware parses them with a naive atoi, so sending hex gives wrong lengths.

The last 2 bytes of every page are always FF and must be dropped. Records are 23 bytes and can cross a page boundary:

| bytes | field |
|-------|-------|
| 0 | satellites |
| 1-4 | seconds since 1980-01-06 (GPS epoch), LE |
| 5-8 | latitude, float LE, radians |
| 9-12 | longitude, float LE, radians |
| 13-14 | pressure altitude QNE, m, signed BE |
| 15-16 | vario, dm/s, signed BE |
| 17-18 | ground speed, km/h, BE |
| 19-20 | altitude QNH, m, signed BE (not exported) |
| 21-22 | GPS altitude, m, signed BE |

`PDGFWMM` and `PDGFRST` also exist; the script never sends them.
