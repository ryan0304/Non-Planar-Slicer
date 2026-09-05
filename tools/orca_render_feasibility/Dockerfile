# Feasibility probe only -- NOT the production Render deployment.
#
# Question this answers: can the real OrcaSlicer CLI subprocess (the one
# trident_gcode/orca_slice.py shells out to) actually complete a hybrid
# planar-base slice inside Render's free-tier resource envelope (512 MB RAM,
# 0.1 vCPU, as of the April 2026 pricing revamp)? Nothing here should be
# copied into render.yaml until run_feasibility_test.py has passed under
# those exact constraints -- see run_feasibility.ps1.
#
# Base image pinned to Ubuntu 24.04 because the OrcaSlicer 2.4.2 Linux
# AppImage release asset is literally named
# "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage" -- matching the
# build's own target glibc/library baseline is the single biggest lever
# against "works locally, segfaults in the container".
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# BEST-EFFORT / UNVERIFIED dependency list. OrcaSlicer is a PrusaSlicer /
# BambuStudio fork built on wxWidgets+GTK3, so this mirrors what that family
# of AppImages typically links against -- it has not been confirmed against
# this exact 2.4.2 build. If the extracted binary fails to start, run
# `ldd /opt/squashfs-root/usr/bin/orca-slicer 2>&1 | grep "not found"`
# inside the container and add whatever's missing here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        libgtk-3-0 libglu1-mesa libgl1 \
        libwebkit2gtk-4.1-0 \
        libsecret-1-0 libssl3 libcurl4 \
        libudev1 libdbus-1-3 libnss3 \
        xvfb \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the exact version trident_gcode/orca_slice.py's docstring says
# was live-verified during development (see CAUTION notes in that file and
# the hybrid-orca-planar-base project memory). Do not float this to
# "latest" -- an untested Orca version can silently change CLI/JSON
# behavior the same way the memory documents 5 prior gotchas.
ARG ORCA_TAG=v2.4.2
ARG ORCA_ASSET=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage

RUN curl -fL -o /opt/orca.AppImage \
        "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/${ORCA_TAG}/${ORCA_ASSET}" \
    && chmod +x /opt/orca.AppImage \
    # Render's (and most PaaS) containers don't have /dev/fuse, so extract
    # the AppImage instead of relying on FUSE-mounting it at runtime.
    && cd /opt && ./orca.AppImage --appimage-extract \
    && rm /opt/orca.AppImage \
    && mv /opt/squashfs-root /opt/orcaslicer

# Two candidate invocations for run_feasibility_test.py to try in order:
# AppRun directly, and an xvfb-wrapped fallback in case the wx toolkit
# refuses to initialize with no display even for a headless --slice call
# (a known pattern in this app family; genuinely unconfirmed for Orca's CLI
# path specifically -- that's part of what this probe exists to find out).
ENV TRIDENT_ORCA_PATH_DIRECT=/opt/orcaslicer/AppRun
RUN printf '#!/bin/sh\nexec xvfb-run -a --server-args="-screen 0 1024x768x24" /opt/orcaslicer/AppRun "$@"\n' \
        > /opt/orca-xvfb-wrapper.sh \
    && chmod +x /opt/orca-xvfb-wrapper.sh
ENV TRIDENT_ORCA_PATH_XVFB=/opt/orca-xvfb-wrapper.sh

# wx/GTK apps commonly fail ungracefully if $HOME isn't writable -- give it
# one rather than inheriting whatever Render sets.
ENV HOME=/tmp/orcahome
RUN mkdir -p "$HOME" && chmod 777 "$HOME"

WORKDIR /app
COPY . /app

CMD ["python3", "tools/orca_render_feasibility/run_feasibility_test.py"]
