variable "region" {
  description = "Region for every resource. The data bucket already lives here."
  type        = string
  default     = "ap-southeast-2"
}

variable "data_bucket" {
  description = "Existing S3 bucket the pipeline publishes databases to, name only."
  type        = string

  validation {
    condition     = !startswith(var.data_bucket, "s3://")
    error_message = "Give the bucket name without the s3:// scheme."
  }
}

variable "alert_email" {
  description = "Address subscribed to the alert topic. Confirm the subscription email AWS sends on first apply, or nothing is delivered."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Must be an email address."
  }
}

variable "notify_email" {
  description = "Address subscribed to the success topic, which emails once each database reaches S3. Defaults to alert_email. Confirm the subscription email AWS sends, or nothing is delivered."
  type        = string
  default     = null

  validation {
    condition     = var.notify_email == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", coalesce(var.notify_email, "x@y.z")))
    error_message = "Must be an email address."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the VPC this stack creates. Must not overlap anything you peer with."
  type        = string
  default     = "10.20.0.0/16"
}

variable "image_tag" {
  description = "ECR tag the scheduled tasks run. Stays 'latest' so a monthly image push needs no terraform apply."
  type        = string
  default     = "latest"
}

variable "history_days" {
  description = "Days of price history each run fetches."
  type        = number
  # Must exceed the longest analysis window by enough to reach a session
  # *before* its anchor. Under return_basis: google_finance a window opens at
  # the last session on or before the calendar anchor, so a bare 365 leaves
  # the 1-year anchor with nothing behind it whenever it lands on a weekend or
  # holiday, and that window silently opens short.
  default = 400
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. Long enough to investigate a failure, short enough not to be an archive."
  type        = number
  default     = 30
}

variable "schedule_enabled" {
  description = "Whether the daily schedules fire. Set false to apply the stack without arming it, per the phased rollout."
  type        = bool
  default     = true
}
