import base64
import csv
import hashlib
import importlib.util
import io
import json
import platform
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

script = Path(__file__).resolve().parents[1] / "scripts" / "unpack-planning.py"
spec = importlib.util.spec_from_file_location("planning_bundle", script)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def wheel(directory, name, version, files, *, tamper=None):
    stem = name.replace("-", "_")
    dist = f"{stem}-{version}.dist-info"
    files = dict(files)
    files.setdefault(
        f"{dist}/WHEEL",
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    )
    files.setdefault(
        f"{dist}/METADATA",
        f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nAuthor: Fixture Publisher\nRequires-Python: ==3.12.*\n".encode(),
    )
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    for path, data in files.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow([path, "sha256=" + digest, len(data)])
    writer.writerow([f"{dist}/RECORD", "", ""])
    files[f"{dist}/RECORD"] = records.getvalue().encode()
    if tamper:
        files[tamper] = b"changed after creating RECORD"
    dest = directory / f"{stem}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(dest, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return dest, hashlib.sha256(dest.read_bytes()).hexdigest()


@unittest.skipUnless(
    sys.version_info[:2] == (3, 12), "Runtime packager requires CPython 3.12"
)
class BundleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dest = self.root / "installed"
        self.wheelhouse = self.root / "wheelhouse"
        self.wheelhouse.mkdir()
        arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}[
            platform.machine().lower()
        ]
        self.target = (
            sys.platform,
            arch,
            "1.13.19",
            "soduwork.client-runtime/v1",
            platform.python_version(),
        )
        self.lock = ""
        for dependency in ["mdiri-agent-base", "agent-client-protocol"]:
            _, digest = wheel(
                self.wheelhouse,
                dependency,
                "1.0.0",
                {dependency.replace("-", "_") + ".py": b""},
            )
            self.lock += f"{dependency}==1.0.0 --hash=sha256:{digest}\n"

    def artifact(
        self,
        *,
        extra=None,
        change=None,
        lock=None,
        tamper=None,
        handshake="planning-harness",
    ):
        module = bundle.PACKAGE
        manifest = {
            "schema": 1,
            "platform": "any",
            "arch": "any",
            "python": "3.12",
            "distribution": bundle.DISTRIBUTION,
            "version": "0.2.0",
            "agentBase": "1.0.0",
            "acp": "1.0.0",
            "harnessId": "ai-planning",
            "desktopBaseline": self.target[2],
            "runtimeSchema": self.target[3],
            "managedPython": self.target[4],
            "protocolVersion": 1,
            "hostExtensionVersion": 1,
            **(change or {}),
        }
        launch = f'import json\ninput()\nprint(json.dumps(dict(id=1,result=dict(protocolVersion=1,agentInfo=dict(name="{handshake}",version="0.2.0")))))\n'
        return wheel(
            self.root,
            bundle.DISTRIBUTION,
            "0.2.0",
            {
                f"{module}/launch.py": launch.encode(),
                f"{module}/manifest.json": json.dumps(manifest).encode(),
                f"{module}/site/planning_cli/__main__.py": b"",
                f"{module}/requirements.lock": (
                    self.lock if lock is None else lock
                ).encode(),
                **(extra or {}),
            },
            tamper=tamper,
        )

    def deploy(self, archive, digest):
        bundle.unpack(archive, digest, self.dest, *self.target, self.wheelhouse)

    def test_verified_wheel_and_offline_dependency_install(self):
        archive, digest = self.artifact()
        self.deploy(archive, digest)
        self.assertTrue((self.dest / "site/mdiri_agent_base.py").is_file())
        self.assertTrue((self.dest / "site/agent_client_protocol.py").is_file())
        self.assertTrue(
            (self.dest / f"{bundle.PACKAGE}-0.2.0.dist-info/METADATA").is_file()
        )
        marker = self.dest / "marker"
        marker.touch()
        archive, digest = self.artifact(handshake="incompatible")
        with self.assertRaises(ValueError):
            self.deploy(archive, digest)
        self.assertTrue(
            marker.exists(), "Failed staging must preserve the installed harness"
        )

    def test_reject_incompatible_or_unverified_bytes(self):
        for change in [
            {"arch": "x64"},
            {"version": "0.1.0"},
            {"harnessId": "pi"},
            {"protocolVersion": True},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.deploy(*self.artifact(change=change))
        archive, _ = self.artifact()
        with self.assertRaises(ValueError):
            self.deploy(archive, "0" * 64)
        with self.assertRaises(ValueError):
            self.deploy(*self.artifact(tamper=f"{bundle.PACKAGE}/launch.py"))
        info = f"{bundle.PACKAGE}-0.2.0.dist-info"
        for suffix in [
            "../escape",
            "CON",
            "../launch.py",
            "hidden:stream",
            "LAUNCH.PY",
        ]:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.deploy(
                    *self.artifact(extra={bundle.PACKAGE + "/" + suffix: b"extra"})
                )
        with self.assertRaises(ValueError):
            self.deploy(
                *self.artifact(
                    extra={info + "/METADATA": b"Name: other\nVersion: 0.2.0\n"}
                )
            )
        with self.assertRaises(ValueError):
            self.deploy(
                *self.artifact(
                    extra={
                        info
                        + "/WHEEL": b"Wheel-Version: 1.0\nTag: cp312-none-win_amd64\n"
                    }
                )
            )

    def test_lock_uses_only_hashed_named_packages(self):
        for lock in [
            "--extra-index-url https://example.invalid",
            "other @ https://example.invalid/other.whl",
            "unpinned>=1",
            "pinned==1.0",
            "pinned==1.0 --hash=md5:0000",
        ]:
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                self.deploy(*self.artifact(lock=self.lock + lock))
        self.assertFalse(self.dest.exists())

    def test_missing_transitive_dependency_rejects_staged_harness(self):
        dep = "mdiri_agent_base-1.0.0.dist-info/METADATA"
        _, digest = wheel(
            self.wheelhouse,
            "mdiri-agent-base",
            "1.0.0",
            {
                dep: b"Metadata-Version: 2.3\nName: mdiri-agent-base\nVersion: 1.0.0\n"
                b"Requires-Dist: missing-support>=1; extra == 'local'\n",
            },
        )
        lock = "\n".join(
            row
            for row in self.lock.splitlines()
            if not row.startswith("mdiri-agent-base")
        )
        lock += f"\nmdiri-agent-base[local]==1.0.0 --hash=sha256:{digest}\n"
        with self.assertRaisesRegex(ValueError, "missing-support"):
            self.deploy(*self.artifact(lock=lock))
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
