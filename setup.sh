#!/usr/bin/env bash
# KubeChaos setup for Ubuntu on WSL2 (Docker Desktop with WSL integration turned on).
# Installs the command-line tools KubeChaos needs. Safe to run more than once.
set -e
command -v kubectl >/dev/null || sudo snap install kubectl --classic
command -v kind    >/dev/null || { curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64; chmod +x ./kind; sudo mv ./kind /usr/local/bin/kind; }
command -v k3d     >/dev/null || curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
command -v microk8s >/dev/null || sudo snap install microk8s --classic
sudo usermod -a -G microk8s "$USER" || true
mkdir -p ~/.kube
sudo microk8s stop || true          # KubeChaos starts each tool itself, one at a time
for img in nginxdemos/hello:plain-text httpd:alpine curlimages/curl nginx:alpine; do docker pull -q "$img"; done
echo "Setup done. Close Ubuntu and open it again (so the microk8s group works), then run:"
echo "  python3 kubechaos.py run --tools kind,k3s,microk8s,swarm,nomad --fresh"
