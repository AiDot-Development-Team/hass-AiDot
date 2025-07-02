# AiDot Integration Communication Protocol

This document outlines the communication protocol used by the AiDot Home Assistant integration, based on the `python-aidot` library.

## Overview

The integration uses a hybrid model that combines cloud-based setup with local network control. 

1.  **Initial Setup**: Requires the AiDot cloud to authenticate and fetch device credentials.
2.  **Runtime Operation**: Relies entirely on the local network for device discovery and control, with no internet dependency after setup.

---

## Phase 1: Initial Setup & Authentication (Cloud-Reliant)

This phase occurs when you first add the AiDot integration in Home Assistant.

-   **What it does:** Authenticates your AiDot account and retrieves a roster of your devices, including the secret keys needed for local communication.
-   **How it works:**
    1.  Home Assistant connects to the AiDot cloud API.
    2.  It sends your username and password to log in and receive an authentication token.
    3.  Using the token, it fetches a list of your registered "homes" and devices.
    4.  Crucially, this device list contains the `deviceId`, `password`, and `aesKey` required for local control.
-   **Communication:** Home Assistant server -> AiDot Cloud Servers (Internet).

---

## Phase 2: Device IP Discovery (Local UDP Broadcast)

This phase runs continuously after the integration has been set up.

-   **What it does:** Finds the current local IP address of your AiDot devices.
-   **How it works:** AiDot devices periodically send out **UDP broadcast packets** on the local network. The `AidotClient` in the integration listens for these broadcasts to dynamically discover the IP addresses of your devices and map them to the `deviceId`s obtained from the cloud.
-   **Communication:** AiDot Device -> Home Assistant (Local Network). This requires that the Home Assistant instance can receive UDP broadcast traffic from the same network segment as the IoT devices.

---

## Phase 3: Device Control & State Updates (Local TCP)

This is the operational phase where Home Assistant interacts with your devices.

-   **What it does:** Sends commands (e.g., turn on/off, set brightness) to your devices and receives their current status.
-   **How it works:**
    1.  Using the IP address discovered in Phase 2, Home Assistant establishes a direct **TCP connection** to the device on **port 10000**.
    2.  It performs a local "login" to the device, using the credentials (`password`, `aesKey`) retrieved from the cloud in Phase 1.
    3.  All subsequent commands and status updates are sent over this encrypted, local TCP connection.
-   **Communication:** Home Assistant <-> AiDot Device (Local Network). No internet connection is required for this phase.
