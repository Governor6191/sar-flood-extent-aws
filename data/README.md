# data/

Local-only scratch space for Sentinel-1 scenes. Everything here except this file
is gitignored.

`docker-compose.yml` mounts this directory at `/data` inside the container, so a
scene dropped here is reachable as `file:///data/<name>.tif`.

To run the Harvey smoke test locally, copy the validated scene from the research
repo:

```
cp ../sar-flood-extent/data/harvey/harvey_s1_houston_2017-08-30.tif data/
```

It is a 2-band (VV, VH) sigma0 dB GeoTIFF, the `COPERNICUS/S1_GRD` product over
the Houston box, the same input used for the Hurricane Harvey 2017 validation.
