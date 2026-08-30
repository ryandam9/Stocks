# infra

Terraform for the daily run: ECR, ECS on Fargate, EventBridge Scheduler, S3,
CloudWatch alarms and SNS. The design and the reasoning behind it are in
[`../docs/AWS_DEPLOYMENT_SPEC.md`](../docs/AWS_DEPLOYMENT_SPEC.md).

## Files never to commit

This repository is public. Two files carry account-identifying values and are
gitignored; `.example` copies of both are committed:

| File | Holds |
|---|---|
| `terraform.tfvars` | data bucket name, alert email, notification email |
| `backend.hcl` | state bucket name |

Neither is reconstructable from the repository, so they are backed up to S3 —
see below.

The account ID appears in neither: IAM ARNs resolve it at plan time through
`aws_caller_identity`.

### Where they are kept instead

Both live in the Terraform state bucket under `stocks/config/`, beside the
state file itself.

That adds no exposure. **The state file already contains all three values** —
the alert email, the data bucket name and the account ID — because Terraform
records resource attributes verbatim. Protecting the inputs more carefully than
the state they produce would be theatre. The bucket is private (an anonymous
GET returns 403), versioned, AES256-encrypted, and has public access fully
blocked.

```bash
STATE=s3://<state-bucket>/stocks/config

# after changing either file
aws s3 cp terraform.tfvars $STATE/ --region <state-bucket-region>
aws s3 cp backend.hcl      $STATE/ --region <state-bucket-region>

# on a new machine, before terraform init
aws s3 cp $STATE/ . --recursive --region <state-bucket-region>
```

Two things to know:

- **`backend.hcl` cannot bootstrap itself.** It is the file that says where the
  state bucket is, so you need the bucket name before you can fetch it. Keep
  that one name somewhere you will find it; everything else follows from it.
- **These copies do not sync themselves.** Nothing detects drift between the
  local file and the stored one, so re-upload as part of changing them — the
  same discipline as running `terraform apply` after an edit.

The state bucket's region may differ from `var.region`; ours holds unrelated
state from other projects and predates this stack, which is why
`infra/bootstrap` does not manage it.

## First run

```bash
# 1. State bucket. Locking is S3-native, so there is no table. Keeps its own
#    state locally: it cannot use the backend it is creating.
cd bootstrap
terraform init
terraform apply -var state_bucket=<globally-unique-name>
#    Paste the backend_hcl output into ../backend.hcl.

# 2. The stack itself.
cd ..
cp terraform.tfvars.example terraform.tfvars   # fill in bucket and email
terraform init -backend-config=backend.hcl
terraform apply
```

`terraform.tfvars` ships with `schedule_enabled = false`, so the first apply
builds everything **without arming the nightly run**. That is deliberate — see
the rollout below.

## Rollout

| Phase | Do | Confirm |
|---|---|---|
| 1 | `terraform apply` with `schedule_enabled = false` | 50 resources, 0 destroyed |
| 2 | Click **both** SNS confirmation emails AWS sends | Neither subscription is left `PendingConfirmation` |
| 3 | Push the image: `./scripts/build_image.sh --push` | Image visible in ECR, tagged `git-<sha>` for the commit you pushed |
| 4 | Run each task by hand — see the `run_task_manually` output | All three exit 0; `us.db`/`asx.db`/`nse.db` timestamps move in S3, and a "published" email arrives for each |
| 5 | Force a failure: set a bad `data_bucket`, apply, run a task | **An email actually arrives** |
| 6 | Set `schedule_enabled = true`, apply | Three consecutive nights green |

**Phase 5 is not optional.** An alarm that has never fired is an untested
alarm, and the failure this design exists to catch is the silent one.

## Routine operations

**Monthly image refresh** — no `terraform apply` needed. The task definitions
run the `latest` tag, so pushing a new image is enough:

```bash
./scripts/build_image.sh --push   # from the repository root
```

Always build through the script. It passes `--platform linux/amd64` (Fargate
x86 rejects an arm64 image at task start with an exec format error), reads the
repository URL and region from these outputs, and refuses to push an image
whose stamped revision does not match a clean, pushed commit — see *Shipping a
code or config change* in the root README.

**Pause the schedule** — set `schedule_enabled = false` and apply. The
schedules stay defined but stop firing.

## Where the mail goes

| Path | Sends | Volume |
|---|---|---|
| SNS `stocks-alerts` | a run broke, or no run happened in 24 h | ~0/day |
| SES, via the `stocks-notify` lambda | `us.db` / `asx.db` / `nse.db` reached S3 | ~3/day |

They are separate because routine success at ~3/day and exceptional failure at
~0/day do not belong on one channel: the volume trains you to filter it, and
the filter then swallows the alerts. The success message is driven by the S3
object rather than by ECS task completion — a task can exit 0 having published
nothing, so "task completed" would read as success on exactly the night there
is no new data. See §8.4 of the spec.

**Alerts.** `alert_email` is subscribed to the topic and AWS emails one
confirmation link on first apply. Until it is clicked the subscription reads
`PendingConfirmation` and nothing is delivered.

```bash
aws sns list-subscriptions --region ap-southeast-2 \
  --query 'Subscriptions[?ends_with(TopicArn,`stocks-alerts`)].[Endpoint,SubscriptionArn]' \
  --output text
```

**Success mail.** Sent through SES rather than SNS, because an SNS email
subscription is plain text only — quoted string, "AWS Notification Message"
subject, unsubscribe footer. The lambda
(`infra/lambda/notify_published.py`) renders an HTML body with the time in
Melbourne local, labelled `AEST` or `AEDT`, and a plain-text alternative
alongside it. Change the zone by changing `local.timezone` in `main.tf` — the
same value the schedules use, so the mail and the cron can never quote two
different clocks. The lambda is redeployed by `terraform apply`: the archive
provider rezips the file and `source_code_hash` notices.

Two SES identities must verify before anything is delivered:

| Identity | Verifies when | Needed because |
|---|---|---|
| `notify_domain` | the DKIM CNAMEs terraform wrote resolve, minutes | mail must be signed by a domain you control, or DMARC alignment fails and it lands in spam |
| the recipient address | you click the link AWS emails | the SES sandbox refuses unverified recipients, and a new account is in the sandbox |

```bash
aws sesv2 get-email-identity --region ap-southeast-2 \
  --email-identity <notify_domain> --query VerifiedForSendingStatus
```

While either is unverified every publish errors, and `stocks-notify-failed`
alarms to the alert topic. In the sandbox the notifier's role needs
`ses:SendEmail` on *both* identities, because SES authorises the send against
each verified destination as well as the From address; scoped to the domain
alone the call is denied on the recipient's ARN, which looks like an
unverified address and is not.

## What is deliberately absent

- **No NAT Gateway.** Public subnets with an egress-only security group. NAT
  would cost ~$43/month against ~$1/month for everything else, to protect a
  task that listens on nothing.
- **No EFS or state volume.** Every artefact is rebuilt each run, so the task
  is stateless. A failed night self-heals: the next run refetches a full year.
- **No secrets.** The price provider needs no API key, and S3 access comes
  from the task role. Nothing for Secrets Manager to hold.
- **No exit-code retries.** EventBridge Scheduler retries the `RunTask` API
  call, not a task that ran and exited non-zero. Retrying on exit code needs
  Step Functions, which the self-healing property above does not justify.
