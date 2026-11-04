import argparse
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from . import __version__
from .config import clash_fragment, load, require_configured
from .containers import Docker
from .probe import connect


def plan(config):
    result = {"mode": "native-and-container" if "container" in config else "native",
              "native": {"gateway_hosts": config["native"]["gateway_hosts"],
                       "ipv4_targets": config["native"]["targets"],
                       "action": "Narrow recognized aTrust IPv4 interface routes; journal changes before applying"},
            "unchanged_by_this_tool": ["physical default route", "system DNS", "Clash subscription", "SSH config"],
            "clash_bypass_required": True,
            "native_route_restore_on_exit": True}
    if "container" in config:
        result["container"] = {"ipv4_targets": config["container"]["targets"],
                               "device": config["routing"]["device"],
                               "socks": f"127.0.0.1:{config['container']['socks_port']}",
                               "protocols": ["IPv4 TCP"]}
    return result


def doctor(config):
    result = {"platform": platform.system(), "architecture": platform.machine(),
              "python": platform.python_version(), "supported_host": platform.system() == "Darwin",
              "mode": "native-and-container" if "container" in config else "native",
              "tools": {},
              "native_app_found": Path("/Applications/aTrust.app").exists(),
              "legacy_split_app_found": Path("/Applications/ATrust Split.app").exists(),
              "ports_listed_in_config": []}
    if "container" in config:
        result["tools"] = {name: shutil.which(name) for name in ("docker", "colima", "tun2socks")}
        result["ports_listed_in_config"] = [config["container"]["socks_port"], config["container"]["desktop_port"]]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="aTrust and Clash split routing on macOS; optional second aTrust")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="config.local.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "doctor", "plan", "clash", "up", "login", "status", "probe", "recover"):
        subparsers.add_parser(command)
    build = subparsers.add_parser("build")
    build.add_argument("--repository", default=".")
    down = subparsers.add_parser("down")
    down.add_argument("--remove", action="store_true", help="Remove the stopped container, preserving data volumes")
    routes = subparsers.add_parser("routes")
    routes.add_argument("--tun2socks", default=shutil.which("tun2socks"))
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "recover":
            from .routing import recover
            recover()
            print("Managed routes recovered, or no journal was present.")
            return 0
        config = load(arguments.config)
        if arguments.command in ("build", "up", "down", "login", "status", "probe") and "container" not in config:
            raise ValueError("This command is for the optional second aTrust. See docs/DUAL_ATRUST.md and config.dual.example.toml")
        if arguments.command in ("up", "login", "routes"):
            require_configured(config)
        if arguments.command == "check":
            print("Configuration structure is valid. Example addresses and real connectivity are not verified.")
        elif arguments.command in ("plan", "doctor", "clash"):
            function = {"plan": plan, "doctor": doctor, "clash": clash_fragment}[arguments.command]
            print(json.dumps(function(config), indent=2, ensure_ascii=False))
        elif arguments.command == "routes":
            if "container" in config and not arguments.tun2socks:
                raise ValueError("Install tun2socks and pass --tun2socks /absolute/path/to/tun2socks")
            from .routing import run_router
            run_router(config, arguments.tun2socks)
        elif arguments.command == "probe":
            with connect(config["container"]["socks_port"], config["probe"]["target"], config["probe"]["port"]):
                print("Allowed target accepted a TCP connection through the container VPN. Application login is not checked.")
        else:
            docker = Docker(config, arguments.config)
            if arguments.command == "build":
                docker.build(arguments.repository)
            elif arguments.command == "up":
                docker.up()
                print("Container started. Use login and authenticate in its desktop; then probe and routes.")
            elif arguments.command == "down":
                docker.down(arguments.remove)
                print("Container stopped. Data volumes retained. Stop the foreground router separately.")
            elif arguments.command == "login":
                docker.login()
                print("Opened the local container desktop. Enter the configured gateway in its official aTrust client.")
            elif arguments.command == "status":
                print(json.dumps(docker.status(), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
