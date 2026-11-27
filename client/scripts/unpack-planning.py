"""Verify the published Planning Harness wheel and assemble its local dependencies."""

import base64
import csv
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections import deque
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PureWindowsPath
from platform import machine, python_version

DISTRIBUTION = "soduwork-planning-harness"
PACKAGE = "soduwork_planning_harness"
VERSION_PATTERN = r"\d+(?:\.\d+)+(?:a\d+|b\d+|rc\d+)?(?:\.post\d+)?(?:\.dev\d+)?"


def extract_wheel(source, name, stage):
    from pip._vendor.packaging.specifiers import SpecifierSet

    match = re.fullmatch(rf"{PACKAGE}-({VERSION_PATTERN})-py3-none-any\.whl", name)
    if not match:
        raise ValueError("Unexpected Planning Harness wheel name or compatibility tag")
    version = match[1]
    dist_info = f"{PACKAGE}-{version}.dist-info"
    seen = set()
    files = {}
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            path = member.filename.rstrip("/")
            parts = path.split("/")
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (
                path.casefold() in seen
                or parts[0] not in {PACKAGE, dist_info}
                or mode not in (0, stat.S_IFDIR, stat.S_IFREG)
                or any(
                    not p
                    or p in (".", "..")
                    or ":" in p
                    or "\\" in p
                    or p.rstrip(" .") != p
                    or PureWindowsPath(p).is_reserved()
                    for p in parts
                )
            ):
                raise ValueError("Invalid file path in Planning Harness wheel")
            seen.add(path.casefold())
            if member.is_dir():
                continue
            if parts[0] == PACKAGE:
                if len(parts) < 2 or parts[1] not in {
                    "__init__.py",
                    "__main__.py",
                    "manifest.json",
                    "launch.py",
                    "site",
                    "requirements.lock",
                }:
                    raise ValueError("Unexpected Planning Harness component")
                output = stage.joinpath(*parts[1:])
            else:
                output = stage.joinpath(*parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, output.open("xb") as out:
                shutil.copyfileobj(src, out)
            files[path] = output

    def metadata(file):
        return BytesParser().parsebytes((stage / dist_info / file).read_bytes())

    wheel, package = metadata("WHEEL"), metadata("METADATA")
    if (
        wheel.get_all("Wheel-Version") != ["1.0"]
        or wheel.get_all("Tag") != ["py3-none-any"]
        or wheel.get("Root-Is-Purelib", "").lower() != "true"
        or package.get_all("Name") != [DISTRIBUTION]
        or package.get_all("Version") != [version]
        or not (package.get("Author") or package.get("Author-email") or "").strip()
        or not package.get("Requires-Python")
        or python_version() not in SpecifierSet(package["Requires-Python"])
    ):
        raise ValueError("Planning Harness wheel metadata mismatch")
    record = f"{dist_info}/RECORD"
    remaining = set(files)
    with files[record].open(encoding="utf-8", newline="") as rows:
        for key, digest, size in csv.reader(rows):
            if key not in remaining:
                raise ValueError(
                    "Unexpected or duplicate Planning Harness RECORD entry"
                )
            remaining.remove(key)
            if key == record:
                if digest or size:
                    raise ValueError("Invalid self-entry in wheel RECORD")
                continue
            with files[key].open("rb") as file:
                actual = (
                    base64.urlsafe_b64encode(
                        hashlib.file_digest(file, "sha256").digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
            if digest != "sha256=" + actual or size != str(files[key].stat().st_size):
                raise ValueError("Planning Harness RECORD digest or size mismatch")
    if remaining:
        raise ValueError("File missing from Planning Harness RECORD")
    return version


def verify_dependencies(stage, deps, requested_extras):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    installed = {
        canonicalize_name(dist.metadata["Name"]): dist
        for dist in metadata.distributions(path=[str(stage), str(deps)])
    }
    checks = deque((name, "") for name in installed)
    checks.extend(requested_extras)
    checked = set()
    while checks:
        name, extra = checks.popleft()
        if (name, extra) in checked:
            continue
        checked.add((name, extra))
        for raw in installed[name].requires or ():
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": extra}):
                continue
            target = canonicalize_name(requirement.name)
            if (
                requirement.url
                or target not in installed
                or installed[target].version not in requirement.specifier
            ):
                raise ValueError(
                    f"Incomplete offline runtime: {name} requires {requirement}"
                )
            checks.extend((target, value) for value in requirement.extras)


def install_dependencies(stage, wheelhouse, temporary, manifest):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    if not wheelhouse.is_absolute() or not wheelhouse.is_dir():
        raise ValueError("Missing offline Planning Harness wheelhouse")
    lock = stage / "requirements.lock"
    requested = {}
    requested_extras = set()
    for row in lock.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        row = row.strip()
        if not row or row.startswith("#"):
            continue
        # Exact named requirements only. In particular, --no-index alone does not
        # prevent pip from following URL requirements or directives in a lock.
        name, *hashes = re.split(r"\s+--hash=", row)
        requirement = Requirement(name.strip())
        versions = list(requirement.specifier)
        if (
            not hashes
            or any(not re.fullmatch(r"sha256:[a-f0-9]{64}", h.strip()) for h in hashes)
            or requirement.url
            or len(versions) != 1
            or versions[0].operator != "=="
            or "*" in versions[0].version
        ):
            raise ValueError("Planning Harness requires hash-pinned named dependencies")
        if not requirement.marker or requirement.marker.evaluate():
            name = canonicalize_name(requirement.name)
            requested[name] = versions[0].version
            requested_extras.update((name, value) for value in requirement.extras)
    if (
        not manifest.get("agentBase")
        or not manifest.get("acp")
        or requested.get("mdiri-agent-base") != manifest["agentBase"]
        or requested.get("agent-client-protocol") != manifest["acp"]
    ):
        raise ValueError("Planning Harness lock is missing its execution dependencies")
    deps = temporary / "deps"
    # All transitive requirements are explicit in the reviewed lock. Package
    # metadata cannot introduce a remote installer or an additional URL here.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-deps",
            "--no-index",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-compile",
            "--find-links",
            str(wheelhouse),
            "--target",
            str(deps),
            "-r",
            str(lock),
        ],
        env=env,
        check=True,
    )
    verify_dependencies(stage, deps, requested_extras)
    for entry in deps.iterdir():
        if entry.name in {"bin", "Scripts"}:
            continue  # ACP is launched by the packaged interpreter, without PATH scripts.
        target = stage / "site" / entry.name
        if target.exists():
            raise ValueError(
                "A runtime dependency would overwrite a Planning Harness component"
            )
        entry.rename(target)


def unpack(
    archive,
    expected,
    destination,
    platform,
    arch,
    desktop,
    runtime_schema,
    managed_python,
    wheelhouse,
):
    native_arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine().lower())
    if (platform, arch, managed_python) != (
        sys.platform,
        native_arch,
        python_version(),
    ):
        raise ValueError(
            "Prepare the Planning Harness with the target product interpreter"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive.open("rb") as source,
        tempfile.TemporaryDirectory(dir=destination.parent) as temp,
    ):
        if hashlib.file_digest(source, "sha256").hexdigest() != expected:
            raise ValueError("Planning Harness wheel digest mismatch")
        source.seek(0)
        stage = Path(temp) / "payload"
        stage.mkdir()
        version = extract_wheel(source, archive.name, stage)
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if any(
            type(manifest.get(k)) is not type(v) or manifest[k] != v
            for k, v in {
                "schema": 1,
                "platform": "any",
                "arch": "any",
                "python": "3.12",
                "desktopBaseline": desktop,
                "runtimeSchema": runtime_schema,
                "managedPython": managed_python,
                "protocolVersion": 1,
                "hostExtensionVersion": 1,
                "distribution": DISTRIBUTION,
                "version": version,
                "harnessId": "ai-planning",
            }.items()
        ):
            raise ValueError("Planning Harness is incompatible with this client")
        for name in ("launch.py", "requirements.lock", "site/planning_cli/__main__.py"):
            if not (stage / name).is_file():
                raise ValueError("Missing Planning Harness component: " + name)
        install_dependencies(stage, wheelhouse, Path(temp), manifest)
        probe(stage)
        previous = Path(temp) / "previous"
        if destination.exists():
            os.replace(destination, previous)
        try:
            os.replace(stage, destination)
        except OSError:
            if previous.exists():
                os.replace(previous, destination)
            raise


def probe(root):
    # Keep stdin open until the handshake reply has arrived.
    with tempfile.TemporaryDirectory() as state:
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", str(root / "launch.py"), "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={
                **{
                    k: v
                    for k, v in os.environ.items()
                    if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
                },
                "PLANNING_STATE_DIR": state,
            },
        )
        try:
            replies = queue.Queue()
            threading.Thread(
                target=lambda: replies.put(child.stdout.readline()), daemon=True
            ).start()
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
            child.stdin.write(json.dumps(request).encode() + b"\n")
            child.stdin.flush()
            reply = json.loads(replies.get(timeout=30))
            result = reply.get("result", {})
            if (
                reply.get("id") != 1
                or result.get("protocolVersion") != 1
                or result.get("agentInfo", {}).get("name") != "planning-harness"
                or result.get("agentInfo", {}).get("version")
                != json.loads((root / "manifest.json").read_text(encoding="utf-8"))[
                    "version"
                ]
            ):
                raise ValueError(
                    "Packaged Planning failed its ACP initialization probe"
                )
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()


if __name__ == "__main__":
    if sys.argv[1] == "--probe":
        probe(Path(sys.argv[2]))
        sys.exit(0)
    archive, digest, dest, *target = sys.argv[1:]
    unpack(Path(archive), digest, Path(dest), *target[:-1], Path(target[-1]))
