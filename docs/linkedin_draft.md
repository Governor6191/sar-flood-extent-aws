# LinkedIn announcement (draft)

Post when ready. Fill in the repo link and live demo URL. Numbers below are the
measured ones; update if a later billing cycle changes the cost line.

---

I shipped the deployment half of my SAR flood-extent project. The Sentinel-1
flood model from my research repo is now a live serverless API on AWS, and the
whole thing comes up from one `cdk deploy`.

What it does: POST a Sentinel-1 SAR scene, the model segments flood water, and
you get back a flood mask plus a GeoJSON of flood polygons. It's the same U-Net I
validated against Copernicus EMS on Hurricane Harvey 2017.

How it's built:
- SageMaker Serverless Inference for the model. It scales to zero, so idle cost is zero.
- API Gateway, a Lambda kicker, S3, and DynamoDB for an async submit, poll, download flow (inference can outrun the 29 second API timeout, so it returns a job id and you poll for the result).
- All of it is AWS CDK in Python. `cdk deploy` brings the stack up from a clean account, `cdk destroy` takes it back to zero.
- GitHub Actions builds the container, pushes to ECR, and deploys on every push to main, using OIDC so there are no long-lived AWS keys anywhere in the repo.
- CloudWatch dashboard and alarms, plus $5 and $25 budget alerts as guardrails.

Numbers: a warm inference on a crop is about 1 second, a cold start is about 14 seconds, and the demo costs cents to run.

The research repo proved the model works. This one proves it ships.

Repo: <repo link>
Live demo (curl): <demo URL>

---

Notes for posting:
- No hashtags soup. One or two at most if any.
- Put the links in a comment if LinkedIn throttles posts with outbound links.
