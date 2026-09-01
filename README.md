# ENTSO-E grabber

An AWS Lambda function that downloads configurable ENTSO-E datasets and stores
them as CSV files in S3. Terraform provisions the Lambda, its schedule, storage,
IAM permissions, and the required private networking with
[fck-nat](https://fck-nat.dev/).

The example configuration collects day-ahead generation forecasts and actual
generation for the Slovak control area. Adding another endpoint only requires a
new entry in the `datasets` Terraform variable.

## Architecture

```text
EventBridge schedule
        |
        v
Lambda (private subnet) ---> fck-nat ---> ENTSO-E API
        |
        +---> SSM Parameter Store (API token)
        |
        +---> S3 gateway endpoint ---> private S3 bucket (CSV)
```

The assignment does not require multi-AZ high availability, so the VPC uses one
public and one private subnet in a single Availability Zone. The fck-nat module
runs one small `t4g.nano` instance in an Auto Scaling Group so it is replaced if
it fails. S3 traffic uses a free gateway endpoint instead of the NAT path.

## Prerequisites

- AWS credentials with permission to create the resources in `infra/`
- An ENTSO-E Transparency Platform API token
- Terraform 1.11 or newer
- AWS CLI, `uv`, and `zip`

The included dev container installs these tools for you.

## Deploy

1. Install the development dependencies and run the checks:

   ```bash
   uv sync --all-extras
   make check
   ```

2. Create the local Terraform configuration:

   ```bash
   cp infra/terraform.tfvars.example infra/terraform.tfvars
   ```

   Edit `infra/terraform.tfvars` if you want a different region, schedule,
   architecture, control area, or endpoint.

3. Initialize, review, and apply Terraform:

   ```bash
   make tf-init
   make plan
   make deploy
   ```

4. Replace the placeholder SSM value with the real API token:

   ```bash
   aws ssm put-parameter \
     --name "$(terraform -chdir=infra output -raw token_parameter_name)" \
     --type SecureString \
     --value "<your-token>" \
     --overwrite
   ```

   The provider's write-only argument does not store this value in Terraform
   state, and the unchanged value version prevents later applies from replacing
   the token with the placeholder.

5. Invoke the function and inspect its logs:

   ```bash
   make invoke
   make logs
   ```

The deployed S3 bucket name is available with:

```bash
terraform -chdir=infra output -raw data_bucket
```

## Add another dataset

Append an entry to `datasets` in `infra/terraform.tfvars`. Each entry contains:

- `path`: endpoint path below `entsoe_base_url`
- `app_state`: the endpoint's `appState` JSON represented as HCL
- `date_offset_days`: optional offset from the invocation date
- `records_path`: optional dotted path when record discovery needs a hint

The literal `{date}` is replaced at runtime wherever it occurs in `app_state`.
`infra/terraform.tfvars.example` demonstrates both endpoints from the
assignment, including their different `df` shapes.

## Terraform design choices

- The S3 bucket blocks public access, enables versioning, and uses SSE-S3.
- The Lambda role can only write to its data bucket, read its one token
  parameter, write to its log group, and manage the ENIs required for VPC
  attachment.
- The Lambda security group has no ingress and permits only HTTPS egress.
- The token starts as a non-secret placeholder and is populated after apply to
  keep credentials out of state.
- Provider versions are constrained in `infra/versions.tf` and pinned in
  `infra/.terraform.lock.hcl`.
- State is local by default for easy review. For persistent use, configure the
  example S3 backend in `infra/backend.tf.example`.

## Common commands

```bash
make check      # lint, type-check, and test
make build      # build build/function.zip
make tf-init    # initialize Terraform
make tf-lint    # check Terraform formatting and validity
make plan       # build and show the Terraform plan
make deploy     # build and apply the Terraform plan
make invoke     # invoke the deployed Lambda once
make logs       # tail the Lambda log group
make destroy    # destroy the deployed resources
```

## Repository layout

```text
src/entsoe_grabber/  Lambda application
tests/               application tests
scripts/build.sh     Lambda package builder
infra/               Terraform configuration
docs/task.md         assignment brief
```
