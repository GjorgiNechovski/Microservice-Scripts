# Observability stack

The monitoring the analyzer reads its measurements from. It is a **standalone
compose project**: it does not overlay your app's compose file, it joins the
docker network your app already runs on. So it works the same whether your app
is in the next directory or anywhere else on the machine, and stopping it
leaves your app untouched.

## What's in it

| Component | Role | Port |
|-----------|------|------|
| **Prometheus** | metrics store (scrapes the below) | 9090 |
| **Grafana** | dashboards (Prometheus datasource pre-provisioned) | 3000 |
| **OTel Collector** | fan-out: metrics → Prometheus, traces → **Honeycomb** | 4317/4318/8889 |
| **cAdvisor** | per-container CPU, memory and replica counts, for every service | 8080 |
| **Beyla (eBPF)** | zero-code traces, where the kernel allows it; **not** a usable source of per-service metrics (see caveats) | — |

Two telemetry halves: **metrics** (Prometheus/Grafana) feed the models;
**traces** (Honeycomb) give per-request, cross-service visibility.

## Setup

1. Start your app first. It creates the network the stack joins.
2. Create the env file (git-ignored, so your key stays local):
   ```bash
   cp observability/.env.example observability/.env
   ```
   and set at least:
   - `HONEYCOMB_API_KEY`: from <https://ui.honeycomb.io/> → Environment
     settings → API Keys (the free tier is plenty).
   - `APP_NETWORK`: the network your app's containers run on. Find it with
     `docker network ls`; a compose network without an explicit `name:` is
     called `<project>_<network>`, e.g. `myapp_default`.
3. Optionally set `SCRAPE_DIR` to a folder of extra Prometheus scrape jobs for
   your system, typically its API gateway. See [Per-system scrape
   jobs](#per-system-scrape-jobs).

## Bring it up (from the repo root)

```bash
docker compose -f observability/docker-compose.observability.yaml up -d
```

Tear it down with the same file and `down`.

## Verify

- **Prometheus targets** → <http://localhost:9090/targets>: `cadvisor`,
  `otel-collector` and any jobs from your `SCRAPE_DIR` should be **UP**.
- **Grafana** → <http://localhost:3000> (anonymous access on; admin/admin) →
  dashboard *"Analyzer - System Overview"*.
- **The analyzer's view** → `python3 -m analyzer.collect <your-spec>.toml --discover`
  lists what it can measure and which services it already sees traffic for.
- **Honeycomb traces** → generate some traffic, then look in the Honeycomb UI.

## Per-system scrape jobs

`prometheus/prometheus.yml` holds only what every system has. Anything specific
to one system goes in its own file, which Prometheus pulls in through
`scrape_config_files`: every `*.yml` in the directory `SCRAPE_DIR` points at.
The default, `prometheus/scrape.d`, is empty.

A gateway is worth adding when you have one, because all traffic passes
through it, so it alone gives per-service arrival rate and latency. Keep the
file with your system's spec and point `SCRAPE_DIR` at it;
[`systems/finki-blogger/prometheus/kong.yml`](../systems/finki-blogger/prometheus/kong.yml)
is a worked example for Kong.

Your app has to expose those metrics on the shared network itself, which may
take a small overlay on your own compose file. For Kong that means enabling the
admin API, as
[`systems/finki-blogger/telemetry.compose.yaml`](../systems/finki-blogger/telemetry.compose.yaml)
does.

## Notes and caveats (read before debugging)

- **Don't count on Beyla for the analyzer.** It needs `privileged` + host PID,
  and on newer kernels the `:latest` image fails to load eBPF at all (its log
  says `Unable to load eBPF ... BTF`). Where it does run, it names services
  after their process (`nginx`, `python3`), which never matches a spec. The
  analyzer's per-service request metrics have to come from a gateway job or
  from the services' own OpenTelemetry instrumentation; see step 2 of
  [CLAUDE.md](../CLAUDE.md). `BEYLA_OPEN_PORT` (default `8000-8090`) selects
  which listening ports it instruments.
- **cAdvisor** sees every container on the host. The analyzer counts a
  service's replicas by compose service name, scoped to `compose_project` when
  the spec sets it, and ignores containers not seen in the last minute (cAdvisor
  keeps reporting removed ones for a while).
- **cAdvisor** runs with its expensive collectors switched off. Leave them off:
  on a host with ~40 containers its disk-usage scans made scrapes time out, and
  a missed scrape makes services look like they have one replica.
- **Kong metric names** vary slightly between Kong versions; the analyzer
  discovers them at runtime, but the Grafana Kong panel may need its query
  adjusted.
- Images use `:latest` for convenience. Pin specific versions for a
  reproducible run.
