# Deploying the public demo to a Nebius Compute VM

This is the runbook for `deploy/demo/` — one small Nebius AI Cloud VM running `docker compose`,
serving the public demo described in `docs/API_V2.md` ("The public demo") behind Caddy with
automatic HTTPS.

**Every `nebius` command below is marked UNRUN.** No Nebius resource described here has been
created, started, or billed: these commands were assembled from the published `docs.nebius.com`
pages linked inline, not from a live account. Run each command yourself, read its output, and
adjust names/regions/sizes to your account before the next one — this is a sequence to execute
deliberately, not a script to pipe through unattended.

**What this deploy needs and does not need.** The demo's whole design (`README.md`, "Demo — a real
model, simulated providers, hard caps") is that a stranger reaches it with no credential of yours.
So this VM holds exactly one secret — the Nebius Token Factory model key — and must never hold a
Tavily key or a Google credential: `demo_session.refuse_if_providers_configured` makes the process
refuse to start the moment either is present, which is correct and not a bug to route around.

---

## 1. Provision the VM (UNRUN)

Commands and flags below are quoted from `docs.nebius.com` (linked inline); adapt names, the
parent/project id, and the region to your account. Where a flag's exact syntax was not confirmed
against the docs (security group rules, below), that is stated rather than guessed.

### 1.1 Authenticate the CLI (UNRUN)

```sh
nebius profile create
```

Interactive: name the profile, accept the default API endpoint, choose federation auth, and
approve the browser login. See
[Getting started with the Nebius AI Cloud CLI](https://docs.nebius.com/cli/quickstart) and
[How to set up the Nebius AI Cloud CLI](https://docs.nebius.com/cli/configure). If you have access
to more than one tenant, the CLI prompts you to pick one.

### 1.2 Network (UNRUN)

A project usually already has a default network/subnet; skip this if `nebius vpc subnet list`
already returns one you want to use.

```sh
nebius vpc network create-default --parent-id <PROJECT_ID>
```

`create-default` provisions a default network, subnet, route table and (where the project has the
feature enabled) a default security group in one call —
[nebius vpc network create-default](https://docs.nebius.com/cli/reference/vpc/network/create-default).

```sh
export SUBNET_ID=$(nebius vpc subnet list --parent-id <PROJECT_ID> \
  --format jsonpath='{.items[0].metadata.id}')
```

[nebius vpc subnet list](https://docs.nebius.com/cli/reference/vpc/subnet/list) documents
`--parent-id` (required — the project id to list subnets under) and the global `--format`
(`yaml|json|jsonpath|table|text`); the `jsonpath` above is that global flag, taking the id of the
first subnet the project returns.

### 1.3 Firewall / security group (UNRUN — rule syntax not confirmed, see note)

```sh
nebius vpc security-group create \
  --name privatemesh-demo-sg \
  --network-id <NETWORK_ID> \
  --parent-id <PROJECT_ID>
```

[nebius vpc security-group create](https://docs.nebius.com/cli/reference/vpc/security-group/create)
documents `--network-id` and `--parent-id`; **the exact flags for adding an inbound rule (TCP
22/80/443 from your IP and from `0.0.0.0/0`) were not confirmed from the CLI reference page during
this pass** — the reference page did not render the rule sub-schema. Before creating the instance,
run `nebius vpc security-group-rule create --help` (or read
[Managing security groups and security rules](https://docs.nebius.com/vpc/security-groups/manage))
and add exactly three inbound allows: TCP/22 from your own IP only (never `0.0.0.0/0` for SSH),
TCP/80 and TCP/443 from `0.0.0.0/0` (Caddy needs 80 for the ACME HTTP-01 challenge as well as the
redirect to 443). Everything else stays denied — the compose stack makes no other inbound call.

### 1.4 The instance itself (UNRUN)

A small CPU-only VM — this demo makes outbound HTTPS calls to Token Factory and serves two small
web processes behind Caddy; it needs no GPU. `cpu-d3` (non-GPU AMD EPYC Genoa) is available in
every region; `2vcpu-8gb` is its smallest documented preset —
[Types of virtual machines and GPUs](https://docs.nebius.com/compute/virtual-machines/types).

```sh
export USER_DATA=$(jq -Rrs '.' <<'EOF'
#cloud-config
users:
  - name: deploy
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - REPLACE_WITH_CONTENTS_OF_YOUR_ed25519.pub
EOF
)

export VM_ID=$(nebius compute instance create \
  --name privatemesh-demo \
  --parent-id <PROJECT_ID> \
  --resources-platform cpu-d3 \
  --resources-preset 2vcpu-8gb \
  --boot-disk-managed-disk-name privatemesh-demo-disk \
  --boot-disk-managed-disk-type network_ssd \
  --boot-disk-managed-disk-size-gibibytes 30 \
  --boot-disk-attach-mode READ_WRITE \
  --cloud-init-user-data "$USER_DATA" \
  --network-interfaces "[{\"name\": \"eth0\", \"subnet_id\": \"$SUBNET_ID\", \"ip_address\": {}, \"public_ip_address\": {}}]" \
  --format jsonpath='{.metadata.id}')
```

Flag names quoted from
[nebius compute instance create](https://docs.nebius.com/cli/reference/compute/instance/create);
the shape of the whole invocation (cloud-init for the SSH key, the `network_interfaces` JSON asking
for a `public_ip_address`) follows the worked CPU/GPU examples in
[Getting started with Compute](https://docs.nebius.com/compute/quickstart) — this pins the same
flags to a small non-GPU box instead of the GPU training/inference examples that page shows. If
your project's default security group is not attached automatically, add
`--network-interfaces` with the security group id (see the reference page) or attach it from the
console after creation.

```sh
export VM_IP=$(nebius compute instance get --id "$VM_ID" --format json \
  | jq -r '.status.network_interfaces[0].public_ip_address.address | split("/")[0]')
echo "$VM_IP"
```

[nebius compute instance get](https://docs.nebius.com/cli/reference/compute/instance/get)
documents the `id` argument / `--id` flag ("ID of the resource to get") and the same global
`--format`. **The `jq` path above is the one part of this snippet not quoted from that page**: it
assumes the address lands under `status.network_interfaces[0].public_ip_address.address`, which is
where the `network_interfaces` shape the `create` call sends puts it. Check the real `get` output
on your first run and adjust the path if your account's response nests it differently.

**Monthly cost:** see [Nebius AI Cloud compute pricing](https://docs.nebius.com/compute/resources/pricing)
for the current `cpu-d3`/`2vcpu-8gb` rate in your region — a number quoted here would go stale the
moment pricing changes, and this repo has no way to keep it current.

---

## 2. Copy the key to the VM (UNRUN)

**Never** put `NEBIUS_API_KEY` in a file this repo tracks, in `demo.env`, or in a build argument.
It reaches the VM as a plain file that only `entrypoint.sh` reads (see `deploy/demo/Dockerfile`),
copied over SSH and never through a shell history or a CI log:

```sh
ssh deploy@"$VM_IP" 'mkdir -p ~/privatemesh/deploy/demo/secrets && chmod 700 ~/privatemesh/deploy/demo/secrets'
scp -p your-nebius-key.txt deploy@"$VM_IP":~/privatemesh/deploy/demo/secrets/nebius_api_key
ssh deploy@"$VM_IP" 'chmod 600 ~/privatemesh/deploy/demo/secrets/nebius_api_key'
```

Then copy (or `git clone` — this repo is **private until submission**, so cloning needs your own
GitHub credential on the VM) the rest of the tree, and on the VM:

```sh
cp deploy/demo/demo.env.example deploy/demo/demo.env
$EDITOR deploy/demo/demo.env      # only if you want different (still-small) caps than the example
```

Install Docker + the compose plugin on the VM (Ubuntu, per Docker's own install docs — not a
Nebius-specific step, so not repeated here). Verify with `docker compose version`.

---

## 3. Start it

```sh
cd ~/privatemesh
docker compose -f deploy/demo/compose.yaml build
PM_DEMO_DOMAIN=your-demo-hostname.example docker compose -f deploy/demo/compose.yaml up -d
```

Point `your-demo-hostname.example`'s DNS `A` record at `$VM_IP` **before** the first `up`, so
Caddy's automatic HTTPS (ACME HTTP-01) can complete on first boot —
[Caddy's automatic HTTPS](https://caddyserver.com/docs/automatic-https). Leaving `PM_DEMO_DOMAIN`
unset defaults to `localhost` and Caddy serves a self-signed cert instead — correct for the local
proof below, not for a public judge-facing URL.

```sh
docker compose -f deploy/demo/compose.yaml ps
curl -s https://your-demo-hostname.example/api/health
```

If the `api` container is not `healthy`, check its logs — the two most likely reasons are the ones
this deploy exists to make loud rather than silent:

```sh
docker compose -f deploy/demo/compose.yaml logs api --tail 50
```

- `NEBIUS_API_KEY not set...` — the secret file at `deploy/demo/secrets/nebius_api_key` is missing,
  empty, or unreadable by the container's non-root user.
- `demo mode refuses to start: this process has real provider credentials configured...` — a
  `TAVILY_API_KEY` or Google credential is present in the compose environment or on the VM's
  filesystem where `google_auth.CREDENTIAL_DEFAULT_PATHS` looks. Remove it; the demo is designed to
  never run beside one.

`api`'s restart policy is `on-failure:3`, not `unless-stopped`: a refusal like the two above is a
configuration fact, not a transient fault, so it stops after three tries instead of restarting
forever. Fix the cause and `docker compose -f deploy/demo/compose.yaml up -d api` again.

---

## 4. Check the spend ledger

```sh
curl -s https://your-demo-hostname.example/api/v2/demo/session -X POST | jq '.spend, .caps'
```

`spend.demo.total_usd` / `.today_usd` are the whole-demo ledger — the numbers `PM_DEMO_TOTAL_USD`
and `PM_DEMO_DAILY_USD` bound. They live in the `demo_ledger` named volume (mounted at
`/home/privatemesh/.privatemesh` in the `api` container) and survive `docker compose restart`;
only `docker compose down -v` (which also deletes it) resets them to zero. To read the ledger
without spending a session:

```sh
docker compose -f deploy/demo/compose.yaml exec api python -c \
  "from privatemesh.spend import SpendLedger; \
   print(SpendLedger('/home/privatemesh/.privatemesh/demo-spend.db').totals())"
```

`SpendLedger.totals()` (`src/privatemesh/spend.py`) returns exactly
`{total_usd, today_usd, cap_total_usd, cap_daily_usd}` — the no-spend read; the `POST` above also
spends nothing extra by itself (starting a session makes no model call), but do prefer the direct
read when you only want the number. Trust `total_usd`/`today_usd` from that call; its
`cap_total_usd`/`cap_daily_usd` come from `Caps.from_env()` (`PM_SPEND_CAP_USD`/
`PM_SPEND_DAILY_USD`, the *general* spend caps this ledger class also serves elsewhere in the
product), not from `PM_DEMO_TOTAL_USD`/`PM_DEMO_DAILY_USD` — for the caps that actually govern this
ledger, read them from `deploy/demo/demo.env` or from the `caps` field the `POST /v2/demo/session`
response already carries.

---

## 5. Rotate or stop the key

**Rotate** (new key, no downtime for the web static build, brief downtime for `api`):

```sh
scp -p new-nebius-key.txt deploy@"$VM_IP":~/privatemesh/deploy/demo/secrets/nebius_api_key
ssh deploy@"$VM_IP" 'chmod 600 ~/privatemesh/deploy/demo/secrets/nebius_api_key'
ssh deploy@"$VM_IP" 'cd ~/privatemesh && docker compose -f deploy/demo/compose.yaml up -d --force-recreate api'
```

**Stop spending entirely** (keeps the ledger and the VM, drops the running containers):

```sh
ssh deploy@"$VM_IP" 'cd ~/privatemesh && docker compose -f deploy/demo/compose.yaml down'
```

**Tear down the VM** (UNRUN — irreversible; only when the demo is retired):

```sh
nebius compute instance delete --id "$VM_ID"
```

[nebius compute instance delete](https://docs.nebius.com/cli/reference/compute/instance/delete)
documents the `id` argument / `--id` flag and `--async`; it deletes the instance and its managed
disks, and fails if any of those disks cannot be deleted — so a project holding an attachment you
did not expect needs that resolved first, not a retry loop.

---

## Why the web app is a Node server here, and what serves the live demo

`web/` is built with `NEXT_PUBLIC_API_MODE=demo` (`web.Dockerfile`), which makes the root page
(`/`) `components/DemoApp.tsx`: a real visitor session against `POST /v2/demo/session` and its two
siblings, driven from `web/lib/demo-api.ts` — see `web/README.md`, "Try it live — the public
demo". The scripted sample workspace (no model) is never removed; it stays reachable at
`/tour`.

This cannot be a static export. A static export is files with no server behind them to reach
`/v2/demo/*` at all, and `web/scripts/build-guards.mjs`'s `assertStaticExportIsSample` guard (run
from `next.config.mjs` at config-load time) refuses `PM_STATIC_EXPORT=1` with anything but
`NEXT_PUBLIC_API_MODE=sample` for exactly that reason. So `web.Dockerfile` never sets
`PM_STATIC_EXPORT`: its runtime stage runs `next start`, a real Node process, not a static file
server.

`lib/demo-api.ts` reads `NEXT_PUBLIC_API_BASE_URL` (unset here, so every `/v2/demo/*` call is
same-origin) — Caddy (see `Caddyfile`) routes `/v2/*` on that same origin to the `api` service, so
the browser never needs to know the API container's address or port. The API is also reachable
directly, through Caddy at `/api/*` (`handle_path` strips the prefix), for a judge or a script to
drive per `docs/API_V2.md`.

`'live'` mode (`NEXT_PUBLIC_API_MODE=live`, `lib/api.ts`) is unrelated to this deploy: it talks to
a v2 host through the **owner-token-gated** routes (`GET /v2/session`, the changes/approval flow,
etc.), none of which the demo host mounts — a demo host answers only `/health`, `/docs` and the
four `/v2/demo/*` routes (`api.py`'s `mount_v1=False` build, `wiring.build_demo`'s docstring), so
building `web/` in `live` mode against this API would be a broken product: every screen past the
first would 404 or refuse.

---

## Local proof (what was actually run and observed)

`docker compose build`; a run with `NEBIUS_API_KEY` absent showing the clean refusal (no crash
loop, no spend); the health endpoint and the web root answering on non-default ports; and
`docker compose down -v` — all four were run and observed on a local Docker host while this
runbook was written. **That says nothing about the Nebius steps above**, every one of which is
still UNRUN. Re-run the four local commands, and the `nebius` ones for the first time, on your own
machine before pointing a public DNS record at the result.
