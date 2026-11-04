import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import webbrowser

NAME = "atrust-split-secondary"
LABEL = "io.github.atrust-split.managed"


class Docker:
    def __init__(self, config, config_path):
        self.config = config["container"]
        self.directory = Path(config_path).resolve().parent / ".local" / "atrust-split"
        self.executable = shutil.which("docker")
        if not self.executable:
            raise RuntimeError("Docker CLI is missing")

    def run(self, *arguments, check=True, capture=True):
        if os.geteuid() == 0:
            raise RuntimeError("Run Docker commands as your normal user, without sudo")
        result = subprocess.run([self.executable, "--context", self.config["context"], *arguments],
                                capture_output=capture, text=True, timeout=1800 if not capture else 60)
        if check and result.returncode:
            raise RuntimeError((result.stderr or "Docker command failed")[-1200:])
        return result

    def inspect(self):
        result = self.run("container", "ls", "--all", "--filter", "name=^/" + NAME + "$", "--format", "{{.ID}}")
        if not result.stdout.strip():
            return None
        value = json.loads(self.run("inspect", "--type", "container", NAME).stdout)[0]
        if value["Config"].get("Labels", {}).get(LABEL) != "1":
            raise RuntimeError("Container name already belongs to another installation")
        return value

    def build(self, repository):
        root = Path(repository).resolve()
        if not (root / "container/Dockerfile").is_file():
            raise RuntimeError("Run build from the repository root, or set --repository")
        self.run("build", "--platform", "linux/" + self.config["architecture"],
                 "--build-arg", "BASE_IMAGE=" + self.config["base_image"],
                 "--tag", self.config["image"], "--file", str(root / "container/Dockerfile"), str(root), capture=False)

    def environment_file(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise RuntimeError("Local data directory must not be a symlink")
        os.chmod(self.directory, 0o700)
        secret_path = self.directory / "vnc-password"
        if not secret_path.exists():
            descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(4) + "\n")
        if secret_path.is_symlink() or secret_path.stat().st_uid != os.getuid() or secret_path.stat().st_mode & 0o077:
            raise RuntimeError("VNC password file must be owned by you and mode 0600")
        secret = secret_path.read_text().strip()
        if len(secret) != 8 or any(character not in "0123456789abcdef" for character in secret):
            raise ValueError("Invalid generated VNC password")
        values = {"PASSWORD": secret, "USE_NOVNC": "1", "CHROMIUM": "1", "VNC_SIZE": "1360x840",
                  "AI_TARGETS": ",".join(self.config["targets"]), "AI_LOGIN_URL": self.config["login_url"],
                  "AI_VPN_INTERFACE": self.config["vpn_interface"]}
        path = self.directory / "container.env"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        return path

    def up(self):
        image = json.loads(self.run("image", "inspect", self.config["image"]).stdout)[0]
        if image["Architecture"] != self.config["architecture"]:
            raise RuntimeError("Image architecture does not match config")
        fingerprint = hashlib.sha256(json.dumps({"config": self.config, "image": image["Id"]}, sort_keys=True).encode()).hexdigest()
        existing = self.inspect()
        if existing:
            if existing["Config"]["Labels"].get(LABEL + ".config") != fingerprint:
                raise RuntimeError("Container configuration/image changed. Stop routing, then down --remove and up; volumes remain")
            if not existing["State"]["Running"]:
                self.run("start", NAME)
            return
        for key in ("socks_port", "desktop_port"):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", self.config[key]))
        environment = self.environment_file()
        mounts = []
        for suffix, destination in (("home", "/root"), ("user", "/home/sangfor")):
            volume = NAME + "-" + suffix
            listing = self.run("volume", "ls", "--filter", "name=^" + volume + "$", "--format", "{{.Name}}")
            if volume in listing.stdout.splitlines():
                info = json.loads(self.run("volume", "inspect", volume).stdout)[0]
                if (info.get("Labels") or {}).get(LABEL) != "1":
                    raise RuntimeError("Volume name belongs to another installation")
            else:
                self.run("volume", "create", "--label", LABEL + "=1", volume)
            mounts.extend(["--mount", f"type=volume,source={volume},target={destination}"])
        self.run("run", "-d", "--name", NAME, "--hostname", NAME,
                 "--label", LABEL + "=1", "--label", LABEL + ".config=" + fingerprint,
                 "--restart", "unless-stopped", "--init", "--shm-size", "512m",
                 "--ulimit", "nofile=1048576:1048576", "--device", "/dev/net/tun",
                 "--cap-add", "NET_ADMIN", "--dns", self.config["dns"],
                 "--sysctl", "net.ipv4.conf.default.route_localnet=1", "--env-file", str(environment),
                 *mounts, "-p", f"127.0.0.1:{self.config['socks_port']}:1080",
                 "-p", f"127.0.0.1:{self.config['desktop_port']}:8080", image["Id"])

    def down(self, remove=False):
        if self.inspect() is not None:
            self.run("stop", "--time", "20", NAME)
            if remove:
                self.run("rm", NAME)

    def login(self):
        existing = self.inspect()
        if not existing or not existing["State"]["Running"]:
            raise RuntimeError("Start the container first")
        secret = (self.directory / "vnc-password").read_text().strip()
        webbrowser.open(f"http://127.0.0.1:{self.config['desktop_port']}/#autoconnect=true&resize=scale&password={secret}")

    def status(self):
        existing = self.inspect()
        return {"present": bool(existing), "running": bool(existing and existing["State"]["Running"]),
                "login_verified": False, "desktop": f"http://127.0.0.1:{self.config['desktop_port']}/"}
