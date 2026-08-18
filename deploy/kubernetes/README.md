# OpenMontage Kubernetes deployment

Use the Kustomize overlays as the only cluster deployment entrypoints:

```text
deploy/kubernetes/overlays/test -> namespace openmontage-test
deploy/kubernetes/overlays/prod -> namespace openmontage-prod
```

The historical `openmontage-test.yaml` is retained for source compatibility with the existing
workspace change and now targets `openmontage-test` rather than the shared `test` namespace. It is
not a production entrypoint: it contains the old Backlot observer shape and must not be applied for
the worker rollout. The overlays deploy headless Runner pools, with `biz-module-llm` remaining the
business-facing control plane.

Before applying an overlay, render it in CI and assert that no `Service` has `type: LoadBalancer`,
no `PersistentVolumeClaim` uses `ReadWriteMany`, and every namespaced object targets the selected
OpenMontage namespace.

Before applying an overlay, provision `openmontage-oss` in that overlay's namespace with
`OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, and optional `OSS_SESSION_TOKEN`. Prefer temporary STS
credentials and rotate the Secret before the session expires. The base fixes the bucket to
`openmontage-oss`, region to `us-east-1`, and endpoint to `oss-us-east-1.aliyuncs.com`. The runner
enforces the task grant before each OSS GET/PUT/HEAD and never places credentials in commands,
events, logs, receipts, or DLQ payloads.
