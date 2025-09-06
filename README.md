# RYLR998\_KISS

Drivers designed for [CircuitPython](https://circuitpython.org/) devices to support full-duplex communication over the [KISS protocol](https://www.ax25.net/kiss.aspx) using base64 ASCII encoding on the REYAX RYLR998 chipset using the stock firmware through the UART interface. The host CircuitPython device can then be attached as a network interface to any Linux device using [markqvist's tncattach](https://github.com/markqvist/tncattach) package, enabling near-continuous bidirectional TCP/IP communication at realistic speeds of up to 10kbps.

## Quick Start (5 Steps)

1. **Prepare hardware:** Two Raspberry Pi Pico W boards (running CircuitPython 9.2.8) and four RYLR998 modules (two per Pico). Power each Pico via USB.
2. **Copy files:**

   * On Device A: copy `code_A.py` → `code.py`.
   * On Device B: copy `code_B.py` → `code.py`.
   * On both devices: copy `rylr998_cp.py` → `/lib/`.
   * Ensure `boot.py` enables both USB console and data ports.
3. **Connect radios:** Each Pico uses two UARTs (GP0/GP1 and GP4/GP5) wired to RYLR998 modules. Share GND, supply stable 3.3V.
4. **Install host software:** On Linux, `pip install tncattach`. Then bring up `tnc0` on each host with:

   ```bash
   sudo tncattach /dev/ttyACM1 115200 --mtu 156 --noipv6 &
   sudo ip addr add 10.10.10.1/30 dev tnc0   # on host A
   sudo ip addr add 10.10.10.2/30 dev tnc0   # on host B
   sudo ip link set tnc0 up
   ```
5. **Test link:** Use `ping 10.10.10.2` from host A and verify replies from host B. For throughput, use `iperf3` at \~3 kbps. Please note that to achieve this throughput you will need to change the stock parameters (read the entire readme).

---

## Prerequisites

At a minimum, 4 RYLR998 modules (2 receiver and transmitter pairs), 2 devices running CircuitPython with UART communication, and a source of 3.3V DC power. This setup was tested using 2x Raspberry Pi Pico W modules running CircuitPython 9.2.8 attached to hosts running Ubuntu 24.04.3 LTS on a x86\_64 architecture.

## High-Level Overview

* **Goal:** Near-continuous, bidirectional data using inexpensive LoRa modules by **splitting directions onto separate radios/frequencies**.
* **Approach:** Each endpoint has **two RYLR998 modules** on separate UARTs:

  * **Uplink radio**: Transmits from Endpoint A to Endpoint B (A→B).
  * **Downlink radio**: Transmits from Endpoint B to Endpoint A (B→A).
* **Transport:** Each radio presents a serial link; software provides framing (e.g., **KISS**), then attached into IP (e.g., **`tncattach`** → `tnc0`) or used as raw framed serial for app-layer messages.
* **Why this works:** True full-duplex is achieved because **TX and RX do not share the same RF chain**; they’re independent links operating at different center frequencies (and ideally slightly different spreading/bandwidth plans to reduce self-interference).

## Logical Topology

```
+-------------------+                         +-------------------+
|   Endpoint A      |                         |    Endpoint B     |
|                   |                         |                   |
|  [UART0]          |  A→B Uplink (f_UL)      |          [UART0]  |
|    ┌─────────┐    |  LoRa (RYLR998 #A-UL)   |    ┌─────────┐    |
|    │ RYLR UL │====|=========================>|===│ RYLR DL │    |
|    └─────────┘    |                         |    └─────────┘    |
|                   |                         |                   |
|  [UART1]          |  B→A Downlink (f_DL)    |          [UART1]  |
|    ┌─────────┐    |  LoRa (RYLR998 #A-DL)   |    ┌─────────┐    |
|    │ RYLR DL │<===|=========================|====│ RYLR UL │    |
|    └─────────┘    |                         |    └─────────┘    |
|                   |                         |                   |
|   KISS/TNC layer  |<------ framed bytes --->|   KISS/TNC layer  |
|   (optional IP)   |<-- tnc0 / point-to-pt ->|   (optional IP)   |
+-------------------+                         +-------------------+
```

* **f\_UL** (uplink frequency): used **only** by radios that TX from A and RX at B.
* **f\_DL** (downlink frequency): used **only** by radios that TX from B and RX at A.

## Minimal Physical Wiring (per endpoint)

Each endpoint has **two UARTs** going to **two RYLR998** boards.

| Signal           | MCU/Host (e.g., Pico W / SBC) | RYLR998 | Notes                                         |
| ---------------- | ----------------------------- | ------- | --------------------------------------------- |
| TX (to module)   | UARTx TX                      | RXD     | Cross TX→RX                                   |
| RX (from module) | UARTx RX                      | TXD     | Cross RX←TX                                   |
| GND              | GND                           | GND     | Common ground                                 |
| VCC              | 3.3V (stable)                 | VDD     | 3.3V; budget \~120–150 mA per module under TX |
| RST (optional)   | GPIO (open-drain ok)          | RST     | For soft resets if needed                     |

**Power:** During testing, the 3.3V pinout from the Pi Pico W was used without decoupling, which provided for somewhat stable TCP/IP communication with \~5% packet loss. For increased stability, consider providing **separate decoupling** (e.g., 10 µF + 0.1 µF near each module). If both modules TX frequently, consider a **dedicated 3.3V regulator** ≥ 600 mA per endpoint to avoid brownouts.

**RF:** Using the stock antennas on the RYLR998 module, 3kbps TCP/IP communication was possible at a distance of about 20 meters. For practical applications, would recommend using **separate antennas** with **physical separation**. If they must be close, consider small shielding or placing the antennas at orthogonal polarizations.

## Frequency & Channel Plan

In the US, the frequencies below should be safe, but please consult your local regulations first. The RYLR998 firmware allows for adjustment of five parameters - **Spreading Factor, Bandwidth, Coding Rate, Preamble, and TX power** - with the optimal parameters depending on the link between your RYLR998 modules. The provided [code\_A.py](https://github.com/ykhan1999/rylr998_KISS/blob/main/code_A.py) and [code\_B.py](https://github.com/ykhan1999/rylr998_KISS/blob/main/code_A.py) files contain tuning knobs for each of the five parameters that can be adjusted to your preference.

* Recommended starting frequency (915 MHz ISM, adjust to your region & legal constraints):

  * **A→B uplink `f_UL`**: `915.000 MHz`
  * **B→A downlink `f_DL`**: `916.000 MHz` (≥ 2× BW spacing if possible)
* Recommended baseline parameters:

  * **TX\_DBM** = 10

    * Tx power in dBm, be mindful of local regulations and power consumption/heat production. range 0-22 dBm
  * **PARAM\_SF**   = 10

    * Spreading factor. Lower = faster communication but shorter distance. Range 5-11
  * **PARAM\_BW**   = 125

    * Bandwidth in KHz. Supported options are 125, 250, and 500.
  * **PARAM\_CR**   = 4

    * Coding rate; increase if bit error rate is high. 1: coding rate 4/5, 2: coding rate 4/6, 3: coding rate 4/7, 4: coding rate 4/8
  * **PARAM\_PRE**  = 12

    * Preamble; increase in noisy links. Range is 4-24 on network ID 18, otherwise limited to 12.

## Addressing & Pairing

Use **distinct addresses** per radio so you can route/label frames cleanly. This is already implemented with the default addressing in code\_A.py and code\_B.py.

* Endpoint A:

  * `ADDR_UL_TX` (A’s uplink TX radio) → set **destination = B\_UL\_RX**
  * `ADDR_DL_RX` (A’s downlink RX radio) ← receives from **B\_DL\_TX**
* Endpoint B:

  * `ADDR_UL_RX` (B’s uplink RX radio) ← receives from **A\_UL\_TX**
  * `ADDR_DL_TX` (B’s downlink TX radio) → set **destination = A\_DL\_RX**

You can keep **`NETWORK_ID`** the same for all four radios, the suggested default is 18 which allows for adjustment of the preamble.

## TX Pacing and Frame Size limits

From the provided code, several limits and pacing constraints are enforced:

* **TX\_MIN\_GAP\_S** = 1.30 seconds (default) — minimum gap enforced between transmissions to reduce collisions and radio overrun.
* **KISS\_MTU\_BYTES** = 156 bytes — maximum KISS frame size accepted from the host.
* **MAX\_RF\_ASCII\_BYTES** = 220 bytes — maximum ASCII payload length that can be sent to the radio.
* **RAW\_LIMIT** ≈ 156–160 bytes — after base64 overhead, the effective maximum raw IP/TCP packet size.
* **Priority Queues:**

  * ACK frames > Data frames > ICMP/low priority traffic.
* Oversized packets are dropped with diagnostic messages (`DROP oversize` / `DROP ascii too long`).
* Queues are capped:

  * ACK queue: 12 entries
  * Data queue: 16 entries
  * Low-priority queue: 4 entries.

This pacing ensures that TCP ACKs are preferentially transmitted even when the data queue is saturated, which helps maintain TCP reliability on lossy links.

## Installation and setup

### On CircuitPython devices (Endpoint A and B)

1. Copy **code\_A.py** as `code.py` onto CircuitPython device A.
2. Copy **code\_B.py** as `code.py` onto CircuitPython device B.
3. Copy the driver [rylr998\_cp.py](https://github.com/ykhan1999/rylr998_KISS/blob/main/lib/rylr998_cp.py "rylr998_cp.py") into the `/lib/` directory on both devices.
4. Ensure `boot.py` enables both console and data USB CDC interfaces.

### Monitoring logs (optional)

* When connected via USB, open a serial monitor to the **console port** (not the data/KISS port). You will see status messages such as radio configuration, statistics, and dropped frame diagnostics.
* Example using `screen` (replace `ttyACM0` with your device):

  ```bash
  screen /dev/ttyACM0 115200
  ```

### On Linux host (for each device)

1. Install prerequisites:

   ```bash
   sudo apt update
   sudo apt install python3-pip git
   pip install tncattach
   ```
2. Connect each Pico via USB. One port will expose the **data CDC interface** (`/dev/ttyACM*`).
3. Bring up the TNC interface:

   ```bash
   sudo tncattach /dev/ttyACM1 115200 --mtu 156 --noipv6 &
   ```

   * Replace `/dev/ttyACM1` with the correct data port.
   * This will create a `tnc0` interface.
4. Assign IP addresses:

   ```bash
   sudo ip addr add 10.10.10.1/30 dev tnc0   # on host A
   sudo ip addr add 10.10.10.2/30 dev tnc0   # on host B
   sudo ip link set tnc0 up
   ```
5. Verify connectivity:

   ```bash
   ping 10.10.10.2   # from host A
   ping 10.10.10.1   # from host B
   ```

### Testing the link

* Start with small pings (e.g., 32–64 bytes).
* Gradually increase payload sizes (e.g., `ping -s 128 10.10.10.2`).
* For TCP tests, use `iperf3` with reduced bandwidth (e.g., 3–5 kbps).

## Important notes and caveats

* **Not a true high-speed link:** Even with frequency-splitting, throughput is typically only a few kbps.
* **Thermal considerations:** Continuous TX at higher power may cause modules to overheat. Consider implementing a **thermal backoff** in code if pushing sustained traffic.
* **Regulatory compliance:** Frequencies, bandwidths, and TX power must comply with your region’s ISM band regulations.
* **Packet loss:** Expect occasional drops. Queue prioritization helps but does not eliminate loss entirely.
* **Power stability:** Two modules transmitting simultaneously can brown out the MCU if not adequately powered.
* **Antenna placement:** Close antennas may desensitize receivers. Provide spatial or polarization separation.

## Acknowledgements

* **markqvist** — for the [tncattach](https://github.com/markqvist/tncattach) utility that makes attaching KISS TNCs as Linux interfaces straightforward.
* **Adafruit** — for CircuitPython and board support.
* **REYAX** — for producing the RYLR998 module.
* **Contributors of pyserial/busio/USB CDC stack** — enabling USB data + console separation.
* **ChatGPT (OpenAI)** — for assistance in documenting, structuring, and clarifying the setup and codebase.
