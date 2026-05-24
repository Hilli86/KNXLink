"""
KNX Remote Access - Techniker App
Gibt den Code ein, verbindet sich mit dem Kunden via Relay-Server.
ETS verbindet sich dann auf localhost:3671 wie ein lokales KNX Interface.

Benötigt: pip install websockets tkinter

Start: python tech_app.py
"""

import asyncio
import websockets
import json
import threading
import socket
import logging
import tkinter as tk
from tkinter import font as tkfont
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tech")

# ── Konfiguration ────────────────────────────────────────────────────────────
RELAY_SERVER = "wss://knx.hilli86.at"
LOCAL_KNX_PORT = 3671   # ETS verbindet sich auf diesen Port
# ─────────────────────────────────────────────────────────────────────────────


class LocalKNXServer:
    """
    Öffnet einen UDP-Port auf localhost:3671.
    ETS verbindet sich hier drauf (als wäre es ein lokales KNX IP Interface).
    Pakete werden über den WebSocket-Tunnel weitergeleitet.
    """

    def __init__(self, ws_send_callback):
        self.ws_send = ws_send_callback
        self.sock = None
        self.ets_addr = None
        self.running = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", LOCAL_KNX_PORT))
        self.sock.settimeout(1.0)
        self.running = True
        log.info(f"Lokaler KNX Server lauscht auf 127.0.0.1:{LOCAL_KNX_PORT}")
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                self.ets_addr = addr   # Merken für Antworten
                import base64
                encoded = base64.b64encode(data).decode()
                asyncio.run_coroutine_threadsafe(
                    self.ws_send(json.dumps({
                        "type": "knx_packet",
                        "data": encoded,
                        "source": "tech"
                    })),
                    loop
                )
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    log.error(f"Lokaler KNX Fehler: {e}")

    def send_to_ets(self, data_b64: str):
        """Empfängt Paket vom Tunnel und schickt es an die ETS."""
        import base64
        if not self.ets_addr:
            log.warning("ETS hat noch keine Pakete gesendet – Adresse unbekannt")
            return
        data = base64.b64decode(data_b64)
        self.sock.sendto(data, self.ets_addr)

    def stop(self):
        self.running = False
        if self.sock:
            self.sock.close()


class TechApp:
    def __init__(self):
        self.ws = None
        self.server = None
        self.connected = False
        self.code_digits = ["", "", "", "", "", ""]
        self._setup_gui()

    def _setup_gui(self):
        self.root = tk.Tk()
        self.root.title("KNX Remote Access – Techniker")
        self.root.geometry("480x420")
        self.root.resizable(False, False)
        self.root.configure(bg="#0f3460")

        title_font = tkfont.Font(family="Helvetica", size=14, weight="bold")
        code_font = tkfont.Font(family="Courier", size=28, weight="bold")
        label_font = tkfont.Font(family="Helvetica", size=11)
        small_font = tkfont.Font(family="Helvetica", size=9)
        input_font = tkfont.Font(family="Courier", size=26, weight="bold")

        # Header
        header = tk.Frame(self.root, bg="#16213e", pady=15)
        header.pack(fill="x")
        tk.Label(header, text="🔧 KNX Remote Access",
                 font=title_font, bg="#16213e", fg="#e94560").pack()
        tk.Label(header, text="Techniker-Konsole",
                 font=small_font, bg="#16213e", fg="#a8a8b3").pack()

        # Code Eingabe
        input_frame = tk.Frame(self.root, bg="#0f3460", pady=25)
        input_frame.pack(fill="x", padx=30)

        tk.Label(input_frame, text="Verbindungscode eingeben",
                 font=label_font, bg="#0f3460", fg="#a8a8b3").pack()

        # Code Entry (großes einzelnes Feld mit Bindestrich)
        entry_frame = tk.Frame(input_frame, bg="#16213e",
                                relief="flat", pady=10, padx=15)
        entry_frame.pack(pady=12)

        self.code_var = tk.StringVar()
        self.code_var.trace("w", self._on_code_change)

        vcmd = (self.root.register(self._validate_code), "%P")
        self.code_entry = tk.Entry(
            entry_frame,
            textvariable=self.code_var,
            font=input_font,
            width=8,
            bg="#16213e", fg="#e94560",
            insertbackground="#e94560",
            relief="flat",
            justify="center",
            validate="key", validatecommand=vcmd
        )
        self.code_entry.pack()
        tk.Label(entry_frame, text="Format: 123-456",
                 font=small_font, bg="#16213e", fg="#555").pack()

        self.code_entry.focus()

        # Verbinden Button
        self.connect_btn = tk.Button(
            self.root, text="Verbinden",
            command=self._connect,
            bg="#e94560", fg="white",
            font=label_font, relief="flat",
            padx=30, pady=10, cursor="hand2",
            state="disabled"
        )
        self.connect_btn.pack(pady=5)

        # Status
        status_frame = tk.Frame(self.root, bg="#16213e", pady=12, padx=20)
        status_frame.pack(fill="x", padx=30, pady=15)

        self.status_dot = tk.Label(status_frame, text="●",
                                   bg="#16213e", fg="#555", font=("Helvetica", 16))
        self.status_dot.pack(side="left")

        self.status_label = tk.Label(
            status_frame, text="  Bereit",
            font=label_font, bg="#16213e", fg="#a8a8b3"
        )
        self.status_label.pack(side="left")

        # ETS Hinweis
        ets_frame = tk.Frame(self.root, bg="#0f3460", pady=5)
        ets_frame.pack(fill="x", padx=30)

        self.ets_info = tk.Label(
            ets_frame,
            text="Nach Verbindung → ETS: IP-Tunneling auf 127.0.0.1:3671",
            font=small_font, bg="#0f3460", fg="#555"
        )
        self.ets_info.pack()

        # Trennen Button
        self.disconnect_btn = tk.Button(
            self.root, text="Trennen",
            command=self._disconnect,
            bg="#333", fg="#a8a8b3",
            font=small_font, relief="flat",
            padx=15, pady=6, cursor="hand2",
            state="disabled"
        )
        self.disconnect_btn.pack(pady=5)

        tk.Label(self.root,
                 text=f"Server: {RELAY_SERVER}",
                 font=small_font, bg="#0f3460", fg="#333").pack(
            side="bottom", pady=5)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Return>", lambda e: self._connect())

    def _validate_code(self, value: str) -> bool:
        """Erlaubt nur Ziffern und einen Bindestrich, max 7 Zeichen."""
        if len(value) > 7:
            return False
        allowed = set("0123456789-")
        return all(c in allowed for c in value)

    def _on_code_change(self, *args):
        val = self.code_var.get()
        # Auto-Bindestrich nach 3 Ziffern
        digits = val.replace("-", "")
        if len(digits) >= 3 and "-" not in val:
            self.code_var.set(digits[:3] + "-" + digits[3:])
            self.code_entry.icursor(tk.END)

        clean = self.code_var.get()
        ready = len(clean) == 7 and clean[3] == "-"
        self.connect_btn.config(state="normal" if ready else "disabled")

    def _set_status(self, text: str, color: str, dot_color: str):
        self.status_label.config(text=f"  {text}", fg=color)
        self.status_dot.config(fg=dot_color)

    def _connect(self):
        code = self.code_var.get().strip()
        if len(code) != 7:
            return
        self.connect_btn.config(state="disabled")
        self.code_entry.config(state="disabled")
        self._set_status("Verbinde...", "#f0a500", "#f0a500")
        asyncio.run_coroutine_threadsafe(self._async_connect(code), loop)

    def _disconnect(self):
        if self.ws:
            asyncio.run_coroutine_threadsafe(self.ws.close(), loop)

    def _on_close(self):
        self._disconnect()
        self.root.destroy()
        sys.exit(0)

    async def _async_connect(self, code: str):
        try:
            async with websockets.connect(RELAY_SERVER) as ws:
                self.ws = ws

                await ws.send(json.dumps({
                    "role": "tech",
                    "code": code
                }))

                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")

                    if t == "connected":
                        self.connected = True
                        knx_h = msg.get("knx_host", "?")
                        knx_p = msg.get("knx_port", 3671)
                        log.info(f"Verbunden! KNX Interface beim Kunden: {knx_h}:{knx_p}")

                        # Lokalen KNX Server starten
                        self.server = LocalKNXServer(ws.send)
                        try:
                            self.server.start()
                            self.root.after(0, lambda: self._set_status(
                                f"Verbunden ✓  –  ETS → 127.0.0.1:{LOCAL_KNX_PORT}",
                                "#4ade80", "#4ade80"))
                            self.root.after(0, lambda: self.disconnect_btn.config(
                                state="normal"))
                            self.root.after(0, lambda: self.ets_info.config(
                                fg="#4ade80"))
                        except OSError as e:
                            self.root.after(0, lambda: self._set_status(
                                f"Port {LOCAL_KNX_PORT} belegt! ETS schließen.",
                                "#e94560", "#e94560"))

                    elif t == "knx_packet" and self.server:
                        self.server.send_to_ets(msg["data"])

                    elif t == "disconnected":
                        self.root.after(0, lambda: self._set_status(
                            "Kunde hat getrennt", "#f0a500", "#f0a500"))
                        break

                    elif t == "error":
                        err = msg.get("message", "Unbekannter Fehler")
                        self.root.after(0, lambda e=err: self._set_status(
                            f"Fehler: {e}", "#e94560", "#e94560"))
                        break

        except Exception as e:
            log.error(f"Verbindungsfehler: {e}")
            self.root.after(0, lambda: self._set_status(
                f"Fehler: {e}", "#e94560", "#e94560"))
        finally:
            self.connected = False
            if self.server:
                self.server.stop()
            self.root.after(0, lambda: self.connect_btn.config(state="normal"))
            self.root.after(0, lambda: self.code_entry.config(state="normal"))
            self.root.after(0, lambda: self.disconnect_btn.config(state="disabled"))
            self.root.after(0, lambda: self.ets_info.config(fg="#555"))


def run_async_loop(loop_ref):
    asyncio.set_event_loop(loop_ref)
    loop_ref.run_forever()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    app = TechApp()

    t = threading.Thread(target=run_async_loop, args=(loop,), daemon=True)
    t.start()

    app.root.mainloop()
