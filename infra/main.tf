locals {
  # One entry drives the task definition, log group, schedule, metric filters
  # and alarms for a universe.
  #
  # Memory is sized from measured peak RSS: 950 MB for the US fetch, ~131 MB
  # for ASX. CPU is sized low on purpose -- the run is network-bound, not
  # compute-bound. On Fargate the US fetch takes ~60 minutes against 3m42s on
  # a developer machine, with zero rate-limit errors: the provider simply
  # answers AWS egress more slowly. Since Fargate bills wall-clock per second,
  # a larger vCPU would buy nothing but a bigger bill for the same waiting.
  universes = {
    us = {
      exchange        = "US"
      instrument_type = "stocks"
      # 0.5 vCPU with 4 GB is a valid Fargate combination; memory keeps its
      # 4x headroom over the measured peak.
      cpu      = 512
      memory   = 4096
      database = "us.db"
      # 20:00 Melbourne. The most recent completed US session is the previous
      # US calendar day, which is what a run at this hour screens.
      cron = "cron(0 20 * * ? *)"
    }
    asx = {
      exchange        = "ASX"
      instrument_type = "etf"
      cpu             = 512
      memory          = 2048
      database        = "asx.db"
      # Staggered so the short run is not queued behind the US image pull.
      cron = "cron(15 20 * * ? *)"
    }
  }

  # Melbourne observes daylight saving. An IANA zone tracks the transitions;
  # a UTC cron would drift an hour twice a year.
  timezone = "Australia/Melbourne"
}
