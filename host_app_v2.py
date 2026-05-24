"""
KNX Remote Access - Host App (Kundengerät) v2
Unterstützt:
  - KNX Interface per LAN (KNXnet/IP, UDP 3671)
  - KNX Interface per USB/COM (über xknx)
  - KNX Interface per USB HID (direkt über hidapi, kein COM-Port nötig)

Benötigt: pip install websockets xknx pyserial hidapi

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
LOCAL_LISTEN  = 3672
# ──────────────────────────────────────────────────────────────────────────────

# ── KNXnet/IP HPAI Patch-Hilfen ───────────────────────────────────────────────
# Wenn KNXnet/IP durch einen Tunnel geroutet wird, müssen die eingebetteten
# HPAI-Adressen in CONNECT_REQUEST und CONNECT_RESPONSE umgeschrieben werden,
# da sonst ETS und KNX-Interface versuchen, direkt miteinander zu kommunizieren.
_SVC_SEARCH_REQ      = 0x0201
_SVC_SEARCH_RESP     = 0x0202
_SVC_DESC_REQ        = 0x0203
_SVC_DESC_RESP       = 0x0204
_SVC_CONNECT_REQ     = 0x0205
_SVC_CONNECT_RESP    = 0x0206
_SVC_CONNSTATE_REQ   = 0x0207
_SVC_CONNSTATE_RESP  = 0x0208
_SVC_DISCONNECT_REQ  = 0x0209
_SVC_DISCONNECT_RESP = 0x020A
_SVC_TUNNELING_REQ   = 0x0420
_SVC_TUNNELING_ACK   = 0x0421
_NAT_HPAI   = bytes([0x08, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])  # 0.0.0.0:0
_LOCAL_HPAI = bytes([0x08, 0x01, 0x7F, 0x00, 0x00, 0x01, 0x0E, 0x57])  # 127.0.0.1:3671

_SVC_NAMES = {
    0x0201: "SEARCH_REQ",     0x0202: "SEARCH_RESP",
    0x0203: "DESC_REQ",       0x0204: "DESC_RESP",
    0x0205: "CONNECT_REQ",    0x0206: "CONNECT_RESP",
    0x0207: "CONNSTATE_REQ",  0x0208: "CONNSTATE_RESP",
    0x0209: "DISCONNECT_REQ", 0x020A: "DISCONNECT_RESP",
    0x0420: "TUNNELING_REQ",  0x0421: "TUNNELING_ACK",
}

def _svc_name(svc: int) -> str:
    return _SVC_NAMES.get(svc, f"0x{svc:04X}")


def _knxip_service(data: bytes) -> int:
    """Gibt den KNXnet/IP Service-Type zurück, oder 0 wenn kein gültiger Header."""
    if len(data) >= 6 and data[0] == 0x06 and data[1] == 0x10:
        return (data[2] << 8) | data[3]
    return 0


def _patch_tech_to_knx(data: bytes) -> bytes:
    """HPAI-Adressen von ETS-Paketen auf NAT-Modus umschreiben, bevor sie
    an das echte KNX IP-Interface gehen.

    ETS bettet seine eigenen 127.0.0.1-Adressen ein - das KNX Interface
    könnte nicht zurück antworten. NAT-Modus (0.0.0.0:0) signalisiert dem
    Interface, die UDP-Quelladresse für Antworten zu verwenden.
    """
    svc = _knxip_service(data)
    if svc == _SVC_CONNECT_REQ and len(data) >= 22:
        d = bytearray(data)
        d[6:14] = _NAT_HPAI   # Control HPAI
        d[14:22] = _NAT_HPAI  # Data HPAI
        return bytes(d)
    if svc in (_SVC_DESC_REQ, _SVC_SEARCH_REQ) and len(data) >= 14:
        d = bytearray(data)
        d[6:14] = _NAT_HPAI   # Control HPAI / Discovery HPAI
        return bytes(d)
    if svc in (_SVC_CONNSTATE_REQ, _SVC_DISCONNECT_REQ) and len(data) >= 16:
        d = bytearray(data)
        d[8:16] = _NAT_HPAI   # Control HPAI (nach channel_id + reserved)
        return bytes(d)
    return data


def _patch_knx_to_tech(data: bytes) -> bytes:
    """HPAI-Adressen in Antworten vom KNX Interface umschreiben auf
    127.0.0.1:3671 - so glaubt ETS, das KNX Interface ist lokal."""
    svc = _knxip_service(data)
    if svc == _SVC_CONNECT_RESP and len(data) >= 16:
        d = bytearray(data)
        d[8:16] = _LOCAL_HPAI  # Data HPAI
        return bytes(d)
    if svc == _SVC_SEARCH_RESP and len(data) >= 14:
        d = bytearray(data)
        d[6:14] = _LOCAL_HPAI  # Control HPAI
        return bytes(d)
    return data
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  KNXnet/IP Server-Emulation für direkt angebundene Interfaces (USB/HID)
# ══════════════════════════════════════════════════════════════════════════════
class KNXnetIPGateway:
    """Vollständige KNXnet/IP-Tunneling-Server-Emulation.

    USB/HID KNX-Interfaces sprechen direkt cEMI - sie verstehen KEIN KNXnet/IP.
    Dieser Gateway nimmt KNXnet/IP-Pakete von ETS entgegen, beantwortet
    Kontroll-Pakete (DESCRIPTION, CONNECT, CONNECTIONSTATE, DISCONNECT)
    selbst und reicht nur die cEMI-Nutzdaten aus TUNNELING_REQUEST an das
    USB/HID-Device weiter.

    Eingehende cEMI-Frames vom Device werden in TUNNELING_REQUEST verpackt
    und an ETS gesendet.
    """

    SVC_SEARCH_REQ      = 0x0201
    SVC_SEARCH_RESP     = 0x0202
    SVC_DESC_REQ        = 0x0203
    SVC_DESC_RESP       = 0x0204
    SVC_CONNECT_REQ     = 0x0205
    SVC_CONNECT_RESP    = 0x0206
    SVC_CONNSTATE_REQ   = 0x0207
    SVC_CONNSTATE_RESP  = 0x0208
    SVC_DISCONNECT_REQ  = 0x0209
    SVC_DISCONNECT_RESP = 0x020A
    SVC_TUNNELING_REQ   = 0x0420
    SVC_TUNNELING_ACK   = 0x0421

    # Connection Types (KNXnet/IP)
    CONN_DEVICE_MGMT  = 0x03
    CONN_TUNNEL       = 0x04
    KNX_LAYER_LINK    = 0x02   # Standard Link Layer (für ETS Tunneling)
    KNX_LAYER_BUSMON  = 0x80   # Bus-Monitor (Group Monitor)

    CHANNEL_TUNNEL    = 0x01
    CHANNEL_DEVMGMT   = 0x02

    def __init__(self, individual_addr: int = 0x00FF, name: str = "KNXLink Gateway"):
        self.individual_addr = individual_addr
        self.name = name
        self.tx_seq = 0  # ausgehende TUNNELING_REQUEST-Sequenz
        self.active_channel = self.CHANNEL_TUNNEL

    def handle(self, data: bytes):
        """Verarbeitet ein KNXnet/IP-Paket von ETS.

        Returns (response_to_ets, cemi_to_device) - beide können None sein.
        """
        svc = _knxip_service(data)

        if svc == self.SVC_DESC_REQ:
            return self._description_resp(), None
        if svc == self.SVC_SEARCH_REQ:
            return self._search_resp(), None
        if svc == self.SVC_CONNECT_REQ:
            # CRI beginnt nach Header(6) + Control HPAI(8) + Data HPAI(8) = offset 22
            cri_type = data[23] if len(data) >= 24 else self.CONN_TUNNEL
            return self._connect_resp(cri_type), None
        if svc == self.SVC_CONNSTATE_REQ:
            return self._connstate_resp(data), None
        if svc == self.SVC_DISCONNECT_REQ:
            return self._disconnect_resp(data), None
        if svc == self.SVC_TUNNELING_REQ:
            if len(data) < 11:
                return None, None
            seq = data[8]
            cemi = data[10:]
            return self._tunneling_ack(seq), cemi
        if svc == self.SVC_TUNNELING_ACK:
            # ACK von ETS für unsere TUNNELING_REQUEST - keine Antwort nötig
            return None, None
        return None, None

    def wrap_cemi(self, cemi: bytes) -> bytes:
        """cEMI-Frame vom Device → TUNNELING_REQUEST für ETS."""
        seq = self.tx_seq
        self.tx_seq = (self.tx_seq + 1) & 0xFF
        total = 6 + 4 + len(cemi)
        return bytes([
            0x06, 0x10, 0x04, 0x20,
            (total >> 8) & 0xFF, total & 0xFF,
            0x04, self.CHANNEL_TUNNEL, seq, 0x00,
        ]) + cemi

    def _description_resp(self) -> bytes:
        ia = self.individual_addr
        serial = bytes([0x00, 0xC5, 0x01, 0x02, 0x03, 0x04])
        mac = bytes(6)
        name_bytes = self.name.encode("ascii", errors="replace")[:29]
        name_padded = name_bytes + bytes(30 - len(name_bytes))

        # DIB DEVICE_INFO (54 Bytes)
        dib_dev = bytes([
            0x36, 0x01,
            0x02,                         # KNX medium TP1
            0x01,                         # device status
            (ia >> 8) & 0xFF, ia & 0xFF,  # KNX individual address
            0x00, 0x00,                   # project install ID
        ]) + serial + bytes([224, 0, 23, 12]) + mac + name_padded

        # DIB SUPP_SVC_FAMILIES (8 Bytes)
        dib_svc = bytes([
            0x08, 0x02,
            0x02, 0x01,  # CORE v1
            0x03, 0x01,  # DEV_MGMT v1
            0x04, 0x01,  # TUNNELING v1
        ])

        body = dib_dev + dib_svc
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x02, 0x04,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body

    def _search_resp(self) -> bytes:
        # SEARCH_RESPONSE = HPAI Control Endpoint + DIB DEVICE_INFO + DIB SUPP_SVC
        body = _LOCAL_HPAI + self._description_resp()[6:]
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x02, 0x02,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body

    def _connect_resp(self, conn_type: int = CONN_TUNNEL) -> bytes:
        """CONNECT_RESPONSE - CRD muss zum CRI im CONNECT_REQUEST passen!

        - TUNNEL_CONNECTION (0x04): CRD = [04 04 IA_hi IA_lo]
        - DEVICE_MGMT_CONNECTION (0x03): CRD = [02 03]
        """
        hpai = _LOCAL_HPAI

        if conn_type == self.CONN_DEVICE_MGMT:
            channel = self.CHANNEL_DEVMGMT
            crd = bytes([0x02, 0x03])  # length=2, type=DEVICE_MGMT
        else:
            channel = self.CHANNEL_TUNNEL
            ia = self.individual_addr
            crd = bytes([
                0x04, 0x04,
                (ia >> 8) & 0xFF, ia & 0xFF,
            ])

        body = bytes([channel, 0x00]) + hpai + crd
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x02, 0x06,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body

    def _connstate_resp(self, req: bytes) -> bytes:
        ch = req[6] if len(req) >= 7 else self.CHANNEL_TUNNEL
        body = bytes([ch, 0x00])
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x02, 0x08,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body

    def _disconnect_resp(self, req: bytes) -> bytes:
        ch = req[6] if len(req) >= 7 else self.CHANNEL_TUNNEL
        body = bytes([ch, 0x00])
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x02, 0x0A,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body

    def _tunneling_ack(self, seq: int) -> bytes:
        body = bytes([0x04, self.CHANNEL_TUNNEL, seq, 0x00])
        total = 6 + len(body)
        return bytes([
            0x06, 0x10, 0x04, 0x21,
            (total >> 8) & 0xFF, total & 0xFF,
        ]) + body
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
    """Kommuniziert mit einem KNX IP Interface im LAN via UDP."""

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
                data, addr = self.sock.recvfrom(4096)
                svc = _knxip_service(data)
                log.info(f"LAN ← KNX {addr}: {_svc_name(svc)} ({len(data)} B)")
                data = _patch_knx_to_tech(data)
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
            data = base64.b64decode(data_b64)
            svc = _knxip_service(data)
            log.info(f"LAN → KNX: {_svc_name(svc)} ({len(data)} B)")
            data = _patch_tech_to_knx(data)
            self.sock.sendto(data, (self.knx_host, self.knx_port))
        except Exception as e:
            log.error(f"LAN forward: {e}")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  BRIDGE: USB via xknx (COM-Port / serielle Schnittstelle)
# ══════════════════════════════════════════════════════════════════════════════
class KNXUSBBridge:
    """Nutzt xknx für USB KNX Interfaces die als COM-Port erscheinen."""

    def __init__(self, usb_port: str, ws_send, tunnel_loop):
        self.usb_port    = usb_port
        self.ws_send     = ws_send
        self.tunnel_loop = tunnel_loop
        self.xknx        = None
        self.usb_loop    = None
        self.running     = False
        self._seq        = 0
        self.gateway     = KNXnetIPGateway(name="KNXLink USB (xknx)")

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
                    import base64
                    cemi = self._telegram_to_cemi(telegram)
                    if not cemi:
                        return
                    pkt = self.gateway.wrap_cemi(cemi)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.ws_send(json.dumps({
                                "type": "knx_packet",
                                "data": base64.b64encode(pkt).decode(),
                                "source": "host"
                            })), self.tunnel_loop
                        )
                    except Exception as e:
                        log.error(f"USB send_to_tech: {e}")

                self.xknx.telegram_queue.register_telegram_received_cb(on_telegram)

                async with self.xknx:
                    log.info(f"USB Bridge (xknx) gestartet")
                    while self.running:
                        await asyncio.sleep(0.1)

            self.usb_loop.run_until_complete(_run())

        except ImportError:
            log.error("xknx fehlt – bitte: pip install xknx")
        except Exception as e:
            log.error(f"USB Bridge Fehler: {e}")

    def _telegram_to_cemi(self, telegram) -> bytes:
        """xknx Telegram → reines cEMI L_Data.ind."""
        try:
            from xknx.telegram.apci import (GroupValueWrite, GroupValueRead,
                                             GroupValueResponse)
            from xknx.dpt import DPTBinary, DPTArray

            payload = telegram.payload
            if isinstance(payload, GroupValueRead):
                apdu = b'\x00\x00'
            elif isinstance(payload, (GroupValueWrite, GroupValueResponse)):
                val  = payload.value
                code = 0x80 if isinstance(payload, GroupValueWrite) else 0x40
                if isinstance(val, DPTBinary):
                    apdu = bytes([0x00, code | (val.value & 0x3F)])
                elif isinstance(val, DPTArray):
                    apdu = bytes([0x00, code]) + bytes(val.value)
                else:
                    return None
            else:
                return None

            src     = telegram.source_address
            dst     = telegram.destination_address
            src_raw = src.raw if hasattr(src, 'raw') else 0
            dst_raw = dst.raw if hasattr(dst, 'raw') else 0

            return bytes([
                0x29, 0x00, 0xBC, 0xE0,
                (src_raw >> 8) & 0xFF, src_raw & 0xFF,
                (dst_raw >> 8) & 0xFF, dst_raw & 0xFF,
                len(apdu) - 1,
            ]) + apdu

        except Exception as e:
            log.error(f"Telegram→cEMI: {e}")
            return None

    def forward(self, data_b64: str):
        """KNXnet/IP-Paket vom Techniker → Gateway → ggf. cEMI an xknx."""
        if not self.xknx or not self.usb_loop:
            return
        try:
            import base64
            data = base64.b64decode(data_b64)
            response, cemi = self.gateway.handle(data)
            if response:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.ws_send(json.dumps({
                            "type": "knx_packet",
                            "data": base64.b64encode(response).decode(),
                            "source": "host"
                        })), self.tunnel_loop)
                except Exception as e:
                    log.error(f"USB send_to_tech: {e}")
            if cemi:
                telegram = self._cemi_to_telegram(cemi)
                if telegram:
                    asyncio.run_coroutine_threadsafe(
                        self.xknx.telegrams.put(telegram), self.usb_loop)
        except Exception as e:
            log.error(f"USB forward: {e}")

    def _cemi_to_telegram(self, cemi: bytes):
        """cEMI → xknx Telegram."""
        try:
            if len(cemi) < 9 or cemi[0] not in (0x11, 0x29):
                return None

            dst_raw  = (cemi[6] << 8) | cemi[7]
            npdu_len = cemi[8]
            if len(cemi) < 9 + npdu_len + 1:
                return None
            apdu = cemi[9:9 + npdu_len + 1]
            if len(apdu) < 2:
                return None

            from xknx.telegram import Telegram, GroupAddress
            from xknx.telegram.apci import (GroupValueWrite, GroupValueRead,
                                             GroupValueResponse)
            from xknx.dpt import DPTBinary, DPTArray

            apci_type = ((apdu[0] & 0x03) << 8 | apdu[1]) & 0x03C0

            if apci_type == 0x0000:
                payload = GroupValueRead()
            elif apci_type == 0x0040:
                val = DPTBinary(apdu[1] & 0x3F) if len(apdu) == 2 else DPTArray(apdu[2:])
                payload = GroupValueResponse(val)
            elif apci_type == 0x0080:
                val = DPTBinary(apdu[1] & 0x3F) if len(apdu) == 2 else DPTArray(apdu[2:])
                payload = GroupValueWrite(val)
            else:
                return None

            return Telegram(destination_address=GroupAddress(dst_raw), payload=payload)
        except Exception as e:
            log.error(f"KNXnet/IP parse: {e}")
            return None

    def stop(self):
        self.running = False


# ══════════════════════════════════════════════════════════════════════════════
#  BRIDGE: USB HID (direkt über hidapi – kein COM-Port)
# ══════════════════════════════════════════════════════════════════════════════
class KNXHIDBridge:
    """
    Kommuniziert direkt mit KNX USB HID Interfaces (Weinzierl, MDT, Siemens …).
    Diese erscheinen NICHT als COM-Port, sondern als HID-Gerät.
    Verwendet CEMI-Protokoll (EMI2/3) über 64-Byte HID Reports.
    """

    # Bekannte KNX USB HID VID/PID Paare
    KNX_HID_IDS = [
        (0x0E77, 0x0111),  # Weinzierl KNX USB Interface
        (0x0E77, 0x0112),  # Weinzierl KNX USB Interface (alt)
        (0x147B, 0x0001),  # Siemens KNX USB
        (0x0681, 0x0015),  # MDT KNX USB
        (0x135E, 0x0021),  # Hager KNX USB
        (0x0BAB, 0x0001),  # ABB KNX USB
        (0x28C2, 0x0010),  # Tapko KNX USB
        (0x04D8, 0xF2B2),  # Microchip-basiert (viele Hersteller)
        (0x0E77, 0x0115),  # Weinzierl KNX-USB Interface
        (0x0E77, 0x0116),  # Weinzierl KNX-USB Interface
    ]

    _HID_REPORT_ID   = 0x01
    _HID_REPORT_SIZE = 64

    def __init__(self, hid_path: bytes, ws_send, tunnel_loop):
        self.hid_path    = hid_path
        self.ws_send     = ws_send
        self.tunnel_loop = tunnel_loop
        self.device      = None
        self.running     = False
        self._seq        = 0
        self.gateway     = KNXnetIPGateway(name="KNXLink USB HID")

    def start(self):
        self.running = True
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            import hid as hidapi
            self.device = hidapi.device()

            if self.hid_path:
                self.device.open_path(self.hid_path)
            else:
                opened = False
                for vid, pid in self.KNX_HID_IDS:
                    try:
                        self.device.open(vid, pid)
                        opened = True
                        break
                    except Exception:
                        continue
                if not opened:
                    raise RuntimeError("Kein KNX HID Interface gefunden")

            self.device.set_nonblocking(False)
            mfr = self.device.get_manufacturer_string()
            prd = self.device.get_product_string()
            log.info(f"HID Bridge: {mfr} {prd}")

            while self.running:
                try:
                    report = self.device.read(self._HID_REPORT_SIZE, timeout_ms=200)
                    if not report:
                        continue
                    self._process_report(bytes(report))
                except Exception as e:
                    if self.running:
                        log.error(f"HID Lesefehler: {e}")
                    break

        except ImportError:
            log.error("hidapi nicht installiert – pip install hidapi")
        except Exception as e:
            log.error(f"HID Bridge Fehler: {e}")
        finally:
            if self.device:
                try:
                    self.device.close()
                except Exception:
                    pass

    def _process_report(self, report: bytes):
        """KNX HID Class Protocol Report → cEMI extrahieren.

        Report-Layout (KNX-Standard, 64 B):
          [Report ID (0x01)]         hidapi liefert dies je nach Plattform mit
          [PacketInfo]               SeqNo<<4 | PacketType (0x05 = Start+End)
          [BodyLength]               TPH + cEMI Länge
          [Transfer Protocol Header 8 B] (nur bei Start):
              00 08 [cEMI-Länge HI] [cEMI-Länge LO] 01 03 00 00
          [cEMI ...]
        """
        offset = 0
        if len(report) > 0 and report[0] == self._HID_REPORT_ID:
            offset = 1

        # Nach Report ID: PacketInfo (1) + BodyLength (1)
        if len(report) < offset + 2:
            return

        body_length = report[offset + 1]
        if body_length == 0 or len(report) < offset + 2 + body_length:
            return

        body = bytes(report[offset + 2: offset + 2 + body_length])

        # Transfer Protocol Header (8 B) bei Start-Paketen abziehen
        if len(body) >= 8 and body[0] == 0x00 and body[1] == 0x08:
            cemi = body[8:]
        else:
            # Fallback: vielleicht ohne TPH (älteres Format)
            cemi = body

        if not cemi:
            return
        log.info(f"HID ← Device: cEMI ({len(cemi)} B) mc=0x{cemi[0]:02X}")
        pkt = self.gateway.wrap_cemi(cemi)
        self._send_to_tech(pkt)

    def _send_to_tech(self, packet: bytes):
        import base64
        try:
            asyncio.run_coroutine_threadsafe(
                self.ws_send(json.dumps({
                    "type": "knx_packet",
                    "data": base64.b64encode(packet).decode(),
                    "source": "host"
                })), self.tunnel_loop
            )
        except Exception as e:
            log.error(f"send_to_tech: {e}")

    def forward(self, data_b64: str):
        """KNXnet/IP-Paket vom Techniker → Gateway → ggf. cEMI an HID."""
        try:
            import base64
            data = base64.b64decode(data_b64)
            svc = _knxip_service(data)
            extra = ""
            if svc == _SVC_CONNECT_REQ and len(data) >= 24:
                ct = data[23]
                extra = f" type=0x{ct:02X}({'DEV_MGMT' if ct==0x03 else 'TUNNEL' if ct==0x04 else '?'})"
            log.info(f"HID ← Tech: {_svc_name(svc)} ({len(data)} B){extra}")
            response, cemi = self.gateway.handle(data)
            if response:
                rsvc = _knxip_service(response)
                log.info(f"HID → Tech: {_svc_name(rsvc)} ({len(response)} B)  [Gateway]")
                self._send_to_tech(response)
            if cemi:
                if not self.device:
                    log.warning("HID Device noch nicht bereit - cEMI verworfen")
                    return
                log.info(f"HID → Device: cEMI ({len(cemi)} B)")
                self._send_cemi_to_hid(cemi)
        except Exception as e:
            log.error(f"HID forward: {e}", exc_info=True)

    def _send_cemi_to_hid(self, cemi: bytes):
        """cEMI nach KNX HID Class Protocol verpacken und schreiben.

        Aufbau:
          [Report ID 0x01] [PacketInfo 0x15 = SeqNo 1 + Start+End]
          [BodyLength = 8 + len(cEMI)]
          [TPH: 00 08 cemi_len_HI cemi_len_LO 01 03 00 00]
          [cEMI...]
          [Padding bis 64 B]
        """
        try:
            cemi_len = len(cemi)
            tph = bytes([
                0x00,                                      # Protocol Version
                0x08,                                      # Header Length
                (cemi_len >> 8) & 0xFF, cemi_len & 0xFF,  # cEMI Length
                0x01,                                      # Protocol ID = KNX Tunnel
                0x03,                                      # EMI ID = cEMI
                0x00, 0x00,                                # Manufacturer Code
            ])
            body = tph + cemi
            body_length = len(body)

            # HID-Report ohne führende Report ID (hidapi hängt sie selbst dran)
            report_data = bytes([
                0x15,           # PacketInfo: SeqNo=1, PacketType=5 (Start+End)
                body_length,
            ]) + body

            max_data = self._HID_REPORT_SIZE - 1
            report_data = report_data[:max_data]
            report_data += bytes(max_data - len(report_data))

            # hidapi: erstes Byte MUSS die echte Report ID sein (0x01 für KNX)
            self.device.write(bytes([self._HID_REPORT_ID]) + report_data)
        except Exception as e:
            log.error(f"HID write: {e}")

    def stop(self):
        self.running = False
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════════════════
def scan_usb_ports() -> list:
    """Findet serielle / USB COM-Ports."""
    import platform, glob
    ports = []
    knx_keywords = {"weinzierl", "mdt", "siemens", "knx", "baos",
                    "hager", "jung", "gira", "usb"}
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc  = p.description or ""
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


def scan_hid_knx_devices() -> list:
    """
    Findet KNX USB HID Interfaces.
    Diese erscheinen NICHT als COM-Port in Windows/Linux!
    Benötigt: pip install hidapi
    """
    KNX_VID_NAMES = {
        0x0E77: "Weinzierl",
        0x147B: "Siemens",
        0x0681: "MDT",
        0x135E: "Hager",
        0x0BAB: "ABB",
        0x28C2: "Tapko",
    }
    KNX_HID_IDS = set(KNXHIDBridge.KNX_HID_IDS)

    devices = []
    try:
        import hid
        for d in hid.enumerate():
            vid     = d.get('vendor_id', 0)
            pid     = d.get('product_id', 0)
            product = (d.get('product_string', '')      or '')
            mfr     = (d.get('manufacturer_string', '') or '')
            path    = d.get('path', b'')

            is_known_id  = (vid, pid) in KNX_HID_IDS
            is_knx_name  = 'knx' in (product + mfr).lower()
            is_knx_vid   = vid in KNX_VID_NAMES

            if not (is_known_id or is_knx_name or (is_knx_vid and pid != 0)):
                continue

            label = product or mfr or f"KNX HID {vid:04X}:{pid:04X}"
            if vid in KNX_VID_NAMES and KNX_VID_NAMES[vid] not in label:
                label = f"{KNX_VID_NAMES[vid]} {label}"

            devices.append({
                'path':    path,
                'vid':     vid,
                'pid':     pid,
                'name':    label,
                'is_knx':  is_known_id or is_knx_name,
            })

    except ImportError:
        log.warning("hidapi nicht installiert – pip install hidapi")
    except Exception as e:
        log.warning(f"HID Scan: {e}")
    return devices


def discover_knx_lan(timeout=2.0) -> list:
    """KNXnet/IP Multicast Discovery."""
    found = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        sock.sendto(bytes([0x06, 0x10, 0x02, 0x01, 0x00, 0x0E,
                           0x08, 0x01, 0, 0, 0, 0, 0, 0]),
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
        self._hid_devices = []   # Liste gefundener HID-Geräte
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("KNX Remote Access – Host")
        self.root.geometry("520x640")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)

        fT = tkfont.Font(family="Helvetica", size=14, weight="bold")
        fL = tkfont.Font(family="Helvetica", size=11)
        fC = tkfont.Font(family="Courier",   size=40, weight="bold")
        fS = tkfont.Font(family="Helvetica", size=9)

        # ── Header
        hdr = tk.Frame(self.root, bg=BG_PANEL, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🔌 KNX Remote Access",
                 font=fT, bg=BG_PANEL, fg=ACCENT).pack()
        tk.Label(hdr, text="Host – Kundengerät",
                 font=fS, bg=BG_PANEL, fg=GRAY).pack()

        # ── Modus-Auswahl
        mf = tk.Frame(self.root, bg=BG_DARK, pady=10)
        mf.pack(fill="x", padx=30)
        tk.Label(mf, text="KNX Interface Verbindung:",
                 font=fL, bg=BG_DARK, fg=GRAY).pack(anchor="w")

        br = tk.Frame(mf, bg=BG_DARK, pady=6)
        br.pack(fill="x")

        self.btn_lan = tk.Button(br, text="🌐  LAN / IP",
                                  command=lambda: self._set_mode("lan"),
                                  font=fL, relief="flat",
                                  padx=14, pady=8, cursor="hand2", width=11)
        self.btn_lan.pack(side="left", padx=(0, 4))

        self.btn_usb = tk.Button(br, text="🔌  USB/COM",
                                  command=lambda: self._set_mode("usb"),
                                  font=fL, relief="flat",
                                  padx=14, pady=8, cursor="hand2", width=11)
        self.btn_usb.pack(side="left", padx=(0, 4))

        self.btn_hid = tk.Button(br, text="🔗  USB HID",
                                  command=lambda: self._set_mode("hid"),
                                  font=fL, relief="flat",
                                  padx=14, pady=8, cursor="hand2", width=11)
        self.btn_hid.pack(side="left")

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
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(row=1, column=2, sticky="w", padx=(10, 0))
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

        # ── USB / COM Panel
        self.pnl_usb = tk.Frame(self.root, bg=BG_PANEL, pady=12, padx=15)
        tk.Label(self.pnl_usb, text="USB/COM KNX Interface (via xknx)",
                 font=fL, bg=BG_PANEL, fg=GRAY).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Label(self.pnl_usb, text="COM Port:",
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
        tk.Label(self.pnl_usb,
                 text="ℹ Nur für Interfaces die als COM-Port erscheinen.\n"
                      "  Für HID-Interfaces (kein COM-Port) → USB HID Modus verwenden.",
                 font=fS, bg=BG_PANEL, fg=ORANGE).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── HID Panel
        self.pnl_hid = tk.Frame(self.root, bg=BG_PANEL, pady=12, padx=15)
        tk.Label(self.pnl_hid,
                 text="USB HID KNX Interface (Weinzierl, MDT, Siemens …)",
                 font=fL, bg=BG_PANEL, fg=GRAY).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(self.pnl_hid,
                 text="Diese Interfaces erscheinen NICHT als COM-Port!",
                 font=fS, bg=BG_PANEL, fg=GREEN).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Label(self.pnl_hid, text="HID Gerät:",
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(row=2, column=0, sticky="w")
        self.v_hid = tk.StringVar()
        self.hid_combo = ttk.Combobox(self.pnl_hid, textvariable=self.v_hid,
                                       font=fL, width=30, state="readonly")
        self.hid_combo.grid(row=2, column=1, padx=6, pady=3)

        tk.Button(self.pnl_hid, text="🔄 HID Geräte suchen",
                  command=self._scan_hid,
                  bg=BG_INPUT, fg=GRAY, font=fS, relief="flat",
                  padx=10, pady=4, cursor="hand2").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.v_hid_info = tk.StringVar(value="")
        tk.Label(self.pnl_hid, textvariable=self.v_hid_info,
                 font=fS, bg=BG_PANEL, fg=GRAY).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Label(self.pnl_hid,
                 text="⚠ Benötigt: pip install hidapi",
                 font=fS, bg=BG_PANEL, fg=ORANGE).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

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

        self._set_mode("lan")
        self._scan_usb()
        self._scan_hid()

    def _set_mode(self, mode: str):
        self.mode = mode
        self.btn_lan.config(bg=BG_INPUT, fg=GRAY)
        self.btn_usb.config(bg=BG_INPUT, fg=GRAY)
        self.btn_hid.config(bg=BG_INPUT, fg=GRAY)
        self.pnl_lan.pack_forget()
        self.pnl_usb.pack_forget()
        self.pnl_hid.pack_forget()

        if mode == "lan":
            self.btn_lan.config(bg=ACCENT, fg="white")
            self.pnl_lan.pack(fill="x", padx=30, pady=(0, 8))
        elif mode == "usb":
            self.btn_usb.config(bg=ACCENT, fg="white")
            self.pnl_usb.pack(fill="x", padx=30, pady=(0, 8))
        else:  # hid
            self.btn_hid.config(bg=ACCENT, fg="white")
            self.pnl_hid.pack(fill="x", padx=30, pady=(0, 8))

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
                self.v_disc.set(f"✓ {len(found)} Interface(s) → {found[0]['ip']}")
            else:
                self.v_disc.set("Kein KNX Interface im Netzwerk gefunden")
        threading.Thread(target=_scan, daemon=True).start()

    # ── USB / COM Scan
    def _scan_usb(self):
        self.v_usb_info.set("Scanne COM-Ports...")
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
                    f"✓ {len(ports)} COM-Port(s) gefunden")
            else:
                self.v_usb_info.set("Kein COM-Port gefunden")
        threading.Thread(target=_scan, daemon=True).start()

    # ── HID Scan
    def _scan_hid(self):
        self.v_hid_info.set("Suche HID Geräte...")
        def _scan():
            devices = scan_hid_knx_devices()
            self._hid_devices = devices
            vals = []
            for d in devices:
                label = d['name']
                label += f"  [{d['vid']:04X}:{d['pid']:04X}]"
                if d['is_knx']:
                    label += "  ⭐"
                vals.append(label)

            self.hid_combo["values"] = vals
            if vals:
                self.hid_combo.current(0)
                self.v_hid_info.set(
                    f"✓ {len(devices)} KNX HID Interface(s) gefunden  (⭐ = erkannt)")
            else:
                self.v_hid_info.set(
                    "Kein KNX HID Interface gefunden.\n"
                    "Prüfen: hidapi installiert? Interface angesteckt?")
        threading.Thread(target=_scan, daemon=True).start()

    def _get_usb_port(self) -> str:
        val = self.v_usb.get()
        return val.split("  –")[0].strip() if val else ""

    def _get_hid_path(self) -> bytes:
        idx = self.hid_combo.current()
        if idx >= 0 and idx < len(self._hid_devices):
            return self._hid_devices[idx]['path']
        return b''

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

        elif self.mode == "usb":
            usb = self._get_usb_port()
            if not usb:
                messagebox.showerror("Fehler", "COM-Port auswählen!")
                return
            self._usb_port = usb

        else:  # hid
            if not self._hid_devices:
                messagebox.showerror(
                    "Fehler",
                    "Kein HID Interface gefunden!\n"
                    "Bitte zuerst 'HID Geräte suchen' klicken.\n"
                    "Prüfen: pip install hidapi")
                return
            self._hid_path = self._get_hid_path()

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

                if self.mode == "lan":
                    self.bridge = KNXLanBridge(
                        self._knx_host, self._knx_port,
                        ws.send, self._async_loop)
                    self.bridge.start()
                elif self.mode == "usb":
                    self.bridge = KNXUSBBridge(
                        self._usb_port, ws.send, self._async_loop)
                    self.bridge.start()
                else:  # hid
                    self.bridge = KNXHIDBridge(
                        self._hid_path, ws.send, self._async_loop)
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
                        self.bridge.forward(msg["data"])

                    elif t == "disconnected":
                        log.warning("Relay meldet: Techniker getrennt (disconnected)")
                        self.root.after(0, lambda: self._set_status(
                            "Techniker getrennt", GRAY, DIM))
                        self.root.after(0, lambda: self.btn_disc.config(
                            state="disabled"))

                    elif t == "error":
                        err = msg.get("message", "")
                        log.error(f"Relay-Fehler: {err}")
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
