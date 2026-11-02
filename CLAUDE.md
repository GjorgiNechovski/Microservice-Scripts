# CLAUDE.md — operating guide for agents

You have been asked to analyse the performance and reliability of someone's
microservice system with this tool. This file is what you need to do that
without hand-holding. **Read all of it before running anything.** The user-facing
overview is [README.md](README.md); this file adds the judgement calls, the
safety rules, and the traps that took this project months to find.

## What the tool does

Given a Docker Compose system, it measures the numbers the models need from the
running system itself (arrival rate λ, service rate μ, replica count c, repair
rate), then runs four analysis layers: M/M/c queueing (speed, bottleneck,
replica sizing), Markov-chain availability per component, a reliability block
diagram + fault tree per user operation (single points of failure), and a
sensitivity analysis (which change buys most). The human supplies **structure
only** (service names, which components each operation needs); you never type
a measured number.

## Rule zero: what you may run without asking

| Tier | Commands | Rule |
|---|---|---|
| **Safe** | reading files; `python3 -m analyzer.tests`; `python3 -m analyzer.run_all <spec>`; `python3 -m analyzer <spec> --no-measure`; `python3 -m analyzer.collect <spec> --discover`; `python3 -m analyzer.collect <spec>` (preview); `python3 -m analyzer.chaos <spec>` (plan only) | Always fine. |
| **Generates load** | `python3 -m analyzer <spec> --skip-chaos`; `python3 -m analyzer.collect <spec> --run-load`; running k6 | Only on an environment the human has said is theirs to hammer (local or dev). Otherwise ask. |
| **Stops containers** | `python3 -m analyzer.chaos <spec> --run`; **`python3 -m analyzer <spec>` with no flags** | **Always ask first, even locally.** Show the output of `python3 -m analyzer.chaos <spec>` with your question so the human sees exactly what would go down. |
| **Changes their app** | editing their source, Dockerfiles or compose; adding instrumentation | Ask first, unless told the app is a throwaway or dev copy. Prefer a separate compose overlay to editing their base file. |

**The bare command `python3 -m analyzer <spec>` is the destructive one.** It
runs the load test *and* fault injection by default, deliberately. It is never
a first step.

## The job, in order

### 1. Survey their system (read-only)

Read their compose file(s) and Dockerfiles. Note: services and languages,
replicas, whether there is a gateway, databases, message brokers, services that
receive no HTTP traffic (e.g. queue consumers), and **the port each service
actually listens on** (read the `CMD`; do not assume 8000: .NET Kestrel and
Spring default to 8080). Once their app is up, `docker network ls` gives the
network name you will need.

### 2. Decide where per-service request rate and latency come from

This step decides whether Layer 1 works at all. The analyzer needs, for every
service in the spec, a **request counter and a latency histogram whose service
label equals the service's name in the spec** (normally its compose service
name). Exactly two sources are known to work:

**A. A gateway that exports per-upstream metrics**, such as Kong with its
Prometheus plugin. Write a scrape-job file and point `SCRAPE_DIR` at its folder
(see [observability/README.md](observability/README.md)). Worked example:
[systems/finki-blogger/](systems/finki-blogger/) (`prometheus/kong.yml` plus
`telemetry.compose.yaml`, which opens Kong's admin API).

**B. OpenTelemetry instrumentation in every service**, exporting HTTP-server
metrics over OTLP to the stack's collector with `OTEL_SERVICE_NAME` set to the
compose service name. Use this when there is no metrics-exporting gateway,
which is most apps; a plain nginx gateway exports nothing per upstream.

**Do not rely on Beyla.** It is in the stack for zero-code traces, but on
newer kernels it fails to load eBPF entirely (check `docker logs
microservice-observability-beyla-1` for `Unable to load eBPF`), and where it
does run it names services after their process (`nginx`, `python3`), which never
matches the spec.

Recipe for **B**, verified with this repository on a FastAPI app that had no
instrumentation:

- add `opentelemetry-distro` and `opentelemetry-exporter-otlp` to the
  requirements;
- in the Dockerfile, run `opentelemetry-bootstrap -a install` after
  `pip install`, and prefix the start command with `opentelemetry-instrument`;
- set these environment variables, ideally in a separate overlay file so their
  base compose stays untouched:

  ```yaml
  OTEL_SERVICE_NAME: <compose service name>     # must match the spec
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
  OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
  OTEL_METRICS_EXPORTER: otlp                     # not "none"
  OTEL_TRACES_EXPORTER: otlp
  OTEL_LOGS_EXPORTER: none
  ```

That produced `http_server_duration_milliseconds_*` with
`service_name="catalog"` etc., which the analyzer picked up with no
configuration. For other languages the environment variables are the same;
instrument with the Java agent (`-javaagent:opentelemetry-javaagent.jar`), the
.NET OpenTelemetry SDK or auto-instrumentation, or
`@opentelemetry/auto-instrumentations-node`. Only the Python route has been
verified against this collector, so confirm with `--discover` (step 6).

Note that the FINKI example sets `OTEL_METRICS_EXPORTER=none` because Kong
supplies its metrics; for route B it must be `otlp`.

### 3. Write the spec

Copy [systems/TEMPLATE.toml](systems/TEMPLATE.toml); every option is documented
inline. The spec can live in their repository. Its measured values are written
next to it as `<name>.measured.toml`, so if it does, that file lands in their
repo too.

- `services`: the application tiers you want queueing analysis for. Each needs
  metrics from step 2.
- `infrastructure`: shared components (gateway, databases, broker, cache).
- `[operations]`: which components each user-facing operation needs. **This is
  a modelling decision, not clerical work**: it drives every availability
  result. Draft it from the code, then confirm it with the human.
- `[containers]`: only where a spec name differs from the compose service name.
- `[readiness]`: for components without a healthcheck, the TCP port meaning
  "the app is serving". Verify the port per language.
- `[instances]`: only for infrastructure that genuinely runs more than one
  instance with failover. Declaring redundancy that does not exist makes the
  availability figures fiction.
- `[failure_rates]`: failure rates cannot be observed before a system has run in
  production, so these are estimates. Label them as estimates to the human.

### 4. Write the k6 load script (if load testing is allowed)

It is always specific to their endpoints, payloads and auth. Cover every
operation in `[operations]`. A relative `[load].script` is read from the spec's
own directory. Log in once in `setup()`, and make sure the user and data it
needs exist; a failed `setup()` makes k6 exit within seconds.

k6 here is a **closed-loop** generator: each virtual user waits for a response
before sending the next request. A pause between iterations ("think-time")
therefore caps the offered load at roughly *users ÷ pause*, whatever the system
could handle, and the results describe the generator rather than the system.
Decide deliberately: no pause to find capacity, a pause to model human users.
Never compare runs made with different settings.
[systems/finki-blogger/load.js](systems/finki-blogger/load.js) reads
`THINK_TIME` for exactly this reason.

Secrets for the script go in `[load].env_passthrough` plus a git-ignored `.env`
in the directory you run from, never in the spec.

### 5. Bring everything up

1. **Their app first.** It creates the network the stack joins.
2. `cp observability/.env.example observability/.env`, then set `APP_NETWORK`,
   `SCRAPE_DIR` if you wrote a gateway job, and `HONEYCOMB_API_KEY` from the
   human. Without a key yet, a placeholder still lets metrics flow, which is all
   the analyzer reads; only traces fail to reach Honeycomb. Tell the human.
3. From this repo's root:
   `docker compose -f observability/docker-compose.observability.yaml up -d`.
4. <http://localhost:9090/targets> should show `cadvisor`, `otel-collector` and
   any gateway job as **UP**.

### 6. Discover, then measure

1. `python3 -m analyzer.collect <spec> --discover`. Every spec service must
   appear under *services seen with traffic*.
2. Drive traffic for **at least a minute** before measuring. A rate needs at
   least two Prometheus scrapes, so straight after setup a service can be
   listed by `--discover` yet read as *not measured*.
3. `python3 -m analyzer.collect <spec>` previews λ, μ and c per service.

If a service is missing: either its telemetry name does not match the spec
(fix `OTEL_SERVICE_NAME` or `[containers]`), or **it is broken. Read its logs.**
A missing service has repeatedly turned out to be crash-looping, not idle,
including after an instrumentation change that broke its build.

### 7. Run the analysis

- `--no-measure`: analyse the saved measurements only. Safe.
- `--skip-chaos`: load test and measure, no containers stopped.
- No flags: the full pipeline, including fault injection. **Ask first.**

A list in `[load].vus` makes it a sweep: one fault-injection pass, then
load, measure and analyse per level, then a cross-level scaling report. Choose
levels that bracket the point where things start to saturate. Output goes to
`analysis/` (or `--out`).

## Before you believe any number

Check all of these. If one fails, the run is invalid: say so, and don't report
its numbers.

1. **cAdvisor stayed up for the whole run.** Query Prometheus for
   `avg_over_time(up{job="cadvisor"}[<run length>])`; it must be 1. One missed
   scrape makes a 5-replica service read as 1 replica.
2. **Replica counts** in the measured file match the compose file.
3. **k6 ran for its full duration** and its checks mostly passed. A run of a
   few seconds means `setup()` failed.
4. **Every spec service has λ and μ.**
5. **Comparing two runs?** Same think-time, same dataset size, same load levels,
   same host conditions. A database that grew between runs changes query cost
   on its own.
6. **Host saturation.** Check per-container CPU. Everything shares one host, and
   `nproc` counts SMT threads, not physical cores.

## Reading results: traps

- **Latency-based μ is poisoned under load.** Mean latency includes queueing,
  so `service_rate` collapses as load rises; feeding it back into a queueing
  model double-counts congestion. Trust `service_rate_effective` (recovered from
  the throughput plateau). A replica recommendation shown as `n/a` is
  deliberate: **never fill one in.**
- **"Saturated" while the CPU idles** means a serialisation point: a small
  connection pool, a pooler's child limit, a single worker process. Find what
  everything queues behind before adding hardware.
- **The model has no notion of a shared host or connection limits.** Its
  availability predictions have been accurate; its performance predictions have
  missed in both directions. (One controlled A/B test so far: availability
  matched to five decimals, peak throughput was off by 1.9× and 3.5×.) Adding
  replicas on one host can *lower* throughput.
- **Fixing a bottleneck moves it.** Re-sweep after every change.
- **The diagnosis says *scale out* or *optimise*.** The specific cause still
  needs code or traces.

## Looks like a tool bug, isn't

| Symptom | Cause |
|---|---|
| *lambda not measured* right after setup | counters too new; drive traffic a minute and retry |
| stack won't start: `APP_NETWORK` missing | `observability/.env` not filled in, or the app isn't up yet |
| can't rebuild their app: network has active endpoints | the stack is attached to it; stop the stack first |
| every MTTR exactly 60 s | the readiness probe never succeeded: check the `[readiness]` ports. On Windows/macOS the probe runs inside the container through `docker exec`, so it also needs `bash` or busybox `nc` in the image |
| k6 can't reach the app (Windows/macOS) | no `--network host` there; set `[load].docker_network` and pass the base URL in `[load].env` |

## Ask the human about

- whether load testing and fault injection are allowed, and on which environment;
- changing their code or images (instrumentation);
- the operations and which components each needs, i.e. what "down" means;
- failure-rate estimates, when there is no failure history;
- the Honeycomb API key.

## If you change this repository

- The engine is standard-library only; numpy and matplotlib only inside
  `analyzer/figures/`.
- Never hard-code an application or metric name in the engine; extend the
  candidate lists in `analyzer/collect/collector.py`.
- Every technical report has a plain-language `*-explained.txt` twin.
- Never invent a number: report `unmeasured` or `n/a` with the reason.
- Fault injection only stops and starts containers; it never removes them.
- A new spec option goes into `systems/TEMPLATE.toml` too. For a generic tool,
  the template is the product.
- Secrets only in git-ignored `.env` files.
- Run `python3 -m analyzer.tests` before and after.
