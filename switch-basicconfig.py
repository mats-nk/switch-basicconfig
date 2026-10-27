#!/usr/bin/env python3
"""Configure a switch's selected interface and SSH over a USB serial console.

Device configuration is loaded from an external JSON file. This tool does not
attempt password recovery, bootloader interaction, or automatic interface discovery.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

VERSION = "1.0.2"
INITIAL_CONFIG_DIALOG = re.compile(r"(?i)would you like to enter the initial configuration dialog\?\s*\[yes/no\]\s*:")
CISCO_SETUP_PROMPT = re.compile(r"(?i)would you like to enter (?:basic management|extended) setup\?\s*\[yes/no\]\s*:")
CISCO_WIZARD_MARKER = re.compile(r"(?i)(at any point you may enter a question mark|basic management setup configures|extended setup will ask you|would you like to enter basic management setup)")
LOGIN_FAIL = re.compile(r"(?i)(authentication failed|login incorrect|invalid password|access denied|bad passwords|invalid username or password)")
CLI_PROMPT = re.compile(r"(?m)(?:[\w.@:/()\- ]+[>#%]|<[^>]+>|\[[^\]]+\])\s*$")
CLI_ERROR = re.compile(r"(?i)(% ?invalid input|unknown command|unrecognized command|incomplete command|ambiguous command|syntax error|permission denied|not permitted|error:)")

SUPPORTED = {
    "cisco": "Cisco IOS / IOS-XE (Catalyst)",
    "hp": "HP ProCurve / ArubaOS-Switch",
    "arubacx": "Aruba CX (AOS-CX)",
    "juniper": "Juniper Junos (EX)",
    "huawei": "Huawei VRP",
}


def excluded_tty(device: str) -> bool:
    """Exclude Linux virtual consoles tty0..tty59 and onboard UARTs ttyS0..ttyS59."""
    name = os.path.basename(device)
    m = re.fullmatch(r"ttyS?(\d+)", name)
    return bool(m and int(m.group(1)) <= 59)


def serial_ports():
    if serial is None:
        raise RuntimeError("pyserial is missing. Install dependencies with: python -m pip install -r requirements.txt")
    result = []
    for port in serial.tools.list_ports.comports():
        name = os.path.basename(port.device)
        if excluded_tty(port.device):
            continue
        if ("ttyUSB" in name or "ttyACM" in name or name.startswith("cu.")
                or name.startswith("COM") or name.startswith("usbserial") or name.startswith("tty.usb")):
            result.append(port)
    return result


class InitialConfigDialogStarted(RuntimeError):
    """Raised after explicitly starting the Cisco setup dialog."""


class SerialConsole:
    def __init__(self, port: str, baud: int):
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.port = serial.Serial(port, baudrate=baud, bytesize=8, parity="N", stopbits=1,
                                  timeout=0.2, write_timeout=3, xonxoff=False, rtscts=False, dsrdtr=False)
        time.sleep(0.25)
        self.port.reset_input_buffer()

    def write_line(self, line: str):
        self.port.write(line.encode("utf-8", "replace") + b"\r\n")
        self.port.flush()

    def read_available(self, max_bytes: int = 8192) -> str:
        n = min(max(self.port.in_waiting, 1), max_bytes)
        data = self.port.read(n)
        return data.decode("utf-8", "replace") if data else ""

    def close(self):
        self.port.close()


def read_until_quiet(console: SerialConsole, timeout: float = 8.0, quiet: float = 0.7) -> str:
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    out = ""
    while time.monotonic() < deadline:
        chunk = console.read_available()
        if chunk:
            out += chunk
            last_data = time.monotonic()
            if LOGIN_FAIL.search(out):
                raise RuntimeError("Login failed or access denied; no configuration commands were sent")
        elif out and time.monotonic() - last_data >= quiet:
            break
    return out


def wake_and_login(console: SerialConsole, platform: str) -> str:
    """Handle normal username/password prompts; does not bypass authentication."""
    # Read before sending a wake-up newline. On a factory-reset Cisco IOS
    # console, a newline sent while the setup question is active is interpreted
    # as an invalid answer and produces "% Please answer 'yes' or 'no'.".
    out = read_until_quiet(console, timeout=1.2, quiet=0.35)
    if not out.strip():
        console.write_line("")
    deadline = time.monotonic() + 90
    sent_user = False
    sent_password = False
    sent_enable = False
    enable_password_sent = False
    initial_dialog_handled = False
    username = None
    password = None
    enable_password = None
    while time.monotonic() < deadline:
        chunk = console.read_available()
        if not chunk:
            time.sleep(0.05)
            continue
        out += chunk
        tail = out[-1800:]
        if LOGIN_FAIL.search(tail):
            raise RuntimeError("Login failed or access denied; no configuration commands were sent")

        # A factory-reset Cisco switch may stop at the initial setup dialog rather
        # than present a CLI prompt. Never send a blank line to this prompt: IOS
        # responds with "% Please answer 'yes' or 'no'." Ask the operator whether
        # to launch the wizard or skip it so this script can configure the switch.
        if platform == "cisco" and not initial_dialog_handled and INITIAL_CONFIG_DIALOG.search(tail):
            initial_dialog_handled = True
            while True:
                choice = input(
                    "Factory-reset Cisco switch detected. Start the initial configuration dialog? "
                    "[yes/no/abort]: "
                ).strip().lower()
                if choice in ("yes", "y"):
                    console.write_line("yes")
                    # Wait for IOS to move into the interactive wizard. The next
                    # prompt commonly asks about basic management setup; it is not
                    # a CLI prompt and must not be answered by automated config.
                    wizard_output = ""
                    wizard_deadline = time.monotonic() + 12.0
                    wizard_detected = False
                    while time.monotonic() < wizard_deadline:
                        chunk = console.read_available()
                        if chunk:
                            wizard_output += chunk
                            if CISCO_SETUP_PROMPT.search(wizard_output) or CISCO_WIZARD_MARKER.search(wizard_output):
                                wizard_detected = True
                                # A prompt means the wizard is waiting for an operator.
                                if CISCO_SETUP_PROMPT.search(wizard_output):
                                    break
                        else:
                            time.sleep(0.05)
                    if wizard_output.strip():
                        print(wizard_output.rstrip())
                    if wizard_detected:
                        print("Cisco setup wizard detected and waiting for interactive input.")
                    else:
                        print("Sent 'yes' to the initial dialog, but could not positively identify the next wizard prompt.")
                    print("Continue the wizard in a serial terminal. Do not run automated configuration until the wizard is complete.")
                    raise InitialConfigDialogStarted(
                        "Cisco setup wizard selected. Complete it using a serial terminal, then rerun switch-basicconfig.py."
                    )
                if choice in ("no", "n"):
                    console.write_line("no")
                    # Do not treat the wizard's [yes/no] prompt as a CLI prompt.
                    out = ""
                    break
                if choice in ("abort", "a", "q"):
                    raise RuntimeError("Cancelled at Cisco initial configuration dialog; no configuration commands were sent")
                print("Please answer yes, no, or abort.")
            continue

        if re.search(r"(?im)(username|user name|login):\s*$", tail) and not sent_user:
            username = input("Current device username: ")
            console.write_line(username)
            sent_user = True
            continue
        if re.search(r"(?im)password:\s*$", tail) and not sent_password:
            password = getpass.getpass("Current device password: ")
            console.write_line(password)
            sent_password = True
            continue
        if platform == "cisco" and re.search(r"(?m)[\w.-]+>\s*$", tail) and not sent_enable:
            console.write_line("enable")
            sent_enable = True
            continue
        if platform == "cisco" and sent_enable and re.search(r"(?im)password:\s*$", tail) and not enable_password_sent:
            enable_password = getpass.getpass("Cisco enable password: ")
            console.write_line(enable_password)
            enable_password_sent = True
            continue
        if CLI_PROMPT.search(tail) and not re.search(r"(?im)(username|user name|login|password):\s*$", tail):
            # Send a blank line once to make the prompt unambiguous, then collect it.
            console.write_line("")
            return read_until_quiet(console, timeout=2.5, quiet=0.4)
    raise TimeoutError("Could not reach an authenticated CLI prompt within 90 seconds")


def issue(console: SerialConsole, command: str, timeout: float = 12.0,
          interactive: list[tuple[str, str]] | None = None, secret: bool = False) -> str:
    """Send one CLI line, answer known prompts, and fail on explicit CLI errors."""
    print("  -> [sensitive command]" if secret else f"  -> {command}")
    console.write_line(command)
    out = ""
    answered: set[int] = set()
    deadline = time.monotonic() + timeout
    last = time.monotonic()
    interactive = interactive or []
    while time.monotonic() < deadline:
        chunk = console.read_available()
        if chunk:
            out += chunk
            last = time.monotonic()
            tail = out[-2500:]
            if LOGIN_FAIL.search(tail):
                raise RuntimeError("Authentication failure reported by device")
            for i, (pattern, answer) in enumerate(interactive):
                if i not in answered and re.search(pattern, tail, re.I | re.M):
                    console.write_line(answer)
                    answered.add(i)
                    last = time.monotonic()
                    break
            if CLI_ERROR.search(tail):
                # Ignore a command echoed as part of output; the error signature must occur after it.
                error_match = CLI_ERROR.search(tail)
                if error_match and error_match.start() > min(len(command), 20):
                    raise RuntimeError(f"Device reported an error for command {command!r}: {tail[-600:].strip()}")
            if CLI_PROMPT.search(tail) and time.monotonic() - last > 0.35:
                return out
        elif out and time.monotonic() - last > 0.8:
            return out
        else:
            time.sleep(0.04)
    if not out:
        raise TimeoutError(f"No response to command {command!r}")
    return out


def mask_from_prefix(cidr: str) -> tuple[str, str]:
    iface = ipaddress.ip_interface(cidr)
    if iface.version != 4:
        raise ValueError("Only IPv4 interface addresses are supported in this version")
    return str(iface.ip), str(iface.network.netmask)


def validate_config(cfg: dict) -> dict:
    required = ["platform", "hostname", "interface", "ip_cidr", "gateway", "domain_name", "ssh_username", "serial"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError("Missing required config key(s): " + ", ".join(missing))
    platform = str(cfg["platform"]).lower()
    if platform not in SUPPORTED:
        raise ValueError(f"Unsupported platform {platform!r}. Supported: {', '.join(SUPPORTED)}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", str(cfg["hostname"])):
        raise ValueError("hostname must be 1-63 characters using letters, digits, dots or hyphens, and start with a letter/digit")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", str(cfg["ssh_username"])):
        raise ValueError("ssh_username may contain only letters, digits, underscore, dot and hyphen (max 32 chars)")
    if not re.fullmatch(r"[A-Za-z0-9./:-]+(?:\s+\d+)?", str(cfg["interface"])):
        raise ValueError("interface must be a single CLI interface identifier, e.g. FastEthernet0/1, GigabitEthernet1/0/1, ge-0/0/0, or 'vlan 1'; whitespace/control characters are not allowed except before a VLAN number")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", str(cfg["domain_name"])):
        raise ValueError("domain_name contains unsupported characters")
    ip, mask = mask_from_prefix(str(cfg["ip_cidr"]))
    gateway = ipaddress.ip_address(str(cfg["gateway"]))
    if gateway.version != 4:
        raise ValueError("gateway must be IPv4")
    iface = ipaddress.ip_interface(str(cfg["ip_cidr"]))
    if gateway not in iface.network:
        raise ValueError(f"gateway {gateway} is not in interface network {iface.network}")
    serial_cfg = cfg["serial"]
    if not isinstance(serial_cfg, dict) or not serial_cfg.get("baudrate"):
        raise ValueError("serial must be an object with baudrate")
    if not isinstance(serial_cfg["baudrate"], int) or not 1200 <= serial_cfg["baudrate"] <= 921600:
        raise ValueError("serial.baudrate must be an integer between 1200 and 921600")
    cfg["platform"] = platform
    cfg["ip_address"] = ip
    cfg["subnet_mask"] = mask
    cfg["prefix_length"] = iface.network.prefixlen
    return cfg


def build_commands(cfg: dict, new_password: str) -> list[tuple[str, bool, list[tuple[str, str]]]]:
    """Build vendor CLI steps. Secrets are tagged so they are never printed."""
    p = cfg["platform"]
    host = cfg["hostname"]
    intf = cfg["interface"]
    ip = cfg["ip_address"]
    mask = cfg["subnet_mask"]
    gw = cfg["gateway"]
    domain = cfg["domain_name"]
    user = cfg["ssh_username"]
    # Each item: command, is_secret, prompt-pattern/answer pairs.
    steps: list[tuple[str, bool, list[tuple[str, str]]]] = []
    if p == "cisco":
        mode = str(cfg.get("interface_mode", "routed")).lower()
        if mode not in ("routed",):
            raise ValueError("Cisco physical-interface IP configuration currently requires interface_mode='routed'; this deliberately converts the selected port from Layer 2 to Layer 3")
        steps += [
            ("terminal length 0", False, []),
            ("configure terminal", False, []),
            (f"hostname {host}", False, []),
            (f"ip domain-name {domain}", False, []),
            (f"username {user} privilege 15 secret {new_password}", True, []),
            (f"interface {intf}", False, []),
            ("no switchport", False, []),
            (f"ip address {ip} {mask}", False, []),
            ("no shutdown", False, []),
            ("exit", False, []),
            (f"ip default-gateway {gw}", False, []),
            ("ip ssh version 2", False, []),
            ("crypto key generate rsa modulus 2048", False, [(r"(?i)replace.*keys|overwrite.*key", "yes"), (r"(?i)how many bits.*modulus.*\[\d+\]", "2048")]),
            ("line vty 0 15", False, []),
            ("login local", False, []),
            ("transport input ssh", False, []),
            ("exec-timeout 10 0", False, []),
            ("end", False, []),
            ("write memory", False, [(r"(?i)\[confirm\]", "")]),
        ]
    elif p == "hp":
        mode = str(cfg.get("interface_mode", "management_vlan")).lower()
        if mode != "management_vlan":
            raise ValueError("HP ProCurve / ArubaOS-Switch generally assigns an IP to a VLAN interface, not a physical FE/GE port. Set interface_mode='management_vlan' and interface to e.g. 'vlan 1'.")
        if not re.fullmatch(r"vlan\s+\d+", intf, re.I):
            raise ValueError("For HP ProCurve, interface must be a VLAN interface such as 'vlan 1'")
        vlan = int(re.search(r"\d+", intf).group(0))
        steps += [
            ("no page", False, []), ("configure terminal", False, []),
            (f"hostname {host}", False, []),
            (f"password manager user-name {user} plaintext {new_password}", True, []),
            (f"vlan {vlan}", False, []),
            (f"ip address {ip} {mask}", False, []), ("exit", False, []),
            ("crypto key generate ssh rsa bits 2048", False, [(r"(?i)replace.*key|overwrite", "y"), (r"(?i)continue.*\(y/n\)", "y")]),
            ("ip ssh", False, []), ("aaa authentication ssh login local", False, []),
            ("exit", False, []), ("write memory", False, [(r"(?i)\[y/n\]", "y"), (r"(?i)\[confirm\]", "")]),
        ]
    elif p == "arubacx":
        mode = str(cfg.get("interface_mode", "routed")).lower()
        if mode != "routed":
            raise ValueError("Aruba CX physical interface IP configuration requires interface_mode='routed'")
        steps += [
            ("no page", False, []), ("configure terminal", False, []),
            (f"hostname {host}", False, []), (f"user {user} group administrators password", True,
             [(r"(?i)new password:\s*$", new_password), (r"(?i)confirm password:\s*$", new_password)]),
            (f"interface {intf}", False, []), (f"ip address {cfg['ip_cidr']}", False, []),
            ("no shutdown", False, []), ("exit", False, []),
            (f"ip route 0.0.0.0/0 {gw}", False, []), ("ssh server vrf default", False, []),
            ("end", False, []), ("write memory", False, []),
        ]
    elif p == "juniper":
        steps += [
            ("set cli screen-length 0", False, []), ("configure", False, []),
            (f"set system host-name {host}", False, []),
            (f"set system domain-name {domain}", False, []),
            (f"set system login user {user} class super-user", False, []),
            (f"set system login user {user} authentication plain-text-password", False,
             [(r"(?i)new password:\s*$", new_password), (r"(?i)retype new password:\s*$", new_password)]),
            (f"set interfaces {intf} unit 0 family inet address {cfg['ip_cidr']}", False, []),
            ("set system services ssh", False, []),
            (f"set routing-options static route 0.0.0.0/0 next-hop {gw}", False, []),
            ("commit", False, [(r"(?i)commit complete", "")]), ("exit", False, []),
        ]
    elif p == "huawei":
        steps += [
            ("screen-length 0 temporary", False, []), ("system-view", False, []),
            (f"sysname {host}", False, []),
            (f"aaa", False, []),
            (f"local-user {user} password irreversible-cipher {new_password}", True, []),
            (f"local-user {user} privilege level 15", False, []),
            (f"local-user {user} service-type ssh", False, []), ("quit", False, []),
            (f"interface {intf}", False, []), (f"ip address {ip} {mask}", False, []),
            ("undo shutdown", False, []), ("quit", False, []),
            (f"stelnet server enable", False, [(r"(?i)continue.*\(y/n\)", "y")]),
            ("rsa local-key-pair create", False, [(r"(?i)(input|enter).*?(bits|modulus).*?(?:\]|:)\s*$", "2048")]),
            ("user-interface vty 0 4", False, []), ("authentication-mode aaa", False, []),
            ("protocol inbound ssh", False, []), ("quit", False, []),
            (f"ip route-static 0.0.0.0 0.0.0.0 {gw}", False, []), ("return", False, []), ("save", False, [(r"(?i)continue\?\s*\[y/n\]", "y"), (r"(?i)are you sure.*\[y/n\]", "y")]),
        ]
    return steps


def verify_commands(platform: str) -> list[str]:
    return {
        "cisco": ["show ip interface brief", "show ip ssh", "show running-config | include username|ip domain-name|ip default-gateway", "show running-config | section line vty"],
        "hp": ["show ip", "show ip ssh", "show authentication"],
        "arubacx": ["show ip interface brief", "show ssh server", "show running-config"],
        "juniper": ["show interfaces terse", "show configuration system services ssh", "show configuration routing-options"],
        "huawei": ["display ip interface brief", "display ssh server status", "display current-configuration | include local-user|stelnet|ssh"],
    }[platform]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a basic IP and SSH configuration to a switch over USB serial console.",
        epilog="Inspired by the original project: https://github.com/dev-sasa/switch-basicconfig\nFork and further development: https://github.com/mats-nk/switch-basicconfig",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", default="switch-basicconfig.json", help="JSON configuration file (default: %(default)s)")
    parser.add_argument("--list-ports", action="store_true", help="List supported serial ports and exit")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and show command plan without connecting")
    parser.add_argument("--version", action="version", version=f"switch-basicconfig.py {VERSION}")
    args = parser.parse_args()

    if args.list_ports:
        try:
            ports = serial_ports()
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for p in ports:
            print(f"{p.device} — {p.description}")
        if not ports:
            print("No supported USB serial ports found.")
        return 0

    try:
        config_path = Path(args.config).expanduser()
        cfg = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
        if args.dry_run:
            steps = build_commands(cfg, "<PASSWORD_REDACTED>")
            print(f"Platform: {SUPPORTED[cfg['platform']]}")
            print(f"Hostname: {cfg['hostname']}; interface: {cfg['interface']}; address: {cfg['ip_cidr']}; gateway: {cfg['gateway']}")
            print(f"Serial baud rate: {cfg['serial']['baudrate']}")
            print("Planned CLI commands (password values redacted):")
            for command, is_secret, _prompts in steps:
                print("  [password command redacted]" if is_secret else f"  {command}")
            if cfg["platform"] == "cisco":
                print("WARNING: Cisco 'no switchport' converts the selected physical port to Layer 3 (routed mode).")
            print("Dry run does not connect to or modify a device. Validate commands against the exact model and software release.")
            return 0
        print(f"Platform: {SUPPORTED[cfg['platform']]}")
        print(f"Configured interface: {cfg['interface']} -> {cfg['ip_cidr']}")
        print("The script will configure the selected interface and enable SSH, then save the configuration.")
        print("Use only on a switch you are authorized to administer. Verify vendor/model-specific syntax first.")
        if cfg["platform"] == "cisco":
            print("WARNING: Cisco mode issues 'no switchport' on the selected port. This converts it to a routed Layer-3 interface.")
        expected = f"CONFIGURE {cfg['hostname']}"
        if input(f"Type exactly '{expected}' to continue: ").strip() != expected:
            print("Cancelled; no device commands sent.")
            return 1
        password = getpass.getpass(f"New SSH account password for {cfg['ssh_username']}: ")
        if len(password) < int(cfg.get("minimum_password_length", 12)):
            raise ValueError(f"New SSH password must be at least {cfg.get('minimum_password_length', 12)} characters")
        if "\r" in password or "\n" in password or any(ord(ch) < 32 for ch in password):
            raise ValueError("Password must not contain newline or control characters")
        if any(ch.isspace() for ch in password):
            raise ValueError("For compatibility with the supported CLI templates, the password must not contain whitespace")
        confirm = getpass.getpass("Confirm new SSH account password: ")
        if password != confirm:
            raise ValueError("Passwords do not match")
        ports = serial_ports()
        port_name = cfg["serial"].get("port")
        if not port_name:
            if not ports:
                raise RuntimeError("No supported USB serial ports found. Set serial.port in the JSON file or connect a USB serial adapter.")
            for idx, p in enumerate(ports, 1):
                suffix = " (default)" if len(ports) == 1 else ""
                print(f" [{idx}] {p.device} — {p.description}{suffix}")
            if len(ports) == 1:
                choice = input("Select serial port [1] (Enter to accept, q to cancel): ").strip().lower()
                if choice == "q":
                    return 1
                if choice == "":
                    port_name = ports[0].device
                elif choice.isdigit() and int(choice) == 1:
                    port_name = ports[0].device
                else:
                    raise ValueError("Invalid serial port selection")
            else:
                while True:
                    choice = input("Select serial port number (q to cancel): ").strip().lower()
                    if choice == "q":
                        return 1
                    if choice.isdigit() and 1 <= int(choice) <= len(ports):
                        port_name = ports[int(choice) - 1].device
                        break
        if excluded_tty(port_name):
            raise ValueError(f"Refusing excluded serial device {port_name!r}")
        console = SerialConsole(port_name, cfg["serial"]["baudrate"])
        try:
            print("Waking console and waiting for authenticated CLI...")
            banner = wake_and_login(console, cfg["platform"])
            print("CLI prompt detected. Initial console output:")
            print(banner[-1000:].rstrip())
            steps = build_commands(cfg, password)
            for command, is_secret, prompts in steps:
                issue(console, command, timeout=float(cfg.get("command_timeout_seconds", 15)), interactive=prompts, secret=is_secret)
            print("\nConfiguration commands completed. Running verification commands:")
            for command in verify_commands(cfg["platform"]):
                print(f"\n--- {command} ---")
                result = issue(console, command, timeout=15)
                # Keep verification output, but do not dump running config wholesale on some platforms.
                print(result[-5000:].rstrip())
            print("\nConfiguration sequence finished. Review verification output and test SSH from another host.")
        finally:
            console.close()
        return 0
    except InitialConfigDialogStarted as exc:
        print(exc)
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
