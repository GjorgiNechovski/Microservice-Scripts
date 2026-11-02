# Per-system scrape jobs

The default `SCRAPE_DIR`. Prometheus loads every `*.yml` file in the directory
`SCRAPE_DIR` points at, so a system can add scrape targets without editing the
shared `prometheus.yml`.

This one is deliberately empty. With no gateway job the analyzer still gets
per-container CPU, memory and replica counts from cAdvisor, but per-service
request rate and latency then have to come from the services' own
OpenTelemetry metrics, exported to the collector. See step 2 of
[CLAUDE.md](../../../CLAUDE.md).

To add a job, keep it with your system's spec rather than in here, and point
`SCRAPE_DIR` at that folder. See `systems/finki-blogger/prometheus/kong.yml`
for a worked example. Each file has the usual top-level key:

```yaml
scrape_configs:
  - job_name: my-gateway          # must be unique across all loaded files
    metrics_path: /metrics
    static_configs:
      - targets: ["my-gateway:9100"]
```
