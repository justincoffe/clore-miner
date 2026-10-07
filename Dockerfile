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

# miners: pinned versions, checked against the sha256 GitHub publishes for each release asset
ARG PEAK_VER=2.18.0
ARG PEAK_SHA=b54167043cf08ef6c0c9546eb037dc94f802264d02920ec4b9f6d7dbae20a717
ARG SRB_VER=3.7.2
ARG SRB_DASH=3-7-2
ARG SRB_SHA=69da8fc2a5a7a9cd38d6f19935130e35fdd36bea465adaf0bb57e9a066aca6de
RUN curl -fsSL -o /opt/miners/peakminer \
      "https://github.com/peakminer/peakminer/releases/download/v${PEAK_VER}/peakminer-${PEAK_VER}-linux-x86_64" \
 && echo "${PEAK_SHA}  /opt/miners/peakminer" | sha256sum -c - \
 && chmod +x /opt/miners/peakminer \
 && curl -fsSL -o /tmp/srb.tgz \
      "https://github.com/doktor83/SRBMiner-Multi/releases/download/${SRB_VER}/SRBMiner-Multi-${SRB_DASH}-Linux.tar.gz" \
 && echo "${SRB_SHA}  /tmp/srb.tgz" | sha256sum -c - \
 && tar -xzf /tmp/srb.tgz -C /opt/miners/srbminer --strip-components=1 \
 && rm /tmp/srb.tgz \
 && chmod +x /opt/miners/srbminer/SRBMiner-MULTI

COPY agent.py start.sh /opt/clore-miner/
RUN chmod +x /opt/clore-miner/start.sh /opt/clore-miner/agent.py

EXPOSE 22 8080
CMD ["/opt/clore-miner/start.sh"]
