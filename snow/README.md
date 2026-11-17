# Snow tracker

Forecast script for Ryan's nightly snow alert (scheduled task, 6 pm MT). Not part of the Kalshi bot.

`python3 snow/snow.py --out /tmp/snowout` writes `data.json` and `index.html`.

Sources: NWS forecast grid (official), MET Norway (ECMWF), Open-Meteo multi-model when not rate-limited, SNOTEL depth, NWS Salt Lake forecast discussion. No resort-reported numbers.
