# Das AIDA64-RemoteSensor-Protokoll (reverse-engineered)

Dieses Dokument beschreibt, wie das Waveshare **ESP32-S3-Knob-Touch-LCD-1.8**
(Firmware `WX-ESP32S3-KNOB_V1.2.bin`, Stand 2026) tatsächlich mit AIDA64
kommuniziert. Weder Waveshare noch AIDA64 dokumentieren dieses Wire-Format
öffentlich – die folgenden Informationen wurden durch Firmware-Analyse
(`strings`) und Wireshark-Mitschnitte einer echten, funktionierenden
AIDA64-Instanz gewonnen.

## Kurzfassung

- Der Knob macht periodisch `GET /sse` an die in seiner Weboberfläche
  hinterlegte "PC Monitor"-IP.
- Der Server antwortet **einmalig pro Anfrage** (kein Dauerstreaming trotz
  SSE-Content-Type) mit HTTP-Headern und einer einzeiligen `data:`-Zeile.
- **Die Antwort muss innerhalb von wenigen hundert Millisekunden kommen.**
  Der Knob gibt anscheinend nach recht kurzer Zeit auf und verarbeitet eine
  zu spät eintreffende (aber sonst korrekte) Antwort nicht mehr. Siehe
  [Fallstrick 3](#fallstrick-3-antwortzeit).

## Der Request

```
GET /sse HTTP/1.1
Host: <ip>
Connection: close

```
(mit `\r\n`-Zeilenenden, ganz normales HTTP/1.1)

Beobachtung: Die Firmware sendet den Request in **zwei separaten TCP-Segmenten**
(zuerst ca. 17 Byte, dann den Rest) – vermutlich zwei aufeinanderfolgende
`send()`-Aufrufe im Firmware-Code. Das ist für einen Server aus Sicht der
Standard-Socket-API transparent, fiel uns aber im Wireshark-Mitschnitt auf.

## Die Response

Byte-genauer Mitschnitt einer echten AIDA64-Antwort:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Access-Control-Allow-Origin: *
Access-Control-Expose-Headers: *
Access-Control-Allow-Credentials: true

data: Page0|{|}Simple1|CPU usage 17^{|}Simple2|CPU freq 1600^{|}Simple3|CPU temp 57^{|}Simple4|CPU fan 6578^{|}Simple5|{|}Simple6|GPU freq 100^{|}Simple7|GPU temp TRIAL^{|}Simple8|GPU fan 0^{|}
```

### Fallstrick 1: Zeilenenden sind `\n`, nicht `\r\n`

Das ist der Kernel des ganzen Problems. Technisch verlangt HTTP/1.1
`\r\n` als Zeilenende. **AIDA64s eingebauter Webserver hält sich nicht
daran und schickt nur `\n`.** Browser und `curl` sind da tolerant und
parsen es trotzdem korrekt – die Knob-Firmware ist es (vermutlich) nicht:
sie scannt wahrscheinlich stur nach der Byte-Folge `\n\n`, um das Ende der
Header zu erkennen. `\r\n\r\n` enthält kein `\n\n` als Teilstring, also
wird bei standardkonformen `\r\n`-Antworten (wie Pythons `http.server` sie
automatisch erzeugt) nie ein Ende der Header gefunden.

**Konsequenz:** Header von Hand als rohe Bytes mit `\n` bauen, nicht die
`send_header()`/`end_headers()`-Helfer der Standardbibliothek verwenden.

### Fallstrick 2: Simple-IDs sind Positions-Slots, keine Sensor-IDs

Man könnte erwarten, dass die ID (`SCPUUTI`, `TCPUPKG`, ...) aus AIDA64s
"Complete Sensor Value List" übertragen wird. **Das ist nicht der Fall.**
Übertragen wird stattdessen `SimpleN`, wobei `N` schlicht die Position
(1-8) des Eintrags in der importierten `.rslcd`-Layoutdatei ist. Die
Firmware kennt daher gar keine AIDA64-Sensor-IDs (im kompilierten Binary
kommt z.B. der String `SCPUUTI` kein einziges Mal vor) – sie matcht rein
positionell gegen ihre eigenen, fest einprogrammierten Labels
(`CPU usage`, `CPU freq`, `CPU temp`, `CPU fan`, `GPU usage`, `GPU freq`,
`GPU temp`, `GPU fan` - exakt in dieser Reihenfolge).

Format pro Eintrag: `SimpleN|<Label> <Wert>^`. Mehrere Einträge werden mit
`{|}` verbunden, die ganze Payload beginnt mit einem leeren `Page0|`-Präfix.

Das `^` am Ende jedes Werts ist **Pflicht** und kommt aus dem
`Show unit`-Feld der `.rslcd` (dort literal auf `^` gesetzt) – es dient
offenbar als Terminator-Zeichen, keine echte Einheit.

### Fallstrick 3: Antwortzeit

Der eigentliche Show-Stopper, nachdem Format und Zeilenenden schon korrekt
waren. Per Wireshark-Timing-Vergleich:

| Server | Zeit von "Request komplett empfangen" bis "Antwort gesendet" |
|---|---|
| Echtes AIDA64 | ~56 ms |
| Unser erster Python-Versuch (mit `intel_gpu_top`/`nvidia-smi`-Aufrufen à 2s Timeout) | ~2200 ms |

Bei >2 Sekunden Verzögerung kam die Antwort zwar TCP-technisch sauber und
vollständig an (wurde vom Knob auch ge-ACKt) – wurde aber nie auf dem
Display angezeigt. Vermutung: Die Firmware hat intern eine kurze
Lese-Timeout-Grenze und gibt den Socket application-seitig auf, bevor
späte Daten noch verarbeitet werden, auch wenn sie auf TCP-Ebene im
Empfangspuffer liegen.

**Konsequenz:** Sensor-Abfragen müssen im niedrigen einstelligen
Millisekundenbereich bleiben. Insbesondere `intel_gpu_top` (braucht oft
Root/`CAP_PERFMON` und kann ohne diese Rechte lange hängen) sollte
vermieden oder mit sehr kurzem Timeout versehen werden.

## Reihenfolge / Positionen (aus der mitgelieferten `.rslcd`)

| Slot | Label | ITMY (Pixel-Position) |
|---|---|---|
| Simple1 | CPU usage | 0 |
| Simple2 | CPU freq | 20 |
| Simple3 | CPU temp | 40 |
| Simple4 | CPU fan | 60 |
| Simple5 | GPU usage | 100 |
| Simple6 | GPU freq | 120 |
| Simple7 | GPU temp | 140 |
| Simple8 | GPU fan | 160 |

## Offene Fragen / nicht abschließend geklärt

- Ob die Firmware wirklich stur nach `\n\n` sucht oder ob es einen anderen
  Grund für die CRLF-Empfindlichkeit gibt, haben wir nicht per Disassembly
  verifiziert (kein Xtensa-Toolchain zur Hand) – nur empirisch bestätigt,
  dass die Umstellung von `\r\n` auf `\n` das Problem behoben hat.
- Warum die Firmware manchmal ihren Request-Stream sofort per FIN
  halbschließt und manchmal nicht (beobachtet auch bei echtem AIDA64,
  dort ca. 1 von 6 Verbindungsversuchen ohne jede Serverantwort), ist
  ungeklärt. Vermutlich eine Eigenheit des ESP32-Netzwerkstacks
  (lwIP) unter WLAN-Last.
- Der genaue Timeout-Wert, ab dem die Firmware eine Antwort verwirft, wurde
  nicht exakt eingegrenzt (wir wissen nur: 56ms geht, 2200ms nicht).
