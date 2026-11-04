from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

STATE_DIRECTORY = Path("/var/db/atrust-split")
GATEWAY_FILE = Path("/Library/Application Support/aTrust/var/conf/addr.conf")


def run(arguments, check=True):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=12)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "Command failed: " + arguments[0])
    return result


def normalized_network(value, host=False):
    if value == "default":
        return "0.0.0.0/0"
    address, separator, prefix = value.partition("/")
    parts = address.split(".")
    if len(parts) > 4 or not all(part.isdigit() for part in parts):
        raise ValueError("Not an IPv4 route")
    bits = int(prefix) if separator else (32 if host else len(parts) * 8)
    return str(ipaddress.ip_network(".".join(parts + ["0"] * (4 - len(parts))) + "/" + str(bits), strict=False))


def parse_routes(output):
    routes = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not re.fullmatch(r"[A-Za-z]+[0-9]+", fields[3]):
            continue
        try:
            network = normalized_network(fields[0], "H" in fields[2])
        except ValueError:
            continue
        routes.append({"net": network, "gateway": fields[1], "flags": fields[2], "iface": fields[3]})
    return routes


def routes():
    return parse_routes(run(["/usr/sbin/netstat", "-rn", "-f", "inet"]).stdout)


def route_command(action, network, interface, tagged=False):
    address = ipaddress.ip_network(network)
    if address.version != 4 or not re.fullmatch(r"utun[0-9]+", interface):
        raise ValueError("Only IPv4 routes on an identified utun are supported")
    arguments = ["/sbin/route", "-n", action]
    arguments += ["-host", str(address.network_address)] if address.prefixlen == 32 else ["-net", str(address)]
    arguments += ["-interface", interface]
    if tagged and action == "add":
        arguments.append("-proto1")
    return arguments


def native_identity(output, gateway, expected_hosts, boot, index=socket.if_nametoindex):
    if gateway not in expected_hosts:
        return None
    found = []
    for block in re.split(r"(?=^\S+: flags=)", output, flags=re.M):
        if "fd53:414e:4746:4f52:" not in block or "--> 2.0.0.1" not in block:
            continue
        name = block.split(":", 1)[0]
        address = re.search(r"\n\s+inet ([0-9.]+)", block)
        if re.fullmatch(r"utun[0-9]+", name) and address:
            found.append({"name": name, "index": index(name), "address": address.group(1),
                          "gateway": gateway, "boot": boot, "kind": "native"})
    if len(found) > 1:
        raise RuntimeError("More than one native aTrust interface matches; refusing to guess")
    return found[0] if found else None


def current_native(config, boot):
    try:
        raw = GATEWAY_FILE.read_text().strip()
    except OSError:
        return None
    gateway = (urlsplit(raw if "://" in raw else "https://" + raw).hostname or "").lower()
    return native_identity(run(["/sbin/ifconfig"]).stdout, gateway, config["native"]["gateway_hosts"], boot)


def removable(route, identity, wanted):
    return (route["iface"] == identity["name"] and route["gateway"] == identity["name"]
            and "I" not in route["flags"] and "1" not in route["flags"] and "L" not in route["flags"]
            and route["net"] not in wanted)


class Journal:
    def __init__(self, directory=STATE_DIRECTORY):
        self.directory = directory
        self.path = directory / "state.json"
        self.value = {"version": 1, "added": [], "removed": []}

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(self.value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def load(self):
        if self.path.is_symlink():
            raise RuntimeError("Recovery journal must not be a symlink")
        self.value = json.loads(self.path.read_text())
        if self.value.get("version") != 1:
            raise RuntimeError("Unknown recovery journal version")


def identity_alive(identity, boot, check_gateway=True):
    if identity["boot"] != boot:
        return False
    try:
        if socket.if_nametoindex(identity["name"]) != identity["index"]:
            return False
    except OSError:
        return False
    output = run(["/sbin/ifconfig", identity["name"]], False)
    if output.returncode or not re.search(r"\binet " + re.escape(identity["address"]) + r"\b", output.stdout):
        return False
    if identity["kind"] == "native" and check_gateway:
        try:
            raw = GATEWAY_FILE.read_text().strip()
            gateway = (urlsplit(raw if "://" in raw else "https://" + raw).hostname or "").lower()
        except OSError:
            return False
        return gateway == identity["gateway"] and "fd53:414e:4746:4f52:" in output.stdout
    return True


def owned(record, listing):
    return any(route["net"] == record["net"] and route["iface"] == record["identity"]["name"]
               and "1" in route["flags"] and "I" not in route["flags"] for route in listing)


def restore(journal, boot):
    pending_removed = []
    pending_added = []
    for record in journal.value["removed"]:
        if not identity_alive(record["identity"], boot):
            continue
        matching = [route for route in routes() if route["net"] == record["net"] and "I" not in route["flags"]]
        if matching:
            if any(route["iface"] != record["identity"]["name"] for route in matching):
                pending_removed.append(record)
            continue
        result = run(route_command("add", record["net"], record["identity"]["name"]), False)
        if result.returncode:
            pending_removed.append(record)
    if pending_removed:
        journal.value["removed"] = pending_removed
        journal.save()
        raise RuntimeError("Some original VPN routes could not be restored; journal retained. Resolve conflicts then recover")
    for record in reversed(journal.value["added"]):
        if identity_alive(record["identity"], boot, check_gateway=False) and owned(record, routes()):
            result = run(route_command("delete", record["net"], record["identity"]["name"]), False)
            if result.returncode and owned(record, routes()):
                pending_added.append(record)
    journal.value = {"version": 1, "removed": [], "added": pending_added}
    if pending_added:
        journal.save()
        raise RuntimeError("Some owned routes could not be removed; journal retained")
    journal.path.unlink(missing_ok=True)


def add_target(journal, identity, address):
    network = address + "/32"
    record = {"net": network, "identity": identity}
    matching = [route for route in routes() if route["net"] == network and "I" not in route["flags"]]
    if matching:
        if any(route["iface"] != identity["name"] for route in matching):
            raise RuntimeError("An exact route already belongs to another interface: " + address)
        return
    if record not in journal.value["added"]:
        journal.value["added"].append(record)
    journal.save()
    result = run(route_command("add", network, identity["name"], tagged=True), False)
    if result.returncode:
        journal.value["added"].remove(record)
        journal.save()
        raise RuntimeError("Could not add target route: " + address)


def reconcile_native(config, identity, journal):
    if current_native(config, identity["boot"]) != identity:
        raise RuntimeError("Native aTrust session changed during reconciliation")
    for address in config["native"]["targets"]:
        add_target(journal, identity, address)
    wanted = {address + "/32" for address in config["native"]["targets"]}
    for route in routes():
        if not removable(route, identity, wanted):
            continue
        record = {"net": route["net"], "identity": identity}
        if record not in journal.value["removed"]:
            journal.value["removed"].append(record)
            journal.save()
        run(route_command("delete", route["net"], identity["name"]))


@contextmanager
def lock(directory=STATE_DIRECTORY):
    if platform.system() != "Darwin" or os.geteuid() != 0:
        raise RuntimeError("Routing requires macOS and sudo; all other commands run as your normal user")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != 0 or directory.stat().st_mode & 0o077:
        raise RuntimeError("Routing state directory must be root-owned and mode 0700")
    descriptor = os.open(directory / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another router is running; stop it before recover") from error
        yield


def boot_identity():
    return run(["/usr/sbin/sysctl", "-n", "kern.boottime"]).stdout.strip()


def tun_command(config, executable):
    return [str(executable), "--device", config["routing"]["device"],
            "--proxy", f"socks5://127.0.0.1:{config['container']['socks_port']}",
            "--interface", "lo0", "--mtu", "1500", "--loglevel", "warn"]


def run_router(config, executable=None):
    container = config.get("container")
    binary = None
    if container is not None:
        binary = Path(executable).resolve() if executable else None
        if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Provide the absolute path of an installed tun2socks binary")
    with lock():
        legacy = run(["/bin/launchctl", "print", "system/local.atrust-split.service"], False)
        if legacy.returncode == 0:
            raise RuntimeError("The previous ATrust Split daemon is installed. Stop/uninstall its routing service before running this router")
        boot = boot_identity()
        journal = Journal()
        if journal.path.exists():
            journal.load()
            restore(journal, boot)
        native = current_native(config, boot)
        if native is None:
            raise RuntimeError("Log in to the configured native aTrust gateway first")
        if container is not None:
            device = config["routing"]["device"]
            address = config["routing"]["address"]
            listing = run(["/sbin/ifconfig"]).stdout
            if re.search(r"^" + re.escape(device) + r":", listing, re.M) or re.search(r"\binet " + re.escape(address) + r"\b", listing):
                raise RuntimeError("Configured TUN name or address is already in use")
            for target in container["targets"]:
                if any(route["net"] == target + "/32" and "I" not in route["flags"] for route in routes()):
                    raise RuntimeError("Container target already has an exact route: " + target)
            with socket.create_connection(("127.0.0.1", container["socks_port"]), timeout=3):
                pass
        stop = threading.Event()
        previous = {kind: signal.signal(kind, lambda *_: stop.set()) for kind in (signal.SIGINT, signal.SIGTERM)}
        process = None
        log = None
        try:
            if container is not None:
                descriptor = os.open(STATE_DIRECTORY / "tun2socks.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                log = os.fdopen(descriptor, "w")
                process = subprocess.Popen(tun_command(config, binary), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                deadline = time.monotonic() + 10
                while run(["/sbin/ifconfig", device], False).returncode:
                    if process.poll() is not None or time.monotonic() >= deadline or stop.wait(0.1):
                        raise RuntimeError("tun2socks did not create its interface; see the local routing log")
                run(["/sbin/ifconfig", device, "inet", address, address, "netmask", "255.255.255.255", "up"])
                secondary = {"name": device, "address": address, "index": socket.if_nametoindex(device),
                             "kind": "container", "boot": boot}
                for target in container["targets"]:
                    add_target(journal, secondary, target)
            reconcile_native(config, native, journal)
            print("IPv4 target routing active. Ctrl-C restores managed routes. DNS and Clash configuration are not changed.", flush=True)
            while not stop.wait(config["routing"]["poll_seconds"]):
                if process is not None and process.poll() is not None:
                    raise RuntimeError("tun2socks stopped; restoring managed routes")
                if current_native(config, boot) != native:
                    raise RuntimeError("Native aTrust session changed; restoring routes. Reauthenticate and run routes again")
                if container is not None:
                    for target in container["targets"]:
                        add_target(journal, secondary, target)
                reconcile_native(config, native, journal)
        finally:
            try:
                restore(journal, boot)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if log is not None:
                    log.close()
                for kind, handler in previous.items():
                    signal.signal(kind, handler)


def recover():
    with lock():
        journal = Journal()
        if journal.path.exists():
            journal.load()
            restore(journal, boot_identity())
