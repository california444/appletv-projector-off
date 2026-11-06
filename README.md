# appletv-projector-off

Daemon, der einen Android-Projektor per ADB ausschaltet, sobald das Apple TV in den
Ruhezustand geht. Das Einschalten übernimmt weiterhin HDMI-CEC.

Entwickelt und getestet mit einem Apple TV 4K (2. Gen., tvOS 27) und einem Fengmi 4K
Cinema Pro (FengOS) auf einem Raspberry Pi 5 mit Docker und Portainer.

## Ablauf

1. Der Daemon hält über [pyatv](https://pyatv.dev) eine Verbindung zum Apple TV und liest
   den Power-Status. Er fragt alle `POLL_INTERVAL` Sekunden ab und nimmt zusätzlich
   Push-Meldungen an, soweit das Apple TV welche schickt.
2. Beim Wechsel **An → Aus** wartet er `OFF_DELAY` Sekunden. Wird das Apple TV in dieser
   Zeit wieder eingeschaltet, bricht er ab.
3. Danach baut er die ADB-Verbindung zum Projektor neu auf und sendet `OFF_KEYCODE`.
   Nach einem Standby ist die alte ADB-Verbindung meist tot, deshalb wird sie jedes Mal
   neu aufgebaut. Hilft das nicht, startet er den adb-Server neu.
4. Ist der Projektor nicht erreichbar, weil er schon aus ist, landet nur eine
   Warnung im Log. Der Daemon läuft weiter.
5. Ein Apple TV, das beim Start des Daemons schon aus ist, löst nichts aus.
6. Vor dem Ausschalten prüft er, ob der Projektor gerade das Apple TV zeigt: Der aktive
   HDMI-Pfad (`mActiveRoutingPath` in `dumpsys hdmi_control`) muss zur CEC-Adresse des
   Apple TV passen, und im Vordergrund muss ein Eingang laufen statt einer App. Sonst bleibt
   der Projektor an, etwa wenn das Apple TV beim Einschalten per CEC mit aufgewacht ist,
   aber eine andere Quelle läuft. Lässt sich die Quelle nicht bestimmen, schaltet er
   trotzdem aus.

pyatv-Credentials (`.pyatv.conf`) und ADB-Schlüssel (`.android/`) liegen im Volume
`/data`. Sie bleiben also bei Neubau und Neustart des Containers erhalten.

## Voraussetzungen

- **Feste IPs** für Apple TV und Projektor, z. B. per DHCP-Reservierung im Router.
  Achtung beim Apple TV: Nutzt es im WLAN eine private (lokal verwaltete) MAC-Adresse und
  wechselt tvOS sie, greift die Reservierung nicht mehr. Abhilfe: die private Adresse für
  dieses WLAN abschalten, falls tvOS das anbietet, oder das Apple TV per LAN-Kabel
  anschließen.
- **ADB über Netzwerk** ist am Projektor aktiv (Entwickleroptionen).
- Der **Tastencode zum Ausschalten** ist bekannt, z. B. `KEYCODE_POWER`.

## Image

Bei jedem Push auf `main` und einmal pro Woche baut GitHub Actions das Image für
`linux/arm64` und veröffentlicht es unter `ghcr.io/california444/appletv-projector-off`.
Verfügbare Tags:

| Tag | Bedeutung |
| --- | --- |
| `latest` | aktueller Stand von `main` |
| `sha-<commit>` | genau dieser Commit – für Rollbacks |
| `<JJJJMMTT>` | Stand des jeweiligen Build-Tages |

Selbst bauen (optional):

```sh
docker build -t appletv-projector-off .
```

## Einrichtung

### 1. Konfiguration

Kopiere `stack.env.example` nach `stack.env` und trage deine Werte ein. In Portainer
trägst du sie stattdessen im Stack unter „Environment variables“ ein oder lädst die Datei
dort hoch. `DATA_DIR` ist das Host-Verzeichnis für Pairing und ADB-Schlüssel.

Die einmaligen Befehle in den Schritten 2–4 führst du direkt mit `docker run` aus:

```sh
DATA_DIR=/srv/atv-projector/data      # wie in stack.env
ATV=192.168.1.20                      # IP des Apple TV
PROJ=192.168.1.30:5555                # IP:Port des Projektors
alias atvp="docker run --rm -it -v $DATA_DIR:/data ghcr.io/california444/appletv-projector-off:latest"
```

### 2. Apple TV pairen (einmalig)

Den Power-Status liefert das MRP-Protokoll. Ab tvOS 15 läuft MRP über AirPlay. Das
Ein- und Ausschalten laufen über Companion
([pyatv Supported Features](https://pyatv.dev/documentation/supported_features/)).
Deshalb pairst du **AirPlay und Companion**. Das Apple TV zeigt dabei jeweils eine PIN an.

```sh
atvp atvremote -s $ATV --protocol airplay pair
atvp atvremote -s $ATV --protocol companion pair
```

Test:

```sh
atvp atvremote -s $ATV power_state
```

Die Ausgabe sollte `PowerState.On` oder `PowerState.Off` sein. Schalte das Apple TV einmal
aus und prüf, ob sich der Wert ändert. Den Identifier für `ATV_ID` liefert
`atvp atvremote scan`.

### 3. ADB-Freigabe für den Container (einmalig)

Der Container hat einen eigenen ADB-Schlüssel. Er muss deshalb einmal am Projektor
bestätigt werden. Wechsle vorher auf den Startbildschirm des Projektors, damit der Dialog
sichtbar ist.

```sh
atvp sh -c "adb connect $PROJ; sleep 20; adb devices"
```

Am Projektor setzt du den Haken bei „immer erlauben“ und bestätigst. Die Ausgabe muss am
Ende `<IP>:5555  device` zeigen. Bei `unauthorized` wiederholst du den Befehl.

### 4. Ausschalten aus dem Container testen

```sh
atvp sh -c "adb connect $PROJ; adb -s $PROJ shell input keyevent KEYCODE_POWER"
```

Der Projektor sollte ausgehen und die Lüfter stoppen. Bei FengOS öffnet `KEYCODE_POWER`
laut AVS-Forum ein Ausschaltmenü, das nach etwa 10 Sekunden von selbst ausschaltet.
Drückt in dieser Zeit jemand eine Taste, bricht das Ausschalten ab.

### 5. Im Probebetrieb starten

Lass `DRY_RUN=true` vorerst gesetzt. Der Daemon protokolliert dann nur, was er tun würde.
Lege den Stack mit dem Inhalt von `docker-compose.yml` an und verfolge das Log:

```sh
docker logs -f atv-projector
```

Beim Start sollte im Log Folgendes erscheinen:

```text
Start: Apple TV <IP>, Projektor <IP>:5555, Keycode KEYCODE_POWER, Verzögerung 20 s (DRY_RUN)
Image-Revision: <commit>
Verbunden mit <Name des Apple TV>
```

Schalte dann das Apple TV aus:

```text
Apple TV: PowerState.On -> PowerState.Off (push|poll)
Projektor wird in 20 s ausgeschaltet
Projektor zeigt das Apple TV (RoutingPath 0x1000, Apple TV 0x1000, Vordergrund …)
DRY_RUN: würde jetzt ggf. KEYCODE_WAKEUP und danach KEYCODE_POWER senden
```

### 6. Scharf schalten

Setz `DRY_RUN=false` und deploy den Stack neu.

Beim ersten echten Durchlauf prüfst du zwei Fälle:

- **Nur das Bild ist aus:** CEC hat die Anzeige abgeschaltet, das System läuft aber
  weiter. `KEYCODE_POWER` muss den Projektor jetzt ganz ausschalten und darf ihn nicht
  wieder einschalten.
- **Wieder einschalten:** Schalt das Apple TV danach wieder ein und prüf, ob der
  Projektor per CEC angeht.

### Aktualisieren

```sh
docker compose pull && docker compose up -d
```

In Portainer: Stack öffnen, „Update the stack“ mit „Re-pull image“.

## Konfiguration

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `ATV_HOST` | – | IP des Apple TV (Pflicht) |
| `ATV_ID` | – | Identifier aus `atvremote scan` (optional) |
| `PROJECTOR_ADDR` | – | `IP:5555` des Projektors (Pflicht) |
| `OFF_KEYCODE` | `KEYCODE_SLEEP` | Tastencode zum Ausschalten, z. B. `KEYCODE_POWER` |
| `WAKE_KEYCODE` | `KEYCODE_WAKEUP` | Weckt das Display vor dem Ausschalten |
| `WAKE_WAIT` | `3` | Sekunden nach dem Aufwecken |
| `OFF_DELAY` | `20` | Sekunden Wartezeit nach „Aus“ |
| `POLL_INTERVAL` | `10` | Sekunden zwischen den Statusabfragen |
| `RECONNECT_DELAY` | `30` | Wartezeit nach Verbindungsverlust zum Apple TV |
| `DRY_RUN` | `false` | `true` = nur protokollieren |
| `ATV_CEC_NAME` | `Apple TV` | CEC-Name des Apple TV in `dumpsys hdmi_control` |
| `INPUT_ACTIVITIES` | `com\.formovie\.tvinput/\|InternalTvActivity` | Regex für Activities, die einen Eingang zeigen |
| `LOG_LEVEL` | `INFO` | `DEBUG` für Fehlersuche |
| `DATA_DIR` | `./data` | Host-Verzeichnis für `/data` (nur in `docker-compose.yml`) |

## Fehlersuche

| Log-Meldung | Ursache / Abhilfe |
| --- | --- |
| `kein Apple TV unter <IP> gefunden` | IP hat sich geändert (MAC-Hinweis oben) oder das Apple TV ist im Tiefschlaf |
| `PowerState wird nicht als verfügbar gemeldet` | AirPlay-/Companion-Pairing fehlt oder ist ungültig → Schritt 2 wiederholen |
| `Projektor per ADB nicht erreichbar … unauthorized` | Die Freigabe für den Container fehlt → Schritt 3 |
| `Projektor per ADB nicht erreichbar … refused` | ADB ist am Projektor aus oder der Projektor ist schon aus |
| `Projektor zeigt nicht das Apple TV … – bleibt an` | Gewollt: andere Quelle oder App aktiv. Steht dort fälschlich eine Eingangs-Activity, `INPUT_ACTIVITIES` ergänzen |
| `Aktive Quelle nicht bestimmbar … – schalte trotzdem aus` | Apple TV fehlt in der CEC-Liste (Name prüfen → `ATV_CEC_NAME`) oder `dumpsys` liefert nichts |

## Hinweise

- **Netzwerk:** Der Container läuft im normalen Bridge-Netz, weil pyatv mit einer festen IP
  scannt (unicast) und ADB nur ausgehend verbindet. `network_mode: host` und mDNS sind
  nicht nötig.
- **Push-Meldungen:** Ob das Apple TV Power-Push-Meldungen schickt, ist nicht belegt.
  Deshalb fragt der Daemon den Status zusätzlich regelmäßig ab.
- **`OFF_DELAY`:** Die Verzögerung fängt kurzes Aus- und Wiedereinschalten des Apple TV ab.
- **Zugangsdaten:** `DATA_DIR` enthält das Apple-TV-Pairing und den privaten ADB-Schlüssel.
  Ein Backup davon sollte nicht unverschlüsselt außer Haus liegen.
- `stack.env` und `.env` sind durch `.gitignore` vom Repo und durch `.dockerignore` vom
  Image-Build ausgeschlossen.
- Neue Packages in der GitHub Container Registry sind zunächst privat. Für einen Pull ohne
  Anmeldung muss das Package in den Package-Einstellungen auf „public“ gestellt werden,
  sonst ist auf dem Host ein `docker login ghcr.io` mit einem PAT (Scope `read:packages`)
  nötig.

## Lizenz

MIT
