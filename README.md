# KNX Remote Access – Einrichtungsanleitung

Dieses System ermöglicht es dir, als Techniker von überall auf KNX-Anlagen
zuzugreifen – ähnlich wie TeamViewer, aber für KNX/ETS.

Die Verbindung läuft **verschlüsselt über WSS** via Cloudflare Tunnel.

---

## Architektur

```
Kunde (host_app_v2.py)         Cloudflare          VPS                  Du (tech_app.py)
        │                          │           relay_server.py                │
        │── wss://knx.hilli86.at ─►│── ws://localhost:8765 ──────────────────►│
        │◄══════════════ KNX Pakete (verschlüsselt) ════════════════════════► │
                                                                       ETS auf
                                                                   127.0.0.1:3671
```

---

## 1. Relay-Server einrichten (VPS)

### Systembenutzer anlegen (Sicherheit)

Der Dienst soll nicht als `root` oder persönlicher Benutzer laufen, sondern als eigener Systembenutzer ohne Login-Shell:

```bash
# Systembenutzer anlegen
sudo useradd --system --no-create-home --shell /usr/sbin/nologin knxlink

# Ordner anlegen und Eigentümer setzen
sudo mkdir -p /opt/knxlink
sudo chown -R knxlink:knxlink /opt/knxlink
```

### Virtual Environment erstellen

Eine Virtual Environment hält die Python-Pakete vom System getrennt und vermeidet Konflikte.

```bash
# Als knxlink-Benutzer in den Ordner wechseln
sudo -u knxlink bash

cd /opt/knxlink

# Virtual Environment erstellen
python3 -m venv venv

# Virtual Environment aktivieren
source venv/bin/activate

# Prompt zeigt nun: (venv) knxlink@server:/opt/knxlink$
```

Zum **Deaktivieren** der Virtual Environment:
```bash
deactivate
```

> Die Virtual Environment muss nach jedem Login neu aktiviert werden (`source venv/bin/activate`), bevor du Pakete installierst oder Skripte startest.

### Skript auf den Server kopieren

```bash
# Von deinem lokalen Rechner (ersetze user@server mit deinen Zugangsdaten)
scp relay_server.py user@server:/opt/knxlink/

# Eigentümer korrigieren
sudo chown knxlink:knxlink /opt/knxlink/relay_server.py
```

### Abhängigkeiten installieren

```bash
sudo -u knxlink /opt/knxlink/venv/bin/pip install websockets
```

### Starten (manuell zum Testen)
```bash
sudo -u knxlink /opt/knxlink/venv/bin/python /opt/knxlink/relay_server.py
```

### Als Dienst einrichten (läuft dauerhaft, auch nach Neustart)
```bash
sudo nano /etc/systemd/system/knx-relay.service
```

```ini
[Unit]
Description=KNX Remote Relay Server
After=network.target

[Service]
ExecStart=/opt/knxlink/venv/bin/python /opt/knxlink/relay_server.py
Restart=always
User=knxlink
WorkingDirectory=/opt/knxlink

[Install]
WantedBy=multi-user.target
```

> `User=knxlink` sorgt dafür, dass der Dienst mit minimalen Rechten läuft. Der direkte Pfad zur venv ersetzt die manuelle Aktivierung.

```bash
sudo systemctl daemon-reload
sudo systemctl enable knx-relay
sudo systemctl start knx-relay

# Status prüfen:
sudo systemctl status knx-relay
```

---

## 2. Cloudflare Tunnel konfigurieren

Im **Cloudflare Dashboard → Zero Trust → Networks → Tunnels**:

1. Bestehenden Tunnel anklicken
2. Tab **„Public Hostname"** → **„Add a public hostname"**
3. Ausfüllen:

| Feld      | Wert            |
|-----------|-----------------|
| Subdomain | `knx`           |
| Domain    | `hilli86.at`    |
| Type      | `HTTP`          |
| URL       | `localhost:8765`|

4. Speichern

→ Ab sofort ist `wss://knx.hilli86.at` erreichbar und verschlüsselt.

---

## 3. Host-App (Kundengerät)

### Installation
```bash
pip install websockets xknx pyserial
```

### Starten
```bash
python host_app_v2.py
```

### Verbindungstyp wählen
- **🌐 LAN / IP** → IP-Adresse des KNX IP Interface eingeben oder automatisch suchen
- **🔌 USB** → USB Port aus Liste wählen (⭐ = wahrscheinlich KNX Interface)

### Als .exe für Kunden verteilen (kein Python beim Kunden nötig)
```bash
pip install pyinstaller
pyinstaller --onefile --windowed host_app_v2.py
# Fertige .exe liegt in: dist/host_app_v2.exe
```

---

## 4. Techniker-App

### Installation
```bash
pip install websockets
```

### Starten
```bash
python tech_app.py
```

### Ablauf
1. Code vom Kunden erhalten (z.B. `483-291`)
2. Code eingeben → „Verbinden"
3. ETS öffnen → IP-Tunneling → `127.0.0.1:3671`
4. KNX Geräte programmieren wie lokal!

---

## 5. ETS Konfiguration (nach Verbindungsaufbau)

1. ETS → **Einstellungen → Bus → Verbindung hinzufügen**
2. **IP-Tunneling** auswählen
3. IP-Adresse: `127.0.0.1`
4. Port: `3671`
5. **NAT-Modus aktivieren** ✓

---

## Troubleshooting

| Problem | Lösung |
|---------|--------|
| Port 3671 belegt | ETS schließen, dann tech_app starten |
| Ungültiger Code | Host-App neu starten, neuen Code holen |
| Verbindung sofort getrennt | Relay-Server läuft? `systemctl status knx-relay` |
| ETS findet kein Interface | NAT-Modus in ETS aktivieren |
| USB Interface nicht sichtbar | `pip install pyserial` → Ports aktualisieren |
| Cloudflare Fehler 502 | relay_server.py läuft nicht auf Port 8765 |
