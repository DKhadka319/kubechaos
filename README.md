# KubeChaos

**Test, compare and choose a container orchestrator.**

**Try the advisor online:** https://dkhadka319.github.io/kubechaos/
KubeChaos runs the same web app on Kubernetes, K3s, MicroK8s, Docker Swarm and Nomad,
measures them under normal load, heavy load and crashes, and then recommends which one fits your need.

Three parts:

| Part | File | What it does |
|---|---|---|
| Benchmark | `kubechaos.py run` | Start-up, deploy, memory, speed, latency, crash recovery for each tool |
| Live dashboard | `kubechaos.py dashboard` | Charts that update while the tests run (http://localhost:8765/dashboard.html) |
| Advisor | `advisor.py` and `site/index.html` | Asks 6 questions about your project and recommends a tool, with reasons |

## Just want advice? (no install)

Open the website (https://dkhadka319.github.io/kubechaos/) and answer the questions.

Or get the code:

```bash
git clone https://github.com/DKhadka319/kubechaos
cd kubechaos
```

Or in a terminal (Python 3 only, no packages):

```
python3 advisor.py
python3 advisor.py --use edge --ram small --care memory,recovery
python3 advisor.py --list        # all question and answer ids
```

On Windows type `py` instead of `python3` (for example `py advisor.py`). If Windows says "Python was not found", install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".

## Benchmark your own machine

Needs Ubuntu (or WSL2) with Docker. `setup.sh` installs kubectl, kind, k3d and MicroK8s; Nomad is installed from HashiCorp.

```
bash setup.sh
sudo -v
python3 kubechaos.py run --tools microk8s,kind,k3s,swarm,nomad
python3 kubechaos.py dashboard          # in a second terminal
python3 kubechaos.py report             # table + results/results.csv
python3 kubechaos.py advise             # advice from YOUR numbers
python3 build_site.py results/all.json  # rebuild the website with your numbers
```

## Publish the website on GitHub Pages

The `docs/` folder holds a copy of the website. In the repository go to Settings, then Pages, choose "Deploy from a branch", branch `main`, folder `/docs`, and press Save.

## Cite

```
@software{khadka2026kubechaos,
  author = {Khadka, Dhiraj},
  title  = {KubeChaos: Testing, Comparing and Recommending Container Orchestrators under Failure and Load},
  year   = {2026},
  institution = {Texas A\&M University-San Antonio}
}
```

Written for CSCI5306 (Computer Networks) Course Project Milestone 3, Texas A&M University-San Antonio.
