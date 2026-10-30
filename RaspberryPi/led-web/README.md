# LED web interface

Small Flask app that controls the table LEDs through the Arduino Nano running
`LED-Arduino/LED-Control` (serial protocol described at the top of that sketch).
The Nano must be connected to the Pi over USB. The page is open to the local network, with no login.

## Try it without hardware

```
python3 -m venv venv && venv/bin/pip install -r requirements.txt
LED_FAKE=1 venv/bin/python app.py     # http://localhost:5001
```

## Install on the Pi

```
cp -r led-web ~/led-web && cd ~/led-web
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo usermod -aG dialout pi           # log out/in afterwards
ls /dev/serial/by-id/                 # find the Nano; put it in LED_PORT in the service file
sudo cp led-web.service /etc/systemd/system/
sudo systemctl enable --now led-web
```

Then open `http://<pi-address>:5001`. Port 5001 is used because OctoPi already
occupies 80 and 5000. The Nano and the GRBL Uno both appear as USB serial
devices, so use the `/dev/serial/by-id/...` path to keep them from swapping.
