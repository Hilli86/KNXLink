"""
KNX Remote Access - Relay Server
Läuft auf dem VPS hinter Cloudflare Tunnel (knx.hilli86.at).
Cloudflare übernimmt SSL → dieser Server spricht nur WS auf localhost:8765.
Von außen ist die Verbindung verschlüsselt (WSS) – kein Zertifikat nötig.

Benötigt: pip install websockets
Start:    python relay_server.py

Cloudflare Dashboard → Tunnel → Public Hostname:
  Subdomain : knx
  Domain    : hilli86.at
  Type      : HTTP
  URL       : localhost:8765
"""

import asyncio
import websockets
import json
import random
import string
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("relay")

# ── Konfiguration ────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 8765
SESSION_TIMEOUT = 3600  # Sekunden bis eine Session abläuft (1 Stunde)
# ─────────────────────────────────────────────────────────────────────────────

# Aktive Sessions: code → {"host": ws, "tech": ws, "created": timestamp}
sessions: dict = {}


def generate_code() -> str:
    """Erstellt einen eindeutigen 6-stelligen numerischen Code."""
    while True:
        code = "".join(random.choices(string.digits, k=3)) + "-" + \
               "".join(random.choices(string.digits, k=3))
        if code not in sessions:
            return code


def cleanup_sessions():
    """Entfernt abgelaufene Sessions."""
    now = time.time()
    expired = [c for c, s in sessions.items()
               if now - s["created"] > SESSION_TIMEOUT]
    for code in expired:
        log.info(f"Session {code} abgelaufen, wird entfernt.")
        del sessions[code]


async def handle_connection(websocket):
    """Haupthandler für jede eingehende WebSocket-Verbindung."""
    cleanup_sessions()
    peer = websocket.remote_address
    log.info(f"Neue Verbindung von {peer}")

    try:
        # Erste Nachricht bestimmt die Rolle
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        role = msg.get("role")

        # ── HOST: Kunde öffnet die App ───────────────────────────────────────
        if role == "host":
            code = generate_code()
            sessions[code] = {
                "host": websocket,
                "tech": None,
                "created": time.time(),
                "knx_host": msg.get("knx_host", "localhost"),
                "knx_port": int(msg.get("knx_port", 3671)),
            }
            log.info(f"Host registriert. Code: {code}")

            await websocket.send(json.dumps({
                "type": "code",
                "code": code
            }))

            # Warten bis Techniker sich verbindet
            while sessions.get(code) and sessions[code]["tech"] is None:
                await asyncio.sleep(0.5)
                # Prüfe ob Verbindung noch aktiv
                try:
                    pong = await asyncio.wait_for(
                        websocket.ping(), timeout=5)
                except Exception:
                    log.info(f"Host {code} nicht mehr erreichbar.")
                    sessions.pop(code, None)
                    return

            if code not in sessions:
                return

            log.info(f"Techniker verbunden für Code {code}. Relay startet.")
            await websocket.send(json.dumps({"type": "connected"}))

            # Relay: Host ↔ Techniker
            tech_ws = sessions[code]["tech"]
            await relay(websocket, tech_ws, code, "host")

        # ── TECHNIKER: Gibt Code ein ─────────────────────────────────────────
        elif role == "tech":
            code = msg.get("code", "").strip()

            if code not in sessions:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": "Ungültiger oder abgelaufener Code."
                }))
                return

            if sessions[code]["tech"] is not None:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": "Es ist bereits ein Techniker verbunden."
                }))
                return

            sessions[code]["tech"] = websocket
            log.info(f"Techniker verbunden für Code {code}")

            await websocket.send(json.dumps({
                "type": "connected",
                "knx_host": sessions[code]["knx_host"],
                "knx_port": sessions[code]["knx_port"],
            }))

            # Relay: Techniker ↔ Host
            host_ws = sessions[code]["host"]
            await relay(websocket, host_ws, code, "tech")

        else:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "Unbekannte Rolle. Sende role=host oder role=tech."
            }))

    except asyncio.TimeoutError:
        log.warning(f"Timeout bei {peer}")
    except websockets.exceptions.ConnectionClosed:
        log.info(f"Verbindung geschlossen: {peer}")
    except Exception as e:
        log.error(f"Fehler bei {peer}: {e}")
    finally:
        # Session aufräumen
        for code, s in list(sessions.items()):
            if s["host"] == websocket or s["tech"] == websocket:
                log.info(f"Session {code} wird beendet.")
                # Gegenseite benachrichtigen
                other = s["tech"] if s["host"] == websocket else s["host"]
                if other:
                    try:
                        await other.send(json.dumps({
                            "type": "disconnected",
                            "message": "Gegenseite hat die Verbindung getrennt."
                        }))
                    except Exception:
                        pass
                del sessions[code]
                break


async def relay(sender, receiver, code: str, role: str):
    """Leitet alle Nachrichten zwischen Host und Techniker weiter."""
    try:
        async for message in sender:
            if code not in sessions:
                break
            if receiver and not receiver.closed:
                await receiver.send(message)
            else:
                log.warning(f"Empfänger nicht verfügbar für Code {code}")
                break
    except websockets.exceptions.ConnectionClosed:
        log.info(f"Relay beendet ({role}) für Code {code}")


async def main():
    log.info(f"KNX Relay Server startet auf {HOST}:{PORT}")
    log.info("Warte auf Verbindungen...")
    async with websockets.serve(handle_connection, HOST, PORT):
        await asyncio.Future()  # läuft für immer


if __name__ == "__main__":
    asyncio.run(main())
