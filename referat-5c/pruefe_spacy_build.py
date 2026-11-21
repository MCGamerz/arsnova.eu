"""Dokumentiert die lokale Hashbindung der vier Sprach-Wheels im Release-Build."""
import argparse
import json
import re
import shlex
from pathlib import Path

from tools.pruefe_5c import RELEASE, provenance


def inspect_build(root):
    provenance(root)
    spacy = root / "docker/spacy"
    requirements = (spacy / "requirements.txt").read_text()
    dockerfile = (spacy / "Dockerfile").read_text()
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    installs = []
    for line in logical_lines:
        if line.startswith("RUN "):
            for command in line[4:].split("&&"):
                words = shlex.split(command)
                if words[:2] == ["pip", "install"] and "-r" in words and words[words.index("-r") + 1] == "requirements.txt":
                    installs.append(words)
    if len(installs) != 1:
        raise ValueError("Installationsschritt nicht eindeutig")
    wheels = [line.strip() for line in requirements.splitlines() if " @ " in line and ".whl" in line]
    provided = [line.split(" @ ", 1)[0] for line in wheels if re.search(r"(#sha256=|--hash=sha256:)[a-fA-F0-9]{64}", line)]
    data = {"id": "L2", "release": RELEASE, "methode": "Statische Prüfung des Release-Dockerfiles und der Paketliste",
            "dockerfile": "docker/spacy/Dockerfile", "paketliste": "docker/spacy/requirements.txt",
            "pip_install": installs[0], "wheel_urls": len(wheels), "wheel_urls_mit_sha256": provided,
            "require_hashes_im_installaufruf": "--require-hashes" in installs[0],
            "require_hashes_in_paketliste": any(line.strip() == "--require-hashes" for line in requirements.splitlines()),
            "pip_require_hashes_im_dockerfile": any(line.lstrip().startswith("ENV ") and "PIP_REQUIRE_HASHES" in line for line in logical_lines)}
    if len(wheels) != 4:
        raise ValueError("Erwartetes Release-Inventar umfasst vier Sprach-Wheels")
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    args.out.parent.mkdir(exist_ok=True, parents=True)
    args.out.write_text(json.dumps(inspect_build(args.repo.resolve()), indent=2, ensure_ascii=False) + "\n")
