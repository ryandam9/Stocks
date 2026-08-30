locals {
  # One entry drives the task definition, log group, schedule, metric filters
  # and alarms for a universe.
  #
  # Memory is sized from measured peak RSS: 950 MB for the US fetch, 614 MB
  # for NSE, ~131 MB for ASX. Peak does not track ticker count alone -- NSE
  # screens a fifth of the US universe and holds nearly two thirds of its
  # memory, because the analysis frames are built per window and NSE runs five
  # of them over 600k price rows. CPU is sized low on purpose -- the run is
  # network-bound, not
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
    nse = {
      exchange        = "NSE"
      instrument_type = "stocks"
      # 3 GB, not the ASX 2 GB: measured peak is 614 MB, and 2048 would leave
      # 3.3x where the US task keeps 4x. Exit 137 -- the OOM killer -- is a
      # failure mode observability.tf can only report after the fact, and the
      # extra gigabyte costs well under a cent a run.
      cpu      = 512
      memory   = 3072
      database = "nse.db"
      # 07:45 Melbourne, Tue-Sat, covering Monday-Friday NSE sessions.
      #
      # India is the easy case. It observes no daylight saving, so only
      # Melbourne's clock moves and the gap is +4:30 (AEST) or +5:30 (AEDT) --
      # not the 14-16 hours, shifting from both ends independently, that makes
      # the US slot delicate.
      #
      # NSE trades 09:15-15:30 IST, with pre-open from 09:00. In Melbourne
      # terms that session closes at 20:00 the same evening (21:00 under AEDT)
      # and the next pre-open is 13:30 the following afternoon (14:30 AEDT).
      # A Melbourne morning therefore sits in a window where nothing is
      # trading, with room at both ends:
      #
      #   07:45 Melbourne = 03:15 IST (AEST) / 02:15 IST (AEDT)
      #     ~11 hours after the previous close, ~6 hours before the next
      #     pre-open, in both DST combinations that exist.
      #
      # Tue-Sat for the same reason as the others, though the arithmetic is
      # different: Tuesday's run screens Monday's Indian session, which closed
      # on Monday *evening* Melbourne time rather than overnight as the US one
      # does.
      #
      # 07:45 rather than 07:15 so the run is not queued behind the ASX image
      # pull. At the ~63 s per 100-ticker batch measured on Fargate, 26 batches
      # take about half an hour and finish well before the US task at 09:30.
      cron = "cron(45 7 ? * TUE-SAT *)"
    }
  }

  # Success notifications go to the same address as alerts unless one is set
  # explicitly. A default cannot reference another variable, so it defaults to
  # null and is resolved here.
  notify_email = coalesce(var.notify_email, var.alert_email)

  # Who the mail comes from. A display name as well as an address, because
  # "Stocks" is what an inbox list shows; the address is what SES checks
  # against the verified domain identity.
  notify_from = "Stocks <stocks@${var.notify_domain}>"

  # Melbourne observes daylight saving. An IANA zone tracks the transitions;
  # a UTC cron would drift an hour twice a year. Note this pins the schedule to
  # Melbourne local time only -- it does not keep the gap to the New York
  # close constant, since that gap moves with US DST independently.
  timezone = "Australia/Melbourne"
}
