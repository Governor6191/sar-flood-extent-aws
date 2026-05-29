# sar-flood-extent-aws

Serverless AWS deployment of the [sar-flood-extent](https://github.com/Governor6191/sar-flood-extent)
model: a U-Net that segments flood water from Sentinel-1 SAR imagery. This repo
takes the released model and ships it as an HTTP inference API, with
infrastructure as code, CI/CD, monitoring, and a documented cost envelope.

Status: work in progress. The containerized inference service and its API
contract are in place and validated against the Hurricane Harvey 2017 scene the
model was tested on. The cloud deployment (SageMaker serverless endpoint, async
API Gateway front end, CDK stack, GitHub Actions) is being built out.

## What works now

A Docker container wraps the model in a FastAPI service. It takes a 2-band
(VV, VH) Sentinel-1 GRD scene and returns a flood-extent mask plus a GeoJSON of
flood polygons. The inference path mirrors the research repo exactly, so the
container reproduces the local result.

```bash
uv sync
docker build -t sar-flood-aws:dev .
docker run --rm -p 8000:8000 -v ${PWD}/data:/data sar-flood-aws:dev

curl -s -X POST localhost:8000/infer \
  -H 'content-type: application/json' \
  -d '{"scene_uri": "file:///data/harvey_s1_houston_2017-08-30.tif"}'
```

See [docs/api_contract.md](docs/api_contract.md) for the full request and
response schema.

## Model

The deployed model is `Governor6191/sar-flood-extent-unet-resnet34` on the
Hugging Face Hub. Architecture, training, benchmark numbers, and the Hurricane
Harvey validation are documented in the [research repo](https://github.com/Governor6191/sar-flood-extent).
This repo does not retrain or modify the model. It deploys it.

## Layout

```
src/            inference core and FastAPI service
tests/          unit tests and a small integration test
docs/           API contract and deployment notes
Dockerfile      multi-stage CPU build, model weights baked in
```

## License

[MIT](LICENSE).
