#!/bin/bash
# Prepare a Linux x86_64 host with KVM to run the official OSWorld VM
# (docs/superpowers/plans/2026-09-24-osworld-official-fidelity.md, Task 9).
set -euo pipefail
DIR="${OSW_KVM_DIR:-/opt/osworld}"
URL="https://huggingface.co/datasets/xlangai/ubuntu_osworld/resolve/main/Ubuntu.qcow2.zip"
[ "$(uname -m)" = "x86_64" ] || { echo "need x86_64"; exit 1; }
[ -e /dev/kvm ] || { echo "/dev/kvm missing: enable virtualization / load kvm module"; exit 1; }
docker info >/dev/null || { echo "docker not usable by $(whoami)"; exit 1; }
docker pull happysixd/osworld-docker
mkdir -p "$DIR"
if [ ! -s "$DIR/Ubuntu.qcow2" ]; then
  curl -L --fail -o "$DIR/Ubuntu.qcow2.zip" "$URL"
  (cd "$DIR" && unzip -o Ubuntu.qcow2.zip && rm -f Ubuntu.qcow2.zip)
fi
SHA=$(sha256sum "$DIR/Ubuntu.qcow2" | cut -d' ' -f1)
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' happysixd/osworld-docker)
CPUS=$(nproc); MEM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1048576 ))
echo "Parallel VMs this host can hold (4 CPU / 4 GB each): $(( CPUS/4 < MEM_GB/5 ? CPUS/4 : MEM_GB/5 ))"
echo "export OSW_KVM_QCOW2=$DIR/Ubuntu.qcow2"
echo "export OSW_KVM_QCOW2_SHA256=$SHA"
echo "export OSW_KVM_IMAGE=$DIGEST"
