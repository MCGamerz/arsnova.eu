"""Prüfe Modellstart, Prozessrechte und Artefaktaustausch an Release 1.3.0."""
import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pruefe_5c import RELEASE, provenance


def run(args, *, env=None, check=True, timeout=900):
    proc = subprocess.run(args, env=env, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} endete mit {proc.returncode}: {proc.stderr[-2000:]}")
    return proc


def docker(*args, **options):
    return run(["docker", *args], **options).stdout.strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def kernel_checks(name):
    # Read the status of the server itself, not the shell used by docker exec.
    find_status = """
for comm in /proc/[0-9]*/comm; do
    read -r name < "$comm" || continue
    if [ "$name" = llama-server ]; then
        cat "${comm%/comm}/status"
        exit
    fi
done
exit 1
"""
    status = {}
    for line in docker("exec", name, "sh", "-c", find_status).splitlines():
        field, value = line.split(":", 1)
        if field in {"Name", "Uid", "CapEff", "CapBnd", "NoNewPrivs"}:
            status[field] = value.strip()
    mounts = {}
    for line in docker("exec", name, "cat", "/proc/self/mountinfo").splitlines():
        fields = line.split()
        if fields[4] in {"/", "/models", "/tmp"}:
            mounts[fields[4]] = fields[5].split(",")
    # The writable test directory belongs to this experiment. No write targets a model.
    probe = run(["docker", "exec", "-e", "LC_ALL=C", name, "touch",
                 "/models/5c-schreibprobe/testdatei"], check=False)
    control = run(["docker", "exec", name, "touch", "/tmp/5c-schreibprobe"], check=False)
    readings = {"modellprozess": status, "mount_optionen": mounts,
                "schreibprobe_models": {"exitcode": probe.returncode,
                                       "meldung": probe.stderr.strip()},
                "schreibprobe_tmp_exitcode": control.returncode}
    checks = {"prozess_ohne_root": status["Uid"].split()[1] == "65532",
              "cap_eff_null": int(status["CapEff"], 16) == 0,
              "cap_bnd_null": int(status["CapBnd"], 16) == 0,
              "no_new_privs": status["NoNewPrivs"] == "1",
              "root_ro": "ro" in mounts["/"], "models_ro": "ro" in mounts["/models"],
              "model_schreiben_blockiert": probe.returncode != 0 and "Read-only file system" in probe.stderr,
              "tmp_schreiben_erlaubt": control.returncode == 0}
    return readings, checks


def stage_original(model, target):
    if model.stat().st_mode & 0o004:
        try:
            os.link(model, target)
            return
        except OSError:
            pass
    shutil.copyfile(model, target)
    target.chmod(0o644)


def lab(root, model, evidence):
    evidence["gepruefte_release_dateien"] = len(provenance(root))
    manifest_path = root / "docker/open-weight-llm/model-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if model.name != manifest["filename"] or model.stat().st_size != manifest["sizeBytes"]:
        raise ValueError("Dateiname oder Größe stimmen nicht mit dem Release-Manifest überein.")
    evidence["docker_version"] = docker("version", "--format", "{{.Server.Version}}")
    evidence["compose_version"] = docker("compose", "version", "--short")
    evidence["release_basisimage"] = manifest["runtimeImage"]
    evidence["modell_quellrevision"] = manifest["sourceRevision"]
    evidence["modell_bytes"] = model.stat().st_size
    evidence["release_pruefer_original"] = run(["node", str(root / "scripts/open-weight-llm/verify-model.mjs"),
                                              str(manifest_path), str(model)]).stdout.strip()
    evidence["original_sha256"] = digest(model)
    if evidence["original_sha256"] != manifest["sha256"]:
        raise ValueError("Originalmodell weicht vom Release-Manifest ab.")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 8080))

    project = "arsnova-5c-" + secrets.token_hex(5)
    image_tag = project + ":labor"
    positive, negative = project + "-original", project + "-variante"
    with tempfile.TemporaryDirectory(prefix=project) as tmp:
        work = Path(tmp)
        config = work / "docker-compose.llm.yml"
        shutil.copyfile(root / config.name, config)
        env_file = work / ".env.llm"
        env_file.write_text("OPEN_WEIGHT_LLM_TOKEN=" + secrets.token_hex(32) +
                            "\nOPEN_WEIGHT_LLM_BIND_ADDRESS=127.0.0.1\n")
        env_file.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPEN_WEIGHT_LLM_")}
        env["OPEN_WEIGHT_LLM_IMAGE"] = image_tag
        compose = ["docker", "compose", "-p", project, "-f", str(config),
                   "--env-file", str(env_file), "--profile", "llm", "run"]
        original_dir, variant_dir = work / "original", work / "variante"
        original_dir.mkdir(mode=0o755)
        variant_dir.mkdir(mode=0o755)
        source = original_dir / manifest["filename"]
        stage_original(model, source)
        probe_dir = original_dir / "5c-schreibprobe"
        probe_dir.mkdir()
        probe_dir.chmod(0o777)
        try:
            docker("build", "--tag", image_tag, str(root / "docker/open-weight-llm"))
            image_id = docker("image", "inspect", "--format", "{{.Id}}", image_tag)
            evidence["lokale_image_id"] = image_id
            env["OPEN_WEIGHT_LLM_MODEL_DIR"] = str(original_dir)
            start = time.monotonic()
            run([*compose, "-d", "-T", "--no-deps", "--name", positive, "open-weight-llm"], env=env)
            deadline = start + 420
            while True:
                state = json.loads(docker("inspect", "--format", "{{json .State}}", positive))
                if state.get("Health", {}).get("Status") == "healthy" or not state["Running"]:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Originalcontainer erreichte innerhalb von 420 s keinen erfolgreichen Healthcheck.")
                time.sleep(2)
            ok = {"health": state.get("Health", {}).get("Status"), "laeuft": state["Running"],
                  "zeit_bis_beobachtung_s": round(time.monotonic() - start, 3)}
            evidence["original"] = ok
            if not ok["laeuft"] or ok["health"] != "healthy":
                ok["exitcode"] = state.get("ExitCode")
                raise RuntimeError("Das Originalmodell erreichte keinen erfolgreichen Healthcheck.")
            inspect = json.loads(docker("inspect", positive))[0]
            host = inspect["HostConfig"]
            mounts = [m for m in inspect["Mounts"] if m["Destination"] == "/models"]
            checks = {"image_id_stimmt": inspect["Image"] == image_id,
                      "nutzer_stimmt": inspect["Config"]["User"] == "65532:65532",
                      "root_schreibgeschuetzt": host["ReadonlyRootfs"] is True,
                      "models_schreibgeschuetzt": len(mounts) == 1 and mounts[0]["RW"] is False,
                      "alle_capabilities_entzogen": any(v in {"ALL", "CAP_ALL"} for v in host.get("CapDrop") or []),
                      "privilegiengewinn_gesperrt": any(v in {"no-new-privileges", "no-new-privileges:true",
                                                             "no-new-privileges=true"} for v in host.get("SecurityOpt") or [])}
            ok["docker_inspect"] = checks
            ok["kernel"], ok["kernel_pruefungen"] = kernel_checks(positive)
            ok["bestanden"] = all(checks.values()) and all(ok["kernel_pruefungen"].values())
            docker("rm", "-f", positive)

            changed = variant_dir / manifest["filename"]
            shutil.copyfile(source, changed)
            changed.chmod(0o644)
            with changed.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                old = stream.read(1)[0]
                stream.seek(-1, os.SEEK_END)
                stream.write(bytes([old ^ 1]))
            env["OPEN_WEIGHT_LLM_MODEL_DIR"] = str(variant_dir)
            bad = {"sha256": digest(changed), "bytes": changed.stat().st_size, "byte_offset": changed.stat().st_size - 1,
                   "byte_vorher": old, "byte_nachher": old ^ 1}
            evidence["gegenprobe"] = bad
            start = time.monotonic()
            # --rm suppresses the deployment restart policy for this deliberate failure.
            stopped = run([*compose, "--rm", "-T", "--no-deps", "--name", negative, "open-weight-llm"],
                          env=env, check=False, timeout=120)
            bad["dauer_s"] = round(time.monotonic() - start, 3)
            bad["exitcode"] = stopped.returncode
            bad["meldung_checksum_mismatch"] = "Pinned GGUF checksum mismatch" in (stopped.stderr + stopped.stdout)
            bad["bestanden"] = (bad["bytes"] == evidence["modell_bytes"] and bad["sha256"] != evidence["original_sha256"]
                                and stopped.returncode == 65 and bad["meldung_checksum_mismatch"])
            evidence["bestanden"] = ok["bestanden"] and bad["bestanden"]
            if not evidence["bestanden"]:
                raise RuntimeError("Mindestens eine überprüfte Erwartung wurde verfehlt. Details im JSON.")
        finally:
            for name in (negative, positive):
                run(["docker", "rm", "-f", name], check=False)
            run(["docker", "image", "rm", image_tag], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--modell", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {"release": RELEASE, "zeit_utc": datetime.now(timezone.utc).isoformat(),
              "system": platform.system(), "architektur": platform.machine(), "bestanden": False}
    if os.environ.get("GITHUB_RUN_ID"):
        result["github_run"] = os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"] + "/actions/runs/" + os.environ["GITHUB_RUN_ID"]
        result["github_versuch"] = os.environ.get("GITHUB_RUN_ATTEMPT")
        result["test_commit"] = os.environ.get("GITHUB_SHA")
    try:
        lab(args.repo.resolve(), args.modell.resolve(), result)
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result["fehler"] = str(exc)
        print(result["fehler"], file=sys.stderr)
    finally:
        args.out.parent.mkdir(exist_ok=True, parents=True)
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    sys.exit(0 if result["bestanden"] else 1)
