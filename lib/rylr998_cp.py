# rylr998_cp.py — RYLR998 minimal driver for CircuitPython
import time, board, busio
from digitalio import DigitalInOut, Direction

class RYLR998:
    def __init__(self, uart=None, tx=board.GP0, rx=board.GP1,
                 baud=115200, rst_pin=None, read_timeout_s=1.2,
                 line_limit=4096):
        # Use provided UART or make one
        self.u = uart or busio.UART(
            tx, rx,
            baudrate=baud,
            timeout=0,
            receiver_buffer_size=4096
        )
        self.rst = None
        if rst_pin is not None:
            self.rst = DigitalInOut(rst_pin)
            self.rst.direction = Direction.OUTPUT
        self.read_timeout_s = read_timeout_s
        self.line_limit = line_limit
        self._buf = bytearray()

    # ---- low level helpers ----
    def _read_nb(self):
        n = self.u.in_waiting
        if n:
            chunk = self.u.read(n) or b""
            if chunk:
                self._buf += chunk
                if len(self._buf) > self.line_limit:
                    self._buf = self._buf[-self.line_limit:]

    def _pop_lines_nb(self):
        """Return list of complete CRLF-terminated lines (nonblocking)."""
        self._read_nb()
        out = []
        while True:
            i = self._buf.find(b"\r\n")
            if i < 0:
                break
            line = bytes(self._buf[:i])
            self._buf = self._buf[i+2:]  # slice instead of del
            if line:
                try:
                    out.append(line.decode("utf-8", "ignore"))
                except Exception:
                    pass
        return out

    def _readlines_block(self, timeout_s):
        out = []
        t0 = time.monotonic()
        while (time.monotonic() - t0) < timeout_s:
            got = self._pop_lines_nb()
            if got:
                out.extend(got)
                if any(("OK" in ln) or ln.startswith("+ERR=") for ln in got):
                    break
            time.sleep(0.01)
        return out

    def cmd(self, s, need_ok=True, timeout_s=None):
        if not s.endswith("\r\n"):
            s = s + "\r\n"
        self.u.write(s.encode("ascii"))
        if not need_ok:
            return []
        lines = self._readlines_block(timeout_s or self.read_timeout_s)
        if not any(("OK" in ln) or ln.startswith("+ERR=") for ln in lines):
            raise RuntimeError("AT failed: %s\n%s" %
                               (s.strip(), "\n".join(lines)))
        return lines

    # ---- module control ----
    def wake(self): self.cmd("AT")
    def reset(self):
        if self.rst:
            self.rst.value = False
            time.sleep(0.05)
            self.rst.value = True
            time.sleep(0.6)
        else:
            self.cmd("AT+RESET", need_ok=False)
            time.sleep(0.6)

    def set_address(self, addr:int): self.cmd(f"AT+ADDRESS={addr}")
    def set_network(self, nid:int):  self.cmd(f"AT+NETWORKID={nid}")
    def set_band(self, hz:int):      self.cmd(f"AT+BAND={hz}")
    def set_power(self, dbm:int):    self.cmd(f"AT+CRFOP={dbm}")
    def set_key(self, key_hex:str):  self.cmd(f"AT+CPIN={key_hex}")

    def set_params(self, sf=7, bw=125, cr=1, preamble=8):
        try:
            self.cmd(f"AT+PARAMETER={sf},{bw},{cr},{preamble}")
        except Exception:
            pass

    # ---- TX/RX ----
    def send_ascii(self, to_addr:int, ascii_payload:str):
        ln = len(ascii_payload)
        self.u.write(
            ("AT+SEND=%d,%d,%s\r\n" %
             (to_addr, ln, ascii_payload)).encode("ascii")
        )

    def poll(self):
        """Nonblocking: parse +RCV lines -> dicts."""
        out = []
        for s in self._pop_lines_nb():
            if not s.startswith("+RCV="):
                continue
            try:
                body = s[5:]
                parts = body.split(",", 4)
                f = int(parts[0]); L = int(parts[1])
                p2 = parts[2]
                if p2 and (p2[0] == '-' or p2.isdigit()):
                    rssi = int(parts[2])
                    snr  = int(parts[3])
                    payload = parts[4]
                else:
                    payload = parts[2]
                    rssi = int(parts[3])
                    snr  = int(parts[4])
                out.append({
                    "from": f,
                    "len": L,
                    "rssi": rssi,
                    "snr": snr,
                    "data": payload
                })
            except Exception:
                pass
        return out
