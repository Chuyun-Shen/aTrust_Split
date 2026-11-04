import ipaddress
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


def target_ip(value):
    if not isinstance(value, str) or "/" in value:
        raise ValueError("Targets must be individual IPv4 addresses, not CIDRs")
    address = ipaddress.ip_address(value)
    excluded = ("100.64.0.0/10", "198.18.0.0/15", "0.0.0.0/8", "240.0.0.0/4")
    if address.version != 4 or address.is_loopback or address.is_link_local or address.is_multicast:
        raise ValueError("Unsupported target address: " + value)
    if any(address in ipaddress.ip_network(network) for network in excluded):
        raise ValueError("Target conflicts with loopback, CGNAT or fake-IP space: " + value)
    return str(address)


def integer(value, lower, upper, field):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} must be an integer from {lower} to {upper}")
    return value


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or not value:
        raise ValueError("Invalid gateway hostname")
    if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
           for label in value.split(".")):
        raise ValueError("Gateway hosts must contain hostnames only, without ports or schemes")
    return value.lower()


def validate(config):
    secondary = "container" in config
    expected = {"version", "native", "routing"}
    if secondary:
        expected |= {"container", "probe"}
    if set(config) != expected or type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Expected version 1 with native/routing; optional container requires probe")
    schemas = {
        "native": {"name", "gateway_hosts", "targets"},
        "container": {"name", "login_url", "targets", "context", "image", "base_image",
                      "architecture", "socks_port", "desktop_port", "dns", "vpn_interface"},
        "routing": {"poll_seconds"} | ({"device", "address"} if secondary else set()),
        "probe": {"target", "port"},
    }
    for section, fields in schemas.items():
        if section not in expected:
            continue
        if not isinstance(config[section], dict) or set(config[section]) != fields:
            raise ValueError(f"Unexpected or missing fields in {section}")
    all_targets = []
    for section in (("native", "container") if secondary else ("native",)):
        entry = config[section]
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            raise ValueError("Institution names must be nonempty")
        targets = entry["targets"]
        if not isinstance(targets, list) or not 1 <= len(targets) <= 128:
            raise ValueError("Each institution needs 1–128 explicit IPv4 targets")
        entry["targets"] = [target_ip(value) for value in targets]
        all_targets.extend(entry["targets"])
    if len(all_targets) != len(set(all_targets)):
        raise ValueError("Target IPs must be unique across both institutions")
    gateways = config["native"]["gateway_hosts"]
    if not isinstance(gateways, list) or not gateways:
        raise ValueError("At least one native gateway hostname is required")
    config["native"]["gateway_hosts"] = [hostname(value) for value in gateways]
    if set(config["native"]["gateway_hosts"]) & set(all_targets):
        raise ValueError("VPN gateways cannot also be business targets")
    integer(config["routing"]["poll_seconds"], 1, 30, "poll_seconds")
    if not secondary:
        return config
    container = config["container"]
    if not isinstance(container["login_url"], str) or any(character.isspace() or ord(character) < 32 for character in container["login_url"]):
        raise ValueError("login_url must not contain whitespace or control characters")
    url = urlsplit(container["login_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("login_url must be an HTTPS URL without credentials, query or fragment")
    hostname(url.hostname)
    if url.port is not None:
        integer(url.port, 1, 65535, "login URL port")
    if url.hostname in all_targets or set(gateways) & set(all_targets):
        raise ValueError("VPN gateways cannot also be business targets")
    for key in ("context", "image", "base_image"):
        if not isinstance(container[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}", container[key]):
            raise ValueError("Invalid Docker " + key)
    if not re.search(r"@sha256:[0-9a-f]{64}$", container["base_image"]):
        raise ValueError("Pin base_image by SHA-256 digest")
    if container["architecture"] not in ("arm64", "amd64"):
        raise ValueError("architecture must be arm64 or amd64")
    for key in ("socks_port", "desktop_port"):
        integer(container[key], 1024, 65535, key)
    if container["socks_port"] == container["desktop_port"]:
        raise ValueError("SOCKS and desktop ports must differ")
    dns = ipaddress.ip_address(container["dns"])
    if dns.version != 4 or dns.is_unspecified or dns.is_loopback or dns.is_multicast:
        raise ValueError("Container DNS must be a reachable IPv4 address")
    if not re.fullmatch(r"utun[0-9]+", container["vpn_interface"]):
        raise ValueError("Container VPN interface must be utun followed by a number")
    routing = config["routing"]
    if not re.fullmatch(r"utun[1-9][0-9]{1,3}", routing["device"]):
        raise ValueError("Choose an unused macOS utun device numbered 10–9999")
    address = ipaddress.ip_address(routing["address"])
    if address.version != 4 or not address.is_link_local or not 1 <= address.packed[2] <= 254:
        raise ValueError("The TUN point-to-point address must be in 169.254.1.0–169.254.254.255")
    integer(routing["poll_seconds"], 1, 30, "poll_seconds")
    probe = config["probe"]
    if probe["target"] not in container["targets"]:
        raise ValueError("The probe must be one of the container's allowed targets")
    integer(probe["port"], 1, 65535, "probe port")
    return config


def load(path):
    with Path(path).open("rb") as stream:
        return validate(tomllib.load(stream))


def require_configured(config):
    hosts = list(config["native"]["gateway_hosts"])
    if "container" in config:
        hosts.append(urlsplit(config["container"]["login_url"]).hostname)
    if any(host.endswith((".example.org", ".example.com", ".example.net", ".invalid")) for host in hosts):
        raise ValueError("Replace the example gateway addresses before running")


def clash_fragment(config):
    targets = config["native"]["targets"] + config.get("container", {}).get("targets", [])
    return {"rules": [f"IP-CIDR,{address}/32,DIRECT,no-resolve" for address in targets],
            "tun": {"route-exclude-address": [address + "/32" for address in targets]}}
