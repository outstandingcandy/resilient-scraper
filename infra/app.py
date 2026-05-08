#!/usr/bin/env python3
"""CDK App entry point for resilient-scraper infrastructure."""

import os

import aws_cdk as cdk

from stack import ResilientScraperStack

app = cdk.App()

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
if not account:
    raise SystemExit("CDK_DEFAULT_ACCOUNT must be set in the environment")

ResilientScraperStack(
    app,
    "ResilientScraperStack",
    env=cdk.Environment(
        account=account,
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

app.synth()
