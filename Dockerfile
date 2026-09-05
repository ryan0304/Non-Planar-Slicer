# Production deployment image (Render, or any Docker-capable host). Adding
# this is what turns "Hybrid planar base ... not available on a hosted
# deployment" (serve.py) from a real limitation into a stale error message:
# this image bundles a real OrcaSlicer CLI, matching the same version
# trident_gcode/orca_slice.py's CAUTION notes say was live-verified during
# development (2.4.2 -- see the hybrid-orca-planar-base project memory for
# the 5 gotchas that verification found).
#
# Feasibility was checked BEFORE this file existed, not assumed: see
# tools/orca_render_feasibility/ -- a throwaway probe (kept for whenever
# Orca or the caps get upgraded and this needs re-checking) that ran three
# real scenarios (a small parametric hybrid, a star with 3 overlapping Zone
# Overrides, and a real user STL) through the exact code path below, inside
# a container capped at Render's free-tier 512 MB RAM / 0.1 vCPU. All three
# passed; the star+zones case took 65s, almost all of it pure-Python wall
# math rather than the Orca subprocess itself -- that finding is why
# serve.py has a TRIDENT_MAX_HYBRID_WALL_POINTS gate, set below and in
# render.yaml, rather than shipping this with no ceiling at all.
#
# Base image pinned to Ubuntu 24.04 because the OrcaSlicer 2.4.2 Linux
# AppImage release asset is literally named
# "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage" -- matching the
# build's own target glibc/library baseline is the single biggest lever
# against "works in the feasibility probe, segfaults in production".
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Same dependency set the feasibility probe validated a real Orca --slice
# invocation against (see tools/orca_render_feasibility/Dockerfile for the
# same caveat: best-effort for this app family, not individually confirmed
# per-library -- but this exact list is now a working combination, not a
# guess, because run_feasibility_test.py actually completed real slices
# with it).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        libgtk-3-0 libglu1-mesa libgl1 \
        libwebkit2gtk-4.1-0 \
        libsecret-1-0 libssl3 libcurl4 \
        libudev1 libdbus-1-3 libnss3 \
        xvfb \
        python3 \
    && rm -rf /var/lib/apt/lists/*

ARG ORCA_TAG=v2.4.2
ARG ORCA_ASSET=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage

RUN curl -fL -o /opt/orca.AppImage \
        "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/${ORCA_TAG}/${ORCA_ASSET}" \
    && chmod +x /opt/orca.AppImage \
    # No /dev/fuse on Render's (or most PaaS) containers, so extract instead
    # of relying on FUSE-mounting the AppImage at runtime.
    && cd /opt && ./orca.AppImage --appimage-extract \
    && rm /opt/orca.AppImage \
    && mv /opt/squashfs-root /opt/orcaslicer

# The feasibility probe's "direct (no xvfb)" candidate was the one that
# actually passed every scenario -- Orca's --slice path needs no display --
# so that is what production points at. The xvfb-wrapped invocation is left
# installed (tiny cost) as a rescue path an operator can switch to by
# changing ONLY this env var, with no rebuild, if a future Orca version
# regresses that finding.
RUN printf '#!/bin/sh\nexec xvfb-run -a --server-args="-screen 0 1024x768x24" /opt/orcaslicer/AppRun "$@"\n' \
        > /opt/orca-xvfb-wrapper.sh \
    && chmod +x /opt/orca-xvfb-wrapper.sh
ENV TRIDENT_ORCA_PATH=/opt/orcaslicer/AppRun

# wx/GTK apps commonly fail ungracefully if $HOME isn't writable.
ENV HOME=/tmp/orcahome
RUN mkdir -p "$HOME" && chmod 777 "$HOME"

WORKDIR /app
COPY . /app

# Render (and most PaaS) inject PORT at runtime; serve.py's _bind_config
# already reads it, same as the native-runtime deployment did. TRIDENT_BIND
# still must come from render.yaml's envVars -- this Dockerfile does not
# set it, so an image run with no env (e.g. a stray local `docker run`)
# keeps the safe loopback-only default rather than exposing itself by
# merely existing.
CMD ["python3", "serve.py"]
