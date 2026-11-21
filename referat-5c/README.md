# Moderationskompass 5C · Prüfung der Artefakte und der Laufzeit

**Fragestellung.** Sind die für den privaten Moderationskompass von arsnova.eu 1.3 eingesetzten Artefakte und ihre technische Ausführungsumgebung ausreichend abgesichert?

Das freigegebene Qwen-Modell startete im Docker-Labor erfolgreich. Der Modellserver lief als UID 65532 ohne effektive Linux-Capabilities. Der Kernel blockierte die Schreibprobe unter `/models` und ließ dieselbe Operation unter `/tmp` zu. Bei einer gleich großen Kopie mit genau einem geänderten Byte meldete der Entrypoint eine falsche Prüfsumme und beendete den Start mit Code 65. Für die vier spaCy-Sprachpakete enthält die ausgelieferte Paketliste jeweils eine feste Version, aber keinen eigenen SHA-256-Sollwert.

## Belege

| Quelle | Inhalt |
| --- | --- |
| [Release-Commit 87c39334](https://github.com/MCGamerz/arsnova.eu/tree/87c39334619fbcb0c5782b061da1ba0df0ba3142) | Unveränderte Referenz für Programmcode, Dockerfile, Compose-Datei und Modellmanifest. |
| [Docker-Lauf auf GitHub](https://github.com/MCGamerz/arsnova.eu/actions/runs/44924586077) und [Messdatei](evidence/dockerlauf.json) | Ein Docker-Lauf mit zwei Fällen. Enthält Zeitpunkte, Hashes, Docker-/Kernelbefunde, Schreibproben und Exitcode. Die gespeicherte Messdatei ist bytegleich mit dem Workflow-Artefakt. |
| [Lauf 1](evidence/messwerte.json), [Lauf 2](evidence/messwerte-lauf-2.json), [Lauf 3](evidence/messwerte-lauf-3.json) | Je zwölf lokale Release-Prüfungen einschließlich positiver und negativer Kontrollen. In öffentlich bereitgestellten Kopien sind absolute Arbeitsverzeichnisse der Node-Fehlermeldungen gekürzt. |
| [spaCy-Buildprüfung](evidence/spacy-build.json) | Statische Auswertung des pip-Installationsschritts und der vier Wheel-Referenzen im Release. |
| [Messbericht](5C-Messbericht.pdf) | Versuchsaufbau, Ergebnisse und Betriebsentscheidung auf drei Seiten. |

## Vergleich im Docker-Labor

| | Unverändertes Original | Kontrollierte Gegenprobe |
| --- | --- | --- |
| Dateiname | Qwen3-4B-Instruct-2507-Q4_K_M.gguf | Gleich |
| Größe | 2.497.281.120 Bytes | Gleich |
| Kontrollierte Änderung | Original laut Release-Manifest | Letztes Byte bei Offset 2.497.281.119 von 166 auf 167 geändert |
| SHA-256 | `3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597` | `ca3b6bcafa8fd2221b2a8fda2d4bd9497024f4a89fea6d00eee022360b706a57` |
| Beobachtung | Docker meldet `healthy`, der Prozess heißt `llama-server` | `Pinned GGUF checksum mismatch`, Exit 65 |
| Messdauer auf diesem Runner | 6,845 s bis zur Beobachtung des Zustands `healthy` | 9,550 s vom Compose-Aufruf bis zum Ende der Gegenprobe |

Die Zeitmessungen betreffen unterschiedliche Vorgänge. Der Docker-Versuch nutzte den privaten Inferenzdienst aus dem Release auf einem GitHub-Ubuntu-Laborrunner mit Loopback-Bindung, eigenem kurzlebigem Labortoken und lokal gebautem Image aus dem festgelegten Basisimage. Die Messung erstreckt sich auf diesen Modellserver und seine Prozessrechte. Sie enthält keine Messung der Antwortqualität oder der Produktionslatenz von arsnova.eu.

## Versuche wiederholen

Vorausgesetzt werden Linux, Python ≥ 3.11, PyYAML und Node.js 22 oder 24. Für die lokalen Mount-Tests ist bubblewrap mit zulässigen Benutzer-Namespaces erforderlich. Für den Docker-Versuch sind zusätzlich Docker Engine mit Compose v2, genügend Speicher für das rund 2,5 GB große Modell und seine Kopie sowie ein freier Loopback-Port 8080 erforderlich. Für den GitHub-Lauf wurden Docker 28.0.4 und Compose 2.38.2 verwendet.

Die Befehle werden im Verzeichnis dieses Dokuments ausgeführt. Im Klon des Testbranches ist `release_root=..` zu setzen. Im entpackten Testpaket ist `release_root=release` zu setzen.

```sh
release_root=release
python3 tools/pruefe_5c.py --repo "$release_root" --out eigener-lauf/lokal.json
python3 pruefe_spacy_build.py --repo "$release_root" --out eigener-lauf/spacy-build.json
```

Das Originalmodell über das im Manifest festgelegte Ziel beziehen. `curl` und `jq` werden für diese beiden Downloadbefehle gebraucht. Das Laborskript überprüft Größe und SHA-256 vor dem Start und verändert nur die für die Gegenprobe angelegte Kopie.

```sh
manifest="$release_root/docker/open-weight-llm/model-manifest.json"
mkdir -p modelle
modell="modelle/$(jq -r .filename "$manifest")"
curl --fail --location --retry 4 "$(jq -r .downloadUrl "$manifest")" --output "$modell"
python3 tools/docker_labor.py --repo "$release_root" --modell "$modell" --out eigener-lauf/docker.json
```

## Auswertung und Urheberschaft

Die getestete Modelldatei ist an eine veröffentlichte Prüfsumme gebunden. Die Containerhärtung war auf dem beobachteten Linux-System wirksam. Die Auswahl einer vertrauenswürdigen Ursprungsquelle bleibt eine eigene Betreiberentscheidung. Für die vier spaCy-Wheels sollte der Build künftig geprüfte SHA-256-Sollwerte erzwingen.

Das KI-System ChatGPT/Codex erstellte den Prüfcode, führte die lokalen Versuche und den GitHub-Lauf auf Anforderung aus und bereitete die Auswertung auf. Die drei lokalen Laufprotokolle, die ergänzende Buildprüfung und der vollständige Docker-Versuch sind getrennt ausgewiesen.
