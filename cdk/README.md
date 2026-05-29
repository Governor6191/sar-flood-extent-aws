# CDK app

Infrastructure as code for the SAR flood-extent serverless API. One stack
(`SarFloodAwsStack`) creates everything: S3 bucket, DynamoDB jobs table,
SageMaker serverless endpoint, Lambda kicker, REST API with an API-key usage
plan, least-privilege IAM, and CloudWatch alarms.

## Prerequisites

- AWS CLI configured for the target account (`aws sts get-caller-identity`).
- Node.js (the CDK CLI runtime) and the CDK CLI: `npm install -g aws-cdk`.
- `uv` (the app declares its deps in `pyproject.toml`; `cdk.json` runs
  `uv run python app.py`).
- Docker, and the container image pushed to ECR. The image is a build artifact
  owned outside this stack. Build and push it once (CI does this on every push to
  main):

  ```bash
  aws ecr create-repository --repository-name sar-flood-aws --region us-east-1
  aws ecr get-login-password --region us-east-1 \
    | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
  docker build --provenance=false --sbom=false -t sar-flood-aws:latest ..
  docker tag sar-flood-aws:latest <account>.dkr.ecr.us-east-1.amazonaws.com/sar-flood-aws:latest
  docker push <account>.dkr.ecr.us-east-1.amazonaws.com/sar-flood-aws:latest
  ```

## Deploy

One-time per account/region:

```bash
cdk bootstrap aws://<account>/us-east-1
```

Then:

```bash
cd cdk
cdk deploy --require-approval never
```

Outputs include the API base URL and the API key id. Read the key value with:

```bash
aws apigateway get-api-key --api-key <ApiKeyId from outputs> --include-value \
  --query value --output text
```

See `../docs/usage.md` for the request flow.

### Context options

| Context             | Default                                              | Use                                  |
|---------------------|------------------------------------------------------|--------------------------------------|
| `image_uri`         | `<account>.dkr.ecr.<region>.amazonaws.com/sar-flood-aws:latest` | point at a specific image tag |
| `create_budgets`    | off                                                  | `-c create_budgets=true` to add the $5/$25 budgets |
| `budget_email`      | none                                                 | `-c budget_email=you@example.com` (required with create_budgets) |

Budgets are account-level and opt-in so a deploy does not duplicate budgets
already set up by hand. The email is passed at deploy time, never committed.

```bash
cdk deploy -c create_budgets=true -c budget_email=you@example.com
```

## Tear down

```bash
cdk destroy
```

The S3 bucket has `autoDeleteObjects`, so destroy empties and removes it.
Everything else (serverless) drops to zero cost on destroy. The ECR repository
and image are not managed by this stack and are left in place.

## What is and is not in the stack

- **In:** S3, DynamoDB, SageMaker model + endpoint config + serverless endpoint,
  Lambda kicker, REST API + usage plan + API key, IAM roles, CloudWatch alarms,
  optional budgets.
- **Out:** the ECR repository and the container image (a CI build artifact the
  stack consumes by URI), and the GitHub OIDC role for CI/CD (created once,
  separately).
