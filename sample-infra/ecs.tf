# --- ECS: checkout-svc ----------------------------------------------------
# Fargate task size (cpu/memory) is inline on the task definition.

resource "aws_ecs_task_definition" "checkout" {
  family                   = "checkout-svc"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  cpu    = 1024
  memory = 2048

  container_definitions = jsonencode([
    {
      name  = "checkout"
      image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/checkout:latest"
    }
  ])
}

resource "aws_ecs_service" "checkout" {
  name            = "checkout-svc"
  cluster         = "prod-cluster"
  task_definition = aws_ecs_task_definition.checkout.arn
  desired_count   = 3
  launch_type     = "FARGATE"
}
