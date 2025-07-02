# AiDot Home Assistant Integration

This is a custom integration for Home Assistant to control AiDot (formerly Linkind) smart home devices locally. It communicates with devices on your local network, providing a fast and reliable connection without relying on the cloud for control.

## Features

*   **Local Control**: No cloud dependency for device control. Commands are sent directly to your devices.
*   **Lights and Switches**: Support for AiDot smart lights and switches.
*   **Manual IP Configuration**: Option to manually specify device IP addresses, bypassing UDP discovery. This is ideal for complex network setups or when running Home Assistant in a Docker container without host networking.
*   **Easy Setup**: Fully configurable through the Home Assistant UI.

## Installation

### HACS (Recommended)

1.  Go to your HACS panel in Home Assistant.
2.  Click on "Integrations".
3.  Click the three dots in the top right and select "Custom repositories".
4.  Enter the URL for this repository, select "Integration" as the category, and click "Add".
5.  The AiDot integration will now appear in the list. Click "Install".
6.  Restart Home Assistant.

### Manual Installation

1.  Download the latest release from the [Releases](https://github.com/TonyD-AiDot/hass-AiDot/releases) page.
2.  Copy the `custom_components/aidot` directory into your Home Assistant `custom_components` directory.
3.  Restart Home Assistant.

## Configuration

1.  In Home Assistant, go to **Settings > Devices & Services**.
2.  Click **Add Integration** and search for **AiDot**.
3.  Enter your AiDot account credentials:
    *   **Email address**: The email you used to register with AiDot.
    *   **Password**: Your AiDot account password.
    *   **Country**: Select the server region for your account.
4.  The integration will log in and fetch a list of your houses. **Select the house** you want to add to Home Assistant.
5.  Next, you will be asked to choose a **Discovery Method**:
    *   **Automatic Discovery (Default)**: The integration will use UDP broadcasts to find your devices on the local network. This is the easiest method if your network configuration allows it.
    *   **Configure device IPs manually**: Choose this option if you have network issues, devices on different subnets, or are running Home Assistant in a Docker container without `--network=host`.
6.  If you chose manual configuration, you will be presented with a list of your devices. **Enter the static IP address** for each device you want to control. You can leave fields blank for devices you wish to be discovered automatically.
7.  Click **Submit**. The integration will be set up, and your devices will appear as entities in Home Assistant.

## Troubleshooting

### Devices are Unavailable (Especially in Docker)

The most common issue is that devices cannot be discovered automatically due to network limitations, particularly when running Home Assistant in a Docker container. Docker's default bridge network mode often blocks the UDP broadcast packets required for discovery.

**Solution**: Use the **Manual IP Configuration** option during setup. By providing a static IP address for your devices, the integration can connect to them directly without relying on UDP discovery.

## Supported Devices

*   Lights (with brightness and color temperature control)
*   Switches

---

*This integration uses the `python-aidot` library.*
