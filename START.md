# AIDA64 SSE Server auf Port 80 ausführen

Port 80 ist ein privilegierter Port (< 1024). Standardmäßig benötigt man `root`-Rechte, um sich an einen solchen Port zu binden. Hier sind zwei Möglichkeiten, das Skript trotzdem ohne (ständiges) `sudo` laufen zu lassen.

## Option 1: `setcap` (empfohlen, kein sudo nötig)

Mit `setcap` gibst du Python3 gezielt die Erlaubnis, sich an privilegierte Ports zu binden – ganz ohne `sudo` beim Start.

```bash
sudo apt install libcap2-bin   # falls nicht vorhanden
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))
```

Danach kannst du dein Skript einfach ohne `sudo` starten:

```bash
cd ~/homebrew/aida64-knob-linux/ && python3 aida_sse_server.py --port 80
```

### ⚠️ Achtung

Dies gibt **jedem** Skript, das mit diesem Python-Interpreter läuft, die Fähigkeit, privilegierte Ports zu nutzen – nicht nur deinem eigenen Skript. Auf einem System, das nur du selbst nutzt, ist das in der Regel unproblematisch, sicherheitstechnisch aber nicht ganz sauber.

---

## Option 2: sudoers NOPASSWD-Regel (gezielt für dieses Kommando)

Damit erlaubst du genau **diesem einen Befehl**, ohne Passwortabfrage per `sudo` zu laufen.

```bash
sudo visudo -f /etc/sudoers.d/aida64knob
```

Dort folgende Zeile eintragen (Benutzernamen anpassen):

```
deinbenutzer ALL=(ALL) NOPASSWD: /usr/bin/python3 /home/deinbenutzer/homebrew/aida64-knob-linux/aida_sse_server.py --port 80
```

### Wichtig

Der Pfad muss **exakt** passen (inklusive Argumente), sonst greift die Regel nicht. Mit `which python3` und dem vollständigen Skriptpfad prüfen, bevor du die Regel einträgst.

---

## Vergleich

| Kriterium | Option 1: `setcap` | Option 2: `sudoers` |
|---|---|---|
| Benötigt `sudo` beim Start | Nein | Ja (aber ohne Passwort) |
| Betrifft nur dieses Skript | Nein, betrifft den gesamten Python-Interpreter | Ja, exakt auf Pfad + Argumente beschränkt |
| Einrichtungsaufwand | Gering | Etwas höher (visudo-Syntax beachten) |
| Empfehlung | Für Single-User-Systeme gut geeignet | Für gezieltere Kontrolle sinnvoll |
