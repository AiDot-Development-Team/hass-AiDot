# AiDot for Home Assistant

AiDot is a custom integration that brings compatible AiDot smart lights into
[Home Assistant](https://www.home-assistant.io/). It uses your AiDot account to
discover devices and then communicates with supported lights over the local
network for responsive control from dashboards, scenes, scripts, and
automations.

> [!NOTE]
> This repository provides a custom integration. It is not included with a
> standard Home Assistant installation.

## Features

- Configuration through the Home Assistant user interface
- Automatic device discovery from your AiDot account
- Local-network control after devices are discovered
- On/off and brightness control
- Color-temperature control on supported lights
- RGBW color control on supported lights
- BLE mesh lights controlled through a compatible AiDot hub
- Passive battery, charging, connectivity, and other advertised telemetry
- Combined on/off, colour, and brightness requests
- Named Wi-Fi presets and advertised BLE effects
- Existing AiDot app groups exposed as separate light entities
- Device availability and automatic reconnection
- Account reauthentication through Home Assistant
- Installation and updates through HACS

The entities and controls exposed for a light depend on the capabilities
reported by its AiDot product model. BLE hub support has been tested with
Linkind SL5C solar spotlights (`LK.A000181`). Other AiDot products are not
necessarily supported simply because they appear in the app.

## Requirements

Before installing the integration, make sure that:

- Your devices are already set up and working in the AiDot app.
- You know the country, username, and password used by the AiDot app.
- Home Assistant and the AiDot devices can communicate on the same local
  network.
- Client isolation, firewall rules, or VLAN boundaries do not block local
  discovery or device connections.

The integration uses the AiDot cloud to authenticate the account and retrieve
the device inventory. It uses UDP broadcast discovery and direct TCP
connections for supported local devices, so cloud access and local network
access are both required during normal setup.

## Installation

### HACS

1. Open HACS in Home Assistant.
2. Select **Integrations**.
3. Open the menu in the upper-right corner and select **Custom repositories**.
4. Add this repository's GitHub URL as an **Integration** repository.
5. Search for **AiDot** in HACS and download it.
6. Restart Home Assistant.

Future releases can be installed from the integration's page in HACS.

### Manual installation

1. Download the latest release from this repository.
2. Copy the `custom_components/aidot` directory into the
   `custom_components` directory inside your Home Assistant configuration
   directory.
3. Confirm that the resulting path is
   `<config>/custom_components/aidot/manifest.json`.
4. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to **Settings > Devices & services**.
2. Select **Add Integration**.
3. Search for **AiDot**.
4. Select the same country used by the AiDot app.
5. Enter your AiDot username and password.

After authentication, Home Assistant creates light entities for compatible
devices. Entity capabilities such as brightness, color temperature, and RGBW
color are detected automatically.

The integration is configured entirely through the Home Assistant user
interface; no YAML configuration is required.

## How it works

During setup, the integration authenticates with AiDot and downloads the
account's houses, devices, and product capability information. Home Assistant
then broadcasts discovery packets on the local network. Compatible devices
respond with their local address, allowing the integration to establish a
direct connection for control and state updates.

Local discovery uses UDP port `6666`, and device connections use TCP port
`10000`. Networks that separate Home Assistant from IoT devices may require
appropriate routing, firewall, and broadcast-relay configuration.

BLE lights use one shared encrypted connection to their powered hub. The hub
pushes device reports to Home Assistant; the integration does not poll sleeping
battery-powered lights. Availability represents the connection to the hub,
while the last-telemetry sensor records when a device actually reported data.
An available hub does not establish that every battery-powered light is awake
or reachable.

Charging is reported only from an actual `chargeState` device report or a
cloud snapshot whose product metadata advertises that attribute. SL5C cloud
records can contain `chargeState=0` without advertising charging support; this
alone leaves the charging sensor **unknown**, rather than **not charging**.
Battery increases, sunlight, and unrelated light reports do not establish the
current charging state. The charging entity's `state_source` and `last_reported`
attributes describe its evidence; a live charging report takes precedence over
later cloud snapshots.

Cloud device information is refreshed periodically and when hub recovery
requires it. Established local BLE commands do not contain a cloud access
token. Invalid cloud sessions are retried once with saved credentials; repeated
authentication failures use Home Assistant's reauthentication flow. Refreshed
hub addresses and credentials are applied without discarding device listeners.

The passive hub connection reconnects after a connection failure or 90 seconds
without incoming hub data. Repeated failures request fresh cloud information
at most once every five minutes.

Colour and brightness in a single `light.turn_on` action are sent together.
Brightness is quantized to integer percentages by the device protocol, so a
requested HA brightness of 128 can be reported as 127. Wi-Fi presets use the
upstream library's named-effect API; BLE effects are exposed only when product
metadata supplies names and numeric mode codes.

Four commands to different BLE lights may be in flight together, with command
order preserved for each light. An ordinary HA light group sends individual
device commands. App group entities use the separate cloud group operation
described below; the underlying BLE multicast encoding remains unverified.

## AiDot app groups

Create a normal mesh group in the AiDot phone app. The integration discovers
eligible groups in homes you own and adds a separate light entity for each.
Every member must be a supported BLE light attached to the same known hub.
Groups are never created, edited or paired by this integration.

Each group action sends one request to AiDot's cloud using your existing
account. Group control requires internet access. The plugin maintains its own
messaging session; no additional account or MQTT broker configuration is
needed. Individual lights continue to use their local transport.

Group state represents the last accepted group command and is marked as
assumed. A cloud acknowledgement does not prove every member received it.
Actual member notifications update the corresponding individual light and
telemetry entities; the integration never creates reports for silent members.
Individual changes do not automatically establish the aggregate group state.

Connection loss makes group entities unavailable. Commands time out without
automatic replay, and restarting or reconnecting does not resend the previous
light action. Group membership is refreshed with the device inventory. A
removed group becomes unavailable; reloading removes obsolete group devices.
Only attributes shared by every member and validated for the group transport
are exposed: on/off, brightness, RGBW colour, colour temperature and advertised
numeric effects. A steady colour command clears an active effect in the same
request. Arbitrary mesh configuration/broadcast commands are not exposed.

Group dispatch is ordered against pending local hub requests. A cloud reply
is not a physical-delivery barrier across the two transports. Avoid having
separate controllers issue competing group and individual changes.

Target the app group's light entity in dashboards, scenes and automations.
For example, for a group that exposes RGBW:

```yaml
action: light.turn_on
target:
  entity_id: light.example_garden
data:
  rgbw_color: [0, 255, 0, 0]
  brightness_pct: 50
```

## Troubleshooting

### Authentication fails

- Confirm that the credentials still work in the AiDot app.
- Select the same country or region used in the app.
- If Home Assistant requests reauthentication, enter the current AiDot
  password.

### No devices are created

- Confirm that the devices appear and work in the AiDot app.
- Make sure Home Assistant can send UDP broadcasts to the device network.
- Disable wireless client isolation or add the required network rules.
- Restart the integration after correcting the network configuration.

### A light is unavailable

- Confirm that the light is powered and connected to the network.
- Check that TCP port `10000` is reachable from Home Assistant.
- Reload the AiDot integration from **Settings > Devices & services**.

### Collecting logs

The following Home Assistant logger configuration can help diagnose setup and
communication problems:

```yaml
logger:
  logs:
    custom_components.aidot: debug
    aidot: debug
```

Restart Home Assistant after changing the logger configuration. Logs can
contain account, device, and network identifiers, so remove private data
before posting them publicly.

Known routine discovery messages are DEBUG rather than WARNING. Downloadable
integration diagnostics redact credentials and identifiers, disable active BLE
probes, and include passive telemetry, connection health, and cloud recovery
statistics.

## Development and validation

Using Python 3.13 or newer, the focused tests can run without a Home Assistant
installation:

```sh
python -m pip install python-aidot==0.3.58 pytest pytest-asyncio
python -m pytest -q
```

Tests exercise the transport, telemetry parsing, session recovery, redaction,
logging, group discovery and command correlation, reconnects without replay,
and light-platform behaviour. Light tests stub HA's entity plumbing;
they do not replace validation in a running Home Assistant instance.

The BLE path was tested on Home Assistant 2026.10.4 with SL5C firmware 3.4.4
and 3.6.36. Combined colours/brightness, passive telemetry, reconnection, and
leaving an app-configured Twinkle effect were checked. The observed hub path
remains limited to about five individual commands per second; concurrent
requests did not remove that limit. CCT commands were acknowledged and HA
displayed the requested values, but physical white-temperature accuracy has
not been independently verified.

## Contributing

Bug reports and pull requests are welcome. When reporting a problem, include:

- The integration and Home Assistant versions
- The affected device model
- The expected and observed behavior
- Relevant logs with credentials and personal identifiers removed
- The network layout when the problem involves discovery or connectivity

Use [GitHub Issues](https://github.com/AiDot-Development-Team/hass-AiDot/issues)
for reproducible bugs and feature requests. Keep pull requests focused, and
describe how the change was tested.

## License

This project is available under the [MIT License](LICENSE).
