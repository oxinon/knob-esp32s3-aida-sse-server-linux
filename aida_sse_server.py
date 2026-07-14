#!/usr/bin/env python3
"""
aida_sse_server.py
-------------------
Repliziert den AIDA64-"RemoteSensor"-SSE-Endpunkt (GET /sse) unter Linux,
mit echten CPU/GPU-Sensordaten statt AIDA64. Format wurde per curl gegen
ECHTES, laufendes AIDA64 verifiziert:

    data: Page0|{|}Simple1|CPU usage 17^{|}Simple2|CPU freq 1600^{|}...

SimpleN ist die Position in der .rslcd (1-8), NICHT die AIDA64-Sensor-ID.
Jeder Wert hat die Form "<Label> <Zahl>^" (das "^" ist Pflicht, kommt aus
dem "Show unit = ^"-Feld der .rslcd und dient als Terminator).

Reihenfolge (aus der .rslcd, ITMX/ITMY-Positionen):
    Simple1  CPU usage   (y=0)
    Simple2  CPU freq    (y=20)
    Simple3  CPU temp    (y=40)
    Simple4  CPU fan     (y=60)
    Simple5  GPU usage   (y=100)
    Simple6  GPU freq    (y=120)
    Simple7  GPU temp    (y=140)
    Simple8  GPU fan     (y=160)

Die Verbindung wird nach jeder Antwort geschlossen (Einzelantwort-Modell,
kein Dauerstreaming) - genau wie beim echten AIDA64-Webserver beobachtet.
WICHTIG: die Antwort muss SCHNELL kommen (wenige ms, siehe TIMING.md) -
der Knob gibt sonst auf, bevor die (korrekte!) Antwort ankommt.

Debug: GET / im Browser oeffnet eine Testseite mit Live-Ansicht der
geparsten Werte (nuetzlich, um das Format unabhaengig vom Knob-Display
zu verifizieren).

Abhaengigkeiten:
    sudo apt install python3-psutil lm-sensors
    sudo sensors-detect   # einmalig

Start (Port 80 braucht root):
    sudo python3 aida_sse_server.py --port 80

Test von einem anderen Rechner:
    curl -sv http://<debian-ip>/sse
"""

import argparse
import glob
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    raise SystemExit("Bitte zuerst installieren: sudo apt install python3-psutil")


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def read_cpu():
    usage = round(psutil.cpu_percent(interval=None))
    freq = psutil.cpu_freq()
    freq_mhz = round(freq.current) if freq else 0

    temp_c = 0
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower"):
            if key in temps and temps[key]:
                entry = next(
                    (t for t in temps[key] if "package" in t.label.lower() or "tctl" in t.label.lower()),
                    temps[key][0],
                )
                temp_c = round(entry.current)
                break
        if not temp_c and temps:
            first_list = next(iter(temps.values()))
            if first_list:
                temp_c = round(first_list[0].current)
    except Exception:
        pass

    # Funktioniert bei Desktop-Boards mit nct6775/it87 & Co. Bei vielen
    # Laptops (herstellerabhaengig) liefert das nichts - Lueftertacho wird
    # dann oft nur ueber den herstellereigenen Embedded Controller exponiert,
    # den Linux standardmaessig nicht ausliest. Bleibt dann bei 0, kein Bug.
    fan_rpm = 0
    try:
        fans = psutil.sensors_fans() or {}
        for entries in fans.values():
            if entries:
                fan_rpm = int(entries[0].current)
                break
    except Exception:
        pass

    return usage, freq_mhz, temp_c, fan_rpm


# ---------------------------------------------------------------------------
# GPU (AMD via sysfs, Intel via sysfs + intel_gpu_top, NVIDIA via nvidia-smi)
# ---------------------------------------------------------------------------

def _read(path, cast=str):
    try:
        with open(path) as f:
            v = f.read().strip()
        return cast(v)
    except Exception:
        return None


def read_nvidia_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,clocks.current.graphics,temperature.gpu,fan.speed",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=0.3,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        usage, clock, temp, fan = (int(float(p)) for p in parts)
        return usage, clock, temp, fan
    except Exception:
        return None


def read_amd_gpu():
    for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = f"{card_path}/device"
        vendor = _read(f"{dev}/vendor")
        if vendor != "0x1002":
            continue

        usage = _read(f"{dev}/gpu_busy_percent", int) or 0

        hwmon = glob.glob(f"{dev}/hwmon/hwmon*")

        # Takt: pp_dpm_sclk zeigt den aktuellen Takt nur zuverlaessig mit "*",
        # wenn power_dpm_force_performance_level != "auto" ist. Im (haeufigen)
        # Auto-Modus bleibt das Feld leer -> Fallback auf hwmon freq1_input.
        freq_mhz = 0
        sclk = _read(f"{dev}/pp_dpm_sclk")
        if sclk:
            for line in sclk.splitlines():
                if "*" in line:
                    m = re.search(r"(\d+)\s*Mhz", line, re.IGNORECASE)
                    if m:
                        freq_mhz = int(m.group(1))
        if not freq_mhz and hwmon:
            # freq1_input liegt in Hz, egal ob auto oder manual
            v = _read(f"{hwmon[0]}/freq1_input", int)
            if v:
                freq_mhz = round(v / 1_000_000)

        temp_c = 0
        fan_rpm = 0
        if hwmon:
            # temp1_input ist "edge" - bei manchen Karten liefert temp2/temp3
            # "junction"/"memory". Edge reicht fuer eine grobe Anzeige.
            t = _read(f"{hwmon[0]}/temp1_input", int)
            if t:
                temp_c = round(t / 1000)
            # fan1_input fehlt bei manchen Karten (0 RPM = Passiv-Modus/Zero-RPM
            # bei geringer Last ist normal, kein Fehler)
            f = _read(f"{hwmon[0]}/fan1_input", int)
            if f:
                fan_rpm = f

        return usage, freq_mhz, temp_c, fan_rpm
    return None


# Cache fuer die Intel-GPU-Auslastungsmessung (busy-Zeit in ns bzw. ms pro
# Engine, je nach Quelle). Gleiches Prinzip wie psutil.cpu_percent(interval=
# None): zwei Zeitpunkte vergleichen statt zu warten -> bleibt schnell
# (siehe PROTOCOL.md Fallstrick 3).
_intel_gpu_busy_cache = {}


def _delta_percent(cache_key, busy_seconds_now):
    """Gemeinsame Cache-Logik: liefert % Auslastung seit dem letzten Aufruf
    mit demselben cache_key, oder 0 beim allerersten Aufruf (kein
    Referenzpunkt, wie bei psutil.cpu_percent(interval=None))."""
    now = time.monotonic()
    prev = _intel_gpu_busy_cache.get(cache_key)
    _intel_gpu_busy_cache[cache_key] = (now, busy_seconds_now)
    if prev is None:
        return 0
    prev_time, prev_busy = prev
    dt = now - prev_time
    if dt <= 0:
        return 0
    usage = round(100 * (busy_seconds_now - prev_busy) / dt)
    return max(0, min(100, usage))


def _read_intel_engine_usage_sysfs(card_path):
    """Bevorzugter Weg: sysfs 'engine/<name>/busy' (ns), kein Root noetig.
    Existiert nur auf manchen Kernel/Treiber-Kombinationen - siehe
    PROTOCOL.md, Abschnitt Intel-GPU."""
    engine_dirs = sorted(glob.glob(f"{card_path}/engine/*"))
    engine_dir = next((d for d in engine_dirs if "rcs" in d.lower()), None) or (engine_dirs[0] if engine_dirs else None)
    if not engine_dir:
        return None
    busy_ns = _read(f"{engine_dir}/busy", int)
    if busy_ns is None:
        return None
    return _delta_percent(f"sysfs:{engine_dir}", busy_ns / 1_000_000_000)


def _read_intel_engine_usage_debugfs(card_index=0, engine_name="rcs0"):
    """Fallback: debugfs 'i915_engine_info' (Runtime: <N>ms pro Engine-
    Block). Braucht Root - bei uns unkritisch, der Server laeuft wegen
    Port 80 sowieso als Root."""
    path = f"/sys/kernel/debug/dri/{card_index}/i915_engine_info"
    try:
        with open(path) as f:
            text = f.read()
    except Exception:
        return None

    in_block = False
    runtime_ms = None
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            in_block = (line.strip() == engine_name)
            continue
        if in_block:
            m = re.search(r"Runtime:\s*(\d+)\s*ms", line)
            if m:
                runtime_ms = int(m.group(1))
                break

    if runtime_ms is None:
        return None
    return _delta_percent(f"debugfs:{path}:{engine_name}", runtime_ms / 1000)


def read_intel_gpu():
    for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = f"{card_path}/device"
        vendor = _read(f"{dev}/vendor")
        if vendor != "0x8086":
            continue

        card_index = int(re.search(r"card(\d+)", card_path).group(1))

        freq_mhz = 0
        for candidate in (f"{card_path}/gt_cur_freq_mhz", f"{card_path}/gt/gt0/rps_cur_freq_mhz"):
            v = _read(candidate, int)
            if v:
                freq_mhz = v
                break

        usage = _read_intel_engine_usage_sysfs(card_path)
        if usage is None:
            usage = _read_intel_engine_usage_debugfs(card_index)
        if usage is None:
            usage = 0

        return usage, freq_mhz, 0, 0  # Intel-iGPU hat meist keinen eigenen Temp/Fan-Sensor
    return None


def read_gpu():
    """Gibt (usage, freq_mhz, temp_c, fan_rpm) zurueck - erste gefundene GPU."""
    for reader in (read_nvidia_gpu, read_amd_gpu, read_intel_gpu):
        result = reader()
        if result is not None:
            return result
    return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# AIDA64-SSE-Payload bauen
# ---------------------------------------------------------------------------

def build_sse_payload():
    cpu_usage, cpu_freq, cpu_temp, cpu_fan = read_cpu()
    gpu_usage, gpu_freq, gpu_temp, gpu_fan = read_gpu()

    # Echtes, per Wireshark/curl verifiziertes Format:
    #   data: Page0|{|}Simple1|CPU usage 17^{|}Simple2|CPU freq 1600^{|}...
    # SimpleN ist die Position in der .rslcd (1-8), NICHT die AIDA64-ID.
    # Jeder Wert endet mit "^" (das ist das Terminator-Zeichen aus dem
    # "Show unit = ^"-Feld der .rslcd, keine echte Einheit).
    items = [
        ("Simple1", "CPU usage", cpu_usage),
        ("Simple2", "CPU freq", cpu_freq),
        ("Simple3", "CPU temp", cpu_temp),
        ("Simple4", "CPU fan", cpu_fan),
        ("Simple5", "GPU usage", gpu_usage),
        ("Simple6", "GPU freq", gpu_freq),
        ("Simple7", "GPU temp", gpu_temp),
        ("Simple8", "GPU fan", gpu_fan),
    ]

    body = "Page0|{|}" + "".join(
        f"{slot}|{label} {value}^{{|}}" for slot, label, value in items
    )
    return f"data: {body}\n\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

DEBUG_HTML = b"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AIDA64 RemoteSensor (1:1 Nachbau)</title>
<style>
body { background-color:#FFFFFF; padding:0; margin:0; }
</style>
</head>
<body>
<div id="page0">
<span id="Simple1" style="left:0px; top:0px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple2" style="left:0px; top:20px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple3" style="left:0px; top:40px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple4" style="left:0px; top:60px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple5" style="left:0px; top:100px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple6" style="left:0px; top:120px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple7" style="left:0px; top:140px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
<span id="Simple8" style="left:0px; top:160px; position:absolute; font-size:8pt; color:#000000; font-family:Tahoma"></span>
</div>
<script>
var source = new EventSource("/sse");
source.onmessage = function(event) {
  var s_data = event.data;
  var s_item, s_items, i_idx;
  while (s_data.indexOf("{|}") > -1) {
    i_idx = s_data.indexOf("{|}");
    s_item = s_data.substr(0, i_idx);
    s_items = s_item.split("|", 2);
    for (var n = 1; n <= 8; n++) {
      if (s_items[0] == "Simple" + n) {
        document.getElementById("Simple" + n).innerHTML = s_items[1];
      }
    }
    s_data = s_data.substr(i_idx + 3);
  }
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.command} {self.path} von {self.client_address[0]}")

    def do_GET(self):
        print(f"[{self.log_date_time_string()}] GET {self.path} von {self.client_address[0]}")

        if self.path.rstrip("/") in ("", "/debug"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(DEBUG_HTML)))
            self.end_headers()
            self.wfile.write(DEBUG_HTML)
            return

        if self.path == "/sse":
            t_start = time.monotonic()
            try:
                body = build_sse_payload()
                # WICHTIG: Byte-genau wie im Wireshark-Dump von echtem AIDA64.
                # Nur "\n" (LF), NIEMALS "\r\n" (CRLF) - die Knob-Firmware
                # sucht woertlich nach "\n\n" als Header-Ende-Marker.
                # Deshalb hier komplett OHNE send_header()/end_headers(),
                # die immer \r\n schreiben wuerden.
                response = (
                    "HTTP/1.1 200 OK\n"
                    "Content-Type: text/event-stream\n"
                    "Cache-Control: no-cache\n"
                    "Access-Control-Allow-Origin: *\n"
                    "Access-Control-Expose-Headers: *\n"
                    "Access-Control-Allow-Credentials: true\n"
                    "\n" + body
                )
                response_bytes = response.encode("utf-8")
                self.wfile.write(response_bytes)
                self.wfile.flush()
                elapsed_ms = round((time.monotonic() - t_start) * 1000)
                print(f"    -> {len(response_bytes)} Bytes gesendet in {elapsed_ms}ms: {response!r}")
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError) as e:
                print(f"    -> Verbindung abgebrochen: {e}")
            except Exception as e:
                print(f"    -> FEHLER beim Senden: {e!r}")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    print(f"AIDA64-kompatibler SSE-Server auf http://{args.host}:{args.port}/sse")
    print("Traegt Knob PC-Monitor-Adresse auf diese IP ein. Strg+C zum Beenden.\n")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Als systemd-Dienst einrichten (empfohlen für Dauerbetrieb)
# ---------------------------------------------------------------------------
# sudo cp aida_sse_server.py /opt/aida_sse_server.py
# sudo tee /etc/systemd/system/aida-sse-server.service <<'EOF'
# [Unit]
# Description=AIDA64-kompatibler SSE Sensor-Server fuer ESP32 Knob
# After=network.target
#
# [Service]
# ExecStart=/usr/bin/python3 /opt/aida_sse_server.py --port 80
# Restart=always
# User=root
#
# [Install]
# WantedBy=multi-user.target
# EOF
# sudo systemctl daemon-reload
# sudo systemctl enable --now aida-sse-server
