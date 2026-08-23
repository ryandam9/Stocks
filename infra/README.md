# infra

Terraform for the daily run: ECR, ECS on Fargate, EventBridge Scheduler, S3,
CloudWatch alarms and SNS. The design and the reasoning behind it are in
[`../docs/AWS_DEPLOYMENT_SPEC.md`](../docs/AWS_DEPLOYMENT_SPEC.md).

## Files never to commit

This repository is public. Two files carry account-identifying values and are
gitignored; `.example` copies of both are committed:

| File | Holds |
|---|---|
| `terraform.tfvars` | data bucket name, alert email |
| `backend.hcl` | state bucket name, lock table |

The account ID appears in neither: IAM ARNs resolve it at plan time through
`aws_caller_identity`.

## First run

```bash
# 1. State bucket and lock table. Keeps its own state locally; nothing here
#    is worth remote state, and it cannot use the backend it is creating.
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
| 1 | `terraform apply` with `schedule_enabled = false` | 43 resources, 0 destroyed |
| 2 | Click the SNS confirmation email AWS sends | Subscription leaves `PendingConfirmation` |
| 3 | Push the image — see the `ecr_push_commands` output | Image visible in ECR |
| 4 | Run each task by hand — see the `run_task_manually` output | Both exit 0; `us.db`/`asx.db` timestamps move in S3 |
| 5 | Force a failure: set a bad `data_bucket`, apply, run a task | **An email actually arrives** |
| 6 | Set `schedule_enabled = true`, apply | Three consecutive nights green |

**Phase 5 is not optional.** An alarm that has never fired is an untested
alarm, and the failure this design exists to catch is the silent one.

## Routine operations

**Monthly image refresh** — no `terraform apply` needed. The task definitions
run the `latest` tag, so pushing a new image is enough:

```bash
terraform output -raw ecr_push_commands   # then run them from the repo root
```

Build with `--platform linux/amd64`. Fargate x86 rejects an arm64 image at task
start with an exec format error.

**Pause the schedule** — set `schedule_enabled = false` and apply. The
schedules stay defined but stop firing.

**Read a run's logs** — `/ecs/stocks/us` and `/ecs/stocks/asx`. A healthy run
ends with `Uploaded to s3://…`; a misconfigured one ends with
`Skipping S3 upload (…)`, which is what the `upload-skipped` alarm watches for.

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
