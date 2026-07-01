# --- Lambda: image-resizer --------------------------------------------------

resource "aws_lambda_function" "image_resizer" {
  function_name = "image-resizer"
  role          = "arn:aws:iam::123456789012:role/lambda-exec"
  handler       = "index.handler"
  runtime       = "nodejs20.x"
  filename      = "build/image-resizer.zip"

  memory_size = 1024
  timeout     = 30
}
