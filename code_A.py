# code.py — KISS <-> RYLR998 bridge (full-duplex, freq-split)
# Side A:
#   - A-RX listens on 915.000 MHz with ADDRESS=3  (peer B-TX sends here)
#   - A-TX sends  to 916.000 MHz with ADDRESS=2 -> target B-RX=1
# Priority queues preserved: ACK > DATA > ICMP; base64 framing; KISS over usb_cdc.data

import time, binascii, usb_cdc
from rylr998_cp import RYLR998
import board, busio

# ========= PER-DEVICE ADDRESSES (Side A) =========
MY_ADDR_RX = 3            # A-RX (listens)
MY_ADDR_TX = 2            # A-TX (sends)
PEER_TX_ADDR_DEFAULT = 1  # default send target = B-RX

# Optional per-destination routing by IP (expand if needed)
IP_TO_ADDR = { "10.10.10.1": 1, "10.10.10.2": 2, "10.10.10.3": 3, "10.10.10.4": 4 }

# ========= RADIO / PHY (freq-split FD) =========
NETWORK_ID = 18
BAND_RX_HZ = 915_000_000  # A-RX listens here (B-TX transmits here)
BAND_TX_HZ = 916_000_000  # A-TX transmits here (B-RX listens here)
TX_DBM     = 10
PARAM_SF   = 10
PARAM_BW   = 125
PARAM_CR   = 4
PARAM_PRE  = 16

# ========= TX pacing (optional) =========
TX_MIN_GAP_S = 1.30

# ========= FRAME SIZE LIMITS =========
KISS_MTU_BYTES      = 156
MAX_RF_ASCII_BYTES  = 220
B64_PREFIX          = "B:"

# ========= DEBUG =========
PRINT_BLOCKS  = True
ENQUEUE_DEBUG = True

# ========= Minimal base64 =========
_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_IDX  = {c: i for i, c in enumerate(_ALPH)}
def b64encode(data):
    out=[]; n=len(data); i=0
    while i<n:
        rem=n-i
        if rem>=3: b0,b1,b2=data[i],data[i+1],data[i+2]; i+=3; pad=0
        elif rem==2: b0,b1,b2=data[i],data[i+1],0; i+=2; pad=1
        else: b0,b1,b2=data[i],0,0; i+=1; pad=2
        triple=(b0<<16)|(b1<<8)|b2
        out.append(_ALPH[(triple>>18)&0x3F]); out.append(_ALPH[(triple>>12)&0x3F])
        out.append(_ALPH[(triple>>6)&0x3F] if pad<2 else '=')
        out.append(_ALPH[triple&0x3F]      if pad<1 else '=')
    return "".join(out)
def b64decode(s):
    s="".join(ch for ch in s if ch not in "\r\n\t "); out=bytearray(); i=0; L=len(s)
    while i<L:
        c0=s[i] if i<L else 'A'; i+=1; c1=s[i] if i<L else 'A'; i+=1
        c2=s[i] if i<L else 'A'; i+=1; c3=s[i] if i<L else 'A'; i+=1
        pad=0
        if c2=='=': pad=2; c2='A'; c3='A'
        elif c3=='=': pad=1; c3='A'
        n=(_IDX.get(c0,0)<<18)|(_IDX.get(c1,0)<<12)|(_IDX.get(c2,0)<<6)|_IDX.get(c3,0)
        out.append((n>>16)&0xFF)
        if pad<2: out.append((n>>8)&0xFF)
        if pad<1: out.append(n&0xFF)
    return bytes(out)
def raw_limit_for_rf(max_ascii=MAX_RF_ASCII_BYTES, prefix_len=len(B64_PREFIX)):
    return ((max_ascii - prefix_len) * 3) // 4
RAW_LIMIT = min(KISS_MTU_BYTES + 4, raw_limit_for_rf())

# ========= Stats =========
tx_frames=0; tx_bytes=0; rx_frames=0; rx_bytes=0
host_to_kiss_bytes=0; kiss_to_host_frames=0; last_stats=time.monotonic()

# ========= KISS =========
FEND=0xC0; FESC=0xDB; TFEND=0xDC; TFESC=0xDD; KISS_PORT_DATA=0x00
_k_in=False; _esc=False; _port=False; _buf=bytearray()
def kiss_encode(payload):
    out=bytearray([FEND, KISS_PORT_DATA])
    for b in payload:
        if b==FEND: out.extend([FESC,TFEND])
        elif b==FESC: out.extend([FESC,TFESC])
        else: out.append(b)
    out.append(FEND); return bytes(out)
def kiss_feed(stream):
    global _k_in,_esc,_port,_buf
    frames=[]
    for b in stream:
        if not _k_in:
            if b==FEND: _k_in=True; _esc=False; _port=False; _buf=bytearray()
            continue
        if b==FEND:
            if len(_buf)>=1 and _buf[0]==KISS_PORT_DATA: frames.append(bytes(_buf[1:]))
            _k_in=False; _esc=False; _port=False; _buf=bytearray(); continue
        if not _port: _buf.append(b); _port=True; continue
        if _esc:
            if b==TFEND: _buf.append(FEND)
            elif b==TFESC: _buf.append(FESC)
            else: _buf.append(b)
            _esc=False
        elif b==FESC: _esc=True
        else: _buf.append(b)
    return frames

# ========= IP/TCP helpers =========
def ip_header_peek(pkt):
    off=0
    if len(pkt)>=4 and pkt[0:2]==b"\x00\x00" and pkt[2:4] in (b"\x08\x00", b"\x86\xDD"): off=4
    if len(pkt)<off+20: return "short", off
    vihl=pkt[off]; v=(vihl>>4)&0xF; ihl=(vihl&0xF)*4
    tot=(pkt[off+2]<<8)|pkt[off+3]; proto=pkt[off+9]
    src=".".join(str(b) for b in pkt[off+12:off+16])
    dst=".".join(str(b) for b in pkt[off+16:off+20])
    return "v%d ihl=%d tot=%d proto=%d %s->%s"%(v,ihl,tot,proto,src,dst), off
def ip_dst_addr(pkt):
    info, off = ip_header_peek(pkt)
    if info.startswith("short"): return ""
    return ".".join(str(b) for b in pkt[off+16:off+20])
def ip_peek(pkt):
    off=0
    if len(pkt)>=4 and pkt[0:2]==b"\x00\x00" and pkt[2:4] in (b"\x08\x00", b"\x86\xDD"): off=4
    if len(pkt)<off+20: return None,0,0,off
    vihl=pkt[off]; ihl=(vihl&0x0F)*4; tot=(pkt[off+2]<<8)|pkt[off+3]; proto=pkt[off+9]
    return proto, tot, ihl, off
def tcp_peek(pkt, off, ihl):
    if len(pkt) < off+ihl+20: return None,None,None
    doff=((pkt[off+ihl]>>4)&0xF)*4
    if len(pkt) < off+ihl+doff: return None,None,None
    flags=pkt[off+ihl+13]; tot=(pkt[off+2]<<8)|pkt[off+3]
    data_len=tot - ihl - doff
    if data_len<0: data_len=0
    return flags, doff, data_len
def is_pure_tcp_ack(pkt):
    proto, tot, ihl, off = ip_peek(pkt)
    if proto != 6: return False
    flags, doff, data_len = tcp_peek(pkt, off, ihl)
    if flags is None: return False
    return (flags & 0x10) and (data_len == 0)

# ========= Hardware: two radios =========
uart0 = busio.UART(tx=board.GP0, rx=board.GP1, baudrate=115200, timeout=0.01)  # RX radio
rx_radio = RYLR998(uart=uart0, baud=115200)
uart1 = busio.UART(tx=board.GP4, rx=board.GP5, baudrate=115200, timeout=0.01)  # TX radio
tx_radio = RYLR998(uart=uart1, baud=115200)

def cfg_radio(r, addr, band_hz):
    for fn,arg in (("set_network",NETWORK_ID),
                   ("set_band",band_hz),
                   ("set_power",TX_DBM),
                   ("set_address",addr)):
        try: getattr(r, fn)(arg)
        except: pass
    try: r.set_params(sf=PARAM_SF,bw=PARAM_BW,cr=PARAM_CR,preamble=PARAM_PRE)
    except: pass

# Apply split frequencies
cfg_radio(rx_radio, MY_ADDR_RX, BAND_RX_HZ)  # listens 915.000 MHz
cfg_radio(tx_radio, MY_ADDR_TX, BAND_TX_HZ)  # sends   916.000 MHz

# ========= USB/KISS =========
ser = usb_cdc.data
try: ser.timeout = 0
except: pass

print("FD up (Side A). RXaddr=%d@%d Hz  TXaddr=%d@%d Hz RAW_LIMIT=%dB"
      % (MY_ADDR_RX, BAND_RX_HZ, MY_ADDR_TX, BAND_TX_HZ, RAW_LIMIT))

# ========= Queues =========
ACK_MAX, DATA_MAX, LO_MAX = 12, 16, 4
q_ack, q_data, q_lo = [], [], []
last_rf_tx = 0.0
_block = {"empty":0}

def classify_for_queue(payload):
    proto, tot, ihl, off = ip_peek(payload)
    if proto == 1: return 'lo'
    if proto == 6: return 'ack' if is_pure_tcp_ack(payload) else 'data'
    return 'data'

def enqueue(payload):
    if len(payload) > RAW_LIMIT:
        print("DROP oversize", len(payload)); return
    dst_ip = ip_dst_addr(payload)
    dest_addr = IP_TO_ADDR.get(dst_ip, PEER_TX_ADDR_DEFAULT)
    ascii_frame = B64_PREFIX + b64encode(payload)
    if len(ascii_frame) > MAX_RF_ASCII_BYTES:
        print("DROP ascii too long", len(ascii_frame)); return
    raw_len = (len(ascii_frame) - len(B64_PREFIX)) * 3 // 4
    cls = classify_for_queue(payload)
    if cls == 'ack':
        if len(q_ack) < ACK_MAX:
            q_ack.append((dest_addr, ascii_frame, raw_len))
            if ENQUEUE_DEBUG: print("ENQACK len=%d" % len(payload))
        else:
            if q_data: q_data.pop(0)
        return
    if cls == 'data':
        if len(q_data) < DATA_MAX:
            q_data.append((dest_addr, ascii_frame, raw_len))
            if ENQUEUE_DEBUG: print("ENQHI len=%d" % len(payload))
        else:
            print("DROP hi full")
        return
    if len(q_lo) < LO_MAX:
        q_lo.append((dest_addr, ascii_frame, raw_len))
        if ENQUEUE_DEBUG: print("ENQLO len=%d" % len(payload))

def read_host_kiss_frames():
    global host_to_kiss_bytes
    n = getattr(ser, "in_waiting", 0)
    if not n: return
    data = ser.read(n)
    if not data: return
    host_to_kiss_bytes += len(data)
    for payload in kiss_feed(data):
        enqueue(payload)

def send_to_host(pkt):
    global kiss_to_host_frames
    ser.write(kiss_encode(pkt))
    try: ser.flush()
    except: pass
    kiss_to_host_frames += 1

def stats_tick():
    global last_stats
    now = time.monotonic()
    if now - last_stats >= 5:
        print("[t+%.1fs] STATS: TX %d/%d RX %d/%d HOST %d KISS %d QACK=%d QDAT=%d QLO=%d BLK(empty=%d)"
              % (now, tx_frames, tx_bytes, rx_frames, rx_bytes,
                 host_to_kiss_bytes, kiss_to_host_frames,
                 len(q_ack), len(q_data), len(q_lo), _block.get("empty",0)))
        last_stats = now

# ========= Main loop =========
while True:
    # 1) Host -> queues
    read_host_kiss_frames()

    # 2) TX path (A-TX @ 916 MHz)
    now = time.monotonic()
    if (now - last_rf_tx) >= TX_MIN_GAP_S:
        item=None
        if   q_ack:  item=q_ack.pop(0)
        elif q_data: item=q_data.pop(0)
        elif q_lo:   item=q_lo.pop(0)
        else:
            if PRINT_BLOCKS: _block["empty"] = _block.get("empty",0) + 1
        if item:
            dest_addr, ascii_frame, raw_len = item
            try:
                tx_radio.send_ascii(dest_addr, ascii_frame)
                last_rf_tx = time.monotonic()
                tx_frames += 1; tx_bytes += raw_len
            except Exception as e:
                print("send_ascii failed:", e)

    # 3) RX path (A-RX @ 915 MHz)
    for r in rx_radio.poll():
        data = r.get("data", "")
        if isinstance(data, bytes):
            try: data = data.decode()
            except: data = ""
        if data.startswith(B64_PREFIX):
            b64 = data[len(B64_PREFIX):]
            try: pkt = b64decode(b64)
            except Exception as e:
                print("bad b64:", e); continue
            rx_frames += 1; rx_bytes += len(pkt)
            info, _ = ip_header_peek(pkt)
            frm = r.get("from")
            head20 = binascii.hexlify(pkt[:20]).decode()
            print("[%.1fs] RX %dB from %s ip=%s head20=%s"
                  % (time.monotonic(), len(pkt), str(frm), info, head20))
            send_to_host(pkt)
        else:
            print("RX text:", data)

    stats_tick()
    time.sleep(0.001)
