"""CDK stack for the SAR flood-extent serverless inference API.

One stack reproduces the whole system: S3 bucket, DynamoDB jobs table, SageMaker
serverless endpoint (from a container image already in ECR), Lambda kicker, REST
API with an API-key usage plan, least-privilege IAM, and CloudWatch alarms.
AWS Budgets are optional (account-level, opt-in via context to avoid duplicating
the ones set up by hand on this account).

Inputs (CDK context):
  -c image_uri=<ecr image uri>    default: <account>.dkr.ecr.<region>.amazonaws.com/sar-flood-aws:latest
  -c create_budgets=true          opt in to the $5/$25 budgets (needs budget_email)
  -c budget_email=<address>       where budget alerts go (never committed to the repo)

The container image is treated as a build artifact owned outside the stack (the
CI pipeline builds and pushes it). The stack consumes it by URI and the SageMaker
execution role is granted pull on the ECR repository.
"""
import json
import os

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_budgets as budgets,
    aws_cloudwatch as cw,
    aws_dynamodb as dynamodb,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_sagemaker as sagemaker,
    aws_s3 as s3,
)
from constructs import Construct

ENDPOINT_NAME = "sar-flood-aws"
FUNCTION_NAME = "sar-flood-aws-kicker"
TABLE_NAME = "sar-flood-aws-jobs"
ECR_REPO_NAME = "sar-flood-aws"
ENDPOINT_MEMORY_MB = 3072  # fresh-account serverless memory quota
MAX_CONCURRENCY = 1

_KICKER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "kicker")
_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "..", "dashboards", "main.json")


class SarFloodAwsStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        image_uri = self.node.try_get_context("image_uri") or (
            f"{Aws.ACCOUNT_ID}.dkr.ecr.{Aws.REGION}.amazonaws.com/{ECR_REPO_NAME}:latest"
        )

        # --- storage ------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=f"sar-flood-aws-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-inputs-7d", prefix="inputs/", expiration=Duration.days(7)),
                s3.LifecycleRule(id="expire-outputs-7d", prefix="outputs/", expiration=Duration.days(7)),
            ],
        )

        table = dynamodb.Table(
            self,
            "Jobs",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- SageMaker serverless endpoint --------------------------------
        repo = ecr.Repository.from_repository_name(self, "Repo", ECR_REPO_NAME)
        sm_role = iam.Role(
            self,
            "SmExecRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description="SageMaker execution role: pull the image, write logs. No S3.",
        )
        repo.grant_pull(sm_role)
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:log-group:/aws/sagemaker/*"],
            )
        )

        model = sagemaker.CfnModel(
            self,
            "Model",
            execution_role_arn=sm_role.role_arn,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(image=image_uri),
        )
        # SageMaker validates the role can pull the image at model-create time, so
        # the role's ECR-pull policy must exist first. Without this the model races
        # the policy and fails with "role cannot pull <image>".
        model.node.add_dependency(sm_role)
        endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "EndpointConfig",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    variant_name="AllTraffic",
                    model_name=model.attr_model_name,
                    serverless_config=sagemaker.CfnEndpointConfig.ServerlessConfigProperty(
                        max_concurrency=MAX_CONCURRENCY,
                        memory_size_in_mb=ENDPOINT_MEMORY_MB,
                    ),
                )
            ],
        )
        endpoint_config.add_dependency(model)
        endpoint = sagemaker.CfnEndpoint(
            self,
            "Endpoint",
            endpoint_name=ENDPOINT_NAME,
            endpoint_config_name=endpoint_config.attr_endpoint_config_name,
        )
        endpoint.add_dependency(endpoint_config)

        endpoint_arn = self.format_arn(service="sagemaker", resource="endpoint", resource_name=ENDPOINT_NAME)

        # --- Lambda kicker ------------------------------------------------
        fn = lambda_.Function(
            self,
            "Kicker",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_KICKER_DIR),
            timeout=Duration.minutes(5),
            memory_size=1024,
            environment={
                "BUCKET": bucket.bucket_name,
                "TABLE": table.table_name,
                "ENDPOINT": ENDPOINT_NAME,
                "TTL_DAYS": "7",
                "MAX_INPUT_BYTES": "3500000",
            },
        )
        bucket.grant_read_write(fn)
        table.grant_read_write_data(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(actions=["sagemaker:InvokeEndpoint"], resources=[endpoint_arn])
        )
        # Async self-invoke for the processing worker. Reference the function by its
        # fixed-name ARN (a string) so there is no construct dependency cycle.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"arn:aws:lambda:{Aws.REGION}:{Aws.ACCOUNT_ID}:function:{FUNCTION_NAME}"],
            )
        )

        # --- REST API + usage plan ---------------------------------------
        api = apigw.RestApi(
            self,
            "Api",
            rest_api_name="sar-flood-aws",
            description="SAR flood-extent async inference API",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            deploy_options=apigw.StageOptions(
                stage_name="demo",
                throttling_rate_limit=5,
                throttling_burst_limit=10,
            ),
        )
        integ = apigw.LambdaIntegration(fn, proxy=True)
        infer = api.root.add_resource("infer")
        infer.add_method("POST", integ, api_key_required=True)
        infer.add_resource("{job_id}").add_method("GET", integ, api_key_required=True)
        api.root.add_resource("uploads").add_method("POST", integ, api_key_required=True)

        key = api.add_api_key("DemoKey", api_key_name="sar-flood-aws-demo")
        plan = api.add_usage_plan(
            "UsagePlan",
            name="sar-flood-aws-demo",
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigw.QuotaSettings(limit=100, period=apigw.Period.DAY),
        )
        plan.add_api_key(key)
        plan.add_api_stage(stage=api.deployment_stage)

        # --- CloudWatch alarms -------------------------------------------
        fn.metric_errors(period=Duration.minutes(5)).create_alarm(
            self,
            "LambdaErrorsAlarm",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Kicker Lambda reported one or more errors in 5 min.",
        )
        api.metric_latency(statistic="p99", period=Duration.minutes(5)).create_alarm(
            self,
            "ApiLatencyP99Alarm",
            threshold=60000,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="API p99 latency over 60 s in 5 min.",
        )
        cw.Metric(
            namespace="AWS/SageMaker",
            metric_name="Invocation5XXErrors",
            dimensions_map={"EndpointName": ENDPOINT_NAME, "VariantName": "AllTraffic"},
            statistic="Sum",
            period=Duration.minutes(5),
        ).create_alarm(
            self,
            "EndpointErrorsAlarm",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="SageMaker endpoint returned a 5XX in 5 min.",
        )

        # --- CloudWatch dashboard ----------------------------------------
        with open(_DASHBOARD_PATH) as f:
            dashboard_body = f.read().replace("${REGION}", self.region)
        cw.CfnDashboard(
            self,
            "Dashboard",
            dashboard_name="sar-flood-aws",
            dashboard_body=json.dumps(json.loads(dashboard_body)),
        )

        # --- AWS Budgets (opt-in) ----------------------------------------
        self._maybe_budgets()

        # --- outputs ------------------------------------------------------
        CfnOutput(self, "ApiUrl", value=api.url, description="Base invoke URL (append /uploads, /infer)")
        CfnOutput(self, "ApiKeyId", value=key.key_id, description="API key id (read the value with the CLI)")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "EndpointName", value=ENDPOINT_NAME)

    def _maybe_budgets(self) -> None:
        if not self.node.try_get_context("create_budgets"):
            return
        email = self.node.try_get_context("budget_email")
        if not email:
            raise ValueError("create_budgets is set but budget_email context is missing")
        for amount in (5, 25):
            budgets.CfnBudget(
                self,
                f"Budget{amount}",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=amount, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type="ACTUAL",
                            threshold=80,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(address=email, subscription_type="EMAIL")
                        ],
                    )
                ],
            )
