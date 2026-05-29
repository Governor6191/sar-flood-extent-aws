# syntax=docker/dockerfile:1
#
# Multi-stage build for the SAR flood-extent inference service.
# Builder resolves the locked dependency set into a venv and bakes the released
# model weights in, so the runtime image is offline and the first request is warm.
#
# rasterio and shapely wheels bundle their own GDAL and GEOS, so the runtime needs
# no system geospatial libraries, only libgomp (OpenMP for torch) and libexpat.

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Bake the released checkpoint into the image (public HF repo, no auth needed).
RUN /opt/venv/bin/python -c "from huggingface_hub import hf_hub_download; hf_hub_download('Governor6191/sar-flood-extent-unet-resnet34', 'model.pt', local_dir='/opt/model')"


FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/venv/bin:$PATH \
    MODEL_LOCAL_PATH=/opt/model/model.pt \
    OUTPUT_DIR=/tmp/outputs \
    DEVICE=cpu \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/model /opt/model
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY src ./src
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000 8080

# `serve` (SageMaker convention) -> uvicorn on 8080. Default CMD -> 8000 for local dev.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
