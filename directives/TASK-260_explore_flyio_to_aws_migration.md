# TASK-260 Explore Fly.io to AWS Migration
**Date:** 2026-10-16
**Status:** ready

## Goal
Evaluate and architect the migration of the Kairos scalping environment monitor from Fly.io (`bom` region) to AWS, delivering an architectural decision record (ADR), comparative infrastructure trade-off matrix, cost & latency assessment, and migration blueprint for running the containerized service in Mumbai (`ap-south-1`).

## Inputs
- Fly.io deployment config: `fly.toml` (app `kairos-yxfydw`, region `bom`, 1 shared CPU, 1GB RAM)
- Container build specification: `Dockerfile` (Python 3.11 multi-stage build executing `python -m kairos.scheduler`)
- Daemon configuration: `kairos.service` (systemd service definitions, restart policy, process lifecycle)
- System architecture: `README.md`, `docs/`, `supabase_schema.sql` (interaction model with Supabase state bridge and Dhan API)
- Operational requirements: Low latency to Indian markets (NSE tick data via Dhan API) requiring Mumbai (`ap-south-1`) placement, continuous in-memory state preservation during market hours (09:15–15:30 IST), automated restarts on failure, and secure secret injection.

## Tools / Scripts to Use
- AWS architecture evaluation framework (ECS Fargate vs App Runner vs Lightsail Containers vs EC2 t4g)
- Cost modeling tools (AWS Pricing Calculator vs Fly.io standard pricing)
- Docker container verification tools
- GitHub Actions CI/CD deployment workflow templates

## Expected Output
- **Architectural Decision Record (ADR):** `directives/adr/TASK-260_flyio_to_aws_migration_architecture.md` authored by Software Architect.
- **Infrastructure Comparison Matrix:**
  1. AWS ECS on Fargate (`ap-south-1`)
  2. AWS App Runner (`ap-south-1`)
  3. AWS Lightsail Containers / Lightsail Linux VPS (`ap-south-1`)
  4. AWS EC2 (t4g.micro/small with systemd / Docker)
- **Trade-off Evaluation:** Cost (monthly run rate), Latency (network proximity to Dhan API/NSE), Operational overhead (maintenance, logging via CloudWatch, secrets management via SSM/Secrets Manager vs `.env`), and Auto-recovery/Reliability.
- **Migration & Rollout Strategy:** Dual-run shadow verification plan, cutover protocol, rollback strategy, and CI/CD deployment pipeline design (e.g. GitHub Actions to ECR/ECS or runner).
- **Obsidian Vault Sync:** Synchronized documentation in `~/Documents/Obsidian/Projects/Kairos/`.

## Acceptance Criteria
- Clear, authoritative architectural recommendation on the target AWS service with explicit rationale.
- Complete trade-off matrix comparing cost, latency, reliability, and maintenance effort across the 4 candidate AWS architectures versus existing Fly.io setup.
- Production-ready secret management pattern for Dhan API tokens and Supabase credentials.
- Failure recovery specification ensuring automatic restart upon unhandled exceptions or network disconnects.
- Clear phased rollout plan detailing zero-downtime transition and shadow validation.

## Edge Cases
- **In-Memory State & Buffers:** Kairos maintains continuous candle history and rolling indicators in memory; ephemeral serverless functions (like standard API Gateway + Lambda) are unsuitable without external state hydrators.
- **Market Hours vs 24/7 Runtime:** Kairos monitors market hours (09:15–15:30 IST). Scheduling compute to run 24/7 vs stopping outside trading hours impacts pricing and requires evaluating restart / warm-up behavior.
- **Geographic Latency:** Non-Mumbai AWS regions (e.g., `us-east-1`, `eu-west-1`) introduce 100–200ms latency to Dhan API, negatively affecting intraday scalping calculations; deployment must target `ap-south-1`.
- **Dhan Token Expiration:** Dhan access tokens expire periodically; secret rotation and daemon re-authentication must be supported without requiring manual container rebuilds.
