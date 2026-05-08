"""CDK Stack for resilient-scraper Worker Auto Scaling Group.

Deploys Docker-based scraper workers on EC2 with auto-scaling.
Workers automatically claim tasks from the shared Aurora PostgreSQL
database and adjust ASG desired capacity directly via boto3 (no
Lambda/CloudWatch intermediary).

Infrastructure:
  - ECR image (built from docker/Dockerfile.worker)
  - Security group (outbound all, inbound to Aurora 5432)
  - IAM role (SSM, ECR, CloudWatch, S3, ASG, SSM Parameters)
  - Launch template (t3.medium, 30GB gp3, user-data with Docker)
  - Auto Scaling Group (0-5 instances, public subnets)
"""

from __future__ import annotations

import os
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Tags,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
)
from constructs import Construct


class ResilientScraperStack(cdk.Stack):
    """Resilient-scraper worker infrastructure with auto-scaling."""

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ── Context parameters ──────────────────────────────────
        vpc_id = self.node.try_get_context("vpc_id") or os.environ.get("VPC_ID", "")
        db_sg_id = self.node.try_get_context("db_sg_id") or os.environ.get("DB_SG_ID", "")

        instance_type = (
            self.node.try_get_context("instance_type")
            or os.environ.get("RSCRAPER_INSTANCE_TYPE", "t3.medium")
        )
        min_capacity = int(
            self.node.try_get_context("min_capacity")
            or os.environ.get("AUTOSCALE_MIN_INSTANCES", "0")
        )
        max_capacity = int(
            self.node.try_get_context("max_capacity")
            or os.environ.get("AUTOSCALE_MAX_INSTANCES", "5")
        )

        if not vpc_id:
            raise ValueError("vpc_id is required (via -c vpc_id=... or VPC_ID env)")
        if not db_sg_id:
            raise ValueError("db_sg_id is required (via -c db_sg_id=... or DB_SG_ID env)")

        # ── Import existing resources ───────────────────────────
        vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=vpc_id)

        db_sg = ec2.SecurityGroup.from_security_group_id(
            self, "DbSecurityGroup", db_sg_id,
            mutable=True,
        )

        # ── Docker image → ECR ──────────────────────────────────
        project_root = Path(__file__).resolve().parent.parent

        docker_image = ecr_assets.DockerImageAsset(
            self,
            "RScraperImage",
            directory=str(project_root),
            file="docker/Dockerfile.worker",
            platform=ecr_assets.Platform.LINUX_AMD64,
            exclude=[
                "data",
                "logs",
                ".pids",
                ".venv",
                ".git",
                "cdk.out",
                "infra/cdk.out",
                "__pycache__",
                "*.pyc",
                "*.log",
                "*.png",
                ".env",
            ],
        )

        image_uri = docker_image.image_uri

        # ── Security Group ──────────────────────────────────────
        worker_sg = ec2.SecurityGroup(
            self,
            "RScraperWorkerSG",
            vpc=vpc,
            description="Security group for resilient-scraper workers",
            allow_all_outbound=True,
        )

        # Allow workers → Aurora PostgreSQL
        db_sg.add_ingress_rule(
            peer=worker_sg,
            connection=ec2.Port.tcp(5432),
            description="Allow resilient-scraper workers to access Aurora",
        )

        # ── IAM Role ────────────────────────────────────────────
        role = iam.Role(
            self,
            "RScraperWorkerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
            ],
        )

        # SSM Parameter Store — read secrets
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/resilient-scraper/*"
                ],
            )
        )

        # S3 — upload screenshots
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
                resources=[
                    "arn:aws:s3:::flight-matrix-*",
                    "arn:aws:s3:::flight-matrix-*/*",
                ],
            )
        )

        # ── User Data ──────────────────────────────────────────
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -ex",
            "",
            "exec > >(tee /var/log/user-data.log) 2>&1",
            "",
            "echo '=== Resilient Scraper Worker Setup ==='",
            "",
            "# Install Docker + AWS CLI",
            "apt-get update",
            "apt-get install -y docker.io unzip curl jq",
            "systemctl enable docker",
            "systemctl start docker",
            "usermod -aG docker ubuntu",
            "",
            'curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"',
            "unzip -q awscliv2.zip",
            "./aws/install",
            "rm -rf aws awscliv2.zip",
            "",
            "# ECR login and pull image",
            f"aws ecr get-login-password --region {self.region} | "
            f"docker login --username AWS --password-stdin "
            f"{self.account}.dkr.ecr.{self.region}.amazonaws.com",
            "",
            f"docker pull {image_uri}",
            "",
            "# Read configuration from SSM Parameter Store",
            f'DB_URL=$(aws ssm get-parameter --name /resilient-scraper/db-url '
            f'--with-decryption --query "Parameter.Value" --output text '
            f'--region {self.region})',
            f'FEISHU_APP_ID=$(aws ssm get-parameter --name /resilient-scraper/feishu-app-id '
            f'--query "Parameter.Value" --output text '
            f'--region {self.region} 2>/dev/null || echo "")',
            f'FEISHU_APP_SECRET=$(aws ssm get-parameter --name /resilient-scraper/feishu-app-secret '
            f'--with-decryption --query "Parameter.Value" --output text '
            f'--region {self.region} 2>/dev/null || echo "")',
            f'S3_BUCKET=$(aws ssm get-parameter --name /resilient-scraper/s3-bucket '
            f'--query "Parameter.Value" --output text '
            f'--region {self.region} 2>/dev/null || echo "")',
            f'S3_PREFIX=$(aws ssm get-parameter --name /resilient-scraper/s3-prefix '
            f'--query "Parameter.Value" --output text '
            f'--region {self.region} 2>/dev/null || echo "")',
            "",
            "# Write environment file",
            f'cat > /etc/default/rscraper-worker << EOFENV',
            f"ECR_IMAGE={image_uri}",
            f"AWS_DEFAULT_REGION={self.region}",
            'DB_URL=$DB_URL',
            'FEISHU_APP_ID=$FEISHU_APP_ID',
            'FEISHU_APP_SECRET=$FEISHU_APP_SECRET',
            'S3_BUCKET=$S3_BUCKET',
            'S3_PREFIX=$S3_PREFIX',
            'SCRAPER_LOG_LEVEL=INFO',
            'EOFENV',
            "",
            "# Create systemd service",
            "cat > /etc/systemd/system/rscraper-worker.service << 'EOFSERVICE'",
            "[Unit]",
            "Description=Resilient Scraper Worker (Docker)",
            "After=docker.service",
            "Requires=docker.service",
            "",
            "[Service]",
            "Type=simple",
            "EnvironmentFile=/etc/default/rscraper-worker",
            "Restart=always",
            "RestartSec=10",
            "ExecStartPre=-/usr/bin/docker stop rscraper-worker",
            "ExecStartPre=-/usr/bin/docker rm rscraper-worker",
            f"ExecStartPre=/bin/bash -c 'aws ecr get-login-password --region {self.region} | "
            f"docker login --username AWS --password-stdin "
            f"{self.account}.dkr.ecr.{self.region}.amazonaws.com'",
            "ExecStartPre=/usr/bin/docker pull ${ECR_IMAGE}",
            "ExecStartPre=/usr/bin/docker volume create rscraper-chrome-profile",
            "ExecStartPre=/usr/bin/docker run --rm --user root --entrypoint chown "
            "-v rscraper-chrome-profile:/app/data/chrome-profile "
            "${ECR_IMAGE} -R scraper:scraper /app/data/chrome-profile",
            "ExecStart=/usr/bin/docker run --name rscraper-worker --rm "
            "--shm-size=2g --network=host "
            "-e DB_URL=${DB_URL} "
            "-e FEISHU_APP_ID=${FEISHU_APP_ID} "
            "-e FEISHU_APP_SECRET=${FEISHU_APP_SECRET} "
            "-e S3_BUCKET=${S3_BUCKET} "
            "-e S3_PREFIX=${S3_PREFIX} "
            "-e SCRAPER_LOG_LEVEL=${SCRAPER_LOG_LEVEL} "
            "-v rscraper-chrome-profile:/app/data/chrome-profile "
            "${ECR_IMAGE}",
            "ExecStop=/usr/bin/docker stop rscraper-worker",
            "StandardOutput=append:/var/log/rscraper-worker/worker.log",
            "StandardError=append:/var/log/rscraper-worker/worker.log",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOFSERVICE",
            "",
            "mkdir -p /var/log/rscraper-worker",
            "chown ubuntu:ubuntu /var/log/rscraper-worker",
            "",
            "systemctl daemon-reload",
            "systemctl enable rscraper-worker",
            "systemctl start rscraper-worker",
            "",
            "echo '=== Resilient Scraper Worker Setup Complete ==='",
        )

        # ── Launch Template ─────────────────────────────────────
        launch_template = ec2.LaunchTemplate(
            self,
            "RScraperLaunchTemplate",
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.generic_linux(
                {
                    "us-east-1": "ami-0c7217cdde317cfec",
                    "us-west-2": "ami-0cf2b4e024cdb6960",
                }
            ),
            security_group=worker_sg,
            role=role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=30,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        delete_on_termination=True,
                    ),
                )
            ],
        )

        # ── Auto Scaling Group ──────────────────────────────────
        asg = autoscaling.AutoScalingGroup(
            self,
            "RScraperASG",
            vpc=vpc,
            launch_template=launch_template,
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            health_checks=autoscaling.HealthChecks.ec2(
                grace_period=Duration.minutes(5),
            ),
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                max_batch_size=1,
                min_instances_in_service=0,
            ),
        )

        Tags.of(asg).add("Name", "rscraper-worker")
        Tags.of(asg).add("Project", "resilient-scraper")

        # ── Outputs ─────────────────────────────────────────────
        cdk.CfnOutput(self, "ASGName", value=asg.auto_scaling_group_name)
        cdk.CfnOutput(self, "DockerImageURI", value=image_uri)
        cdk.CfnOutput(self, "WorkerSecurityGroupId", value=worker_sg.security_group_id)
