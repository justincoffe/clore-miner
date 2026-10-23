#!/usr/bin/env python3
"""clore-miner agent: runs ONE miner (peakminer or SRBMiner) for one coin, restarts it when it dies, and serves its
state + a small control API over HTTP -- the engine talks to this instead of HiveOS.

Config: /root/miner.json (kept across container restarts, so a coin switched mid-rental survives one); first start
takes it from env: COIN (QTC|PRL), MINER (peakminer|srbminer), POOLS ("host:port host:port" -- SPACES: Clore refuses
',' '!' '|' ';' in env values, 10-07), WALLET, WORKER,
DEVICES (CUDA indices to use, e.g. "2,3" -- hides broken GPUs), EXTRA (extra miner args).  TOKEN: bearer token for
every endpoint but /health.  Stdlib only.

  GET  /health   {ok, version, rev}                       (no token)
  GET  /status   config, miner process, miner stats (one format for both miners), nvidia-smi GPUs
  GET  /log?n=   last n lines of the miner log
  POST /config   {coin?, miner?, pools?, wallet?, worker?, devices?, extra?} -> validated, saved, miner restarted
  POST /restart  restart the miner
"""
import hmac
import http.server
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

VERSION = "1.2.0"
CONF = os.environ.get("AGENT_CONF", "/root/miner.json")
LOG = os.environ.get("AGENT_LOG", "/var/log/miner/miner.log")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
TOKEN = os.environ.get("TOKEN", "")
BIN = {"peakminer": os.environ.get("PEAKMINER_BIN", "/opt/miners/peakminer"),
       "srbminer": os.environ.get("SRBMINER_BIN", "/opt/miners/srbminer/SRBMiner-MULTI")}
API_PORT = {"peakminer": int(os.environ.get("PEAKMINER_API", "4068")),
            "srbminer": int(os.environ.get("SRBMINER_API", "21550"))}
ALGO = {("peakminer", "QTC"): "quantus", ("peakminer", "PRL"): "pearl", ("srbminer", "QTC"): "quantus"}
LOG_MAX, LOG_KEEP = 20 << 20, 2 << 20         # rotate the miner log past 20 MB, keep the last 2 MB
RE = {"pool": re.compile(r"^[A-Za-z0-9.-]{1,100}:\d{2,5}$"), "wallet": re.compile(r"^[A-Za-z0-9]{10,120}$"),
      "worker": re.compile(r"^[A-Za-z0-9_-]{1,32}$"), "devices": re.compile(r"^(\d{1,2}(,\d{1,2})*)?$"),
      "extra": re.compile(r"^[A-Za-z0-9 ._=,:/+-]{0,300}$")}
_LOCK = threading.Lock()
GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


# ── config ───────────────────────────────────────────────────────────────────

def validate(c: dict) -> dict:
    """The config as the miner will run it, or ValueError with what is wrong."""
    c = {k: c.get(k) for k in ("coin", "miner", "pools", "wallet", "worker", "devices", "extra", "rev")}
    c["coin"] = str(c.get("coin") or "").upper()
    c["miner"] = str(c.get("miner") or "").lower()
    if (c["miner"], c["coin"]) not in ALGO:
        raise ValueError(f"unsupported miner/coin {c['miner']}/{c['coin']} (have {sorted(ALGO)})")
    pools = c.get("pools")
    pools = [p for p in (re.split(r"[\s,]+", pools.strip()) if isinstance(pools, str) else pools or []) if p]
    if not 1 <= len(pools) <= 3 or not all(RE["pool"].match(p) for p in pools):
        raise ValueError("pools: 1-3 entries host:port")
    c["pools"] = pools
    for k in ("wallet", "worker"):
        if not RE[k].match(str(c.get(k) or "")):
            raise ValueError(f"{k}: invalid")
    for k in ("devices", "extra"):
        c[k] = str(c.get(k) or "").strip()
        if not RE[k].match(c[k]):
            raise ValueError(f"{k}: invalid")
    c["rev"] = int(c.get("rev") or 0)
    return c


def load_conf() -> dict:
    try:
        with open(CONF, encoding="utf-8") as f:
            return validate(json.load(f))
    except (OSError, ValueError):
        pass
    e = os.environ
    return validate({"coin": e.get("COIN"), "miner": e.get("MINER", "peakminer"), "pools": e.get("POOLS"),
                     "wallet": e.get("WALLET"), "worker": e.get("WORKER"), "devices": e.get("DEVICES", ""),
                     "extra": e.get("EXTRA", ""), "rev": 0})


def save_conf(c: dict) -> None:
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, CONF)


def build_cmd(c: dict) -> list:
    algo, b = ALGO[(c["miner"], c["coin"])], BIN[c["miner"]]
    if c["miner"] == "peakminer":       # = HiveOS's peakminer line (RIG8 10-07), API on localhost
        cmd = [b]
        for p in c["pools"]:
            cmd += ["-o", p]
        cmd += ["-u", c["wallet"], "-w", c["worker"], "-p", "x", "-c", algo, "-a", f"127.0.0.1:{API_PORT['peakminer']}"]
    else:                               # = HiveOS's SRBMiner line (RIG1 10-07): '!' separates failover pools
        cmd = [b, "--algorithm", algo, "--pool", "!".join(c["pools"]), "--wallet", f"{c['wallet']}.{c['worker']}",
               "--api-enable", "--api-port", str(API_PORT["srbminer"])]
    return cmd + shlex.split(c["extra"])


# ── the miner process ────────────────────────────────────────────────────────

class Supervisor(threading.Thread):
    """Starts the miner, restarts it when it exits (5 s backoff, doubling to 60 s while it keeps dying within 2 min of
    its start), rotates its log. restart() = stop the current run; the loop starts the (new) config."""

    def __init__(self, conf: dict, error: str = None):
        super().__init__(daemon=True)
        self.conf, self.proc, self.started, self.restarts, self.fails = conf, None, 0.0, 0, 0
        self.last_exit, self.last_cmd, self._kick, self.error = None, [], threading.Event(), error

    def run(self):
        while True:
            while self.conf is None:          # no valid config yet (bad env): wait for POST /config
                self._kick.wait(5)
                self._kick.clear()
            c = self.conf
            env = dict(os.environ)
            if c["devices"]:
                env["CUDA_VISIBLE_DEVICES"] = c["devices"]
            self.last_cmd = build_cmd(c)
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "ab") as log:
                log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start rev {c['rev']}: "
                          f"{' '.join(self.last_cmd)}\n".encode())
                log.flush()
                try:
                    self.proc = subprocess.Popen(self.last_cmd, stdout=log, stderr=subprocess.STDOUT,
                                                 stdin=subprocess.DEVNULL, cwd=os.path.dirname(self.last_cmd[0]) or None,
                                                 env=env, **GROUP)
                    self.started = time.time()
                    while self.proc.poll() is None:
                        if self._kick.wait(2):
                            break
                        rotate_log()
                except OSError as e:
                    log.write(f"=== cannot start: {e!r}\n".encode())
                    self.started = time.time()
            kicked = self._kick.is_set()
            self._stop_proc()
            self.last_exit = None if self.proc is None else self.proc.returncode
            self.restarts += 1
            if kicked:
                self._kick.clear()
                self.fails = 0
                continue
            self.fails = self.fails + 1 if time.time() - self.started < 120 else 1
            time.sleep(min(60, 5 * 2 ** (self.fails - 1)))

    def _stop_proc(self):
        """The miner AND anything it started (own process group): a leftover child would keep the GPUs / API port."""
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if os.name == "nt":                                   # local tests only
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGTERM)
            p.wait(10)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(p.pid, signal.SIGKILL)
            p.wait(5)
        except ProcessLookupError:
            pass

    def restart(self, conf: dict = None):
        if conf:
            self.conf, self.error = conf, None
        self._kick.set()

    def state(self) -> dict:
        p = self.proc
        return {"running": bool(p and p.poll() is None), "pid": p.pid if p else None, "started": self.started,
                "uptime_s": round(time.time() - self.started) if p and p.poll() is None else 0,
                "restarts": self.restarts, "last_exit": self.last_exit, "cmd": self.last_cmd}


def rotate_log():
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, "rb") as f:
                f.seek(-LOG_KEEP, 2)
                tail = f.read()
            with open(LOG, "r+b") as f:     # the miner writes O_APPEND: truncating in place is safe
                f.truncate(0)
                f.write(tail)
    except OSError:
        pass


# ── stats: one format for both miners ────────────────────────────────────────

def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def miner_stats(c: dict) -> dict:
    """{hash (H/s), accepted, rejected, pool, connected, ping_ms, uptime_s, version, gpus: [{id, bus, name, hash, temp,
    fan, power, accepted}]} from the running miner's own API; {"error": ...} when it doesn't answer."""
    try:
        if c["miner"] == "peakminer":
            s = _get(f"http://127.0.0.1:{API_PORT['peakminer']}/summary")
            pool = s.get("pool") or {}
            return {"hash": float(s.get("hashrate") or 0), "accepted": int(s.get("accepted_shares") or 0),
                    "rejected": int(s.get("invalid_shares") or 0), "pool": pool.get("url"),
                    "connected": pool.get("connected"), "ping_ms": pool.get("ping_ms"), "uptime_s": s.get("uptime"),
                    "version": s.get("version"), "last_share_at": s.get("last_share_at"),
                    "gpus": [{"id": g.get("id"), "bus": g.get("pci_bus_id"), "name": g.get("name"),
                              "hash": float(g.get("hashrate") or 0), "temp": g.get("temperature_c"),
                              "fan": g.get("fan_pct"), "power": g.get("power_w"), "accepted": g.get("accepted_shares")}
                             for g in s.get("gpus") or []]}
        s = _get(f"http://127.0.0.1:{API_PORT['srbminer']}/")
        a = (s.get("algorithms") or [{}])[0]
        hr, sh, pool = a.get("hashrate") or {}, a.get("shares") or {}, a.get("pool") or {}
        gh, acc = hr.get("gpu") or {}, a.get("gpu_accepted_shares") or {}
        gpus = []
        for d in s.get("gpu_devices") or []:
            k = d.get("device") or f"gpu{d.get('id')}"
            gpus.append({"id": d.get("id"), "bus": d.get("topology_id"), "name": d.get("model"),
                         "hash": float(gh.get(k) or 0), "temp": d.get("temperature"), "fan": d.get("fan_speed_percent"),
                         "power": d.get("asic_power"), "accepted": acc.get(k)})
        return {"hash": float(gh.get("total") or hr.get("1min") or 0), "accepted": int(sh.get("accepted") or 0),
                "rejected": int(sh.get("rejected") or 0), "pool": pool.get("pool"),
                "connected": bool(pool.get("pool")) and (pool.get("last_job_received") or 0) < 300,
                "ping_ms": pool.get("latency"), "uptime_s": s.get("mining_time"), "version": s.get("miner_version"),
                "last_share_at": None, "gpus": gpus}
    except Exception as e:
        return {"error": repr(e)[:200]}


_GPU = {"t": 0.0, "v": []}


def gpu_info() -> list:
    """nvidia-smi, cached 30 s: index, name, bus, driver, vbios, temp, fan, power, utilization."""
    if time.time() - _GPU["t"] < 30:
        return _GPU["v"]
    q = "index,name,pci.bus_id,driver_version,vbios_version,temperature.gpu,fan.speed,power.draw,utilization.gpu"
    out = []
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            v = [x.strip() for x in line.split(",")]
            if len(v) >= 9:
                out.append(dict(zip(("index", "name", "bus", "driver", "vbios", "temp", "fan", "power", "util"), v)))
    except Exception as e:
        out = [{"error": repr(e)[:120]}]
    _GPU.update(t=time.time(), v=out)
    return out


def tail(n: int) -> list:
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200 * max(n, 1)))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


# ── HTTP ─────────────────────────────────────────────────────────────────────

SUP: Supervisor = None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"clore-miner/{VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self) -> bool:
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not got:
            got = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if TOKEN and hmac.compare_digest(got.encode(), TOKEN.encode()):
            return True
        self._send(401, {"error": "token"})
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": SUP.conf is not None, "version": VERSION,
                                    "rev": (SUP.conf or {}).get("rev", 0), "error": SUP.error})
        if not self._authed():
            return
        if u.path == "/status":
            c = SUP.conf
            return self._send(200, {"version": VERSION, "t": time.time(), "conf": c, "error": SUP.error,
                                    "miner": SUP.state(), "stats": miner_stats(c) if c else {"error": SUP.error},
                                    "gpus": gpu_info()})
        if u.path == "/log":
            n = int((parse_qs(u.query).get("n") or ["200"])[0])
            return self._send(200, {"lines": tail(min(max(n, 1), 2000))})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if u.path == "/restart":
            SUP.restart()
            return self._send(200, {"ok": True, "rev": (SUP.conf or {}).get("rev", 0)})
        if u.path == "/config":
            with _LOCK:
                new = {**(SUP.conf or {}), **{k: v for k, v in body.items()
                                      if k in ("coin", "miner", "pools", "wallet", "worker", "devices", "extra")}}
                try:
                    new = validate(new)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                new["rev"] = (SUP.conf or {}).get("rev", 0) + 1
                save_conf(new)
                SUP.restart(new)
            return self._send(200, {"ok": True, "rev": new["rev"], "conf": new})
        self._send(404, {"error": "not found"})


def main():
    global SUP
    try:
        conf, err = load_conf(), None
        save_conf(conf)
    except ValueError as e:                   # bad env: stay up, report it, wait for POST /config
        conf, err = None, f"config: {e}"
    SUP = Supervisor(conf, err)
    SUP.start()
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
