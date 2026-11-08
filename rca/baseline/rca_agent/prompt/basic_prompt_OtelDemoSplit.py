cand = """## POSSIBLE ROOT CAUSE REASONS:

- productCatalogFailure
- recommendationCacheFailure
- adManualGc
- adHighCpu
- adFailure
- cartFailure
- paymentFailure
- paymentUnreachable
- imageSlowLoad
- kafkaQueueProblems
- emailMemoryLeak
- loadGeneratorFloodHomepage
- failedReadinessProbe
- llmInaccurateResponse
- llmRateLimitError

## POSSIBLE ROOT CAUSE COMPONENTS:

- accounting
- ad
- cart
- checkout
- currency
- email
- flagd
- fraud-detection
- frontend
- frontend-proxy
- image-provider
- load-generator
- payment
- product-catalog
- product-reviews
- quote
- recommendation
- shipping"""

schema = f"""## TELEMETRY DIRECTORY STRUCTURE:

- You can access the telemetry directory in our microservices system: `dataset/OtelDemoSplit/telemetry/`.

- This directory contains subdirectories organized by a date (e.g., `dataset/OtelDemoSplit/telemetry/2026_04_20/`).

- Within each date-specific directory, you’ll find these subdirectories: `metric`, `trace`, and `log` (e.g., `dataset/OtelDemoSplit/telemetry/2026_04_20/metric/`).

- The telemetry data in those subdirectories is stored in CSV format (e.g., `dataset/OtelDemoSplit/telemetry/2026_04_20/metric/metric_service.csv`).

- Available dates are 2026_04_19 through 2026_04_24. The first and last are partial: data begins 2026-04-19 03:40 UTC and ends 2026-04-24 19:25 UTC.

## DATA SCHEMA

1.  **Metric Files**:

    Metrics are split across several files by what they measure. Every file
    except `metric_app.csv` shares the columns `timestamp,cmdb_id,kpi_name,value`.

    1. `metric_app.csv` -- per-minute transaction summary, one row per service:

        ```csv
        timestamp,rr,sr,cnt,mrt,tc
        1776570000,100.0,100.0,4,15485.75,accounting
        ```

    2. `metric_service.csv` -- application and protocol level: HTTP and gRPC
       server/client request counts and durations, and app-specific counters
       (`http_*`, `rpc_*`, `app_*`, `db_*`, `feature_flag_*`). The widest file,
       ~830k rows/day over 19 components:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,cart,app_cart_add_item_latency_seconds_count,16615
        ```

    3. `metric_mesh.csv` -- RED metrics derived from spans by the collector's
       spanmetrics connector: call counts and durations per operation, broken
       down by `span_kind`, `span_name` and `status_code`. **This is where
       per-operation errors appear**, as `status_code=STATUS_CODE_ERROR`:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,flagd,traces_span_metrics_calls_total-span_kind=SPAN_KIND_SERVER-span_name=GET-status_code=STATUS_CODE_UNSET,70
        ```

    4. `metric_node.csv` -- host and OS-process level: `system_*` CPU, memory,
       disk and network for the host `otel-demo-prod-01` (and a few services
       whose SDK also exports them), `process_*` per-process CPU/memory/threads,
       plus DNS lookups and synthetic HTTP checks:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,accounting,process_cpu_count,8
        ```

    5. `metric_runtime.csv` -- language runtime internals, by service:
       `jvm_*` (Java), `dotnet_*`/`aspnetcore_*`/`kestrel_*` (.NET), `v8js_*`
       and `nodejs_*` (Node), `cpython_*` (Python), `go_*` (Go). Garbage
       collection, heap and thread behaviour live here:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,accounting,dotnet_assembly_count,120
        ```

    6. `metric_container.csv` -- Docker container resource usage for all 25
       containers: `container_*` CPU, memory, block I/O and network:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,accounting,container_cpu_usage_nanoseconds_total,198782373000
        ```

    7. `metric_infra.csv` -- backing services and the telemetry pipeline:
       `kafka_*`, `postgresql_*`, `redis_*` (the Valkey cart store), and
       `otelcol_*`/`otlp_*`/`jaeger_*` for the collector and trace backend:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776816000,kafka,kafka_controller_active_count,1
        ```

2.  **Trace Files**:

    1. `trace_span.csv`:

        ```csv
        timestamp,cmdb_id,parent_id,span_id,trace_id,duration
        1776569992864,flagd,,3c42728b7b227146,4e82c5aa918f184e215a7bf33421f07c,599997
        ```

3.  **Log Files**:

    1. `log_service.csv`:

        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        68737053aa7a7b7e833bfc2572819044,1776570005,kafka,kafka.utils.Log4jControllerRegistration$,"Registered kafka:type=kafka.Log4jController MBean"
        ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This microservice system is an e-commerce web store (the OpenTelemetry Astronomy Shop demo): a browser frontend behind an Envoy proxy, with services for the product catalog, cart, checkout, payment, shipping, currency, email, ads, recommendations, product reviews, quotes, accounting and fraud detection, plus a load generator driving synthetic user traffic.

2. Faults in this system are introduced by **feature flags**. Each flag turns on a specific defect in a specific service — for example `paymentFailure` makes the payment service throw on a fraction of charges, and `adManualGc` makes the ad service force a full garbage collection on every ad request. The root cause reason is therefore the name of the feature flag that was switched on, and the root cause component is the service whose behaviour the flag changes. Note that the two are not always the same service: `paymentUnreachable` is read in the **checkout** service, which then cannot reach payment.

3. Choose the metric file by what you are looking for. Request failures and error rates: `metric_mesh.csv` (`status_code=STATUS_CODE_ERROR`) and `metric_service.csv` (`rpc_grpc_status_code`, `http_response_status_code`). Latency: the `_sum` and `_count` pair of any duration metric, whose ratio is the mean. Resource exhaustion: `metric_container.csv` for a container, `metric_node.csv` for the host. Garbage collection and heap: `metric_runtime.csv`. Queue and datastore behaviour: `metric_infra.csv`.

4. `kpi_name` is a Prometheus metric name followed by any remaining label values, joined with `-` (e.g. `app_ads_ad_requests_total-app_ads_ad_request_type=TARGETED`). Histogram bucket series are not included, but every latency metric is present as a `_sum` and a `_count`.

5. The `metric_app.csv` file only contains four KPIs: rr, sr, cnt, and mrt. `rr` is the percentage of transactions that returned a response, `sr` the percentage that returned without an error, `cnt` the number of transactions, and `mrt` the mean response time in milliseconds. `tc` is the service that served the transaction. It covers only services that start a trace, so it is a subset of the services in the other files.

6. In different telemetry files, the timestamp units and cmdb_id formats may vary:

- Metric: Timestamp units are in seconds (e.g., 1776570120).

- Trace: Timestamp units are in milliseconds (e.g., 1776569992864).

- Log: Timestamp units are in seconds (e.g., 1776570005).

7. `cmdb_id` is the service name and is the same identifier across all telemetry types, so metrics, traces and logs can be joined on it directly. Besides the application services, `metric_container.csv` and `metric_infra.csv` also carry infrastructure that emits no traces (`jaeger`, `otel-collector`, `opensearch`, `prometheus`, `grafana`, `postgresql`, `valkey-cart`, `kafka`, `llm`), and `metric_node.csv` carries the host pseudo-component `otel-demo-prod-01`.

8. `parent_id` in `trace_span.csv` is empty for a root span. Spans for the operation `flagd.evaluation.v1.Service/EventStream` are normal ~600 second long-poll reconnection cycles for feature flag streaming; they appear in every observation window regardless of incidents and are background noise, not a symptom.

9. Counters are cumulative and are not reset at the start of each day, so a counter's value at midnight carries over from earlier days. Take the difference between consecutive samples to get the per-minute rate.

10. Please use the UTC time zone in all analysis steps. All timestamps in this dataset are UTC."""
