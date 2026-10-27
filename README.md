# switch-basicconfig.py

A companion utility for applying a basic IPv4 interface and SSH configuration to a switch over a USB serial console. All device settings are held in `switch-basicconfig.json`; passwords are prompted for at runtime and are not stored in the config file.

**This is a starting implementation, not a universal network-device configurator.** CLI syntax and support vary by exact model and firmware. Test with the specific switch model and software release before using on production equipment.

## Features

- USB serial console only; no network/SSH connection is used to perform the initial configuration.
- Separate JSON configuration file.
- Explicit interface selection in the config file; no interface is automatically guessed. If exactly one supported USB serial port is detected, it is offered as the default and Enter accepts it.
- Creates/configures a local SSH account, enables SSH, assigns IPv4 settings, saves configuration, and displays verification output.
- Prompts for the new SSH account password without storing it in the JSON file.
- Requires typing `CONFIGURE <hostname>` before sending configuration commands.
- Excludes Linux `/dev/tty0`–`/dev/tty59` and `/dev/ttyS0`–`/dev/ttyS59` from serial-port selection.

## Requirements and installation

Python 3.10+ and `pyserial` are required. No Fedora-specific package manager or paths are assumed.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell activation:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configure

Edit `switch-basicconfig.json` before running. The included values are documentation-only examples; `192.0.2.0/24` is a TEST-NET range and must be replaced with addresses appropriate for your network.

Important keys:

- `platform`: one of `cisco`, `hp`, `arubacx`, `juniper`, `huawei`.
- `interface`: exact interface name as accepted by that device's CLI, e.g. `FastEthernet0/1`, `GigabitEthernet1/0/1`, `1/1/1`, or `ge-0/0/0`.
- `interface_mode`: for Cisco physical interfaces, currently `routed`. This sends `no switchport`, changing the selected port from Layer 2 switching to Layer 3 routing.
- `ip_cidr`: IPv4 address and prefix, e.g. `192.0.2.10/24`.
- `gateway`: IPv4 gateway in the same subnet as the selected interface.
- `domain_name`: required by several vendors for SSH key generation.
- `ssh_username`: account created for SSH access.
- `serial.port`: set to a device path such as `/dev/ttyUSB0`, `/dev/ttyACM0`, `COM3`, or `null` to show a selection menu.
- `serial.baudrate`: console baud rate, commonly 9600 for older Cisco Catalyst and ProCurve models; Aruba CX devices may use 115200. Check the hardware guide.

## Run

Validate the JSON settings without connecting to a switch:

```bash
python switch-basicconfig.py --dry-run
```

List detected USB serial adapters:

```bash
python switch-basicconfig.py --list-ports
```

Apply configuration:

```bash
python switch-basicconfig.py --config switch-basicconfig.json
```

You must type `CONFIGURE <hostname>` exactly. The script then asks for the new SSH account password twice. It subsequently logs into the existing console account if prompted. It does not bypass or recover credentials.

## Platform notes and limitations

- **Cisco IOS/IOS-XE:** The selected physical interface is configured as a routed port using `no switchport`. This is not appropriate if you need to keep the port as a Layer-2 switchport. The script uses `ip default-gateway`, which is suitable for a switch operating without IP routing; routed deployments may need a routing configuration instead. Some older IOS releases use different RSA key-generation prompts.
- **HP ProCurve / ArubaOS-Switch:** Physical switchports generally do not accept an IP address directly. Configure `interface` as a VLAN interface such as `vlan 1` and set `interface_mode` to `management_vlan`. SSH and local-account syntax varies by software release.
- **Aruba CX:** Physical routed-interface syntax is used. User creation, default route, SSH service and save syntax can vary by AOS-CX release.
- **Juniper Junos:** The selected interface is configured as `unit 0 family inet`; this assumes the selected interface can be configured as a routed interface. The account password is entered at the Junos interactive prompt.
- **Huawei VRP:** The command template uses common VRP syntax, but RSA-key prompts and AAA/SSH syntax differ across releases.

If the device reports an invalid command, stop and check the exact model and software version. Do not repeatedly retry commands blindly. This script stops on common CLI error text, but not every vendor's error message is covered.

## Verification

The script prints the output of vendor-specific commands after saving. Confirm the selected interface has the expected IP and link state, SSH is enabled, and the configuration is saved. Then test SSH from a separate host. A successful command sequence alone does not prove remote SSH access will work; VLAN membership, cabling, routing, ACLs and host-key behavior can still prevent access.

## Attribution

Inspired by the original project:
https://github.com/dev-sasa/switch-basicconfig

Fork and further development:
https://github.com/mats-nk/switch-basicconfig

## Cisco initial configuration dialog after factory reset

On a factory-reset Cisco IOS switch, the console may ask:

`Would you like to enter the initial configuration dialog? [yes/no]:`

The script detects this prompt and asks whether to start the Cisco setup wizard:

- **yes**: sends `yes`, waits for and reports the next wizard prompt (commonly `Would you like to enter basic management setup? [yes/no]:`), and exits without sending any basic-configuration commands. Complete the wizard in a serial terminal, then rerun this script to apply the settings from the JSON configuration file.
- **no**: sends `no` and continues to the CLI so this script can apply the JSON configuration.
- **abort**: exits without sending configuration commands.

This is intentional: the Cisco wizard is interactive and its questions vary by IOS release. The script does not guess answers or mix wizard input with automated CLI commands.
