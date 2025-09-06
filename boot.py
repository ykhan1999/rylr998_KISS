# boot.py — enable separate console+data USB CDC ports
import usb_cdc
# REPL on console, raw KISS on data
usb_cdc.enable(console=True, data=True)
print("boot.py: usb_cdc console+data enabled")
