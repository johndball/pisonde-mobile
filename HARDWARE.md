# PiSonde Mobile hardware


This is the field build's hardware list, rather than a promise that every adapter or power source is interchangeable.

| Item | Used for / notes |
| --- | --- |
| Raspberry Pi 4 | Receiver, local database and web dashboard; 64-bit Raspberry Pi OS Lite |
| CanaKit case with fan | Cooling and physical protection |
| 128 GB microSD card | OS, application, packets and local backups; keep an additional card for initial testing |
| RTL-SDR Blog V4 | The single radio receiver; the installer discovers its USB serial locally |
| 2 m / 70 cm antenna and suitable coax/adapters | The existing field antenna; performance near 400–406 MHz depends on the antenna, rather than its amateur-band label alone |
| Short, good-quality USB extension | Flexible SDR/antenna placement; cable quality and voltage drop matter |
| Linksys AE6000 USB Wi-Fi adapter | Existing dual-band upstream radio; onboard Pi Wi-Fi stays dedicated to the local AP |
| USB-C power cable | Connects the Pi to its power source |
| Vehicle cigarette-lighter USB power adapter | The field adapter is rated 40 W; that total rating alone does not establish stable power at the Pi |
| Ethernet cable and setup computer | Recommended for the first installation, downloads, flashing and SSH |
| Optional momentary button and two leads | Controlled shutdown using GPIO3 and ground; wiring is described below |

A receiver GPS puck, display and second SDR are not required. Payload coordinates come from decoded telemetry. The two Wi-Fi adapters have separate jobs; they do not provide a second radio-frequency receiver.


See [INSTALL.md](INSTALL.md) for OS preparation, connections and the optional shutdown button.
