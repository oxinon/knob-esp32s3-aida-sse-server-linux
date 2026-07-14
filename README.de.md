# AIDA64 RemoteSensor Server für Linux (ESP32-S3-Knob-Touch-LCD-1.8)

Ein kleiner, abhängigkeitsarmer Python-Server, der das AIDA64-"RemoteSensor"-
Protokoll für den **Waveshare ESP32-S3-Knob-Touch-LCD-1.8** nachbildet – für
alle, die ihre CPU-/GPU-Werte auf dem Knob-Display anzeigen wollen, aber
**kein Windows/AIDA64 zur Hand haben oder wollen**, weil AIDA64 nur unter
Windows läuft.

Das Werksfirmware-Feature "PC Monitor" ist offiziell nur mit echtem AIDA64
(Windows-only) dokumentiert. Dieses Projekt implementiert das (nirgends
öffentlich dokumentierte) Wire-Protokoll direkt unter Linux nach, gespeist
aus `psutil`, `lm-sensors` und sysfs.

📖 Wie genau das Protokoll aussieht und wie wir es gefunden haben, steht in
[PROTOCOL.md](./PROTOCOL.md) – falls du eigene Clients/Server dafür bauen
willst oder einfach neugierig bist, wie zäh dieses Reverse Engineering war.

## Unterstützte Hardware

- **CPU:** Auslastung, Takt, Temperatur, Lüfterdrehzahl via `psutil` +
  `lm-sensors` (Intel `coretemp`, AMD `k10temp`/`zenpower`)
- **GPU:** automatische Erkennung, erste gefundene Karte wird genutzt
  - **AMD** (amdgpu-Treiber): Auslastung, Takt (inkl. Fallback für den
    `auto`-Performance-Modus), Temperatur, Lüfter – rein über sysfs, kein
    Root nötig
  - **NVIDIA**: über `nvidia-smi`
  - **Intel iGPU**: Takt über sysfs; Auslastung bewusst nicht implementiert
    (würde `intel_gpu_top` mit Root/`CAP_PERFMON` brauchen und kann sonst
    mehrere Sekunden hängen – siehe PROTOCOL.md, Fallstrick 3)

## Voraussetzungen

```bash
sudo apt install python3-pip lm-sensors
pip install psutil --break-system-packages
```

`sensors-detect` ist **nicht immer nötig**: moderne CPU-/GPU-Sensortreiber
(`k10temp`, `zenpower`, `coretemp`, `amdgpu`) sind kernel-native Treiber und
laden sich automatisch, sobald der Kernel die passende Hardware erkennt –
das betrifft die meisten Laptops. `sensors-detect` sucht stattdessen nach
älteren Super-I/O-Chips (z.B. `nct6775`, `it87`), wie sie auf **Desktop-
Mainboards** für Lüftersteuerung/Spannungen verbaut sind. Lohnt sich also
eher bei Desktop-PCs auszuprobieren:

```bash
sudo sensors-detect   # optional, v.a. bei Desktop-Mainboards relevant
```

Kurzer Check, ob überhaupt schon was da ist, bevor du dir die Mühe machst:
```bash
sensors
```
Zeigt das schon plausible Werte (Tctl, Package id 0, edge, ...), kannst du
`sensors-detect` getrost überspringen.

## Firmware auf dem Knob

Das Board hat zwei Chips (ESP32 + ESP32-S3), die je nach Orientierung des
Type-C-Steckers unterschiedliche Download-Kanäle ansprechen.

```bash
pip install esptool --break-system-packages
sudo usermod -aG dialout $USER   # danach neu einloggen

# ESP32 (Sub-Chip):
esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 115200 \
  write_flash -z 0x0 ESP32-KNOB_ESP32_0.bin

# Kabel umdrehen, dann ESP32-S3 (Hauptchip):
esptool.py --chip esp32s3 --port /dev/ttyUSB0 --baud 115200 \
  write_flash -z 0x0 WX-ESP32S3-KNOB_V1.2.bin
```

Firmware + `.rslcd`-Konfigurationsdatei gibt es offiziell bei Waveshare:
https://www.waveshare.com/wiki/ESP32-S3-Knob-Touch-LCD-1.8

## Server starten

```bash
sudo python3 aida_sse_server.py --port 80
```

Danach in der Weboberfläche des Knobs (IP des Knobs im Browser öffnen) unter
"PC Monitor" / "Secondary Screen Host Address" die IP dieses Linux-Rechners
eintragen.

### Debug-Ansicht

`http://<server-ip>/` im Browser zeigt eine 1:1-Kopie der echten
AIDA64-RemoteSensor-Seite (reiner Text, absolut positioniert, kein Styling)
– nützlich, um das Datenformat unabhängig vom physischen Knob-Display zu
verifizieren.

### Als systemd-Dienst (empfohlen für Dauerbetrieb)

```bash
sudo cp aida_sse_server.py /opt/aida_sse_server.py
sudo tee /etc/systemd/system/aida-sse-server.service <<'EOF'
[Unit]
Description=AIDA64-kompatibler SSE Sensor-Server fuer ESP32 Knob
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/aida_sse_server.py --port 80
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now aida-sse-server
```

## Bekannte Einschränkungen

- Nur die erste erkannte GPU wird ausgelesen (kein Multi-GPU-Support)
- Intel-GPU-Auslastung wird nicht angezeigt (siehe oben)
- **Lüfterdrehzahl bei vielen Laptops nicht auslesbar** (zeigt dann `0` an,
  ist kein Bug): Laptop-Hersteller geben die RPM oft nur über ihren eigenen
  Embedded Controller frei, den Linux standardmäßig nicht ausliest. Getestet
  z.B. auf einem HP ZBook 14 G1 (AMD Ryzen + integrierte AMD-GPU) – CPU-/GPU-
  Temperatur und -Takt funktionieren dort einwandfrei, Lüfter-RPM nicht.
- Getestet mit Firmware `WX-ESP32S3-KNOB_V1.2.bin` / `ESP32-KNOB_ESP32_0.bin`
  (Stand 2026) – andere Firmware-Versionen könnten ein leicht anderes
  Protokoll sprechen

## Verwandte Projekte

Dieses Projekt fokussiert sich **nur** auf das AIDA64-"PC Monitor"-Feature der
originalen Werksfirmware unter Linux. Wenn du stattdessen die **komplette
Werksfirmware ersetzen** und den Knob in **Home Assistant** einbinden willst
(Uhr/Wetter-UI, Media-Player-Steuerung, Batterie, SD-Karte, Haptik-Motor,
etc.), schau dir diese ESPHome-basierten Projekte an:

- [nkinnan/Waveshare-ESP32-S3-Knob-Touch-LCD-1.8_and_Guition-K5-Knob-Series-JC3636K518](https://github.com/nkinnan/Waveshare-ESP32-S3-Knob-Touch-LCD-1.8_and_Guition-K5-Knob-Series-JC3636K518) – volle Peripherie-Unterstützung für Waveshare- und Guition-Klon
- [KrX3D/WaveShare-Knob-Esp32S3](https://github.com/KrX3D/WaveShare-Knob-Esp32S3) – Display, Touch, Encoder, LVGL-UI, Media-Player, Batterie, SD-Karte, Haptik

**Wichtiger Unterschied:** Diese Projekte werfen die Werksfirmware komplett
weg und holen sich PC-Sensordaten (falls überhaupt gewünscht) über die
Home-Assistant-API (z.B. deren `systemmonitor`-Integration), nicht über das
originale AIDA64-Wire-Protokoll. Sie ersetzen also die Firmware, während
dieses Projekt hier die **originale Werksfirmware unverändert weiterbenutzt**
und nur den fehlenden Windows/AIDA64-Teil unter Linux nachbaut.

Beide nkinnan und KrX3D bestätigen übrigens, dass auch sie **keinen
Quellcode für die originale AIDA64-Funktion der Werksfirmware** finden
konnten – die [PROTOCOL.md](./PROTOCOL.md) in diesem Repo dürfte damit
aktuell die einzige öffentliche Dokumentation dieses Wire-Formats sein.

## Mitmachen

Pull Requests willkommen, insbesondere für:
- Multi-GPU-Unterstützung
- Weitere Sensor-Slots (RAM, Netzwerk, Disk – Positionen einfach in der
  `.rslcd` ergänzen und in `build_sse_payload()` nachziehen)
- Bestätigung/Widerlegung der offenen Fragen in PROTOCOL.md (idealerweise
  mit Xtensa-Disassembly der Firmware)

## Lizenz

MIT (siehe [LICENSE](./LICENSE)) – nutzt, forkt, verbessert, wie ihr wollt.
