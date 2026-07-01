variable "region" {
  type    = string
  default = "us-east-1"
}

variable "worker_instance_type" {
  description = "Instance types for the app-workers EKS node group"
  type        = list(string)
  default     = ["m5.xlarge"]
}

variable "db_password" {
  type      = string
  sensitive = true
}
