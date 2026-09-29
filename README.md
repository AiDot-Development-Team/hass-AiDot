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
- Device availability and automatic reconnection
- Account reauthentication through Home Assistant
- Installation and updates through HACS

The entities and controls exposed for a light depend on the capabilities
reported by its AiDot product model. Devices other than compatible Wi-Fi
lights may appear in the AiDot app but are not necessarily supported by this
integration.

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
4. Add `https://github.com/AiDot-Development-Team/hass-AiDot` as an
   **Integration** repository.
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
