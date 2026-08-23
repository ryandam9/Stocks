locals {
  # One entry drives the task definition, log group, schedule, metric filters
  # and alarms for a universe. Sizing comes from measuring the real stages:
  # the US fetch peaks at 950 MB over 3m42s, the ASX run at ~131 MB over ~1min.
  universes = {
    us = {
      exchange        = "US"
      instrument_type = "stocks"
      cpu             = 1024
      memory          = 4096
      database        = "us.db"
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
