# ENTSO-E grabber

An AWS Lambda function that downloads ENTSO-E datasets and stores them as CSV
files in S3. Terraform creates the Lambda, its schedule, the bucket, the IAM
permissions, and the private network with [fck-nat](https://fck-nat.dev/).

The example configuration collects the two documents named in the assignment
for the Slovak control area: the day-ahead generation forecast (`A71`) and the
actual generation per unit (`A73`).

## Architecture

```text
EventBridge schedule
        |
        v
Lambda (private subnet) ---+---> fck-nat ---+---> ENTSO-E API
        |                                   |
        |                                   +---> SSM Parameter Store (token)
        |
        +---> S3 gateway endpoint ---> private S3 bucket (CSV and raw XML)
```

The VPC has one public and one private subnet in a single Availability Zone.
The assignment does not ask for high availability, so a second zone would only
add cost. fck-nat runs one `t4g.micro` instance in an Auto Scaling Group, so AWS
replaces it if it fails.

S3 is the only service with a VPC endpoint. That endpoint is free, so the CSV
uploads stay off the NAT. Everything else leaves through fck-nat, including the
call that reads the API token from SSM.

A change to the launch template replaces the running NAT instance. Internet
access is interrupted for a short time, so apply such changes outside the
scheduled run.

## Flexible data handling

The assignment asks for a generic approach that adapts to changes in the
response, and for new endpoints that can be added by configuration. The
solution has two parts.

### 1. A new endpoint is configuration, not code

One dataset is one entry in the `datasets` Terraform variable:

```hcl
generation_actual_per_unit = {
  date_offset_days = -1        # actuals lag, so request yesterday

  params = {                   # sent to the API without changes
    documentType = "A73"
    processType  = "A16"
    in_Domain    = "10YSK-SEPS-----K"
  }
}
```

**Why this shape.** The ENTSO-E API has a single endpoint. Every query is only
a set of query parameters. A map of parameters can therefore describe any
document. The handler never looks inside `params`, so it does not need to know
what `A73` means. A stricter model, with one field per known parameter, would
need a code change for every new document type. The second dataset above was
added exactly this way, with no new code.

Three parameters are rejected by variable validation instead of accepted:

- `securityToken` is read from SSM at runtime, so the token never reaches
  Terraform state.
- `periodStart` and `periodEnd` are derived from `date_offset_days`, so a
  scheduled run always asks for the correct day.

### 2. The CSV is built from structure, not from field names

`src/entsoe_grabber/serializer.py` converts one XML document into CSV. It holds
no list of expected fields. It reads the shape of the document instead:

- Every element becomes a column. The column name is the path of the element
  without the namespace, for example `TimeSeries/Period/Point/quantity`.
- An element named `TimeSeries`, `Period` or `Point` becomes a row. An element
  that *contains* one of them also becomes a row.
- Values are written exactly as the platform sent them.

**Why this shape.** If ENTSO-E adds a field, it appears as a new column and no
code changes. If a document type is new to the serializer, it still produces
rows, because those three level names exist in every market document. A fixed
mapping of known field names would silently drop everything it does not know.

The rule about containment is needed for real documents. Unavailability
documents keep their points inside `Available_Period`, a name the serializer
has never seen. That element contains a `Point`, so it becomes a row anyway.

The complete rules, including the cases where a series stops early, are in the
module docstring of `src/entsoe_grabber/serializer.py`.

## CSV output

One CSV file per document, one row per record.

Only the market document itself is always present. `TimeSeries`, `Period` and
`Point` are all optional, and two series of one document can stop at different
levels. Each branch therefore produces rows at the deepest level it reaches:

| a branch reaching | produces                     | typical case             |
| ----------------- | ---------------------------- | ------------------------ |
| `Point`           | one row per point            | a published curve        |
| `Period`          | one row, point cells empty   | a period without data    |
| `TimeSeries`      | one row, deeper cells empty  | a withdrawn series       |
| no series at all  | one row for the document     | a header-only document   |

A document with a normal curve, a withdrawn series and an empty period:

```text
mRID,TimeSeries/mRID,TimeSeries/cancelledTS,TimeSeries/Period/resolution,TimeSeries/Period/Point/position
doc,A,,PT60M,1
doc,A,,PT60M,2
doc,B,A09,,
doc,C,,PT15M,
```

The header is the union of the columns found in that document, in the order
they first appear. A cell is empty where a record had no value.

**Where the table gets wide.** An element that repeats *outside* those row
levels costs columns, not rows, because it is copied onto every row below it.
An `A78` unavailability document that lists 40 assets of four fields each
therefore adds 160 columns to every row. No dataset in the example
configuration does this: `A71` and `A73` repeat nothing outside the row levels.
If you add one that does, the fix is to let those elements be rows too, which
is a code change in the serializer.

**Timestamps are not calculated.** `position` is written as received. The start
of an interval is `Period/timeInterval/start + (position - 1) * resolution`,
and all three values are columns. Two details of the platform make this
calculation unsafe to hide: resolutions such as `P1M` are not fixed spans, and
with `curveType` `A03` a position marks a block of variable size.

### Where the files land

```text
<output_prefix>/data/<year>/<month>/<dataset>-<yyyymmdd>-<nn>.csv
<output_prefix>/raw/<year>/<month>/<dataset>-<yyyymmdd>-<nn>.xml
```

`output_prefix` defaults to `entsoe`, so the example configuration writes
`entsoe/data/2026/09/generation_forecast_day_ahead-20260902-00.csv`. The `raw/`
copy is the XML the platform returned, kept when `store_raw_xml` is on.

The date in the key is the day the data belongs to, not the day it was
downloaded. Running the same day again overwrites those objects instead of
creating duplicates.

`<nn>` counts the documents of one response, starting at `00`. A ZIP response
contains several documents, and each one gets its own object. The documents of
one response can have different shapes, so a single shared header would invent
columns that some documents never had.

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

2. Create the S3 bucket for Terraform state once, using the commands in
   `infra/backend.hcl.example`, then point the stacks at it:

   ```bash
   cp infra/backend.hcl.example infra/backend.hcl
   ```

   Set `bucket` and `region` in `infra/backend.hcl`.

3. Create the app configuration:

   ```bash
   cp infra/app/terraform.tfvars.example infra/app/terraform.tfvars
   ```

   Edit the file if you want a different schedule, architecture, control area,
   or document. Set `alarm_email_addresses` to receive alarm notifications.

4. Initialize and apply the stacks in this order. `make deploy` shows the plan
   and asks for confirmation; `make plan` only shows it:

   ```bash
   make tf-init STACK=network && make deploy STACK=network
   make tf-init STACK=storage && make deploy STACK=storage
   make tf-init STACK=app && make deploy STACK=app
   ```

5. Write the real API token into SSM. Terraform creates the parameter with a
   placeholder, so the token never enters the Terraform state:

   ```bash
   aws ssm put-parameter \
     --name "$(terraform -chdir=infra/storage output -raw token_parameter_name)" \
     --type SecureString \
     --value "<your-token>" \
     --overwrite
   ```

6. Confirm the SNS subscription emails, then invoke the function and read its
   logs:

   ```bash
   make invoke
   make logs
   ```

The bucket name is available with
`terraform -chdir=infra/storage output -raw data_bucket`.

Later changes are applied to the affected stack only, for example
`make deploy STACK=app` after a code or dataset change. Destroy in reverse
order: `app`, then `storage`, then `network`.

## Add another dataset

Append an entry to `datasets` in `infra/app/terraform.tfvars` and run
`make deploy STACK=app`.
The name of the entry becomes the S3 partition, so keep it stable. Every
dataset needs a `documentType`. Documents scoped to an area also need an EIC
code such as `in_Domain` or `biddingZone_Domain`.

**Which day is requested.** `date_offset_days` is counted from the date of the
event. The handler turns it into a `periodStart`/`periodEnd` pair that covers
that whole market day, midnight to midnight in `market_timezone`, converted to
UTC. Forecasts look forward (`1`), actuals lag behind (`-1`).
Scheduled runs use the `time` field of the EventBridge event, so a retry after
midnight still requests the original day. A manual `make invoke` with `{}` uses
the current UTC date. To repeat a run for an older day, pass a timestamp, for
example `{"time": "2026-09-01T13:30:00Z"}`.

The window is a market day and not a UTC day because the platform publishes by
market day. A UTC day reaches into the next market day, which is not published
yet when the run happens, and the next run starts after it, so those hours
would never be fetched. `market_timezone` is one setting for all datasets
(default `Europe/Bratislava`); an area in a different zone needs its own
deployment.

Two limits observed on the live platform:

- **Some documents limit the window.** `A73` answers HTTP 400 if the interval
  is longer than one day. `date_offset_days` always produces a single day, so
  this is not a problem today. A dataset that needs a longer window will have
  to send several requests.
- **Reason code `999` is ambiguous.** The platform returns it for an empty
  result (HTTP 200), for a rejected query (400) and for a rejected token (401).
  The client separates them by HTTP status. Only `NoMatchingDataError` means
  "no data today".

**Timeouts.** The client allows 180 seconds per dataset for retries.
`lambda_timeout_seconds` (600 by default) limits the whole invocation. If you
add many datasets, raise it or split them across several functions.

## Testing against the IOP environment

ENTSO-E runs an interoperability (IOP) environment for testing at
`https://web-api.tp-iop.entsoe.eu/api`. It uses the same protocol as
production, but holds much less data. An empty result there is normal.

`tests/test_smoke_iop.py` runs the client against it. These tests are excluded
from the normal suite, so `make check` and `make test` stay offline and
deterministic:

```bash
export ENTSOE_SECURITY_TOKEN="<your-IOP-token>"
make smoke
```

## Failure monitoring and recovery

Scheduled invocations are asynchronous. Lambda retries a failed run twice. When
all attempts fail, the complete invocation record is sent to an encrypted SQS
queue and kept for 14 days. Synchronous calls such as `make invoke` return the
error directly and do not use this queue.

CloudWatch alarms notify an SNS topic when a run fails, when Lambda cannot
deliver a failure record, when a record is waiting in SQS, or when a run uses
at least 80% of its timeout. Missing data points do not trigger an alarm,
because the function normally runs only once a day. Set
`alarm_email_addresses` and confirm each subscription email, or subscribe your
own system to the `alarm_topic_arn` output. Without a confirmed subscriber the
alarms are visible in CloudWatch, but nobody is notified.

To recover a failed run, find the queue with
`terraform -chdir=infra/app output -raw failure_queue_url`, read the record and the
Lambda logs, and fix the cause. Then invoke the function with the
**`requestPayload`** field of the record, keeping its original `time` so the run
requests the intended day. Do not pass the whole record and do not pass `{}`.
Delete the message after you checked the result; the queue is not consumed
automatically.

## Terraform design choices

- The S3 bucket blocks public access, has versioning enabled, and uses SSE-S3.
- `store_raw_xml` decides whether the returned XML is also kept, under `raw/`
  instead of `data/`. It defaults to `true`: storage is cheap, and the rate
  limit is counted per token, so a day can be converted again without spending
  request budget.
- The Lambda role can write to its bucket, read its one token parameter, write
  to its log group, and send failure records to its queue. The service can
  manage the VPC network interfaces, but a conditional deny on
  `lambda:SourceFunctionArn` blocks those EC2 calls from function code.
- The Lambda security group has no ingress and allows only HTTPS egress.
- Terraform is split into three stacks. Each is a separate root configuration
  with its own state, so a plan or apply only ever touches one part:
  - `infra/network`: VPC, subnets, fck-nat, and the S3 endpoint. Rarely
    changes, and replacing the NAT interrupts internet access.
  - `infra/storage`: the data bucket and the token parameter. Kept apart so
    redeploying or destroying the app cannot touch the data or the token.
  - `infra/app`: Lambda, IAM, security group, schedule, failure queue, and
    alarms. Changes with every code or dataset change.
- The app stack finds the resources of the other two with data sources, by
  name, instead of `terraform_remote_state`. It needs no access to their state,
  and its plan fails early if they are not deployed. All three stacks must
  therefore use the same `aws_region`, `project_name`, and `environment`.
- State is stored in S3, one key per stack, with S3-native locking. The keys in
  each stack's `versions.tf` contain `dev`, so another environment needs its
  own keys.
- Provider versions are constrained in each stack's `versions.tf` and pinned in
  its `.terraform.lock.hcl`.

## Common commands

```bash
make check      # lint, type-check, and test
make smoke      # live smoke tests against the ENTSO-E IOP environment
make build      # build build/function.zip
make tf-lint    # check formatting and validity of every Terraform stack
make tf-test    # test failure monitoring and IAM with mocked providers
make tf-init STACK=app   # initialize a stack against the S3 backend
make plan STACK=app      # show the plan of a stack (app builds the zip first)
make deploy STACK=app    # apply a stack (app builds the zip first)
make destroy STACK=app   # destroy the resources of a stack
make invoke     # invoke the deployed Lambda once
make logs       # tail the Lambda log group
```

## Repository layout

```text
src/entsoe_grabber/  Lambda application
tests/               unit tests, plus live IOP smoke tests
scripts/build.sh     Lambda package builder
infra/network/       Terraform stack: VPC and fck-nat
infra/storage/       Terraform stack: data bucket and token parameter
infra/app/           Terraform stack: Lambda, schedule, IAM, and monitoring
docs/task.md         assignment brief
```
