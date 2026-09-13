# aws_region, project_name and environment must match the network and storage
# stacks: this stack finds their resources by the names they produce.

variable "aws_region" {
  description = "Region to deploy into. ENTSO-E data is European, so an eu-* region keeps latency and egress cost low."
  type        = string
  default     = "eu-north-1"
}

variable "project_name" {
  description = "Prefix for all resource names."
  type        = string
  default     = "entsoe-grabber"
}

variable "environment" {
  description = "Deployment environment (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "lambda_architecture" {
  description = "Lambda CPU architecture. arm64 is the cheaper default."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.lambda_architecture)
    error_message = "lambda_architecture must be arm64 or x86_64."
  }
}

variable "lambda_memory_mb" {
  description = "Memory allocated to the lightweight Lambda function."
  type        = number
  default     = 256

  validation {
    condition     = var.lambda_memory_mb >= 128 && var.lambda_memory_mb <= 10240
    error_message = "lambda_memory_mb must be between 128 and 10240."
  }
}

variable "lambda_timeout_seconds" {
  description = <<-EOT
    ENTSO-E can be slow under load: the platform allows itself up to 300s per
    request, and a plain maintenance page has been observed taking 19s. This is
    the hard limit for the complete invocation, including all datasets and all
    S3 writes.

    The client's total_timeout only limits retry scheduling. A download already
    in progress continues past it while bytes keep arriving, so adding the
    socket timeouts to it does not give a worst-case duration. Allow time for
    every configured dataset and watch runs for timeouts; 600s is the initial
    operational allowance.
  EOT
  type        = number
  default     = 600

  validation {
    condition     = var.lambda_timeout_seconds >= 1 && var.lambda_timeout_seconds <= 900
    error_message = "lambda_timeout_seconds must be between 1 and 900."
  }
}

variable "schedule_expression" {
  description = <<-EOT
    When to run. Day-ahead data is published around 12:45 CET for the following
    day, so early afternoon UTC is a safe default.
  EOT
  type        = string
  default     = "cron(30 13 * * ? *)"
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Unset retention keeps logs forever and costs money."
  type        = number
  default     = 30

  validation {
    condition = contains([
      1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365,
      400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653,
    ], var.log_retention_days)
    error_message = "log_retention_days must be a value supported by CloudWatch Logs."
  }
}

variable "alarm_email_addresses" {
  description = "Email recipients for CloudWatch alarms. Confirm each SNS subscription after apply. Empty means no email notifications; other consumers can subscribe to the alarm_topic_arn output."
  type        = set(string)
  default     = []
}

# --- data sources ------------------------------------------------------------

variable "entsoe_base_url" {
  description = <<-EOT
    The Transparency Platform RESTful API endpoint, documented in the Postman
    collection the assignment links to. It serves XML market documents, and it
    is a single endpoint: every query is expressed in query parameters, with no
    path component of its own.

    ENTSO-E also runs an interoperability (IOP) tier for testing, at
    https://web-api.tp-iop.entsoe.eu/api. It speaks the same protocol and takes
    the same token, but holds noticeably less data than production, so queries
    that return documents in production may legitimately come back empty there.
    The README records what it held when last checked, and `make smoke`
    exercises the client against it.

    The iop-transparency.entsoe.eu links in the assignment are deliberately not
    used here. They are pages of the R3 web front end, not an API: a GET returns
    `Content-Type: text/html` -- a JavaScript application shell -- and the data
    arrives afterwards over XHR. Every path on that host returns the same shell.
  EOT
  type        = string
  default     = "https://web-api.tp.entsoe.eu/api"

  validation {
    condition     = can(regex("^https://[^/]+", var.entsoe_base_url))
    error_message = "entsoe_base_url must be an absolute HTTPS URL."
  }
}

variable "output_prefix" {
  description = <<-EOT
    First segment of every S3 key, so an object lands at
    `<output_prefix>/data/<year>/<month>/<dataset>-<yyyymmdd>-<nn>.csv`, and at
    the same path under `raw/` when `store_raw_xml` is on. Set it to "" to write
    at the root of the bucket.
  EOT
  type        = string
  default     = "entsoe"
}

variable "store_raw_xml" {
  description = <<-EOT
    Whether to keep the XML the platform returned, under the `raw/` prefix,
    alongside the CSV under `data/`. Storage is cheap next to a second request,
    and the API's rate limit is per token rather than per IP -- so this defaults
    on, and a day can be re-serialized without spending request budget. Turn it
    off if the CSV alone is enough.
  EOT
  type        = bool
  default     = true
}

variable "market_timezone" {
  description = <<-EOT
    IANA time zone of the areas in `datasets`, for example Europe/Bratislava.
    The platform publishes by market day, midnight to midnight in this zone, so
    every request covers such a day. One setting for all datasets: an area in a
    different zone needs its own deployment.
  EOT
  type        = string
  default     = "Europe/Bratislava"
}

variable "datasets" {
  description = <<-EOT
    Documents to fetch, keyed by dataset name. The name becomes the S3
    partition, so keep it stable. Adding a document is a change here and
    nothing else -- no code change.

    Per dataset:

      params            Query parameters, passed to the API verbatim. Which
                        ones a document needs is set by the API guide; every
                        one needs at least a `documentType`, and area-scoped
                        documents need an EIC domain such as `in_Domain`.
      date_offset_days  Which day to request, counted from the event's UTC date.
                        The request covers that market day in
                        `market_timezone`. Forecasts look forward (1); actuals
                        lag behind (-1). Optional, defaults to 0.

    `securityToken` is not listed here on purpose: it comes from SSM at
    runtime, so it never reaches Terraform state. Nor are `periodStart` and
    `periodEnd`, which are derived from `date_offset_days`. The validations
    below reject all three.

    Note the 4 KB ceiling on a Lambda's total environment: this map is passed in
    as JSON, so a few dozen datasets would hit it. Move to S3 or SSM at that point.
  EOT

  type = map(object({
    params           = map(string)
    date_offset_days = optional(number, 0)
  }))

  validation {
    condition     = length(var.datasets) > 0
    error_message = "Define at least one dataset; the Lambda has nothing to do otherwise."
  }

  validation {
    condition = alltrue([
      for _, dataset in var.datasets : can(dataset.params["documentType"])
    ])
    error_message = "Every dataset needs a `documentType` in `params`."
  }

  validation {
    condition = alltrue([
      for _, dataset in var.datasets :
      length(setintersection(
        keys(dataset.params),
        ["securityToken", "periodStart", "periodEnd"],
      )) == 0
    ])
    error_message = "Set neither `securityToken` (it comes from SSM) nor `periodStart`/`periodEnd` (derived from `date_offset_days`) in `params`."
  }
}
