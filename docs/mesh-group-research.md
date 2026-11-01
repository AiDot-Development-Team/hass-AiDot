# Native mesh group command investigation

Branch: `codex/mesh-group-commands`, based on consolidated fork main
`5ba7abc04c6d72d8801e452a369471b8bb7894a9`.
The existing contribution remains separate in upstream PR #42.

## Confirmed

An AiDot account's existing groups can be read with `GET /groups?houseId=...`,
alongside the `/houses` and `/devices?houseId=...` endpoints already used by
python-aidot. A normal outdoor group exposes:

- `id`: cloud group identifier; this is distinct from a mesh destination.
- `deviceIds`: explicit cloud device membership.
- `bleMeshGroupType`: `normal` on the inspected group.
- `bleMeshGroupAddress`: a decimal string in the dynamic mesh group range.
- `bleMeshLocalGroupDevices`: additional local group metadata; its meaning has
  not been established. Do not treat its entries as cloud device identifiers.

The inspected installation has a normal outdoor group containing all 14 SL5C
lights, with a dynamic mesh group address. All members use the same hub.
No group membership, pairing, firmware or Home Assistant integration files
were changed during discovery.

Bluetooth SIG defines dynamic group destinations in `0xC000`–`0xFEFF` and
describes group publication/subscription in the
[Mesh Protocol](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/MshPRT_v1.1/out/en/index-en.html).
An address in this range is evidence for a possible multicast destination;
it does not establish AiDot's TCP command encoding or successful delivery.

The current integration and python-aidot 0.3.58 only expose the per-device
`setDevAttrReq` path. A normal HA light group therefore still sends one command
per member. Prior live tests measured approximately five individual commands
per second, with a 14-light operation taking about 2.7 seconds.

## Read-only discovery tool

Run `python tools/mesh_groups.py --config-dir /config` in an environment with
the integration's python-aidot dependency and read access to HA's config.
Use `--entry-id` if the installation has more than one AiDot entry.

The tool reads existing account data, retrieves owned-home groups and devices,
then reports anonymous group/hub identifiers, address and member count. It
rejects unknown members, mixed hubs, non-BLE members and non-dynamic addresses.
It sends no device command and never edits group membership. Raw account/API
responses and device names are not printed or written to an output file.
`localCommandVerified` remains false until there is independent wire evidence.
The tool was also run successfully with the installed python-aidot 0.3.58 API
base (v35), finding the same 14-member group. The original research suite had 107 passing tests,
including address classification, exact membership, shared-hub eligibility and
output redaction checks added for this tool.

## Isolated local probes (2026-10-31)

The inspected hub runs firmware 1.06.11. Opening a second control session while
HA's listener was active interrupted both connections. The exact existing
single-light transport also failed in that arrangement. After temporarily
disabling just the AiDot entry through HA's supported config-entry API, the
same single-light on/off baseline succeeded with code 200 and member reports.
HA was re-enabled afterward and normal control was verified again. The tested
hub therefore needs exclusive access for a standalone probe; use HA's shared
session for any eventual integration feature.

Earlier `getDevAttrReq` probes made with a competing session are inconclusive.
They do not establish that either reads or native group writes are unsupported.

With the normal single-light baseline passing in isolation, the bounded
on/off-only write experiments produced:

| Experimental request | Result |
| --- | --- |
| Group cloud ID in the ordinary `setDevAttrReq` device fields | Rejected, code -2 |
| Decimal mesh group address in those fields | Rejected, code -2 |
| Hub as destination plus group ID/address payload fields | Rejected, code -10 |
| Known member as destination plus those group payload fields | Code 200; only that member reported a change |
| Guessed `setGroupAttrReq` / `setGroupDevAttrReq` method names, including a `group` service variant | No correlated acknowledgement within the probe timeout |

These are unsuccessful encoding hypotheses, not a verified list of supported
protocol methods. No unverified method has been added to the integration.
Normal individual off commands for all 14 members were acknowledged during
cleanup. The entry returned to loaded/enabled, with a connected hub and no
connection error; HA on/off control was checked after restoration.

## Synchronized phone-app capture

A second test used a ready/recording/done handshake. The user operated the app
group on, waited five seconds, then off while other controllers were paused.
The capture ran for 57.2 seconds, with all 14 entities initially reporting off
and the HA hub connection healthy.

The trace contained two `pingreq`/`pingresp` pairs, no light commands and no
device attribute reports. The diagnostic device-update counters did not
increase for any of the 14 lights. Thus this capture did not expose the app's
group operation. It cannot distinguish direct phone Bluetooth, phone LAN
traffic outside the HA host's visibility, or a cloud path. Repeating the same
capture point will not by itself reveal a command encoding.

The user then confirmed physical group on/off operation with Bluetooth off in
iPhone Settings, followed by a second successful test with both Bluetooth and
Wi-Fi off and cellular data on. This establishes a cloud-to-hub control path;
direct phone Bluetooth and phone LAN access are not required for that path.
It does not establish whether the cloud sends one mesh group request or fans
out individual member commands, nor does it establish a latency improvement.

## Cloud observation

AiDot's [official web client](https://app.aidot.com/SignIn), examined on
2026-10-31, obtains a per-session MQTT configuration from
`GET /v29/commons/mqttConfig?source=WebPC&sessionId=...`. Its public bundle
constructs a TLS WebSocket connection to the returned host at `/mqtt` and
contains the ordinary individual `setDevAttrReq` builder. No mesh group
request builder was found in that bundle; this is not evidence that the
phone app lacks one.

A bounded read-only observer authenticated successfully using the existing
HA account token and a separate WebPC session nonce. TLS certificate
validation remained enabled. The broker accepted the account's client
notification channel and the owned hub's broadcast channel, but rejected
subscriptions to the server-request, hub-client and user-broadcast channels.
Those rejected subscriptions were not retried or bypassed. No cloud device
command was published, and no MQTT password was saved.

During controlled HA single-light on/off tests, the hub's broadcast channel
yielded acknowledgements with code 200, the member's `devId` and `attr.OnOff`
values 1 and 0. These establish acknowledgement visibility, not independent
physical feedback or access to the phone's outgoing request. Spotlight 14 was
returned to off.

## Cloud group operation captured

The synchronized cellular-only app test at the cloud observation point ran for
72.785 seconds and recorded six messages. Two arrived on
`iot/v1/c/{userId}/group/excGroupResp`, one for on and one for off. Each had
`service: "group"`, `method: "excGroupResp"` and `ack.code: 200`. The echoed
payload contained:

```json
{
  "method": "set_prop",
  "groupId": "<owned cloud group ID>",
  "bleMeshGroupAddress": "<decimal mesh group address>",
  "source": "<redacted app value>",
  "params": {"OnOff": 1},
  "homeId": "<owned home ID>"
}
```

The off response used `OnOff: 0`. Four later hub broadcast messages, on
`device/setDevAttrNotif`, reported two members on and then off. The two group
responses were about 7.3 seconds apart. These are response/notification times,
not measured phone-to-light latency, and the two reporting members cannot be
treated as confirmation from all 14 lights.

Using the observed response format and the official web client's documented
server-request topic convention, a single off-only probe was sent to
`iot/v1/s/{userId}/group/excGroupReq`. Its envelope used `method: "excGroupReq"`,
`service: "group"`, a unique `seq`, `srcAddr: "0.{userId}"`, an integer Unix
`tst`, the current WebPC `clientId` and the payload above with `OnOff: 0`.
The probe omitted `source`. A matching `excGroupResp` with code 200 arrived in
369.5 ms. This establishes cloud acceptance of that request; an off-only
operation on already-off lights does not establish physical delivery.

### Watched prototype on/off test

The tested request builder was then used from the HA Core container to send
one group-on request, hold for eight seconds after acceptance, and send one
group-off request. Both replies matched the requested group, home, address,
state and sequence and returned code 200:

| Operation | Cloud acknowledgement latency |
| --- | --- |
| Group on | 297.8 ms |
| Group off | 193.6 ms |

The process completed successfully after exactly two outgoing cloud group
requests, with no local requests and no fallback cleanup required. The user
watched and confirmed that all the lights followed the on/off sequence
together. This establishes physical group control using the prototype, with
the `source` field omitted. The lights finished off.

Hub broadcast reports were incomplete: three members reported on, and one
reported off within the observation window. They do not replace the user's
physical confirmation. The measured times above end at cloud acceptance;
phone-to-light latency and the spread between individual light changes were
not instrumented. They cannot be treated as a measured speedup over the
approximately 2.7-second individual-command baseline.

This verifies a cloud group API. The cloud-to-hub wire operation and any
native Bluetooth multicast remain unverified; the cloud or hub could still
fan out to members internally.

`tools/cloud_group_protocol.py` contains the request builder and reply
validator for this operation. It performs no network I/O. It reuses exact
membership/shared-hub validation, supports only integer `OnOff` 0 or 1,
and requires a WebPC client ID belonging to the selected user. Reply handling
ignores unrelated topics/sequences and refuses an acknowledgement for a
different group, home, mesh address or state. Its tests cover those cases
and rejected acknowledgements without exposing upstream error details.
Neither helper infers physical delivery from code 200 or retries a command.

Do not present a successful hub acknowledgement as proof that all members
received a command. Verification needs a single outgoing group request plus
independent member reports, explicit read-back or physical observation.
An unacknowledged mesh broadcast may not generate individual status reports;
the lack of passive reports alone does not prove delivery failed.

## Additional group attributes verified

A watched sequence using the new production cloud client sent half-bright
blue, full-bright red, warm white, effect mode 5, steady green and off. Every
compound request received a matching code-200 reply, 61 member reports arrived,
and the user confirmed that every physical step worked. Acknowledgement times
were respectively 243.3, 325.5, 522.0, 376.1, 262.1 and 215.3 ms. These are
cloud-acceptance times, not instrumented physical latency.

This validates `Dimming`, signed 32-bit `RGBW`, `CCT`, `EffectMode`, combined
attributes, and clearing an effect with `EffectMode: 0` alongside a steady
colour request. Effects beyond the tested mode are selected from the exact
name/code intersection advertised by all group members; they have not each
been physically tested. Colour temperature was checked qualitatively rather
than measured with a colour meter.

## HA integration (1.1.6-beta.2)

The branch now contains group inventory validation, a protocol builder,
an account-scoped cloud client and a group light adapter under
`custom_components/aidot/group_*.py`. `coordinator.py` discovers owned groups,
maintains separate group coordinators and routes real member notifications.
`light.py` exposes existing app groups as ordinary light entities with only
their shared, validated controls. Group IDs remain stable across app renames.
Group state is assumed; it does not fabricate confirmations from every member.

The cloud client validates TLS, obtains a distinct WebPC session, recovers
expired tokens through HA's existing login and uses unique sequence IDs with
bounded reply waits. Non-retained QoS-0 writes are never replayed on reconnect.
Optional group failures do not disable local individual lights. Group dispatch
drains outstanding local requests without replacing the per-device pipeline;
this orders dispatch rather than guaranteeing physical ordering across paths.

Tests cover compound requests, exact targets and complete reply attributes,
thread-to-event-loop callbacks, timeouts without retry, fresh clients after
disconnect, cancellation cleanup, capabilities shared by all members, group
removal/re-addition, auth recovery, and notification routing without bulk state
fabrication. All 146 automated tests pass, and the changed runtime modules pass
the code checks.

The beta was installed on HA 2026.10.4 after verifying all 19 integration files
against the published commit and saving a rollback backup. The registered
14-member group exposes RGBW, colour temperature and the common advertised
effects. Five ordinary HA light actions passed: blue with brightness 128,
warm white at 2700 K, Bonfire, steady green, and off. HA service times ranged
from 225.6 to 527.7 ms; group states and mode/attribute conversions matched.

An entry reload re-established its cloud connection with zero commands replayed
and preserved the group entity ID. Individual local control was checked after
the group tests and reload: on/off passed in 15.2/14.7 ms. The cloud and local
hub connections remained healthy, and the test lights finished off. These are
small functional checks rather than an end-to-end latency benchmark.

For a future local implementation, the actual hub group frame is still required.
The cloud envelope and local TCP protocol are not interchangeable.

1. Discover existing eligible AiDot groups and resolve every member to a hub.
2. Use the verified cloud operation, or, once a local group frame is verified,
   add it to the existing shared `_HubSession` reader and acknowledgement
   dispatcher. Coordinate group writes with member commands; separate locks
   must not permit conflicting writes.
3. Expose the app group as its own HA light, supporting only attributes proven
   to work. A caller selecting ordinary individual entities keeps the existing
   semantics; do not silently broadcast to unselected group members.
4. Keep device telemetry authoritative. Track hub acceptance separately from
   member report freshness. Avoid inventing individual confirmations after a
   multicast acknowledgement.
5. Test mixed firmware, offline/sleeping members, rejected acknowledgements,
   reconnects and overlapping group/individual commands; compare a 14-light
   group action against the existing approximately 2.7-second baseline.

No native group write or all-nodes broadcast is enabled by this branch yet.
