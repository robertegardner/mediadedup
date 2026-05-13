# syntax=docker/dockerfile:1.6
#
# Base on a known-good Ubuntu 22.04 image that already has CUDA + an
# ffmpeg built with NVENC/NVDEC/cuvid/scale_cuda. Then layer Python and
# the app on top. This avoids fragile cross-image library copying.
#
# The image's default ENTRYPOINT is `ffmpeg`; we override it with tini so
# RQ workers / uvicorn run cleanly with proper signal handling.

FROM jrottenberg/ffmpeg:6.1-nvidia2204

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

# NOTE: do NOT install `libchromaprint-tools` from apt -- it transitively
# pulls Ubuntu's libavcodec58 -> librsvg2-2 -> libpango -> fontconfig, and
# fontconfig's post-install fc-cache crashes inside this trimmed base image.
# Instead, drop in the upstream static fpcalc release. (`nfs-common` is also
# omitted: the host mounts NFS, the container only sees the bind mount.)
ARG FPCALC_VERSION=1.5.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3-pip \
        python3.10-venv \
        ca-certificates \
        tini \
        wget \
    && wget -qO /tmp/fpcalc.tgz \
        "https://github.com/acoustid/chromaprint/releases/download/v${FPCALC_VERSION}/chromaprint-fpcalc-${FPCALC_VERSION}-linux-x86_64.tar.gz" \
    && tar -xzf /tmp/fpcalc.tgz -C /tmp \
    && install -m 0755 \
        "/tmp/chromaprint-fpcalc-${FPCALC_VERSION}-linux-x86_64/fpcalc" \
        /usr/local/bin/fpcalc \
    && rm -rf /tmp/fpcalc.tgz "/tmp/chromaprint-fpcalc-${FPCALC_VERSION}-linux-x86_64" \
    && apt-get purge -y --auto-remove wget \
    && ln -sf /usr/bin/python3.10 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.10 /usr/local/bin/python3 \
    && rm -rf /var/lib/apt/lists/* \
    && fpcalc -version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Override the upstream ffmpeg entrypoint -- our services are Python.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.scanner"]
