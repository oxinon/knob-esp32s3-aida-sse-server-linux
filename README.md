# AIDA64 RemoteSensor Server for Linux (ESP32-S3-Knob-Touch-LCD-1.8)

A small, low-dependency Python server that replicates the AIDA64
"RemoteSensor" protocol for the **Waveshare ESP32-S3-Knob-Touch-LCD-1.8** —
for anyone who wants to display their CPU/GPU stats on the Knob's screen but
**doesn't have (or want) Windows/AIDA64**, since AIDA64 is Windows-only.

The stock firmware's "PC Monitor" feature is officially documented only for
real AIDA64 (Windows-only). This project reimplements the (nowhere publicly
documented) wire protocol natively on Linux, fed by `psutil`, `lm-sensors`,
and sysfs.

📖 For the full protocol write-up and how we reverse-engineered it, see
[PROTOCOL.md](./PROTOCOL.md) — useful if you want to build your own
clients/servers for it, or are just curious how tricky this reverse
engineering turned out to be.

*(German originals: [README.de.md](./README.de.md) / [PROTOCOL.de.md](./PROTOCOL.de.md))*

## Supported hardware

- **CPU:** usage, clock speed, temperature, fan speed via `psutil` +
  `lm-sensors` (Intel `coretemp`, AMD `k10temp`/`zenpower`)
- **GPU:** auto-detected, first card found is used
  - **AMD** (amdgpu driver): usage, clock (including a fallback for `auto`
    performance mode), temperature, fan — purely via sysfs, no root needed
  - **NVIDIA**: via `nvidia-smi`
  - **Intel iGPU**: clock via sysfs; usage via sysfs `engine/*/busy` where
    available, with a debugfs fallback (root, since the server already runs
    as root for port 80) — see PROTOCOL.md for details

## Requirements

```bash
sudo apt install python3-pip lm-sensors
pip install psutil --break-system-packages
```

`sensors-detect` is **not always necessary**: modern CPU/GPU sensor drivers
(`k10temp`, `zenpower`, `coretemp`, `amdgpu`) are native kernel drivers and
load automatically once the kernel recognizes the matching hardware — this
covers most laptops. `sensors-detect` instead looks for older Super-I/O
chips (e.g. `nct6775`, `it87`) as found on **desktop mainboards** for fan
control/voltages. So it's mainly worth trying on desktop PCs:

```bash
sudo sensors-detect   # optional, mainly relevant for desktop mainboards
```

Quick check whether something's already there before bothering with
`sensors-detect`:
```bash
sensors
```
If this already shows plausible values (Tctl, Package id 0, edge, ...), you
can safely skip `sensors-detect`.

## Firmware on the Knob

The board has two chips (ESP32 + ESP32-S3) that respond to different
download channels depending on the orientation of the USB-C plug.

```bash
pip install esptool --break-system-packages
sudo usermod -aG dialout $USER   # log out and back in afterward

# ESP32 (sub-chip):
esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 115200 \
  write_flash -z 0x0 ESP32-KNOB_ESP32_0.bin

# Flip the cable, then ESP32-S3 (main chip):
esptool.py --chip esp32s3 --port /dev/ttyUSB0 --baud 115200 \
  write_flash -z 0x0 WX-ESP32S3-KNOB_V1.2.bin
```

Firmware + `.rslcd` config file are officially available from Waveshare:
https://www.waveshare.com/wiki/ESP32-S3-Knob-Touch-LCD-1.8

## Starting the server

```bash
sudo python3 aida_sse_server.py --port 80
```

Then, in the Knob's web UI (open the Knob's IP in a browser), enter this
Linux machine's IP under "PC Monitor" / "Secondary Screen Host Address".

### Debug view

`http://<server-ip>/` in a browser shows a 1:1 copy of the real AIDA64
RemoteSensor page (plain text, absolutely positioned, no styling) — useful
for verifying the data format independently of the physical Knob display.

### As a systemd service (recommended for permanent use)

```bash
sudo cp aida_sse_server.py /opt/aida_sse_server.py
sudo tee /etc/systemd/system/aida-sse-server.service <<'EOF'
[Unit]
Description=AIDA64-compatible SSE sensor server for ESP32 Knob
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

## Known limitations

- Only the first detected GPU is read (no multi-GPU support)
- **Fan speed is not readable on many laptops** (shows `0`, not a bug):
  laptop vendors often only expose RPM through their own embedded
  controller, which Linux doesn't read by default. Tested e.g. on an HP
  ZBook 14 G1 (AMD Ryzen + integrated AMD GPU) — CPU/GPU temperature and
  clock work fine there, fan RPM doesn't.
- Tested with firmware `WX-ESP32S3-KNOB_V1.2.bin` / `ESP32-KNOB_ESP32_0.bin`
  (as of 2026) — other firmware versions might speak a slightly different
  protocol

## Related projects

This project focuses **exclusively** on replicating AIDA64's "PC Monitor"
feature of the original stock firmware on Linux. If you'd rather **replace
the entire stock firmware** and integrate the Knob into **Home Assistant**
(clock/weather UI, media player control, battery, SD card, haptic motor,
etc.), check out these ESPHome-based projects:

- [nkinnan/Waveshare-ESP32-S3-Knob-Touch-LCD-1.8_and_Guition-K5-Knob-Series-JC3636K518](https://github.com/nkinnan/Waveshare-ESP32-S3-Knob-Touch-LCD-1.8_and_Guition-K5-Knob-Series-JC3636K518) — full peripheral support for both the Waveshare board and the Guition clone
- [KrX3D/WaveShare-Knob-Esp32S3](https://github.com/KrX3D/WaveShare-Knob-Esp32S3) — display, touch, encoder, LVGL UI, media player, battery, SD card, haptics

**Important difference:** those projects discard the stock firmware
entirely and (if they want PC sensor data at all) fetch it via the Home
Assistant API (e.g. its `systemmonitor` integration), not via the original
AIDA64 wire protocol. So they replace the firmware, while this project here
keeps the **original stock firmware unmodified** and only fills in the
missing Windows/AIDA64 piece on Linux.

Incidentally, both nkinnan and KrX3D confirm that they, too, could not find
any **source code for the original AIDA64 feature of the stock firmware** —
[PROTOCOL.md](./PROTOCOL.md) in this repo is currently probably the only
public documentation of this wire format.

## Contributing

Pull requests welcome, especially for:
- Multi-GPU support
- Additional sensor slots (RAM, network, disk — just add the positions in
  the `.rslcd` and follow through in `build_sse_payload()`)
- Confirming/disproving the open questions in PROTOCOL.md (ideally with
  Xtensa disassembly of the firmware)

## License

MIT (see [LICENSE](./LICENSE)) — use it, fork it, improve it, however you
like.
