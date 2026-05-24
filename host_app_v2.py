"""
KNX Remote Access - Host App (Kundengerät) v2
Unterstützt:
  - KNX Interface per LAN (KNXnet/IP, UDP 3671)
  - KNX Interface per USB (über xknx als Middleware)

Benötigt: pip install websockets xknx pyserial

Start: python host_app_v2.py
"""

import asyncio
import websockets
import json
import threading
import socket
import logging
import tkinter as tk
from tkinter import font as tkfont, ttk, messagebox
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("host")

# ── Konfiguration ─────────────────────────────────────────────────────────────
RELAY_SERVER  = "wss://knx.hilli86.at"
LOCAL_LISTEN  = 3672   # Lokaler UDP Port für LAN-Bridge (nicht 3671!)
# ──────────────────────────────────────────────────────────────────────────────

BG_DARK  = "#1a1a2e"
BG_PANEL = "#16213e"
BG_INPUT = "#0f3460"
ACCENT   = "#e94560"
GREEN    = "#4ade80"
ORANGE   = "#f0a500"
GRAY     = "#a8a8b3"
DIM      = "#555555"


# ══════════════════════════════════════════════════════════════════════════════
#  BRIDGE: LAN / KNXnet/IP
# ══════════════════════════════════════════════════════════════════════════════
class KNXLanBridge:
    """
    Kommuniziert mit einem KNX IP Interface im lokalen Netzwerk via UDP.
    Leitet Pakete bidirektional durch den WebSocket-Tunnel.
    """

    def __init__(self, knx_host: str, knx_port: int, ws_send, loop):
        self.knx_host = knx_host
        self.knx_port = knx_port
        self.ws_send  = ws_send
        self._loop    = loop
        self.sock     = None
        self.running  = False

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", LOCAL_LISTEN))
        self.sock.settimeout(1.0)
        self.running = True
        log.info(f"LAN Bridge aktiv → {self.knx_host}:{self.knx_port}")
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        import base64
        while self.running:
            try:
                data, _ = self.sock.recvfrom(4096)
                asyncio.run_coroutine_threadsafe(
                    self.ws_send(json.dumps({
                        "type": "knx_packet",
                        "data": base64.b64encode(data).decode(),
                        "source": "host"
                    })), self._loop
                )
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    log.error(f"LAN recv: {e}")

    def forward(self, data_b64: str):
        import base64
        try:
            self.sock.sendto(base64.b64decode(data_b64),
                             (self.knx_host, self.knx_port))
        except Exception as e:
            log.error(f"LAN forward: {e}")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  BRIDGE: USB (via xknx)
# ══════════════════════════════════════════════════════════════════════════════
class KNXUSBBridge:
    """
    Nutzt xknx um ein USB KNX Interface (Weinzierl, MDT, Siemens …) anzusprechen.
    xknx übersetzt USB ↔ CEMI-Frames. Telegrams werden in den Tunnel geschickt.
    """

    def __init__(self, usb_port: str, ws_send, tunnel_loop):
        self.usb_port    = usb_port
        self.ws_send     = ws_send
        self.tunnel_loop = tunnel_loop
        self.xknx        = None
        self.usb_loop    = None
        self.running     = False

    def start(self):
        self.running = True
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            from xknx import XKNX
            from xknx.io import ConnectionConfig, ConnectionType

            self.usb_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.usb_loop)

            async def _run():
                cfg = ConnectionConfig(connection_type=ConnectionType.USB)
                self.xknx = XKNX(connection_config=cfg)

                async def on_telegram(telegram):
                    payload = {
                        "type": "knx_telegram",
                        "source": "host",
                        "dst": str(telegram.destination_address),
                        "src": str(telegram.source_address),
                        "payload": repr(telegram.payload),
                    }
                    asyncio.run_coroutine_threadsafe(
                        self.ws_send(json.dumps(payload)), self.tunnel_loop
                    )

                self.xknx.telegram_queue.register_telegram_received_cb(on_telegram)

                async with self.xknx:
                    log.info(f"USB Bridge gestartet ({self.usb_port})")
                    while self.running:
                        await asyncio.sleep(0.1)

            self.usb_loop.run_until_complete(_run())

        except ImportError:
            log.error("xknx fehlt – bitte: pip install xknx")
        except Exception as e:
            log.error(f"USB Bridge Fehler: {e}")

    def forward(self, telegram_dict: dict):
        """Telegramm aus dem Tunnel → ans USB Interface schicken."""
        if not self.xknx or not self.usb_loop:
            return
        try:
            from xknx.telegram import Telegram, GroupAddress
            t = Telegram(destination_address=GroupAddress(
                telegram_dict.get("dst", "0/0/0")))
            asyncio.run_coroutine_threadsafe(
                self.xknx.telegrams.put(t), self.usb_loop)
        except Exception as e:
            log.error(f"USB forward: {e}")

    def stop(self):
        self.running = False


# ══════════════════════════════════════════════════════════════════════════════
#  HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════════════════
def scan_usb_ports() -> list:
    """Findet serielle / USB Ports, markiert wahrscheinliche KNX Interfaces."""
    import platform, glob
    ports = []
    knx_keywords = {"weinzierl", "mdt", "siemens", "knx", "baos",
                    "hager", "jung", "gira", "usb"}
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc = p.description or ""
            is_knx = any(k in desc.lower() for k in knx_keywords)
            ports.append({"port": p.device, "desc": desc, "knx": is_knx})
    except ImportError:
        if platform.system() == "Windows":
            for i in range(1, 9):
                ports.append({"port": f"COM{i}", "desc": "—", "knx": False})
        else:
            for p in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"):
                ports.append({"port": p, "desc": "USB Serial", "knx": False})
    return ports


def discover_knx_lan(timeout=2.0) -> list:
    """KNXnet/IP Multicast Discovery."""
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        # Search Request
        sock.sendto(bytes([0x06,0x10,0x02,0x01,0x00,0x0E,
                           0x08,0x01,0,0,0,0,0,0]),
                    ("224.0.23.12", 3671))
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                _, addr = sock.recvfrom(1024)
                ip = addr[0]
                if ip not in [x["ip"] for x in found]:
                    found.append({"ip": ip, "port": 3671})
                    log.info(f"KNX Interface: {ip}")
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        log.warning(f"Discovery: {e}")
    return found


# ══════════════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════════════
class HostApp:

    def __init__(self):
        self.ws          = None
        self.bridge      = None
        self.mode        = "lan"
        self._async_loop = None
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("KNX Remote Access – Host")
        self.root.geometry("500x600")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)

        fT = tkfont.Font(family="Helvetica", size=14, weight="bold")
        fL = tkfont.Font(family="Helvetica", size=11)
        fC = tkfont.Font(family="Courier",   size=44, weight="bold")
        fS = tkfont.Font(family="Helvetica", size=9)

        # ── Header
        hdr = tk.Frame(self.root, bg=BG_PANEL, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🔌 KNX Remote Access",
                 font=fT, bg=BG_PANEL, fg=ACCENT).pack()
        tk.Label(hdr, text="Host – Kundengerät",
                 font=fS, bg=BG_PANEL, fg=GRAY).pack()

        # ── Modus-Auswahl
        mf = tk.Frame(self.root, bg=BG_DARK, pady=14)
        mf.pack(fill="x", padx=30)
        tk.Label(mf, text="KNX Interface Verbindung:",
                 font=fL, bg=BG_DARK, fg=GRAY).pack(anchor="w")

        br = tk.Frame(mf, bg=BG_DARK, pady=6)
        br.pack(fill="x")

        self.btn_lan = tk.Button(br, text="🌐  LAN / IP",
                                  command=lambda: self._set_mode("lan"),
                                  font=fL, relief="flat",
                                  padx=20, pady=8, cursor="hand2", width=14)
        self.btn_lan.pack(side="left", padx=(0, 6))

        self.btn_usb = tk.Button(br, text="🔌  USB",
                                  command=lambda: self._set_mode("usb"),
                                  font=fL, relief="flat",
                                  padx=20, pady=8, cursor="hand2", width=14)
        self.btn_usb.pack(side="left")

        # ── LAN Panel
        self.pnl_lan = tk.Frame(self.root, bg=BG_PANEL, pady=12, padx=15)
        tk.Label(self.pnl_lan, text="KNX IP Interface Einstellungen",
                 font=fL, bg=BG_PANEL, fg=GRAY).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        tk.Label(self.pnl_lan, text="IP-Adresse:",
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(row=1, column=0, sticky="w")
        self.v_ip = tk.StringVar(value="192.168.1.100")
        tk.Entry(self.pnl_lan, textvariable=self.v_ip, font=fL,
                 bg=BG_INPUT, fg="white", insertbackground="white",
                 relief="flat", width=18).grid(row=1, column=1, padx=6, pady=3)

        tk.Label(self.pnl_lan, text="Port:",
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(row=1, column=2, sticky="w", padx=(10,0))
        self.v_port = tk.StringVar(value="3671")
        tk.Entry(self.pnl_lan, textvariable=self.v_port, font=fL,
                 bg=BG_INPUT, fg="white", insertbackground="white",
                 relief="flat", width=6).grid(row=1, column=3, padx=6, pady=3)

        tk.Button(self.pnl_lan, text="🔍 Automatisch suchen",
                  command=self._discover,
                  bg=BG_INPUT, fg=GRAY, font=fS, relief="flat",
                  padx=10, pady=4, cursor="hand2").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.v_disc = tk.StringVar(value="")
        tk.Label(self.pnl_lan, textvariable=self.v_disc,
                 font=fS, bg=BG_PANEL, fg=GREEN).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # ── USB Panel
        self.pnl_usb = tk.Frame(self.root, bg=BG_PANEL, pady=12, padx=15)
        tk.Label(self.pnl_usb, text="USB KNX Interface Einstellungen",
                 font=fL, bg=BG_PANEL, fg=GRAY).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Label(self.pnl_usb, text="USB Port:",
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(row=1, column=0, sticky="w")
        self.v_usb = tk.StringVar()
        self.usb_combo = ttk.Combobox(self.pnl_usb, textvariable=self.v_usb,
                                       font=fL, width=28, state="readonly")
        self.usb_combo.grid(row=1, column=1, padx=6, pady=3)

        tk.Button(self.pnl_usb, text="🔄 Ports aktualisieren",
                  command=self._scan_usb,
                  bg=BG_INPUT, fg=GRAY, font=fS, relief="flat",
                  padx=10, pady=4, cursor="hand2").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.v_usb_info = tk.StringVar(value="")
        tk.Label(self.pnl_usb, textvariable=self.v_usb_info,
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(self.pnl_usb, text="⚠ Benötigt: pip install xknx pyserial",
                 font=fS, bg=BG_PANEL, fg=ORANGE).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── Start Button
        bf = tk.Frame(self.root, bg=BG_DARK, pady=10)
        bf.pack(fill="x", padx=30)
        self.btn_connect = tk.Button(
            bf, text="▶  Verbindung starten",
            command=self._start,
            bg=ACCENT, fg="white",
            font=fL, relief="flat",
            padx=25, pady=10, cursor="hand2")
        self.btn_connect.pack()

        # ── Code Anzeige
        cf = tk.Frame(self.root, bg=BG_DARK, pady=5)
        cf.pack(fill="x", padx=30)
        tk.Label(cf, text="Ihr Verbindungscode",
                 font=fL, bg=BG_DARK, fg=GRAY).pack()
        self.lbl_code = tk.Label(cf, text="------",
                                  font=fC, bg=BG_DARK, fg=ACCENT, pady=4)
        self.lbl_code.pack()
        tk.Label(cf, text="Diesen Code dem Techniker mitteilen",
                 font=fS, bg=BG_DARK, fg=DIM).pack()

        self.btn_disc = tk.Button(
            cf, text="Verbindung trennen",
            command=self._stop,
            bg=DIM, fg="white",
            font=fS, relief="flat",
            padx=12, pady=5, cursor="hand2", state="disabled")
        self.btn_disc.pack(pady=(8, 0))

        # ── Status Bar
        sb = tk.Frame(self.root, bg=BG_PANEL, pady=10, padx=15)
        sb.pack(fill="x", side="bottom")
        self.lbl_dot = tk.Label(sb, text="●", bg=BG_PANEL,
                                 fg=DIM, font=("Helvetica", 14))
        self.lbl_dot.pack(side="left")
        self.lbl_status = tk.Label(sb, text="  Bereit",
                                    font=fL, bg=BG_PANEL, fg=GRAY)
        self.lbl_status.pack(side="left")
        tk.Label(sb, text=RELAY_SERVER, font=fS,
                 bg=BG_PANEL, fg=DIM).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial
        self._set_mode("lan")
        self._scan_usb()

    def _set_mode(self, mode: str):
        self.mode = mode
        if mode == "lan":
            self.btn_lan.config(bg=ACCENT, fg="white")
            self.btn_usb.config(bg=BG_INPUT, fg=GRAY)
            self.pnl_lan.pack(fill="x", padx=30, pady=(0, 8))
            self.pnl_usb.pack_forget()
        else:
            self.btn_usb.config(bg=ACCENT, fg="white")
            self.btn_lan.config(bg=BG_INPUT, fg=GRAY)
            self.pnl_usb.pack(fill="x", padx=30, pady=(0, 8))
            self.pnl_lan.pack_forget()

    def _set_status(self, text, fg=GRAY, dot=DIM):
        self.lbl_status.config(text=f"  {text}", fg=fg)
        self.lbl_dot.config(fg=dot)

    # ── LAN Discovery
    def _discover(self):
        self.v_disc.set("🔍 Suche läuft...")
        def _scan():
            found = discover_knx_lan()
            if found:
                self.v_ip.set(found[0]["ip"])
                self.v_port.set(str(found[0]["port"]))
                self.v_disc.set(f"✓ {len(found)} Interface(s) gefunden → {found[0]['ip']}")
            else:
                self.v_disc.set("Kein KNX Interface im Netzwerk gefunden")
        threading.Thread(target=_scan, daemon=True).start()

    # ── USB Scan
    def _scan_usb(self):
        self.v_usb_info.set("Scanne USB Ports...")
        def _scan():
            ports = scan_usb_ports()
            vals, knx_idx = [], 0
            for i, p in enumerate(ports):
                label = p["port"]
                if p["desc"] and p["desc"] != "—":
                    label += f"  –  {p['desc']}"
                if p["knx"]:
                    label += "  ⭐"
                    knx_idx = i
                vals.append(label)
            self.usb_combo["values"] = vals
            if vals:
                self.usb_combo.current(knx_idx)
                self.v_usb_info.set(
                    f"✓ {len(ports)} Port(s) gefunden  (⭐ = wahrscheinlich KNX)")
            else:
                self.v_usb_info.set("Kein USB Port gefunden")
        threading.Thread(target=_scan, daemon=True).start()

    def _get_usb_port(self) -> str:
        val = self.v_usb.get()
        return val.split("  –")[0].strip() if val else ""

    # ── Verbindung starten / stoppen
    def _start(self):
        if self.mode == "lan":
            ip = self.v_ip.get().strip()
            try:
                port = int(self.v_port.get())
            except ValueError:
                messagebox.showerror("Fehler", "Ungültiger Port!")
                return
            if not ip:
                messagebox.showerror("Fehler", "IP-Adresse eingeben!")
                return
            self._knx_host = ip
            self._knx_port = port
        else:
            usb = self._get_usb_port()
            if not usb:
                messagebox.showerror("Fehler", "USB Port auswählen!")
                return
            self._usb_port = usb

        self.btn_connect.config(state="disabled")
        self._set_status("Verbinde...", ORANGE, ORANGE)
        asyncio.run_coroutine_threadsafe(self._run(), self._async_loop)

    def _stop(self):
        if self.ws:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self._async_loop)

    def _on_close(self):
        self._stop()
        self.root.destroy()
        sys.exit(0)

    # ── Async Hauptschleife
    async def _run(self):
        try:
            async with websockets.connect(RELAY_SERVER) as ws:
                self.ws = ws
                await ws.send(json.dumps({
                    "role": "host",
                    "mode": self.mode,
                    "knx_host": getattr(self, "_knx_host", "localhost"),
                    "knx_port": getattr(self, "_knx_port", 3671),
                }))

                # Bridge starten
                if self.mode == "lan":
                    self.bridge = KNXLanBridge(
                        self._knx_host, self._knx_port,
                        ws.send, self._async_loop)
                    self.bridge.start()
                else:
                    self.bridge = KNXUSBBridge(
                        self._usb_port, ws.send, self._async_loop)
                    self.bridge.start()

                async for raw in ws:
                    msg = json.loads(raw)
                    t   = msg.get("type")

                    if t == "code":
                        code = msg["code"]
                        self.root.after(0, lambda c=code:
                            self.lbl_code.config(text=c))
                        self.root.after(0, lambda: self._set_status(
                            "Warte auf Techniker...", GRAY, ORANGE))

                    elif t == "connected":
                        self.root.after(0, lambda: self._set_status(
                            "Techniker verbunden ✓", GREEN, GREEN))
                        self.root.after(0, lambda: self.btn_disc.config(
                            state="normal"))

                    elif t == "knx_packet" and self.bridge:
                        if self.mode == "lan":
                            self.bridge.forward(msg["data"])
                        else:
                            self.bridge.forward(msg.get("telegram", {}))

                    elif t == "disconnected":
                        self.root.after(0, lambda: self._set_status(
                            "Techniker getrennt", GRAY, DIM))
                        self.root.after(0, lambda: self.btn_disc.config(
                            state="disabled"))

                    elif t == "error":
                        err = msg.get("message", "")
                        self.root.after(0, lambda e=err: self._set_status(
                            f"Fehler: {e}", ACCENT, ACCENT))

        except Exception as e:
            log.error(f"Fehler: {e}")
            self.root.after(0, lambda: self._set_status(
                f"Verbindungsfehler: {e}", ACCENT, ACCENT))
        finally:
            if self.bridge:
                self.bridge.stop()
            self.ws = None
            self.root.after(0, lambda: self.btn_connect.config(state="normal"))
            self.root.after(0, lambda: self.btn_disc.config(state="disabled"))


# ══════════════════════════════════════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    app  = HostApp()
    app._async_loop = loop

    def _run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()
    app.root.mainloop()
