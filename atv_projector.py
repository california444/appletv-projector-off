#!/usr/bin/env python3
"""
atv-projector: schaltet den Projektor per ADB aus, sobald das Apple TV
in den Ruhezustand geht (Übergang On -> Off, mit Verzögerung).

Konfiguration über Umgebungsvariablen (siehe README.md).
Credentials von pyatv und der ADB-Schlüssel liegen unter $HOME (= /data).
"""

import asyncio
import logging
import os
import re
import signal
import sys

import pyatv
from pyatv import interface
from pyatv.const import FeatureName, FeatureState, PowerState
from pyatv.storage.file_storage import FileStorage


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Umgebungsvariable {name} fehlt")
    return value


ATV_HOST = env("ATV_HOST", required=True)             # IP des Apple TV
ATV_ID = env("ATV_ID") or None                        # optional: Identifier aus "atvremote scan"
PROJECTOR = env("PROJECTOR_ADDR", required=True)      # z. B. 192.168.1.30:5555
OFF_KEYCODE = env("OFF_KEYCODE", "KEYCODE_SLEEP")     # oder KEYCODE_POWER
WAKE_KEYCODE = env("WAKE_KEYCODE", "KEYCODE_WAKEUP")  # weckt nur, schaltet nie aus
WAKE_WAIT = float(env("WAKE_WAIT", "3"))              # Sekunden nach dem Aufwecken
OFF_DELAY = float(env("OFF_DELAY", "20"))             # Sekunden Wartezeit nach "Off"
POLL_INTERVAL = float(env("POLL_INTERVAL", "10"))     # Sekunden zwischen Statusabfragen
RECONNECT_DELAY = float(env("RECONNECT_DELAY", "30")) # Sekunden bis zum Neuverbinden
DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")
ATV_CEC_NAME = env("ATV_CEC_NAME", "Apple TV")        # CEC-Name in dumpsys hdmi_control
# Activities, die einen Eingang anzeigen (Regex). Alles andere gilt als App.
INPUT_ACTIVITIES = env(
    "INPUT_ACTIVITIES", r"com\.formovie\.tvinput/|InternalTvActivity"
)

logging.basicConfig(
    level=env("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("atv-projector")


# --------------------------------------------------------------------------- ADB

async def adb(*args, timeout=15):
    """Führt einen adb-Befehl aus und liefert (returncode, Ausgabe)."""
    proc = await asyncio.create_subprocess_exec(
        "adb", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timeout"
    return proc.returncode, out.decode(errors="replace").strip()


async def projector_ready():
    """Baut die ADB-Verbindung frisch auf und prüft, ob sie nutzbar ist."""
    # Alte Verbindung verwerfen: nach einem Standby ist sie oft tot.
    await adb("disconnect", PROJECTOR)
    _, connect_out = await adb("connect", PROJECTOR)
    rc, state = await adb("-s", PROJECTOR, "get-state")
    if rc == 0 and state == "device":
        return True, state
    return False, f"{connect_out} / {state}"


async def projector_off():
    ok, info = await projector_ready()
    if not ok:
        log.info("ADB nicht bereit (%s) – adb-Server wird neu gestartet", info)
        await adb("kill-server")
        ok, info = await projector_ready()
    if not ok:
        log.warning(
            "Projektor per ADB nicht erreichbar (%s) – vermutlich bereits aus "
            "oder Freigabe fehlt", info,
        )
        return

    # Wurde das Apple TV nur per CEC mit aufgeweckt und läuft eine andere
    # Quelle, schläft es irgendwann von selbst ein. Dann bleibt der Projektor an.
    shows, info = await shows_apple_tv()
    if shows is False:
        log.info("Projektor zeigt nicht das Apple TV (%s) – bleibt an", info)
        await adb("disconnect", PROJECTOR)
        return
    if shows is None:
        log.warning("Aktive Quelle nicht bestimmbar (%s) – schalte trotzdem aus", info)
    else:
        log.info("Projektor zeigt das Apple TV (%s)", info)

    # Nach dem CEC-Standby des Apple TV schaltet der Projektor oft nur das
    # Display ab (Display Power: state=OFF), das System läuft weiter. Ein
    # KEYCODE_POWER weckt ihn in diesem Zustand nur auf, statt auszuschalten.
    # Deshalb erst das Display sicher einschalten, dann ausschalten.
    state = await display_state()
    log.info("Projektor: Display-Status vor dem Ausschalten: %s", state)

    if DRY_RUN:
        log.info("DRY_RUN: würde jetzt ggf. %s und danach %s senden",
                 WAKE_KEYCODE, OFF_KEYCODE)
    else:
        if state != "ON":
            await key(WAKE_KEYCODE)
            await asyncio.sleep(WAKE_WAIT)
            log.info("Projektor: Display-Status nach dem Aufwecken: %s",
                     await display_state())
        await key(OFF_KEYCODE)

    await adb("disconnect", PROJECTOR)


async def shows_apple_tv():
    """Prüft, ob der Projektor gerade den HDMI-Eingang des Apple TV zeigt.

    Liefert (True/False, Info) oder (None, Info), wenn es sich nicht
    bestimmen lässt.
    """
    rc, out = await adb("-s", PROJECTOR, "shell", "dumpsys", "hdmi_control")
    if rc != 0:
        return None, f"dumpsys hdmi_control: {out}"
    # Port des Apple TV aus der CEC-Geräteliste, z. B.
    # "display_name: Apple TV power_status: 0 physical_address: 0x1000 port_id: 1"
    atv = re.search(
        rf"display_name: {re.escape(ATV_CEC_NAME)} .*?physical_address: (0x[0-9a-fA-F]+)",
        out,
    )
    routing = re.search(r"mActiveRoutingPath: (0x[0-9a-fA-F]+)", out)
    if not atv or not routing:
        return None, (f"Apple TV in CEC-Liste: {atv.group(1) if atv else 'fehlt'}, "
                      f"RoutingPath: {routing.group(1) if routing else 'fehlt'}")

    rc, out = await adb("-s", PROJECTOR, "shell", "dumpsys", "activity", "activities")
    resumed = re.search(r"mResumedActivity: ActivityRecord\{\S+ \S+ (\S+)", out)
    if rc != 0 or not resumed:
        return None, "Vordergrund-Activity nicht lesbar"

    activity = resumed.group(1)
    info = (f"RoutingPath {routing.group(1)}, Apple TV {atv.group(1)}, "
            f"Vordergrund {activity}")
    is_input = re.search(INPUT_ACTIVITIES, activity) is not None
    same_port = int(routing.group(1), 16) == int(atv.group(1), 16)
    return is_input and same_port, info


async def key(keycode):
    rc, out = await adb("-s", PROJECTOR, "shell", "input", "keyevent", keycode)
    if rc == 0:
        log.info("Projektor: %s gesendet", keycode)
    else:
        log.warning("Senden von %s fehlgeschlagen: %s", keycode, out)


async def display_state():
    """Liest 'Display Power: state=ON/OFF/…' aus dumpsys power."""
    rc, out = await adb("-s", PROJECTOR, "shell", "dumpsys", "power")
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Display Power: state="):
                return line.split("=", 1)[1]
    return "unbekannt"


# ------------------------------------------------------------------ Apple TV

class Watcher(interface.PowerListener, interface.DeviceListener):
    """Verfolgt den Power-Status und löst das Ausschalten aus."""

    def __init__(self):
        self.state = None            # letzter bekannter Status (On/Off)
        self.pending = None          # laufender Ausschalt-Task
        self.lost = asyncio.Event()  # Verbindung zum Apple TV verloren

    # PowerListener (Push, nur wenn das Apple TV Updates schickt)
    def powerstate_update(self, old_state, new_state):
        self.handle(new_state, "push")

    # DeviceListener
    def connection_lost(self, exception):
        log.warning("Verbindung zum Apple TV verloren: %s", exception)
        self.lost.set()

    def connection_closed(self):
        self.lost.set()

    def handle(self, new, source):
        if new not in (PowerState.On, PowerState.Off):
            return
        old = self.state
        if new == old:
            return
        self.state = new
        log.info("Apple TV: %s -> %s (%s)", old, new, source)

        if new == PowerState.On:
            if self.pending and not self.pending.done():
                self.pending.cancel()
                log.info("Ausschalten abgebrochen – Apple TV ist wieder an")
        elif old == PowerState.On:
            # Nur beim echten Übergang On -> Off, nicht beim Start des Daemons
            self.pending = asyncio.create_task(self.delayed_off())

    async def delayed_off(self):
        log.info("Projektor wird in %.0f s ausgeschaltet", OFF_DELAY)
        try:
            await asyncio.sleep(OFF_DELAY)
        except asyncio.CancelledError:
            return
        if self.state == PowerState.Off:
            await projector_off()


async def sleep_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def close_atv(atv):
    try:
        tasks = atv.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:  # noqa: BLE001
        pass


def exception_handler(loop, context):
    # pyatv "klopft" beim Scan Ports an, ohne die Tasks abzuwarten. Ist das
    # Apple TV nicht erreichbar, landet sonst jeder Fehlschlag als Traceback
    # ("Task exception was never retrieved") im Log.
    task = context.get("future")
    coro = task.get_coro() if isinstance(task, asyncio.Task) else None
    if getattr(coro, "__qualname__", "") == "_async_knock":
        log.debug("Knock fehlgeschlagen: %s", context.get("exception"))
        return
    loop.default_exception_handler(context)


async def run():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(exception_handler)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    watcher = Watcher()
    last_error = None  # gleiche Fehler nur einmal als Warnung loggen

    log.info(
        "Start: Apple TV %s, Projektor %s, Keycode %s, Verzögerung %.0f s%s",
        ATV_HOST, PROJECTOR, OFF_KEYCODE, OFF_DELAY, " (DRY_RUN)" if DRY_RUN else "",
    )
    log.info("Image-Revision: %s", os.environ.get("APP_REVISION") or "unbekannt")

    while not stop.is_set():
        atv = None
        try:
            # Bei jedem Verbindungsversuch neu laden, damit ein Pairing, das nach
            # dem Start des Daemons erfolgt ist, ohne Neustart übernommen wird.
            storage = FileStorage.default_storage(loop)
            await storage.load()

            confs = await pyatv.scan(
                loop, hosts=[ATV_HOST], identifier=ATV_ID, storage=storage, timeout=5
            )
            if not confs:
                raise RuntimeError(f"kein Apple TV unter {ATV_HOST} gefunden")

            creds = ", ".join(
                f"{s.protocol.name}={'ja' if s.credentials else 'nein'}"
                for s in confs[0].services
            )
            log.info("Credentials aus %s: %s", storage, creds)

            atv = await pyatv.connect(confs[0], loop, storage=storage)
            if not atv.features.in_state(FeatureState.Available, FeatureName.PowerState):
                log.warning(
                    "PowerState wird nicht als verfügbar gemeldet – "
                    "AirPlay- und Companion-Pairing prüfen"
                )

            watcher.lost.clear()
            atv.listener = watcher
            atv.power.listener = watcher
            log.info("Verbunden mit %s", confs[0].name)
            last_error = None

            while not stop.is_set() and not watcher.lost.is_set():
                watcher.handle(atv.power.power_state, "poll")
                await sleep_or_stop(stop, POLL_INTERVAL)

        except Exception as exc:  # noqa: BLE001
            if str(exc) != last_error:
                log.warning("Apple TV: %s (weitere Versuche alle %.0f s)",
                            exc, RECONNECT_DELAY)
                last_error = str(exc)
            else:
                log.debug("Apple TV: %s", exc)
        finally:
            if atv:
                await close_atv(atv)

        if not stop.is_set():
            await sleep_or_stop(stop, RECONNECT_DELAY)

    log.info("Beendet")


if __name__ == "__main__":
    asyncio.run(run())
