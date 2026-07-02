# --- EC2: dev-runner --------------------------------------------------------
# Non-prod build box, idle nights and weekends - scheduling candidate.

resource "aws_instance" "dev_runner" {
  ami           = "ami-0abcdef1234567890"
  instance_type = "m5.2xlarge"
  subnet_id     = aws_subnet.private_a.id

  tags = {
    Name        = "dev-runner"
    Environment = "dev"
    Team        = "platform"
  }
}

# --- RDS: staging-db ---------------------------------------------------------
# Staging database, connections only during business hours.

resource "aws_db_instance" "staging" {
  identifier        = "staging-db"
  engine            = "postgres"
  engine_version    = "15.4"
  instance_class    = "db.r5.large"
  allocated_storage = 100

  username = "app"
  password = var.db_password # sensitive - never modified by automation

  tags = {
    Name        = "staging-db"
    Environment = "staging"
  }
}
