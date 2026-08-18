# OpenMontage Kubernetes deployment

Use the Kustomize overlays as the only cluster deployment entrypoints:

```text
deploy/kubernetes/overlays/test -> namespace openmontage-test
deploy/kubernetes/overlays/prod -> namespace openmontage-prod
```

The historical `openmontage-test.yaml` is retained for source compatibility with the existing
workspace change, but it is not a production entrypoint. It contains the old Backlot observer
shape and must not be applied for the worker rollout. The overlays deploy headless Runner pools,
with `biz-module-llm` remaining the business-facing control plane.

Before applying an overlay, render it in CI and assert that no `Service` has `type: LoadBalancer`,
no `PersistentVolumeClaim` uses `ReadWriteMany`, and every namespaced object targets the selected
OpenMontage namespace.

Before applying an overlay, provision `openmontage-oss` in that overlay's namespace with these
keys: `OPENMONTAGE_OSS_REGION`, `OPENMONTAGE_OSS_ENDPOINT`,
`ALIBABA_CLOUD_ROLE_ARN`, and `ALIBABA_CLOUD_OIDC_PROVIDER_ARN`. Do not store AccessKey ID/Secret
in this Secret. The bucket is fixed to `openmontage-oss` in the base. The runner exchanges its
projected `sts.aliyuncs.com` service-account token for short-lived RRSA/OIDC credentials and
enforces the task grant before each OSS GET/PUT/HEAD.
