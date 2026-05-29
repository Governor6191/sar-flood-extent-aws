# Latency baseline (SageMaker serverless)

Measured by `scripts/invoke_sm.py` against the live serverless endpoint
`sar-flood-aws` in `us-east-1`. The payload is a Sentinel-1 Harvey crop sent as a
multipart upload to the endpoint's `/invocations` route.

## Setup

| Item                | Value                                            |
|---------------------|--------------------------------------------------|
| Endpoint type       | SageMaker Serverless Inference                   |
| Memory              | 3072 MB                                          |
| Max concurrency     | 1                                                |
| Provisioned conc.   | none (scales to zero, idle cost $0)              |
| Container           | the Stage 1 image, CPU torch + geospatial stack  |
| Payload             | `tests/data/harvey_crop.tif`, ~1.5 MB multipart  |
| Region              | us-east-1                                         |

## Numbers

| Call    | Round-trip | Server-side inference | water_fraction | polygons |
|---------|------------|-----------------------|----------------|----------|
| cold    | 14.41 s    | 13.66 s               | 0.456871       | 25       |
| warm 1  | 1.14 s     | 0.89 s                | 0.456871       | 25       |
| warm 2  | 1.16 s     | 0.89 s                | 0.456871       | 25       |
| warm 3  | 1.25 s     | 0.92 s                | 0.456871       | 25       |

- **Cold start: 14.4 s.** This is the serverless container spin-up plus model
  load on the first request after idle. It sits under the 20 to 60 s range a CPU
  serverless container can hit, helped by baking the weights into the image so
  there is no model download at startup.
- **Warm median: 1.16 s** round-trip for a crop, of which ~0.9 s is inference.
- Results are identical across calls (deterministic), and match the Stage 1
  validation: the endpoint runs the exact image digest that reproduced the Harvey
  mask at 99.9985% pixel agreement.

## Notes on input size

The endpoint is sized at 3072 MB, the fresh-account serverless memory quota. That
comfortably handles the Sentinel-1 crops the demo accepts. The full Harvey scene
(340 MB on disk) was validated locally and in the container in Stage 1; pushing a
full scene through the serverless endpoint would need a memory quota increase or
the tiled S3 streaming read path, noted as future work. The async API (next stage)
is what makes longer inferences practical from a client: submit, poll, download.
