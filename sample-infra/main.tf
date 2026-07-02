# Sample infrastructure for finops-mcp testing.

terraform {
  required_version = ">= 1.5.0"
}

provider "aws" {
  region = var.region
}

# --- Networking (PROTECTED: finops-mcp must never touch these blocks) ---

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  tags = {
    Name = "prod-vpc"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}

# --- EKS Node Group: app-workers -----------------------------------------
# instance type is passed via variable (traced to terraform.tfvars);
# scaling dimensions are inline.

resource "aws_eks_node_group" "app_workers" {
  cluster_name    = "prod-cluster"
  node_group_name = "app-workers"
  node_role_arn   = "arn:aws:iam::123456789012:role/eks-node-role"
  subnet_ids      = [aws_subnet.private_a.id]

  instance_types = var.worker_instance_type

  scaling_config {
    min_size     = 3
    max_size     = 10
    desired_size = 6
  }

  tags = {
    Team        = "platform"
    CostCenter  = "eng-infra"
  }
}

# --- RDS: orders-db -------------------------------------------------------
# instance class is inline; password comes from a variable (PROTECTED line).

resource "aws_db_instance" "orders" {
  identifier        = "orders-db"
  engine            = "postgres"
  engine_version    = "15.4"
  instance_class    = "db.r5.2xlarge"
  allocated_storage = 500

  username = "app"
  password = var.db_password # sensitive - never modified by automation

  tags = {
    Team = "orders"
  }
}
