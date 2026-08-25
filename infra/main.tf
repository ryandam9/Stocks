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
      # 09:30 Melbourne, Tue-Sat, which covers Monday-Friday US sessions.
      #
      # Not 07:00, which is when the ASX task runs. Melbourne and New York are
      # 14-16 hours apart depending on which DST regimes are active, and the
      # gap moves with US DST independently of Melbourne's. At 07:00 Melbourne
      # the US market is still open from early November to late March -- 15:00
      # EST -- and fetch_prices requests through the exchange's current date,
      # so the provider can return a partial in-progress bar and latest_price
      # becomes an intraday quote rather than a close.
      #
      # 09:30 is 17:30 ET at the worst point of the year, comfortably after the
      # 16:00 close in every DST combination. The ~63 minute run still finishes
      # before 10:35 local.
      cron = "cron(30 9 ? * TUE-SAT *)"
    }
    asx = {
      exchange        = "ASX"
      instrument_type = "etf"
      cpu             = 512
      memory          = 2048
      database        = "asx.db"
      # 07:15 Melbourne, Tue-Sat, covering Monday-Friday ASX sessions.
      # Staggered so the short run is not queued behind the US image pull.
      #
      # The hour suits ASX well: the market opens at 10:00 local, so a 07:15
      # run is clear of any in-progress session and the newest close is the
      # previous trading day's.
      cron = "cron(15 7 ? * TUE-SAT *)"
    }
  }

  # Success notifications go to the same address as alerts unless one is set
  # explicitly. A default cannot reference another variable, so it defaults to
  # null and is resolved here.
  notify_email = coalesce(var.notify_email, var.alert_email)

  # Melbourne observes daylight saving. An IANA zone tracks the transitions;
  # a UTC cron would drift an hour twice a year. Note this pins the schedule to
  # Melbourne local time only -- it does not keep the gap to the New York
  # close constant, since that gap moves with US DST independently.
  timezone = "Australia/Melbourne"
}
