# clore-miner: peakminer + SRBMiner + a small control agent (HTTP :8080) for GPU rentals on clore.ai -- no HiveOS.
FROM nvidia/cuda:12.8.1-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 openssh-server procps ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd /opt/miners/srbminer /var/log/miner \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/; s/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config

# miners: pinned versions, checked against the sha256 GitHub publishes for each release asset. Pedro 10-23: the HiveOS
# packages he runs (srbminer_custom 3.8.0, peakminer 2.20.0) -- the binaries go where the agent / Vast engine expect them
ARG PEAK_VER=2.20.0
ARG PEAK_SHA=8b13bcf5038a7e694d0e5d91b5c358c49d6e80dc4219624a069104f8b321084e
ARG SRB_VER=3.8.0
ARG SRB_SHA=1207a4e2ae0397948d7154c6e2edf7d1ca18211dce6ad3406799bbc95d664bb7
RUN curl -fsSL -o /tmp/peak.tgz "https://github.com/peakminer/peakminer/releases/download/v${PEAK_VER}/peakminer-${PEAK_VER}.tar.gz" \
 && echo "${PEAK_SHA}  /tmp/peak.tgz" | sha256sum -c - \
 && tar -xzf /tmp/peak.tgz -C /tmp \
 && install -m 755 /tmp/peakminer/peakminer /opt/miners/peakminer \
 && curl -fsSL -o /tmp/srb.tgz "https://github.com/doktor83/SRBMiner-Multi/releases/download/${SRB_VER}/srbminer_custom-${SRB_VER}.tar.gz" \
 && echo "${SRB_SHA}  /tmp/srb.tgz" | sha256sum -c - \
 && tar -xzf /tmp/srb.tgz -C /tmp \
 && install -m 755 /tmp/srbminer_custom/srbminer_custom_bin /opt/miners/srbminer/SRBMiner-MULTI \
 && rm -rf /tmp/peak.tgz /tmp/peakminer /tmp/srb.tgz /tmp/srbminer_custom \
 && test -x /opt/miners/peakminer && test -x /opt/miners/srbminer/SRBMiner-MULTI

COPY agent.py start.sh /opt/clore-miner/
RUN chmod +x /opt/clore-miner/start.sh /opt/clore-miner/agent.py

EXPOSE 22 8080
CMD ["/opt/clore-miner/start.sh"]
