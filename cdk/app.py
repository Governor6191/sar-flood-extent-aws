#!/usr/bin/env python3
"""CDK entrypoint for the sar-flood-aws stack."""
import os

from aws_cdk import App, Environment

from sar_flood_aws.stack import SarFloodAwsStack

app = App()

SarFloodAwsStack(
    app,
    "SarFloodAwsStack",
    env=Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Serverless SAR flood-extent inference API (SageMaker, Lambda, REST API).",
)

app.synth()
