#!/usr/bin/env python3
"""
KubeChaos - one tool that tests five container orchestrators the same way,
shows the results live, and recommends which one fits a user's need.

Tools tested (one at a time, so they never fight for memory):
  kind      upstream Kubernetes, 3 nodes in Docker
  k3s       K3s through k3d, 3 nodes in Docker
  microk8s  MicroK8s, 1 node in Ubuntu (WSL)
  swarm     Docker Swarm, built into Docker
  nomad     HashiCorp Nomad, dev agent with the Docker driver

For every tool the same web app (nginxdemos/hello:plain-text, 3 copies) is deployed,
then KubeChaos measures, under different conditions:
  1. Start-up time         how long until the orchestrator itself is ready
  2. Deploy time           how long until 3 copies of the app are running
  3. Memory and CPU        how much the orchestrator + app use when idle
  4. Normal traffic        ApacheBench 5,000 requests, 10 at a time  (speed + latency)
  5. Heavy traffic         ApacheBench 20,000 requests, 200 at a time (speed + latency + failures)
  6. Crash                 kill one copy: recovery time + requests that failed meanwhile

Usage (in Ubuntu / WSL, inside this folder):
  python3 kubechaos.py run --tools kind,k3s,microk8s,swarm,nomad
  python3 kubechaos.py dashboard          (second terminal, then open http://localhost:8765)
  python3 kubechaos.py recommend --need edge
  python3 kubechaos.py report
  python3 kubechaos.py advise             (6 questions -> which tool to use; see advisor.py)

Written by Dhiraj Khadka for CSCI5306 Course Project Milestone 3 (extends compare.ps1 from M2).
"""

import argparse, json, os, re, signal, statistics, subprocess, sys, time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
ALL_FILE = os.path.join(RESULTS, "all.json")
IMAGE = "nginxdemos/hello:plain-text"
AB_IMAGE = "httpd:alpine"
CURL_IMAGE = "curlimages/curl"
NAMES = {"kind": "Kubernetes", "k3s": "K3s", "microk8s": "MicroK8s",
         "swarm": "Docker Swarm", "nomad": "Nomad"}

# ---------------------------------------------------------------- helpers

def sh(cmd, check=False, timeout=900, quiet=True):
    """Run a shell command and return its output as text."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\n{out}")
    if not quiet:
        print(out)
    return out

def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)
    data = load_all()
    data.setdefault("log", []).append(f"{stamp}  {msg}")
    data["log"] = data["log"][-60:]
    save_all(data)

def load_all():
    try:
        with open(ALL_FILE) as f:
            return json.load(f)
    except Exception:
        return {"tools": {}, "log": [], "status": "idle"}

def save_all(data):
    os.makedirs(RESULTS, exist_ok=True)
    tmp = ALL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ALL_FILE)

def record(tool, key, value):
    """Save one measurement right away, so the dashboard shows it live."""
    data = load_all()
    data["tools"].setdefault(tool, {"name": NAMES[tool]})[key] = value
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    save_all(data)

def set_status(text):
    data = load_all(); data["status"] = text; save_all(data)

def mem_used_mb():
    """Memory used by the whole WSL2 VM (Docker Desktop shares the same VM)."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0])
    return (info["MemTotal"] - info["MemAvailable"]) / 1024

def _to_mb(text):
    """Turn '123.4MiB' or '1.2GiB' (docker stats) into megabytes."""
    m = re.match(r"([\d.]+)\s*([KMG]i?B|B)", text.strip())
    if not m: return 0.0
    v, u = float(m.group(1)), m.group(2)
    return v * {"B": 1/1048576, "KiB": 1/1024, "KB": 1/1000, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1000}[u]

def docker_mem_mb(pattern):
    """Memory of the running containers whose name matches the pattern."""
    out = sh("docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}'", timeout=120)
    total = 0.0
    for line in out.splitlines():
        if "|" in line:
            name, usage = line.split("|", 1)
            if re.search(pattern, name):
                total += _to_mb(usage.split("/")[0])
    return total

def proc_mem_mb(match):
    """Resident memory of Linux processes in this Ubuntu whose command line or cgroup matches."""
    total = 0.0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="ignore")
            with open(f"/proc/{pid}/cgroup") as f:
                cg = f.read()
            if match(cmd, cg):
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1]) / 1024
        except Exception:
            pass
    return total

def footprint_mb(key):
    """Memory used by one orchestrator: its nodes or agent plus the 3 app copies."""
    if key == "kind":
        return docker_mem_mb(r"^kc-k8s-")
    if key == "k3s":
        return docker_mem_mb(r"^k3d-kc-k3s-")
    if key == "microk8s":
        return proc_mem_mb(lambda cmd, cg: "/snap/microk8s" in cmd or "snap.microk8s" in cg or "kubepods" in cg)
    if key == "swarm":   # the Swarm manager lives inside the Docker engine, so only the copies can be measured
        return docker_mem_mb(r"^web-swarm\.")
    if key == "nomad":
        return (proc_mem_mb(lambda cmd, cg: cmd.startswith("nomad agent"))
                + docker_mem_mb(r"^web-[0-9a-f]{8}-|^kc-nomad-lb$"))
    return 0.0

def cpu_percent(seconds=10):
    """Average CPU use of the VM over a few idle seconds."""
    def snap():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        return sum(vals), vals[3] + vals[4]
    t1, i1 = snap(); time.sleep(seconds); t2, i2 = snap()
    return round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)

def parse_ab(out):
    """Pull speed, latency and failures out of ApacheBench output."""
    def grab(pattern):
        m = re.search(pattern, out, re.M)
        return float(m.group(1)) if m else None
    return {
        "rps": grab(r"Requests per second:\s+([\d.]+)"),
        "latency_ms": grab(r"Time per request:\s+([\d.]+) \[ms\] \(mean\)"),
        "p95_ms": grab(r"^\s*95%\s+(\d+)"),
        "failed": int(grab(r"Failed requests:\s+(\d+)") or 0)
                  + int(grab(r"Non-2xx responses:\s+(\d+)") or 0),
        "complete": int(grab(r"Complete requests:\s+(\d+)") or 0),
    }

def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None

def sd(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.stdev(xs), 2) if len(xs) > 1 else 0.0

def wait_until(fn, timeout=300, step=0.25):
    start = time.time()
    while time.time() - start < timeout:
        if fn():
            return round(time.time() - start, 2)
        time.sleep(step)
    return None

# ---------------------------------------------------------------- adapters
# Every tool gets the same set of actions, so the tests below never change.

WEB_YAML = f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: web}}
spec:
  replicas: 3
  selector: {{matchLabels: {{app: web}}}}
  template:
    metadata: {{labels: {{app: web}}}}
    spec:
      containers:
      - name: web
        image: {IMAGE}
        ports: [{{containerPort: 80}}]
---
apiVersion: v1
kind: Service
metadata: {{name: web}}
spec:
  type: NodePort
  selector: {{app: web}}
  ports: [{{port: 80, targetPort: 80, nodePort: 32000}}]
"""

NOMAD_JOB = f"""job "web" {{
  datacenters = ["*"]
  group "web" {{
    count = 3
    network {{
      port "http" {{ to = 80 }}
    }}
    service {{
      name     = "web"
      port     = "http"
      provider = "nomad"
    }}
    task "web" {{
      driver = "docker"
      config {{
        image = "{IMAGE}"
        ports = ["http"]
      }}
    }}
  }}
}}
"""

KIND_CONFIG = """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
"""

class KubeFamily:
    """Kubernetes, K3s and MicroK8s all speak kubectl, so they share one adapter."""
    has_lb = True

    def __init__(self, key):
        self.key = key
        if key == "kind":
            self.kc = "kubectl --context kind-kc-k8s"
        elif key == "k3s":
            self.kc = "kubectl --context k3d-kc-k3s"
        else:
            self.kc = "microk8s kubectl"

    def start(self):
        if self.key == "kind":
            path = os.path.join(RESULTS, "kind3.yaml")
            with open(path, "w") as f: f.write(KIND_CONFIG)
            sh("kind delete cluster --name kc-k8s")          # remove any half-built cluster
            sh(f"kind create cluster --name kc-k8s --config {path} --wait 300s", check=True)
        elif self.key == "k3s":
            sh("k3d cluster delete kc-k3s")                  # remove any half-built cluster
            sh("k3d cluster create kc-k3s --agents 2 --wait", check=True)
        else:
            sh("sudo -n microk8s start", check=True)
            sh("sudo -n microk8s status --wait-ready", check=True)
            sh("sudo -n microk8s enable dns", check=False)
        sysp = f"{self.kc} -n kube-system get pods --no-headers --field-selector=status.phase!=Succeeded"
        wait_until(lambda: "Running" in sh(sysp) and " 0/" not in sh(sysp), 300, 2)

    def stop(self):
        sh(f"{self.kc} delete deployment web --ignore-not-found")
        sh(f"{self.kc} delete service web --ignore-not-found")
        if self.key == "kind":
            sh("kind delete cluster --name kc-k8s")
        elif self.key == "k3s":
            sh("k3d cluster delete kc-k3s")
        else:
            sh("sudo -n microk8s stop")

    def deploy(self):
        path = os.path.join(RESULTS, "web.yaml")
        with open(path, "w") as f: f.write(WEB_YAML)
        # a brand-new cluster can be busy for a few seconds (etcd timeout), so try up to 6 times
        for attempt in range(6):
            out = sh(f"{self.kc} apply -f {path}")
            if "deployment.apps/web" in out and "rror" not in out:
                break
            time.sleep(10)
        else:
            raise RuntimeError(f"kubectl apply failed: {out}")
        return wait_until(lambda: len(self.copies()) >= 3, 600)

    def undeploy(self):
        sh(f"{self.kc} delete deployment web --wait=true")
        wait_until(lambda: len(self.copies()) == 0, 120)

    def copies(self):
        out = sh(f"{self.kc} get pods -l app=web -o jsonpath='{{range .items[*]}}"
                 f"{{.metadata.name}} {{.status.containerStatuses[0].ready}} "
                 f"{{.metadata.deletionTimestamp}}{{\"\\n\"}}{{end}}'")
        ready = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "true":   # ready and not being deleted
                ready.append(parts[0])
        return ready

    def kill(self, name):
        sh(f"{self.kc} delete pod {name} --wait=false")

    def ab(self, n, c, tag):
        sh(f"{self.kc} delete pod ab-{tag} --ignore-not-found")
        return sh(f"{self.kc} run ab-{tag} --rm -i --restart=Never --image={AB_IMAGE} "
                  f"-- ab -l -r -s 10 -n {n} -c {c} http://web/", timeout=900)

    def probe_start(self, count):
        """A tiny client pod that asks for the page every 0.1 s and prints each HTTP code."""
        sh(f"{self.kc} delete pod probe --ignore-not-found --wait=true")
        loop = (f"for i in \\$(seq 1 {count}); do curl -s -o /dev/null -m 1 "
                f"-w '%{{http_code}}\\n' http://web/; sleep 0.1; done")
        self.probe = subprocess.Popen(
            f"{self.kc} run probe --rm -i --restart=Never --image={CURL_IMAGE} -- sh -c \"{loop}\"",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ok = wait_until(lambda: "Running" in sh(
            f"{self.kc} get pod probe -o jsonpath='{{.status.phase}}'"), 180, 0.5)
        if ok is None:
            log("probe pod did not start: " + sh(f"{self.kc} get pod probe")[-200:])

    def probe_result(self):
        try:
            out, _ = self.probe.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            self.probe.kill(); out = ""
        sh(f"{self.kc} delete pod probe --ignore-not-found")
        return out

class Swarm:
    key, has_lb = "swarm", True

    def start(self):
        out = sh("docker swarm init")
        if "already part of a swarm" not in out and "Swarm initialized" not in out:
            raise RuntimeError(out)

    def stop(self):
        sh("docker service rm web-swarm")
        sh("docker swarm leave --force")

    def deploy(self):
        sh(f"docker service create --detach --name web-swarm --replicas 3 -p 8080:80 {IMAGE}", check=True)
        return wait_until(lambda: len(self.copies()) >= 3, 600)

    def undeploy(self):
        sh("docker service rm web-swarm")
        wait_until(lambda: len(self.copies()) == 0, 120)

    def copies(self):
        return sh("docker ps -q --filter name=web-swarm --filter status=running").split()

    def kill(self, cid):
        sh(f"docker rm -f {cid}")

    def ab(self, n, c, tag):
        return sh(f"docker run --rm --network host {AB_IMAGE} ab -l -r -s 10 -n {n} -c {c} "
                  f"http://127.0.0.1:8080/", timeout=900)

    def probe_start(self, count):
        sh("docker rm -f kc-probe")
        loop = (f"for i in \\$(seq 1 {count}); do curl -s -o /dev/null -m 1 "
                f"-w '%{{http_code}}\\n' http://127.0.0.1:8080/; sleep 0.1; done")
        sh(f"docker run -d --name kc-probe --network host {CURL_IMAGE} sh -c \"{loop}\"")

    def probe_result(self):
        sh("docker wait kc-probe", timeout=300)
        out = sh("docker logs kc-probe")
        sh("docker rm -f kc-probe")
        return out

class Nomad:
    """Nomad has no built-in load balancer. Real Nomad users put NGINX in front of the copies,
    so KubeChaos does the same: an NGINX container on port 8090 that spreads traffic over the
    3 copies. If that fails, it falls back to sending traffic to one copy directly."""
    key, has_lb = "nomad", True
    agent = None
    url = None

    def start(self):
        logf = open(os.path.join(RESULTS, "nomad-agent.log"), "w")
        Nomad.agent = subprocess.Popen(["nomad", "agent", "-dev"], stdout=logf, stderr=logf,
                                       preexec_fn=os.setsid)
        wait_until(lambda: " ready" in sh("nomad node status"), 180, 1)
        wait_until(lambda: "Healthy" in sh("nomad node status -self -verbose"), 60, 1)

    def stop(self):
        sh("docker rm -f kc-nomad-lb")
        sh("nomad job stop -purge web")
        wait_until(lambda: len(self.copies()) == 0, 60)
        if Nomad.agent:
            os.killpg(os.getpgid(Nomad.agent.pid), signal.SIGINT)
            Nomad.agent.wait(timeout=60)

    def deploy(self):
        path = os.path.join(RESULTS, "web.nomad.hcl")
        with open(path, "w") as f: f.write(NOMAD_JOB)
        sh(f"nomad job run -detach {path}", check=True)
        t = wait_until(lambda: len(self.copies()) >= 3, 600)
        self.setup_lb()
        return t

    def setup_lb(self):
        """Point an NGINX load balancer at the host ports Nomad gave the 3 copies."""
        sh("docker rm -f kc-nomad-lb")
        out = sh("docker ps --filter status=running --format '{{.Names}} {{.Ports}}'")
        ports = re.findall(r"(?:[\d.]+):(\d+)->80/tcp", " ".join(
            l for l in out.splitlines() if re.match(r"web-[0-9a-f]{8}-", l)))
        ports = sorted(set(ports))
        if len(ports) >= 3:
            conf = "upstream web { %s }\nserver { listen 8090; location / { proxy_pass http://web; } }\n" % \
                   " ".join(f"server 127.0.0.1:{p} max_fails=1 fail_timeout=5s;" for p in ports)
            cpath = os.path.join(RESULTS, "nomad-lb.conf")
            with open(cpath, "w") as f: f.write(conf)
            sh(f"docker run -d --name kc-nomad-lb --network host "
               f"-v {cpath}:/etc/nginx/conf.d/default.conf:ro nginx:alpine")
            time.sleep(3)
            test = sh(f"docker run --rm --network host {CURL_IMAGE} -s -m 3 http://127.0.0.1:8090/")
            if "Server name" in test:
                Nomad.url, Nomad.has_lb = "http://127.0.0.1:8090/", True
                log("Nomad: NGINX load balancer in front of the 3 copies on port 8090")
                return
        Nomad.url, Nomad.has_lb = None, False
        log("Nomad: could not set up NGINX, testing one copy directly (no load balancer)")

    def undeploy(self):
        sh("docker rm -f kc-nomad-lb")
        sh("nomad job stop -purge web")
        wait_until(lambda: len(self.copies()) == 0, 120)

    def copies(self):
        out = sh("docker ps --filter status=running --format '{{.ID}} {{.Names}}'")
        return [l.split()[0] for l in out.splitlines()
                if re.search(r" web-[0-9a-f]{8}-", l)]

    def kill(self, cid):
        sh(f"docker rm -f {cid}")

    def ab(self, n, c, tag):
        if Nomad.url:
            return sh(f"docker run --rm --network host {AB_IMAGE} ab -l -r -s 10 -n {n} -c {c} "
                      f"{Nomad.url}", timeout=900)
        target = self.copies()[0]
        return sh(f"docker run --rm --network container:{target} {AB_IMAGE} ab -l -r -s 10 "
                  f"-n {n} -c {c} http://127.0.0.1:80/", timeout=900)

    def probe_start(self, count):
        if not Nomad.url: return
        sh("docker rm -f kc-probe")
        loop = (f"for i in \\$(seq 1 {count}); do curl -s -o /dev/null -m 1 "
                f"-w '%{{http_code}}\\n' {Nomad.url}; sleep 0.1; done")
        sh(f"docker run -d --name kc-probe --network host {CURL_IMAGE} sh -c \"{loop}\"")

    def probe_result(self):
        if not Nomad.url: return None
        sh("docker wait kc-probe", timeout=300)
        out = sh("docker logs kc-probe")
        sh("docker rm -f kc-probe")
        return out

def adapter(key):
    if key in ("kind", "k3s", "microk8s"):
        return KubeFamily(key)
    return {"swarm": Swarm, "nomad": Nomad}[key]()

# ---------------------------------------------------------------- the test plan

def measure_idle(key, name):
    """Memory of the tool itself (median of 3 readings, 10 s apart) and idle CPU of the machine."""
    time.sleep(30)
    vals = []
    for _ in range(3):
        vals.append(footprint_mb(key)); time.sleep(10)
    mem = round(statistics.median(vals))
    cpu = cpu_percent(10)
    record(key, "memory_mb", mem); record(key, "idle_cpu_pct", cpu)
    log(f"{name}: uses about {mem} MB of memory, idle CPU {cpu}%")

def test_tool(key, runs, memory_only=False):
    a = adapter(key)
    name = NAMES[key]
    log(f"===== {name} =====")
    set_status(f"Testing {name}")

    # 1. Start-up time and memory baseline
    #    A warm-up start/stop first, so downloading the tool's own images is not counted.
    log(f"{name}: warm-up start (downloads the tool's images, not timed)")
    a.start(); a.stop()
    time.sleep(10)
    log(f"{name}: starting the orchestrator")
    t0 = time.time(); a.start(); startup = round(time.time() - t0, 1)
    record(key, "startup_s", startup)
    log(f"{name}: ready after {startup} s")

    # 2. Deploy time (first deploy pulls the image, so we time the second one)
    log(f"{name}: warm-up deploy (downloads the app image)")
    a.deploy(); a.undeploy()
    deploy = a.deploy()
    record(key, "deploy_s", deploy)
    log(f"{name}: 3 copies running after {deploy} s")

    # 3. Memory and CPU when idle
    measure_idle(key, name)
    if memory_only:
        a.stop(); log(f"{name}: memory measured, stopped"); return

    # 4 + 5. Normal and heavy traffic (warm-up run first, not counted)
    a.ab(1000, 10, "warm")
    for label, n, c in (("normal", 5000, 10), ("heavy", 20000, 200)):
        rs = []
        for i in range(1, runs + 1):
            r = parse_ab(a.ab(n, c, f"{label}{i}"))
            rs.append(r)
            log(f"{name}: {label} traffic run {i}: {r['rps']} req/s, "
                f"mean {r['latency_ms']} ms, p95 {r['p95_ms']} ms, failed {r['failed']}")
        record(key, f"{label}_rps", mean([r["rps"] for r in rs]))
        record(key, f"{label}_rps_sd", sd([r["rps"] for r in rs]))
        record(key, f"{label}_latency_ms", mean([r["latency_ms"] for r in rs]))
        record(key, f"{label}_p95_ms", mean([r["p95_ms"] for r in rs]))
        record(key, f"{label}_failed", sum(r["failed"] for r in rs))

    # 6. Crash: kill one copy while a client keeps asking for the page
    recs, fails, sent = [], 0, 0
    for i in range(1, runs + 1):
        before = a.copies()
        a.probe_start(150)                     # 150 requests, one every 0.1 s (about 15 s)
        time.sleep(2)
        a.kill(before[0])
        rec = wait_until(lambda: any(c not in before for c in a.copies())
                         and len(a.copies()) >= 3, 180)
        out = a.probe_result()
        recs.append(rec)
        if out is not None:
            codes = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
            fails += sum(1 for c in codes if c != "200"); sent += len(codes)
        log(f"{name}: crash {i}: back to 3 copies after {rec} s")
        time.sleep(5)
    record(key, "recovery_s", mean(recs)); record(key, "recovery_sd", sd(recs))
    record(key, "crash_failed_pct", round(100 * fails / sent, 2) if sent else None)
    if sent:
        log(f"{name}: {fails} of {sent} requests failed during the crashes")
    elif key == "nomad" and not Nomad.url:
        log(f"{name}: no load balancer, so clients of the killed copy wait until it restarts")
    else:
        log(f"{name}: the crash client sent no requests (see log above)")
    record(key, "load_balancer", type(a).has_lb if key == "nomad" else a.has_lb)

    # clean up so the next tool starts on an empty machine
    log(f"{name}: stopping and cleaning up")
    a.stop()
    record(key, "done", True)

# ---------------------------------------------------------------- scoring + recommendation

# (metric, higher_is_better)
METRICS = {
    "startup_s": False, "deploy_s": False, "memory_mb": False, "idle_cpu_pct": False,
    "normal_rps": True, "normal_latency_ms": False, "heavy_rps": True, "heavy_p95_ms": False,
    "heavy_failed": False, "recovery_s": False, "crash_failed_pct": False,
}

NEEDS = {
    "edge":        ("Small edge device (low memory and CPU)",
                    {"memory_mb": .40, "idle_cpu_pct": .20, "startup_s": .20, "recovery_s": .20}),
    "traffic":     ("High traffic web app (speed and latency)",
                    {"heavy_rps": .35, "heavy_p95_ms": .30, "heavy_failed": .15, "normal_rps": .20}),
    "reliability": ("Always-on service (survives crashes) = Survival Score",
                    {"recovery_s": .40, "crash_failed_pct": .40, "heavy_failed": .20}),
    "setup":       ("Beginner or quick setup",
                    {"startup_s": .40, "deploy_s": .40, "memory_mb": .20}),
    "balanced":    ("Balanced, a bit of everything", {m: 1 / len(METRICS) for m in METRICS}),
}

# Lower-is-better metrics use a "softened" ratio: k is a small amount nobody would notice
# (10 s of start-up, 100 MB of memory ...), so 0.7 s vs 4.3 s does not look like a 6x gap.
SOFTEN = {"startup_s": 10, "deploy_s": 2, "memory_mb": 100, "idle_cpu_pct": 0.5, "normal_latency_ms": 1, "heavy_p95_ms": 10, "heavy_failed": 10, "recovery_s": 2, "crash_failed_pct": 0.5}

def metric_score(tools, metric, value):
    """0-100: the best tool gets 100, others get a share of it."""
    vals = [t.get(metric) for t in tools.values() if t.get(metric) is not None]
    if value is None or not vals:
        return None
    if METRICS[metric]:
        best = max(vals); return 100.0 if best <= 0 else 100 * value / best
    k = SOFTEN.get(metric, 0); best = min(vals)
    return 100.0 if value + k <= 0 else 100 * (best + k) / (value + k)

def scores_for(need, tools):
    label, weights = NEEDS[need]
    out = {}
    for key, t in tools.items():
        total, wsum = 0.0, 0.0
        for m, w in weights.items():
            s = metric_score(tools, m, t.get(m))
            if s is None and m == "crash_failed_pct" and t.get("load_balancer") is False:
                s = 0.0          # no load balancer: clients of the killed copy get nothing
            if s is not None:
                total += w * s; wsum += w
        out[key] = round(total / wsum, 1) if wsum else None
    return label, out

def recommend(need):
    data = load_all()
    tools = {k: v for k, v in data["tools"].items() if v.get("done")}
    if not tools:
        print("No finished results yet. Run:  python3 kubechaos.py run --tools ...")
        return
    label, sc = scores_for(need, tools)
    ranked = sorted(sc.items(), key=lambda kv: -(kv[1] or 0))
    print(f"\nYour need: {label}\n")
    for i, (k, s) in enumerate(ranked, 1):
        print(f"  {i}. {NAMES[k]:<13} score {s}")
    best = ranked[0][0]; t = tools[best]
    print(f"\nRecommendation: use {NAMES[best]}.")
    why = {
        "edge": f"It added only {t.get('memory_mb')} MB of memory and started in {t.get('startup_s')} s.",
        "traffic": f"It served {t.get('heavy_rps')} requests per second under heavy traffic, p95 latency {t.get('heavy_p95_ms')} ms.",
        "reliability": f"It recovered from a crash in {t.get('recovery_s')} s and lost {t.get('crash_failed_pct')}% of requests.",
        "setup": f"It started in {t.get('startup_s')} s and deployed the app in {t.get('deploy_s')} s.",
        "balanced": "It had the best overall mix across all measurements.",
    }[need]
    print(f"Why: {why}\n")
    data.setdefault("recommendations", {})[need] = {"label": label, "scores": sc, "best": best, "why": why}
    save_all(data)

def report():
    data = load_all()
    tools = data["tools"]
    cols = ["startup_s", "deploy_s", "memory_mb", "idle_cpu_pct", "normal_rps", "normal_latency_ms",
            "heavy_rps", "heavy_p95_ms", "heavy_failed", "recovery_s", "crash_failed_pct"]
    print("tool," + ",".join(cols))
    lines = ["tool," + ",".join(cols)]
    for k, t in tools.items():
        row = NAMES[k] + "," + ",".join(str(t.get(c, "")) for c in cols)
        print(row); lines.append(row)
    for need in NEEDS:
        label, sc = scores_for(need, {k: v for k, v in tools.items() if v.get("done")})
        print(f"\n{label}: " + ", ".join(f"{NAMES[k]} {s}" for k, s in sc.items()))
    with open(os.path.join(RESULTS, "results.csv"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\nSaved results/results.csv")

# ---------------------------------------------------------------- dashboard

def dashboard(port):
    data = load_all(); save_all(data)
    handler = partial(SimpleHTTPRequestHandler, directory=HERE)
    print(f"Live dashboard: open  http://localhost:{port}/dashboard.html  (Ctrl+C to stop)")
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()

# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="KubeChaos: test, compare and recommend orchestrators")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--tools", default="kind,k3s,microk8s,swarm,nomad")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--fresh", action="store_true", help="forget old results first")
    r.add_argument("--memory-only", action="store_true", help="only re-measure start-up, deploy and memory")
    d = sub.add_parser("dashboard"); d.add_argument("--port", type=int, default=8765)
    c = sub.add_parser("recommend"); c.add_argument("--need", choices=list(NEEDS), default="balanced")
    sub.add_parser("report")
    sub.add_parser("advise", help="ask 6 questions and recommend a tool (same as advisor.py)", add_help=False)
    if len(sys.argv) > 1 and sys.argv[1] == "advise":
        import advisor
        extra = sys.argv[2:]
        if os.path.exists(ALL_FILE) and "--results" not in extra:
            extra += ["--results", ALL_FILE]
        return advisor.main(extra)
    args = p.parse_args()
    os.makedirs(RESULTS, exist_ok=True)

    if args.cmd == "run":
        if args.fresh: save_all({"tools": {}, "log": [], "status": "idle"})
        for key in args.tools.split(","):
            key = key.strip()
            try:
                test_tool(key, args.runs, args.memory_only)
            except Exception as e:
                log(f"{NAMES.get(key, key)}: ERROR {str(e)[:300]}")
                try: adapter(key).stop()
                except Exception: pass
        set_status("Finished")
        log("All tests finished. Try:  python3 kubechaos.py recommend --need edge")
    elif args.cmd == "dashboard":
        dashboard(args.port)
    elif args.cmd == "recommend":
        recommend(args.need)
    else:
        report()

if __name__ == "__main__":
    main()
