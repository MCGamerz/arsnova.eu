"""Prüfungen für den privaten Moderationskompass in arsnova.eu 1.3.0."""
import argparse
import errno
import hashlib
import json
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

RELEASE = "87c39334619fbcb0c5782b061da1ba0df0ba3142"
HERE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=180, **kwargs)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def provenance(root):
    names = json.loads((HERE / "sources.json").read_text())
    for name, expected in names.items():
        data = (root / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != expected:
            raise ValueError(f"Abweichende Quelldatei: {name}")
    return names


def launch_script(directory, model_dir, token):
    # Nur die Original-Skripte und das jeweilige Testverzeichnis einbinden.
    # In diesem Test wird kein llama-server bereitgestellt und kein Socket geöffnet.
    mounts = []
    for name in ["/usr", "/bin", "/lib", "/lib64"]:
        if Path(name).exists():
            mounts += ["--ro-bind", name, name]
    return run(["bwrap", "--unshare-user", "--die-with-parent", "--new-session",
                "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", *mounts,
                "--dev", "/dev", "--tmpfs", "/tmp",
                "--ro-bind", str(directory), "/opt/arsnova",
                "--ro-bind", str(model_dir), "/models",
                "--setenv", "OPEN_WEIGHT_LLM_TOKEN", token,
                "--", "/bin/sh", "/opt/arsnova/entrypoint.sh"])


def model_mount_probe(path, readonly):
    # Kontrollierter Schreibversuch nur im eigens erzeugten Wegwerfverzeichnis.
    probe = """import errno,os,sys
try:
    with open(sys.argv[1], 'xb') as f:
        f.write(b'5C')
    print('Schreiben erfolgreich')
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
    sys.exit(e.errno)
"""
    return run(["bwrap", "--unshare-user", "--die-with-parent", "--new-session",
                "--ro-bind", "/", "/", "--ro-bind" if readonly else "--bind", str(path), str(path),
                "--", sys.executable, "-c", probe, str(path / "probe")])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    root = args.repo.resolve()
    sources = provenance(root)
    runtime = root / "docker/open-weight-llm"
    manifest_path = runtime / "model-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js wird für den Release-Prüfer benötigt.")
    rows = []

    def record(key, case, expected, actual, passed, **data):
        rows.append(dict(id=key, fall=case, soll=expected, ist=actual,
                         status="BESTANDEN" if passed else "FEHLER", **data))

    def process(key, case, argv_result, rc, marker, **data):
        combined = (argv_result.stdout + argv_result.stderr).strip()
        line = next((line for line in combined.splitlines() if marker in line and not line.lstrip().startswith("throw ")), "")
        record(key, case, f"Exit {rc}, {marker}", f"Exit {argv_result.returncode}, {line or combined[:250]}",
               argv_result.returncode == rc and bool(line), exitcode=argv_result.returncode,
               stdout=argv_result.stdout, stderr=argv_result.stderr, **data)

    sha = manifest["sha256"]
    model_pin = re.search(r"^model_sha256=([0-9a-f]{64})$", (runtime / "entrypoint.sh").read_text(), re.M)[1]
    checksum = (runtime / "model.sha256").read_text().split()[0]
    image = re.search(r"^FROM (\S+)", (runtime / "Dockerfile").read_text(), re.M)[1]
    record("A1", "Release: Modell-Pins in Manifest, Prüfsummendatei und Startskript", "3 identische SHA-256-Werte",
           [sha, checksum, model_pin], sha == model_pin == checksum and bool(re.fullmatch("[0-9a-f]{64}", sha)))
    record("A2", "Release: Basisimage in Manifest und Dockerfile", "identische Referenz mit SHA-256-Digest",
           image, image == manifest["runtimeImage"] and bool(re.search(r"@sha256:[0-9a-f]{64}$", image)))

    service = yaml.safe_load((root / "docker-compose.llm.yml").read_text())["services"]["open-weight-llm"]
    model_volumes = [v for v in service["volumes"] if v.endswith(":/models:ro")]
    settings = {"root_schreibgeschuetzt": service["read_only"], "cap_drop": service["cap_drop"],
                "security_opt": service["security_opt"], "model_mounts": model_volumes}
    expected = service["read_only"] is True and service["cap_drop"] == ["ALL"] and "no-new-privileges:true" in service["security_opt"] and len(model_volumes) == 1
    record("H1", "Release: deklarierte Containerhärtung", "Root-FS ro, cap_drop ALL, no-new-privileges, /models ro", settings, expected)
    wheels = [line for line in (root / "docker/spacy/requirements.txt").read_text().splitlines() if " @ https://" in line and not line.startswith("#")]
    hashed = [line for line in wheels if re.search(r"(?:#sha256=|--hash=sha256:)[0-9a-fA-F]{64}", line)]
    model_packages = [line.split(" @ ")[0] for line in wheels]
    notices = (root / "docker/spacy/NOTICE").read_text()
    license_lines = [line for line in notices.splitlines() if any(line.startswith("- " + name.replace("-", "_") + " ") for name in model_packages)]
    record("L1", "Release: spaCy-Sprachpakete und eigene Hash-Pins in requirements.txt", "Inventar von vier versionierten Wheel-URLs und Lizenzangaben",
           {"pakete": model_packages, "wheel_urls": len(wheels), "mit_hash_in_requirements": len(hashed), "notice": license_lines},
           len(wheels) == len(license_lines) == 4, bewertung="Inventarbefund, keine Sicherheitsfreigabe")

    with tempfile.TemporaryDirectory(prefix="arsnova-5c-") as tmp:
        work = Path(tmp)
        candidate = work / manifest["filename"]
        verify = [node, str(root / "scripts/open-weight-llm/verify-model.mjs")]
        # Bekannte Bytes und separat festgelegter Sollwert für die Positivkontrolle.
        control = b"abc"
        control_sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        fixture_manifest = work / "kontrolle.json"
        fixture_manifest.write_text(json.dumps({"filename": "fixture.bin", "sizeBytes": 3, "sha256": control_sha}))
        fixture = work / "fixture.bin"
        fixture.write_bytes(control)
        process("I1", "Positivkontrolle: Release-Prüfer, Prüfmanifest, 3 Referenzbytes", run([*verify, fixture_manifest, fixture]), 0, "verified 3 bytes")
        fixture.write_bytes(b"abd")
        process("I2", "Gegenprobe zu I1: nur Byte 3 von c nach d geändert", run([*verify, fixture_manifest, fixture]), 1, "Error: GGUF digest mismatch",
                bytezahl=fixture.stat().st_size, sha256=digest(fixture.read_bytes()))
        candidate.write_bytes(b"GGUF")
        process("I3", "Release-Manifest mit verkürzter Ersatzdatei", run([*verify, manifest_path, candidate]), 1, "Error: GGUF size mismatch")
        with candidate.open("r+b") as target:
            target.truncate(manifest["sizeBytes"])
        start = time.perf_counter()
        res = run([*verify, manifest_path, candidate])
        process("I4", "Release-Manifest mit gleich großer Ersatzdatei (GGUF, dann Nullbytes)", res, 1, "Error: GGUF digest mismatch",
                bytezahl=candidate.stat().st_size, dauer_s=round(time.perf_counter()-start, 3))
        if shutil.which("bwrap"):
            empty = work / "leer"
            empty.mkdir()
            token = secrets.token_hex(24)
            process("E1", "Release-Entrypoint: leeres Modellverzeichnis bei gültigem Test-Token", launch_script(runtime, empty, token), 66, "Pinned GGUF is not readable")
            process("E2", "Release-Entrypoint: gleiche Umgebung, gleich große Ersatzdatei vorhanden", launch_script(runtime, work, token), 65, "Pinned GGUF checksum mismatch")
            mount = work / "mount-probe"
            mount.mkdir()
            process("H2", "Linux-Mountprobe: separates Testverzeichnis wie /models schreibgeschützt eingebunden", model_mount_probe(mount, True), errno.EROFS, "EROFS")
            process("H3", "Gegenprobe zu H2: derselbe Mount schreibbar eingebunden", model_mount_probe(mount, False), 0, "Schreiben erfolgreich")
        else:
            for key in ("E1", "E2", "H2", "H3"):
                rows.append({"id":key, "status":"AUSGELASSEN", "grund":"Linux mit bubblewrap erforderlich"})

    result = dict(release=RELEASE, zeit_utc=datetime.now(timezone.utc).isoformat(), linux=platform.platform(),
                  python=platform.python_version(), node=run([node, "--version"]).stdout.strip(),
                  bubblewrap=run(["bwrap","--version"]).stdout.strip() if shutil.which("bwrap") else None,
                  quelle_git_blobs=sources, ergebnisse=rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for row in rows:
        print(row["id"], row["status"], row.get("fall", ""))
    return int(any(row["status"] == "FEHLER" for row in rows))


if __name__ == "__main__":
    sys.exit(main())
