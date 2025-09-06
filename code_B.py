# code.py — KISS <-> RYLR998 bridge (full-duplex, freq-split)
# Side B:
#   - B-RX listens on 916.000 MHz with ADDRESS=1  (peer A-TX sends here)
#   - B-TX sends  to 915.000 MHz with ADDRESS=4 -> target A-RX=3

import time, binascii, usb_cdc
from rylr998_cp import RYLR998
import board, busio

# ========= PER-DEVICE ADDRESSES (Side B) =========
MY_ADDR_RX = 1            # B-RX (listens)
MY_ADDR_TX = 4            # B-TX (sends)
PEER_TX_ADDR_DEFAULT = 3  # default send target = A-RX

IP_TO_ADDR = { "10.10.10.1": 1, "10.10.10.2": 3, "10.10.10.3": 3, "10.10.10.4": 4 }

# ========= RADIO / PHY (freq-split FD) =========
NETWORK_ID = 18
BAND_RX_HZ = 916_000_000  # B-RX listens here (A-TX transmits here)
BAND_TX_HZ = 915_000_000  # B-TX transmits here (A-RX listens here)
TX_DBM     = 10
PARAM_SF   = 10
PARAM_BW   = 125
PARAM_CR   = 4
PARAM_PRE  = 16

TX_MIN_GAP_S = 1.30
KISS_MTU_BYTES = 156
MAX_RF_ASCII_BYTES = 220
B64_PREFIX = "B:"
PRINT_BLOCKS=True; ENQUEUE_DEBUG=True

_ALPH="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_IDX={c:i for i,c in enumerate(_ALPH)}
def b64encode(d):
    o=[]; n=len(d); i=0
    while i<n:
        r=n-i
        if r>=3: b0,b1,b2=d[i],d[i+1],d[i+2]; i+=3; p=0
        elif r==2: b0,b1,b2=d[i],d[i+1],0; i+=2; p=1
        else: b0,b1,b2=d[i],0,0; i+=1; p=2
        t=(b0<<16)|(b1<<8)|b2
        o.append(_ALPH[(t>>18)&0x3F]); o.append(_ALPH[(t>>12)&0x3F])
        o.append(_ALPH[(t>>6)&0x3F] if p<2 else '=')
        o.append(_ALPH[t&0x3F]      if p<1 else '=')
    return "".join(o)
def b64decode(s):
    s="".join(ch for ch in s if ch not in "\r\n\t "); o=bytearray(); i=0; L=len(s)
    while i<L:
        c0=s[i] if i<L else 'A'; i+=1; c1=s[i] if i<L else 'A'; i+=1
        c2=s[i] if i<L else 'A'; i+=1; c3=s[i] if i<L else 'A'; i+=1
        p=0
        if c2=='=': p=2; c2='A'; c3='A'
        elif c3=='=': p=1; c3='A'
        n=(_IDX.get(c0,0)<<18)|(_IDX.get(c1,0)<<12)|(_IDX.get(c2,0)<<6)|_IDX.get(c3,0)
        o.append((n>>16)&0xFF)
        if p<2: o.append((n>>8)&0xFF)
        if p<1: o.append(n&0xFF)
    return bytes(o)
def raw_limit_for_rf(max_ascii=MAX_RF_ASCII_BYTES, prefix_len=len(B64_PREFIX)):
    return ((max_ascii - prefix_len) * 3) // 4
RAW_LIMIT=min(KISS_MTU_BYTES+4, raw_limit_for_rf())

tx_frames=0; tx_bytes=0; rx_frames=0; rx_bytes=0
host_to_kiss_bytes=0; kiss_to_host_frames=0; last_stats=time.monotonic()

FEND=0xC0; FESC=0xDB; TFEND=0xDC; TFESC=0xDD; KISS_PORT_DATA=0x00
_k_in=False; _esc=False; _port=False; _buf=bytearray()
def kiss_encode(p):
    o=bytearray([FEND,KISS_PORT_DATA])
    for b in p:
        if b==FEND: o.extend([FESC,TFEND])
        elif b==FESC: o.extend([FESC,TFESC])
        else: o.append(b)
    o.append(FEND); return bytes(o)
def kiss_feed(s):
    global _k_in,_esc,_port,_buf
    f=[]
    for b in s:
        if not _k_in:
            if b==FEND: _k_in=True; _esc=False; _port=False; _buf=bytearray()
            continue
        if b==FEND:
            if len(_buf)>=1 and _buf[0]==KISS_PORT_DATA: f.append(bytes(_buf[1:]))
            _k_in=False; _esc=False; _port=False; _buf=bytearray(); continue
        if not _port: _buf.append(b); _port=True; continue
        if _esc:
            if b==TFEND: _buf.append(FEND)
            elif b==TFESC: _buf.append(FESC)
            else: _buf.append(b)
            _esc=False
        elif b==FESC: _esc=True
        else: _buf.append(b)
    return f

def ip_header_peek(pkt):
    off=0
    if len(pkt)>=4 and pkt[0:2]==b"\x00\x00" and pkt[2:4] in (b"\x08\x00",b"\x86\xDD"): off=4
    if len(pkt)<off+20: return "short", off
    vihl=pkt[off]; v=(vihl>>4)&0xF; ihl=(vihl&0xF)*4
    tot=(pkt[off+2]<<8)|pkt[off+3]; proto=pkt[off+9]
    src=".".join(str(b) for b in pkt[off+12:off+16]); dst=".".join(str(b) for b in pkt[off+16:off+20])
    return "v%d ihl=%d tot=%d proto=%d %s->%s"%(v,ihl,tot,proto,src,dst), off
def ip_dst_addr(pkt):
    info, off = ip_header_peek(pkt)
    if info.startswith("short"): return ""
    return ".".join(str(b) for b in pkt[off+16:off+20])
def ip_peek(pkt):
    off=0
    if len(pkt)>=4 and pkt[0:2]==b"\x00\x00" and pkt[2:4] in (b"\x08\x00",b"\x86\xDD"): off=4
    if len(pkt)<off+20: return None,0,0,off
    vihl=pkt[off]; ihl=(vihl&0x0F)*4; tot=(pkt[off+2]<<8)|pkt[off+3]; proto=pkt[off+9]
    return proto, tot, ihl, off
def tcp_peek(pkt,off,ihl):
    if len(pkt)<off+ihl+20: return None,None,None
    doff=((pkt[off+ihl]>>4)&0xF)*4
    if len(pkt)<off+ihl+doff: return None,None,None
    flags=pkt[off+ihl+13]; tot=(pkt[off+2]<<8)|pkt[off+3]
    data_len=tot-ihl-doff
    if data_len<0: data_len=0
    return flags,doff,data_len
def is_pure_tcp_ack(pkt):
    proto, tot, ihl, off = ip_peek(pkt)
    if proto != 6: return False
    flags,doff,data_len = tcp_peek(pkt,off,ihl)
    if flags is None: return False
    return (flags & 0x10) and (data_len == 0)

uart0=busio.UART(tx=board.GP0, rx=board.GP1, baudrate=115200, timeout=0.01)  # RX radio
rx_radio=RYLR998(uart=uart0, baud=115200)
uart1=busio.UART(tx=board.GP4, rx=board.GP5, baudrate=115200, timeout=0.01)  # TX radio
tx_radio=RYLR998(uart=uart1, baud=115200)

def cfg_radio(r, addr, band_hz):
    for fn,arg in (("set_network",NETWORK_ID),("set_band",band_hz),("set_power",TX_DBM),("set_address",addr)):
        try: getattr(r, fn)(arg)
        except: pass
    try: r.set_params(sf=PARAM_SF,bw=PARAM_BW,cr=PARAM_CR,preamble=PARAM_PRE)
    except: pass

cfg_radio(rx_radio, MY_ADDR_RX, BAND_RX_HZ)  # B-RX listens 916 MHz
cfg_radio(tx_radio, MY_ADDR_TX, BAND_TX_HZ)  # B-TX sends   915 MHz

ser=usb_cdc.data
try: ser.timeout=0
except: pass

print("FD up (Side B). RXaddr=%d@%d Hz  TXaddr=%d@%d Hz RAW_LIMIT=%dB"
      % (MY_ADDR_RX, BAND_RX_HZ, MY_ADDR_TX, BAND_TX_HZ, RAW_LIMIT))

ACK_MAX, DATA_MAX, LO_MAX = 12, 16, 4
q_ack, q_data, q_lo = [], [], []
last_rf_tx=0.0
_block={"empty":0}

def enqueue(payload):
    if len(payload)>RAW_LIMIT:
        print("DROP oversize", len(payload)); return
    dst_ip=ip_dst_addr(payload)
    dest_addr=IP_TO_ADDR.get(dst_ip, PEER_TX_ADDR_DEFAULT)
    ascii_frame=B64_PREFIX + b64encode(payload)
    if len(ascii_frame)>MAX_RF_ASCII_BYTES:
        print("DROP ascii too long", len(ascii_frame)); return
    raw_len=(len(ascii_frame)-len(B64_PREFIX))*3//4
    proto, tot, ihl, off = ip_peek(payload)
    cls='data'
    if proto==1: cls='lo'
    elif proto==6:
        flags, doff, data_len = tcp_peek(payload, off, ihl)
        if flags is not None and (flags & 0x10) and (data_len==0): cls='ack'
    if cls=='ack':
        if len(q_ack)<ACK_MAX:
            q_ack.append((dest_addr, ascii_frame, raw_len))
            if ENQUEUE_DEBUG: print("ENQACK len=%d"%len(payload))
        else:
            if q_data: q_data.pop(0)
        return
    if cls=='data':
        if len(q_data)<DATA_MAX:
            q_data.append((dest_addr, ascii_frame, raw_len))
            if ENQUEUE_DEBUG: print("ENQHI len=%d"%len(payload))
        else:
            print("DROP hi full"); return
    else:
        if len(q_lo)<LO_MAX:
            q_lo.append((dest_addr, ascii_frame, raw_len))
            if ENQUEUE_DEBUG: print("ENQLO len=%d"%len(payload))

def kiss_feed_and_enqueue():
    global host_to_kiss_bytes
    n=getattr(ser,"in_waiting",0)
    if not n: return
    data=ser.read(n)
    if not data: return
    host_to_kiss_bytes+=len(data)
    for payload in kiss_feed(data):
        enqueue(payload)

def send_to_host(pkt):
    global kiss_to_host_frames
    ser.write(kiss_encode(pkt))
    try: ser.flush()
    except: pass
    kiss_to_host_frames+=1

def stats_tick():
    global last_stats
    now=time.monotonic()
    if now-last_stats>=5:
        print("[t+%.1fs] STATS: TX %d/%d RX %d/%d HOST %d KISS %d QACK=%d QDAT=%d QLO=%d BLK(empty=%d)"
              % (now, tx_frames, tx_bytes, rx_frames, rx_bytes,
                 host_to_kiss_bytes, kiss_to_host_frames,
                 len(q_ack), len(q_data), len(q_lo), _block.get("empty",0)))
        last_stats=now

while True:
    kiss_feed_and_enqueue()

    now=time.monotonic()
    if (now-last_rf_tx)>=TX_MIN_GAP_S:
        item=None
        if   q_ack:  item=q_ack.pop(0)
        elif q_data: item=q_data.pop(0)
        elif q_lo:   item=q_lo.pop(0)
        else:
            if PRINT_BLOCKS: _block["empty"]=_block.get("empty",0)+1
        if item:
            dest_addr, ascii_frame, raw_len = item
            try:
                tx_radio.send_ascii(dest_addr, ascii_frame)
                last_rf_tx=time.monotonic()
                tx_frames+=1; tx_bytes+=raw_len
            except Exception as e:
                print("send_ascii failed:", e)

    for r in rx_radio.poll():
        data=r.get("data","")
        if isinstance(data,bytes):
            try: data=data.decode()
            except: data=""
        if data.startswith(B64_PREFIX):
            b64=data[len(B64_PREFIX):]
            try: pkt=b64decode(b64)
            except Exception as e:
                print("bad b64:", e); continue
            rx_frames+=1; rx_bytes+=len(pkt)
            info,_=ip_header_peek(pkt)
            frm=r.get("from")
            head20=binascii.hexlify(pkt[:20]).decode()
            print("[%.1fs] RX %dB from %s ip=%s head20=%s"
                  % (time.monotonic(), len(pkt), str(frm), info, head20))
            send_to_host(pkt)
        else:
            print("RX text:", data)

    stats_tick()
    time.sleep(0.001)
