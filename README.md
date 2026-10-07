# clore-miner

Docker image for mining on [clore.ai](https://clore.ai) rentals without HiveOS: **peakminer 2.18.0** (QTC, PRL) and
**SRBMiner-Multi 3.7.2** (QTC), plus a small control agent (`agent.py`, Python stdlib) that runs the miner, restarts it
when it dies, and serves its state over HTTP on port 8080.

Image: `ghcr.io/justincoffe/clore-miner:<version>` (built by `.github/workflows/build.yml`).

## Order env
| var | example | |
|---|---|---|
| `COIN` | `QTC` / `PRL` | |
| `MINER` | `srbminer` / `peakminer` | srbminer: QTC only |
| `POOLS` | `qtc-ru.kryptex.network:7049,qtc-eu.kryptex.network:7049` | first = primary, rest = failover |
| `WALLET` | address | |
| `WORKER` | `RIG31` | |
| `DEVICES` | `2,3` | optional: CUDA indices to use (hides broken GPUs) |
| `EXTRA` | `--cmp-unlock` | optional extra miner args |
| `TOKEN` | random | bearer token for the API (all but `/health`) |
| `SSH_PUBKEY` | `ssh-ed25519 ...` | optional: root ssh key |

Ports: `22` (ssh), `8080` (agent).

## API
- `GET /health` -> `{ok, version, rev}` (no token)
- `GET /status` -> config, miner process (running, uptime, restarts), stats (hash H/s, accepted, rejected, pool,
  connected, per GPU), `nvidia-smi` (driver, vbios, temp, fan, power)
- `GET /log?n=200`
- `POST /config` `{"coin": "PRL", "miner": "peakminer", "pools": ["prl-ru.kryptex.network:7048"]}` -> saved, miner restarted
- `POST /restart`

The config is kept in `/root/miner.json`, so a container restart keeps the last `/config`.
