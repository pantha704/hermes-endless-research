# =============================================================================
# hermes-endless-research — Docker image
#
# Layers a small bootstrap that copies the endless-research skill into the
# official `hermes-agent` image's skills tree. We do NOT fork Hermes — we only
# add our skill files and a tiny init hook.
#
# Base image contract (from the official image):
#   - ENTRYPOINT is ["/init", ...]  (s6-overlay PID 1). If you override it you
#     MUST keep /init as the FIRST command in the chain.
#   - Persistent Hermes data lives at /opt/data (== the host ~/.hermes mount).
#   - The container drops to the `hermes` user; set HERMES_UID/HERMES_GID to
#     your host uid/gid so files stay readable on the host.
#
# Build (from this repo):
#   docker build -t hermes-endless-research . --build-arg BASE_IMAGE=hermes-agent
# =============================================================================
ARG BASE_IMAGE=hermes-agent
FROM ${BASE_IMAGE}

# The official image's Hermes home.
ENV HERMES_HOME=/opt/data

# Stash our skill into the image. Runtime installation (copying it into the
# mounted /opt/data/skills tree) is handled by the container-init hook in
# docker/init-endless-research.sh so it lands in the persistent volume.
COPY skill /usr/local/share/hermes-endless-research/skill
COPY docker/init-endless-research.sh /etc/cont-init.d/99-endless-research
RUN chmod +x /etc/cont-init.d/99-endless-research
