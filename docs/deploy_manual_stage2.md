# Manual SageMaker deploy (first endpoint)

These are the exact commands used to stand up the first SageMaker serverless
endpoint by hand, before the CDK stack takes over the infrastructure. They are
here for reference and reproducibility. Once the CDK app lands, `cdk deploy`
owns all of this and you do not run these commands.

Region is `us-east-1`. Account id and image digest are this account's; swap in
your own. The image is the Stage 1 container, pushed once by hand to ECR.

## 1. ECR repository and image push

```bash
# Create the repository.
aws ecr create-repository \
  --repository-name sar-flood-aws \
  --region us-east-1 \
  --image-scanning-configuration scanOnPush=false

# Build a single-platform image (disable buildkit attestations so the pushed
# manifest is a plain image, not an index; SageMaker pulls that cleanly).
docker build --provenance=false --sbom=false -t sar-flood-aws:dev .

# Authenticate Docker to ECR.
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com

# Tag and push.
docker tag sar-flood-aws:dev \
  <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/sar-flood-aws:latest
docker push <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/sar-flood-aws:latest
```

## 2. SageMaker execution role

The role SageMaker assumes to pull the image and write logs.

```bash
# Trust policy: allow sagemaker.amazonaws.com to assume the role.
cat > sm-trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Principal": { "Service": "sagemaker.amazonaws.com" },
      "Action": "sts:AssumeRole" }
  ]
}
JSON

aws iam create-role \
  --role-name sar-flood-aws-sagemaker-exec \
  --assume-role-policy-document file://sm-trust.json

# Broad managed policy for the manual stage. The CDK stack scopes this down to
# ECR pull, CloudWatch Logs, and S3 read on the project bucket only.
aws iam attach-role-policy \
  --role-name sar-flood-aws-sagemaker-exec \
  --policy-arn arn:aws:iam::aws:policy/AmazonSageMakerFullAccess
```

## 3. Model, endpoint config, endpoint

```bash
# Model: points at the ECR image and the execution role.
aws sagemaker create-model \
  --model-name sar-flood-aws \
  --primary-container Image=<ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/sar-flood-aws:latest \
  --execution-role-arn arn:aws:iam::<ACCOUNT>:role/sar-flood-aws-sagemaker-exec \
  --region us-east-1

# Endpoint config: serverless, scales to zero. Memory 3072 MB, max concurrency 1.
cat > sm-variants.json <<'JSON'
[
  { "VariantName": "AllTraffic",
    "ModelName": "sar-flood-aws",
    "ServerlessConfig": { "MemorySizeInMB": 3072, "MaxConcurrency": 1 } }
]
JSON

aws sagemaker create-endpoint-config \
  --endpoint-config-name sar-flood-aws \
  --production-variants file://sm-variants.json \
  --region us-east-1

aws sagemaker create-endpoint \
  --endpoint-name sar-flood-aws \
  --endpoint-config-name sar-flood-aws \
  --region us-east-1

# Wait until InService (a few minutes).
aws sagemaker wait endpoint-in-service \
  --endpoint-name sar-flood-aws --region us-east-1
```

## Notes

- **Memory is 3072 MB, not the 6 GB service maximum.** A fresh AWS account caps
  "Memory size in MB per serverless endpoint" at 3072 MB until you request a
  Service Quotas increase. 3072 MB handles the Sentinel-1 crops the demo accepts.
  The full Hurricane Harvey scene (340 MB on disk, larger in memory) was validated
  locally and in the container in Stage 1 at 99.9985% pixel agreement; running a
  full scene through the endpoint would need a quota increase or the tiled S3
  streaming read, noted as future work.
- **Max concurrency 1** keeps this a single-user demo and caps cost. There is no
  provisioned concurrency, so idle cost is zero.
- **Invoke it** with `scripts/invoke_sm.py` (boto3 sagemaker-runtime, multipart
  payload). See `docs/latency_baseline.md` for the measured cold and warm numbers.

## Teardown

```bash
aws sagemaker delete-endpoint        --endpoint-name sar-flood-aws --region us-east-1
aws sagemaker delete-endpoint-config --endpoint-config-name sar-flood-aws --region us-east-1
aws sagemaker delete-model           --model-name sar-flood-aws --region us-east-1
```
