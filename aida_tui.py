#!/usr/bin/env python3
"""
aida_tui.py — AIDA64 RemoteSensor Server, htop/nvtop-artiges Terminal-Dashboard

Startet denselben AIDA64-kompatiblen SSE-Server wie aida_sse_server.py
(die komplette Sensor- und Protokoll-Logik wird von dort importiert und
NICHT verändert) und zeigt zusätzlich live im Terminal:

    - ob der ESP32-Knob gerade verbunden ist (Zeitpunkt der letzten
      erfolgreichen /sse-Anfrage, Client-IP, Antwortzeit)
    - die aktuellen CPU-/GPU-Sensorwerte (gleiche Quelle wie das, was an
      den Knob gesendet wird)
    - eine kurze Historie der letzten Anfragen (Zeit, IP, Dauer, Status)

Der eigentliche Webserver läuft dabei unverändert im Hintergrund; dieses
Skript ist nur eine alternative, interaktive "Frontend"-Sicht darauf -
im Stil von hmradio.py / dockerdash.py / news.py.

WICHTIG: Während curses aktiv ist, darf NICHTS mit print() auf stdout
geschrieben werden (das würde die Anzeige zerstören) - deshalb wird
hier ein eigener, stiller Handler verwendet, der Ereignisse in einen
Monitor statt auf die Konsole schreibt.

Bedienung:
    r     Sensoren sofort aktualisieren
    c     Anfragen-Historie leeren
    q     Beenden (stoppt auch den Server)

Start (Port 80 braucht root, wie beim Original):
    sudo python3 aida_tui.py --port 80
"""
import argparse
import collections
import curses
import ipaddress
import os
import socket
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aida_sse_server as core

try:
    from common import die
except ImportError:
    def die(msg, code=1):
        print(f"[FEHLER] {msg}", file=sys.stderr)
        sys.exit(code)


# ─────────────────────────────────────────────────────────
# GPU-Anbieter mitverfolgen (read_gpu() in aida_sse_server.py gibt nur
# die Werte zurück, nicht welcher Reader zugeschlagen hat - für die
# Anzeige ist das aber nützlich).
# ─────────────────────────────────────────────────────────

def read_gpu_labeled():
    for reader, label in (
        (core.read_nvidia_gpu, "NVIDIA"),
        (core.read_amd_gpu, "AMD"),
        (core.read_intel_gpu, "Intel"),
    ):
        result = reader()
        if result is not None:
            return label, result
    return "–", (0, 0, 0, 0)


# ─────────────────────────────────────────────────────────
# Hintergrund-Poller für die Sensor-Anzeige (unabhängig davon, ob/wann
# der Knob gerade abfragt - zeigt immer den aktuellen Live-Wert).
# ─────────────────────────────────────────────────────────

class SensorMonitor:
    def __init__(self, interval=1.5):
        self.interval = interval
        self.lock = threading.Lock()
        self.cpu = (0, 0, 0, 0)
        self.gpu_label = "–"
        self.gpu = (0, 0, 0, 0)
        self.last_update = 0.0
        self._stop = threading.Event()
        self._force = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._force.set()

    def refresh_now(self):
        self._force.set()

    def _loop(self):
        while not self._stop.is_set():
            self._poll()
            self._force.wait(timeout=self.interval)
            self._force.clear()

    def _poll(self):
        try:
            cpu = core.read_cpu()
        except Exception:
            cpu = (0, 0, 0, 0)
        try:
            label, gpu = read_gpu_labeled()
        except Exception:
            label, gpu = "–", (0, 0, 0, 0)
        with self.lock:
            self.cpu = cpu
            self.gpu_label = label
            self.gpu = gpu
            self.last_update = time.time()

    def snapshot(self):
        with self.lock:
            return self.cpu, self.gpu_label, self.gpu, self.last_update


# ─────────────────────────────────────────────────────────
# Verbindungs-Monitor: was hat der Knob zuletzt tatsächlich abgerufen?
# ─────────────────────────────────────────────────────────

class ConnectionMonitor:
    def __init__(self, history_len=10):
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.total_requests = 0
        self.errors = 0
        self.last_time = None
        self.last_ip = None
        self.last_elapsed_ms = None
        self.last_payload = None
        self.history = collections.deque(maxlen=history_len)

    def record(self, ip, elapsed_ms, payload, ok=True, note=""):
        with self.lock:
            self.total_requests += 1
            if not ok:
                self.errors += 1
            else:
                self.last_time = time.time()
                self.last_ip = ip
                self.last_elapsed_ms = elapsed_ms
                self.last_payload = payload
            self.history.appendleft(
                (time.strftime("%H:%M:%S"), ip or "–", elapsed_ms, ok, note)
            )

    def clear_history(self):
        with self.lock:
            self.history.clear()

    def snapshot(self):
        with self.lock:
            return (
                self.total_requests, self.errors, self.last_time, self.last_ip,
                self.last_elapsed_ms, self.last_payload, list(self.history),
            )


# ─────────────────────────────────────────────────────────
# Zugriff auf einen IP-Bereich einschränken
# ─────────────────────────────────────────────────────────
# TCP kann nur an EINE Adresse binden, nicht an einen "Bereich" - für
# "nur aus meinem LAN erreichbar" ist deshalb eine Allow-Liste (CIDR-
# Netze/Einzel-IPs) der richtige Mechanismus: der Server bindet ganz
# normal (z.B. weiterhin an 0.0.0.0), prüft aber bei jeder Anfrage die
# Client-IP dagegen und weist alles andere mit 403 ab.

def parse_allow_list(spec):
    nets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print(f"Warnung: '{part}' ist kein gültiges IP/CIDR - wird ignoriert.", file=sys.stderr)
    return nets


def ip_allowed(ip, allow_nets):
    if not allow_nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in allow_nets)


# ─────────────────────────────────────────────────────────
# Stiller HTTP-Handler (identische Wire-Logik wie aida_sse_server.py,
# nur ohne print() - stattdessen Meldungen an den ConnectionMonitor)
# ─────────────────────────────────────────────────────────

def make_handler(monitor, allow_nets=None):
    class QuietHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # kein stdout - würde curses zerstören

        def do_GET(self):
            client_ip = self.client_address[0]
            if not ip_allowed(client_ip, allow_nets):
                monitor.record(client_ip, None, None, ok=False, note="blockiert (--allow)")
                self.send_response(403)
                self.end_headers()
                return

            if self.path.rstrip("/") in ("", "/debug"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(core.DEBUG_HTML)))
                self.end_headers()
                self.wfile.write(core.DEBUG_HTML)
                return

            if self.path == "/sse":
                t_start = time.monotonic()
                try:
                    body = core.build_sse_payload()
                    # Byte-genau wie im Original: nur "\n", kein send_header()/
                    # end_headers() (die würden \r\n schreiben) - siehe
                    # aida_sse_server.py für die Begründung.
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
                    elapsed_ms = round((time.monotonic() - t_start) * 1000, 1)
                    monitor.record(client_ip, elapsed_ms, body.strip(), ok=True)
                    self.close_connection = True
                except (BrokenPipeError, ConnectionResetError) as e:
                    monitor.record(client_ip, None, None, ok=False, note=str(e))
                except Exception as e:
                    monitor.record(client_ip, None, None, ok=False, note=repr(e))
            else:
                monitor.record(client_ip, None, None, ok=False, note=f"404 {self.path}")
                self.send_response(404)
                self.end_headers()

    return QuietHandler


class BoundThreadingHTTPServer(ThreadingHTTPServer):
    """Wie ThreadingHTTPServer, kann den Socket zusätzlich per
    SO_BINDTODEVICE an eine bestimmte Netzwerkschnittstelle binden
    (z.B. 'eth0'), statt nur an eine IP-Adresse. Linux-spezifisch,
    braucht root - üblicherweise sowieso vorhanden (Port 80)."""

    def __init__(self, server_address, handler_cls, bind_iface=None):
        self.bind_iface = bind_iface
        super().__init__(server_address, handler_cls)

    def server_bind(self):
        if self.bind_iface:
            so_bindtodevice = getattr(socket, "SO_BINDTODEVICE", 25)  # 25 = Linux-Fallback
            try:
                self.socket.setsockopt(
                    socket.SOL_SOCKET, so_bindtodevice, (self.bind_iface + "\0").encode()
                )
            except OSError as e:
                raise RuntimeError(
                    f"Konnte nicht an Schnittstelle '{self.bind_iface}' binden "
                    f"(nur Linux, root nötig, Interface muss existieren): {e}"
                )
        super().server_bind()


def start_server(host, port, monitor, bind_iface=None, allow_nets=None):
    handler_cls = make_handler(monitor, allow_nets)
    server = BoundThreadingHTTPServer((host, port), handler_cls, bind_iface=bind_iface)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread




# ─────────────────────────────────────────────────────────
# curses Basis-Widgets (im Stil der anderen ox-*/aida-Tools)
# ─────────────────────────────────────────────────────────

def safe_addstr(win, y, x, text, attr=0):
    """addstr, das curses.error verschluckt - z.B. wenn nach einem
    Terminal-Resize eine Zeile (kurzzeitig, bis zum nächsten Redraw)
    über den neuen, kleineren Fensterrand hinausragt. Das bekannte
    ncurses-Verhalten ist, dass genau das einen Fehler wirft (selbst
    fürs zuletzt beschriebene Zeichen unten rechts), obwohl der Text
    inhaltlich passt - deshalb wird hier bewusst PRO ZEILE gefangen
    statt einen ganzen Block in ein try/except zu packen (sonst würde
    eine einzelne zu lange Zeile alle nachfolgenden Zeilen mit
    verschlucken)."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


# ── Breiten-bewusste String-Helfer ──────────────────────────────────────
# Rein zur Robustheit bei schmalen/verkleinerten Terminals: schneidet und
# pad'det nach TATSÄCHLICHER Anzeigebreite (Umlaute/°/einfache Symbole
# zählen 1 Spalte, es kommen hier aber ohnehin keine Breitzeichen vor -
# die Helfer kosten nichts und machen die Größenrechnung konsistent mit
# den anderen ox-*-Tools).

def _char_width(ch):
    cp = ord(ch)
    if cp == 0:
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s):
    return sum(_char_width(ch) for ch in s)


def truncate_to_width(s, width):
    """Schneidet s auf höchstens `width` Terminal-Spalten ab (statt nur
    `width` Zeichen) - wichtig, damit eine Zeile nie über den (ggf. nach
    einem Resize kleineren) Fensterrand hinausragt."""
    out = []
    w = 0
    for ch in s:
        cw = _char_width(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def pad_to_width(s, width):
    return s + " " * max(0, width - display_width(s))


def draw_box(win, y, x, h, w, title=None, border_pair=3):
    try:
        win.addstr(y, x, "┌" + "─" * (w - 2) + "┐", curses.color_pair(border_pair))
        for i in range(1, h - 1):
            win.addstr(y + i, x, "│", curses.color_pair(border_pair))
            win.addstr(y + i, x + w - 1, "│", curses.color_pair(border_pair))
        win.addstr(y + h - 1, x, "└" + "─" * (w - 2) + "┘", curses.color_pair(border_pair))
        if title:
            win.addstr(y, x + 2, f" {title} ", curses.color_pair(border_pair) | curses.A_BOLD)
    except curses.error:
        pass


def draw_meter(win, y, x, bar_w, pct):
    """htop-artiger Meter-Balken (grün/gelb/rot je nach %)."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * bar_w)
    safe_addstr(win, y, x, "[", curses.color_pair(3))
    for i in range(bar_w):
        p = (i / bar_w) * 100
        ch = "|" if i < filled else " "
        pair = 5 if p < 60 else (6 if p < 85 else 7)
        safe_addstr(win, y, x + 1 + i, ch, curses.color_pair(pair) | curses.A_BOLD)
    safe_addstr(win, y, x + 1 + bar_w, "]", curses.color_pair(3))


def temp_pair(temp_c):
    if not temp_c:
        return 8
    if temp_c < 60:
        return 5
    if temp_c < 80:
        return 6
    return 7


def format_age(seconds):
    if seconds is None:
        return "–"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ─────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────

def connection_state(last_time, timeout_s):
    """Grobe Herleitung des Verbindungsstatus: der Knob fragt periodisch
    ab und öffnet dabei jedes Mal eine neue Verbindung (kein Dauer-
    Streaming, siehe aida_sse_server.py). 'Verbunden' heißt hier also:
    innerhalb der letzten `timeout_s` Sekunden kam eine gültige
    /sse-Anfrage an."""
    if last_time is None:
        return "WARTE AUF KNOB", 6
    age = time.time() - last_time
    if age <= timeout_s:
        return "VERBUNDEN", 2
    return "GETRENNT (TIMEOUT)", 7


# Mindestgröße, ab der das Layout garantiert ohne Überlappungen passt
# (siehe Berechnung in draw_dashboard: feste Kopf-Boxen + minimale
# Historien-Box + 2-zeiliger Footer + Abstandszeilen). Darunter wird
# nur ein Hinweis angezeigt statt eines kaputt zusammengequetschten
# Layouts - das ist robuster als jede Box einzeln gegen sehr kleine
# Terminals abzusichern.
MIN_W, MIN_H = 72, 26


def draw_dashboard(stdscr, host, port, server_start, sensors, conn, timeout_s, status_msg,
                    bind_iface=None, allow_nets=None):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    if w < MIN_W or h < MIN_H:
        msg = f"Terminal zu klein ({w}x{h}) - mindestens {MIN_W}x{MIN_H} nötig. Bitte vergrößern ..."
        safe_addstr(stdscr, min(1, max(h - 1, 0)), 0, truncate_to_width(msg, w), curses.color_pair(6) | curses.A_BOLD)
        stdscr.refresh()
        return

    cpu, gpu_label, gpu, sensors_updated = sensors
    total_req, errors, last_time, last_ip, last_ms, last_payload, history = conn

    state_txt, state_pair = connection_state(last_time, timeout_s)
    header_left = f" AIDA64-SENSOR-SERVER · {host}:{port}"
    header = pad_to_width(header_left, w - display_width(state_txt) - 2) + state_txt + " "
    safe_addstr(stdscr, 0, 0, truncate_to_width(header, w), curses.color_pair(1) | curses.A_BOLD)

    margin = 1
    gap = 2
    avail_w = max(w - 3, 20)
    left_w = max(avail_w // 2, 34)
    right_x = margin + left_w + gap
    right_w = max(avail_w - left_w - gap, 30)

    # ── Footer-Höhe VORAB berechnen (wie in meshchat.py): die Historien-
    # Box weiter unten schrumpft, statt dass der Footer aus Versehen bis
    # an oder über den unteren Fensterrand gedrückt wird. Genau DAS war
    # das gemeldete Problem ("Menü unten am Rand zu tief") - ncurses
    # wirft beim Beschreiben der letzten Bildschirmzeile/-spalte
    # (bekannter ncurses-Rand-/Resize-Effekt) einen curses.error, wenn
    # dort ohne Abstandszeile direkt hingeschrieben wird. Eine Leerzeile
    # als Puffer über dem Footer verhindert das zuverlässig.
    footer_lines = [
        truncate_to_width(status_msg, w - 4),
        "r Sensoren aktualisieren   c Historie leeren   q Beenden",
    ]
    footer_total = min(len(footer_lines) + 1, h - 3)  # +1 Abstandszeile über dem Fensterrand
    footer_total = max(footer_total, 2)
    footer_y = h - footer_total

    y = 2
    box_h = 8

    # ── Box: Knob-Verbindung ──
    draw_box(stdscr, y, margin, box_h, left_w, title="Knob-Verbindung")
    safe_addstr(stdscr, y + 1, margin + 2, f"[{state_txt}]", curses.color_pair(state_pair) | curses.A_BOLD)
    since_txt = format_age(time.time() - last_time) if last_time else "–"
    safe_addstr(stdscr, y + 2, margin + 2, f"Letzte Anfrage vor   {since_txt}", curses.color_pair(8))
    safe_addstr(stdscr, y + 3, margin + 2, f"Client-IP            {last_ip or '–'}", curses.color_pair(8))
    ms_txt = f"{last_ms} ms" if last_ms is not None else "–"
    safe_addstr(stdscr, y + 4, margin + 2, f"Letzte Antwortzeit   {ms_txt}", curses.color_pair(8))
    safe_addstr(stdscr, y + 5, margin + 2, f"Anfragen gesamt      {total_req}  (Fehler: {errors})", curses.color_pair(8))
    payload_preview = (last_payload or "–")[:left_w - 4]
    safe_addstr(stdscr, y + 6, margin + 2, payload_preview, curses.color_pair(8) | curses.A_DIM)

    # ── Box: Server ──
    draw_box(stdscr, y, right_x, box_h, right_w, title="Server")
    uptime_txt = format_age(time.time() - server_start)
    safe_addstr(stdscr, y + 1, right_x + 2, f"Adresse       http://{host}:{port}/sse", curses.color_pair(8))
    safe_addstr(stdscr, y + 2, right_x + 2, f"Laufzeit      {uptime_txt}", curses.color_pair(8))
    safe_addstr(stdscr, y + 3, right_x + 2, f"Debug-Ansicht http://{host}:{port}/", curses.color_pair(8) | curses.A_DIM)
    age = time.time() - sensors_updated if sensors_updated else 0
    safe_addstr(stdscr, y + 4, right_x + 2, f"Sensoren     vor {age:.1f}s aktualisiert", curses.color_pair(8) | curses.A_DIM)
    if bind_iface:
        safe_addstr(stdscr, y + 5, right_x + 2, f"Interface     {bind_iface}", curses.color_pair(8))
    elif allow_nets:
        allow_txt = ", ".join(str(n) for n in allow_nets)
        safe_addstr(stdscr, y + 5, right_x + 2, truncate_to_width(f"Zugriff nur   {allow_txt}", right_w - 4), curses.color_pair(8))
    safe_addstr(stdscr, y + 6, right_x + 2, f"Verbindungs-Timeout  {timeout_s:.0f}s", curses.color_pair(8) | curses.A_DIM)

    # ── Box: CPU ──
    y += box_h + 1
    box_h2 = 6
    draw_box(stdscr, y, margin, box_h2, left_w, title="CPU")
    cpu_usage, cpu_freq, cpu_temp, cpu_fan = cpu
    safe_addstr(stdscr, y + 1, margin + 2, "Auslastung", curses.color_pair(8))
    draw_meter(stdscr, y + 1, margin + 14, max(left_w - 24, 8), cpu_usage)
    safe_addstr(stdscr, y + 1, margin + 16 + max(left_w - 24, 8), f"{cpu_usage:>3}%", curses.color_pair(8))
    safe_addstr(stdscr, y + 3, margin + 2, f"Takt      {cpu_freq:>5} MHz", curses.color_pair(8))
    safe_addstr(stdscr, y + 4, margin + 2, "Temperatur", curses.color_pair(8))
    safe_addstr(stdscr, y + 4, margin + 14, f"{cpu_temp:>3} °C", curses.color_pair(temp_pair(cpu_temp)) | curses.A_BOLD)
    fan_txt = f"{cpu_fan} RPM" if cpu_fan else "– (nicht auslesbar)"
    safe_addstr(stdscr, y + 4, margin + 26, f"Lüfter {fan_txt}", curses.color_pair(8) | curses.A_DIM)

    # ── Box: GPU ──
    draw_box(stdscr, y, right_x, box_h2, right_w, title=f"GPU ({gpu_label})")
    gpu_usage, gpu_freq, gpu_temp, gpu_fan = gpu
    safe_addstr(stdscr, y + 1, right_x + 2, "Auslastung", curses.color_pair(8))
    draw_meter(stdscr, y + 1, right_x + 14, max(right_w - 24, 8), gpu_usage)
    safe_addstr(stdscr, y + 1, right_x + 16 + max(right_w - 24, 8), f"{gpu_usage:>3}%", curses.color_pair(8))
    safe_addstr(stdscr, y + 3, right_x + 2, f"Takt      {gpu_freq:>5} MHz", curses.color_pair(8))
    safe_addstr(stdscr, y + 4, right_x + 2, "Temperatur", curses.color_pair(8))
    safe_addstr(stdscr, y + 4, right_x + 14, f"{gpu_temp:>3} °C", curses.color_pair(temp_pair(gpu_temp)) | curses.A_BOLD)
    gfan_txt = f"{gpu_fan} RPM" if gpu_fan else "– (nicht auslesbar)"
    safe_addstr(stdscr, y + 4, right_x + 26, f"Lüfter {gfan_txt}", curses.color_pair(8) | curses.A_DIM)

    # ── Box: Anfragen-Historie (füllt den Rest bis zur Footer-Leerzeile) ──
    y += box_h2 + 1
    history_top = y
    content_bottom = max(history_top + 5, footer_y - 1)   # 1-Zeile-Abstand über dem Footer
    list_h = content_bottom - history_top
    box_w = left_w + gap + right_w
    draw_box(stdscr, y, margin, list_h, box_w, title="Letzte Anfragen")
    header_row = f"   {'Zeit':<10}{'Client-IP':<16}{'Dauer':<10}{'Status'}"
    safe_addstr(stdscr, y + 1, margin + 1, truncate_to_width(header_row, box_w - 2), curses.color_pair(8) | curses.A_UNDERLINE)

    visible_rows = max(list_h - 3, 0)
    if not history:
        safe_addstr(stdscr, y + 3, margin + 3, "Noch keine Anfrage vom Knob eingegangen.", curses.color_pair(8) | curses.A_DIM)
    else:
        for i, (ts, ip, ms, ok, note) in enumerate(history[:visible_rows]):
            row = y + 2 + i
            ms_txt = f"{ms} ms" if ms is not None else "–"
            status_txt = "OK" if ok else f"FEHLER {note}"
            pair = curses.color_pair(2) if ok else curses.color_pair(7)
            line = f" {ts:<10}{ip:<16}{ms_txt:<10}{status_txt}"
            safe_addstr(stdscr, row, margin + 1, pad_to_width(truncate_to_width(line, box_w - 2), box_w - 2), pair)

    # ── Footer: an der vorab reservierten Position, mit Leerzeile Puffer
    # über dem Fensterrand (statt direkt in der letzten Zeile zu landen). ──
    safe_addstr(stdscr, footer_y, 2, footer_lines[0], curses.color_pair(6))
    if footer_total >= 2:
        safe_addstr(stdscr, footer_y + 1, 2, truncate_to_width(footer_lines[1], w - 4), curses.color_pair(3))

    stdscr.refresh()


def run_tui(stdscr, host, port, timeout_s, bind_iface=None, allow_nets=None):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(300)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_GREEN)   # Kopfzeile (grüner Balken)
    curses.init_pair(2, curses.COLOR_GREEN, -1)                   # verbunden / OK
    curses.init_pair(3, curses.COLOR_CYAN, -1)                    # Box-Rahmen
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)    # ausgewählte Zeile (ungenutzt hier)
    curses.init_pair(5, curses.COLOR_GREEN, -1)                   # Meter grün
    curses.init_pair(6, curses.COLOR_YELLOW, -1)                  # Meter gelb / Warnung / warte
    curses.init_pair(7, curses.COLOR_RED, -1)                     # Meter rot / Fehler / getrennt
    curses.init_pair(8, curses.COLOR_WHITE, -1)                   # normaler Text

    conn_monitor = ConnectionMonitor(history_len=12)
    sensor_monitor = SensorMonitor(interval=1.5)
    sensor_monitor.start()

    server_start = time.time()
    try:
        server, _thread = start_server(host, port, conn_monitor, bind_iface=bind_iface, allow_nets=allow_nets)
    except (OSError, RuntimeError) as e:
        curses.endwin()
        die(f"Server konnte nicht auf {host}:{port} gestartet werden: {e}")
        return

    bind_info = f" · Interface {bind_iface}" if bind_iface else ""
    allow_info = f" · Zugriff nur aus {', '.join(str(n) for n in allow_nets)}" if allow_nets else ""
    status_msg = f"Server läuft auf http://{host}:{port}/sse{bind_info}{allow_info} - warte auf den Knob ..."

    try:
        while True:
            sensors = sensor_monitor.snapshot()
            conn = conn_monitor.snapshot()
            draw_dashboard(stdscr, host, port, server_start, sensors, conn, timeout_s, status_msg,
                           bind_iface=bind_iface, allow_nets=allow_nets)

            try:
                key = stdscr.getch()
            except curses.error:
                key = -1

            if key == curses.KEY_RESIZE:
                # Bekanntes ncurses-Problem: nach SIGWINCH (Terminal-
                # Resize) kennt die C-Bibliothek die neue Größe intern
                # oft noch nicht, bis man sie explizit anstößt - sonst
                # liefert stdscr.getmaxyx() im nächsten draw_dashboard()
                # weiterhin die ALTE Größe und das Layout bleibt
                # verzerrt/abgeschnitten. curses.update_lines_cols()
                # aktualisiert curses.LINES/COLS, stdscr.erase() räumt
                # eventuelle Reste der alten Größe weg, bevor im
                # nächsten Schleifendurchlauf neu gezeichnet wird.
                curses.update_lines_cols()
                stdscr.erase()
                stdscr.refresh()
                continue
            elif key == ord('r'):
                sensor_monitor.refresh_now()
                status_msg = "Sensoren aktualisiert."
            elif key == ord('c'):
                conn_monitor.clear_history()
                status_msg = "Historie geleert."
            elif key == ord('q'):
                break
    finally:
        sensor_monitor.stop()
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(
        prog="aida-tui",
        description="AIDA64-RemoteSensor-Server für den ESP32-Knob - Terminal-Dashboard",
    )
    parser.add_argument("--port", type=int, default=80, help="Port des SSE-Servers (Standard: 80)")
    parser.add_argument("--host", default="0.0.0.0",
                         help="Bind-Adresse (Standard: 0.0.0.0 = alle Interfaces). "
                              "Für ein bestimmtes Gerät/Interface hier direkt dessen IP eintragen, "
                              "z.B. --host 192.168.1.50")
    parser.add_argument("--iface", default=None,
                         help="Optional: an genau diese Netzwerkschnittstelle binden (z.B. eth0), "
                              "statt nur an eine IP - nur Linux, braucht root (SO_BINDTODEVICE)")
    parser.add_argument("--allow", default=None,
                         help="Optional: nur Anfragen aus diesem/n IP-Bereich(en) zulassen, "
                              "kommagetrennt, z.B. --allow 192.168.1.0/24,10.0.0.5 - "
                              "alle anderen bekommen 403")
    parser.add_argument("--timeout", type=float, default=5.0,
                         help="Nach wie vielen Sekunden ohne Anfrage der Knob als 'getrennt' gilt (Standard: 5)")
    args = parser.parse_args()

    if args.port < 1024 and os.geteuid() != 0:
        die(f"Port {args.port} < 1024 benötigt Root-Rechte. Bitte mit sudo starten.")
    if args.iface and os.geteuid() != 0:
        die("--iface (an eine Netzwerkschnittstelle binden) benötigt Root-Rechte. Bitte mit sudo starten.")

    allow_nets = parse_allow_list(args.allow)

    try:
        curses.wrapper(run_tui, args.host, args.port, args.timeout, args.iface, allow_nets)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
