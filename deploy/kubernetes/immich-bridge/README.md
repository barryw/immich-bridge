# Immich Bridge Kubernetes Deployment

This directory contains the app-side copy of the manifests used by the homelab
ArgoCD deployment. The live GitOps copy belongs in:

`~/Git/infrastructure/kubernetes-default/immich-bridge/`

## Required Cluster Prerequisites

- Existing Immich service at `immich-server.default.svc.cluster.local:2283`.
- Existing Redis service at `redis.default.svc.cluster.local:6379`.
- Existing `ghcr-secret` image pull secret in the `default` namespace.
- An `immich-bridge-secrets` secret with `GRANT_SIGNING_SECRET` and
  `SUPERADMIN_PASSWORD`.

## Routing

- `https://immich-bridge.barrywalker.io/admin` serves the admin UI/API.
- `https://immich-bridge.barrywalker.io/` serves WebDAV.

Cloudflare terminates public TLS, then forwards to Traefik over the `web`
entrypoint. The app emits HSTS because `PUBLIC_BASE_URL` is HTTPS.

## Initial Admin Access

The homelab sealed secret configures local superadmin username `barry`. Rotate the
generated `SUPERADMIN_PASSWORD` after first login, or replace the sealed secret
with one from your password manager.

## Release Flow

Woodpecker publishes immutable images as:

`ghcr.io/barryw/immich-bridge:sha-<commit>`

On `main`, the `gitops-image-bump` step copies this manifest into infrastructure
and rewrites the image to the new immutable tag. ArgoCD then reconciles
`kubernetes-default/immich-bridge`.
