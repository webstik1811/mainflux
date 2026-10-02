"""Render docker/docker-compose.yml into a Coolify-friendly, self-contained compose file."""
import os, re, subprocess, sys, yaml

repo = sys.argv[1]
d = os.path.join(repo, "docker")
DOMAIN = "mainflux.lab.scalewest.com"

# Placeholders survive `docker compose config` as literal text, restored to ${...} afterwards.
def ph(name):
    return f"ZZPH[{name}]ZZPH"

overrides = {
    "MF_AUTH_SECRET": ph("SERVICE_PASSWORD_64_AUTHSECRET"),
    "MF_FILESTORE_SECRET": ph("SERVICE_PASSWORD_64_FILESTORESECRET"),
    "MF_WEBHOOKS_SECRET": ph("SERVICE_PASSWORD_64_WEBHOOKSSECRET"),
    "MF_USERS_ADMIN_EMAIL": ph("MF_USERS_ADMIN_EMAIL:-admin@example.com"),
    "MF_USERS_ADMIN_PASSWORD": ph("SERVICE_PASSWORD_MFADMIN"),
    "MF_HOST": f"https://{DOMAIN}",
    "MF_EMAIL_HOST": ph("MF_EMAIL_HOST:-smtp.example.com"),
    "MF_EMAIL_PORT": ph("MF_EMAIL_PORT:-587"),
    "MF_EMAIL_USERNAME": ph("MF_EMAIL_USERNAME:-"),
    "MF_EMAIL_PASSWORD": ph("MF_EMAIL_PASSWORD:-"),
    "MF_EMAIL_FROM_ADDRESS": ph("MF_EMAIL_FROM_ADDRESS:-noreply@scalewest.com"),
    "MF_EMAIL_FROM_NAME": ph("MF_EMAIL_FROM_NAME:-Mainflux"),
    "MF_RABBITMQ_PASS": ph("SERVICE_PASSWORD_MFRABBIT"),
}

lines = []
for line in open(os.path.join(d, ".env"), encoding="utf-8").read().splitlines():
    m = re.match(r"^([A-Z0-9_]+)=", line)
    if m:
        k = m.group(1)
        if k in overrides:
            line = f"{k}='{overrides[k]}'"
        elif re.fullmatch(r"MF_\w+_DB_PASS", k):
            line = f"{k}='{ph('SERVICE_PASSWORD_MFDB')}'"
    lines.append(line)
env_path = os.path.join(d, ".env.coolify-gen")
open(env_path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

out = subprocess.run(
    ["docker", "compose", "--env-file", ".env.coolify-gen",
     "-f", "docker-compose.yml", "config", "--no-path-resolution", "--format", "yaml"],
    capture_output=True, text=True, cwd=d)
if out.returncode: sys.exit(out.stderr)
out = out.stdout
os.remove(env_path)
c = yaml.safe_load(out)
c.pop("name", None)
# drop compose-project-derived names (docker_...) so Coolify scopes networks/volumes per resource
for section in ("networks", "volumes"):
    for v in (c.get(section) or {}).values():
        if isinstance(v, dict):
            v.pop("name", None)

# nginx must publish no host ports: Coolify routes the domain to the first published port.
KEEP_PORTS = {
    "coap-adapter": ["5683:5683/udp", "5683:5683/tcp"],
}
for name, s in c["services"].items():
    s.pop("container_name", None)
    s.pop("ports", None)
    if name in KEEP_PORTS:
        s["ports"] = KEEP_PORTS[name]
    if name == "nginx":
        # Coolify/Traefik terminates TLS and routes the domain to nginx:80
        # env_file .env got inlined: keep only what entrypoint.sh needs (ports), drop secrets
        s.pop("env_file", None)
        env = {k: v for k, v in s.get("environment", {}).items() if k.endswith("_PORT") or k == "MF_MQTT_CLUSTER"}
        env["SERVICE_FQDN_NGINX_80"] = None
        s["environment"] = env

S = c["services"]
net = list(S["nginx"]["networks"])[0]

# Coolify injects SERVICE_NAME_<x> for every service into every service (grows with n^2) and ships the
# whole compose as one shell argument (~128 KB limit), so keep the service count and the file small.

# 1) One Postgres for all services; the old *-db hostnames stay valid as network aliases.
pg = {n: sv for n, sv in S.items() if str(sv.get("image", "")).startswith("postgres:")}
images = {sv["image"] for sv in pg.values()}
users = {sv["environment"]["POSTGRES_USER"] for sv in pg.values()}
assert len(images) == 1 and len(users) == 1, (images, users)
dbnames = sorted({sv["environment"]["POSTGRES_DB"] for sv in pg.values()})
for n in pg:
    del S[n]
S["mainflux-db"] = {
    "image": images.pop(),
    "restart": "on-failure",
    "environment": {
        "POSTGRES_USER": users.pop(),
        "POSTGRES_PASSWORD": ph("SERVICE_PASSWORD_MFDB"),
        "POSTGRES_DB": "postgres",
        "MF_DATABASES": " ".join(dbnames),
    },
    "volumes": ["mainflux-db-volume:/var/lib/postgresql/data", "./coolify/init-dbs.sh:/docker-entrypoint-initdb.d/init-dbs.sh:ro"],
    "networks": {net: {"aliases": sorted(pg)}},
}
c["volumes"] = {k: v for k, v in c["volumes"].items() if not any(k == f"mainfluxlabs-{n}-volume" for n in pg)}
c["volumes"]["mainflux-db-volume"] = None

# 2) One TCP forwarder for MQTT and MQTTS (TLS stays end-to-end to nginx's stream proxy)
S["mqtt-proxy"] = {
    "image": "alpine/socat:1.8.0.3",
    "entrypoint": ["/bin/sh", "-c"],
    "command": ["socat TCP-LISTEN:8883,fork,reuseaddr TCP:nginx:8883 & exec socat TCP-LISTEN:1883,fork,reuseaddr TCP:nginx:1883"],
    "restart": "on-failure",
    "depends_on": ["nginx"],
    "ports": ["1883:1883", "8883:8883"],
    "networks": [net],
}

# 3) Compact syntax
for n, sv in S.items():
    dep = sv.get("depends_on")
    if isinstance(dep, dict):
        dep = list(dep)
    if dep:
        sv["depends_on"] = sorted({"mainflux-db" if x in pg else x for x in dep})
    vols = []
    for v in sv.get("volumes", []):
        if isinstance(v, dict):
            if v.get("type") == "bind" and not v["source"].startswith((".", "/")):
                v["source"] = "./" + v["source"]  # paths from `extends` files lose their ./ prefix
            v = f"{v['source']}:{v['target']}" + (":ro" if v.get("read_only") else "")
        vols.append(v)
    if vols:
        sv["volumes"] = vols
    if isinstance(sv.get("networks"), dict) and all(x is None for x in sv["networks"].values()):
        sv["networks"] = list(sv["networks"])

# 4) Non-secret MF settings shared by all Mainflux services go to one env file
mf = [sv for sv in S.values() if str(sv.get("image", "")).startswith("mainfluxlabs/")]
seen = {}
for sv in mf:
    for k, v in (sv.get("environment") or {}).items():
        seen.setdefault(k, set()).add(v)
shared = {k: vs.pop() for k, vs in seen.items()
          if len(vs) == 1 and isinstance(next(iter(vs)), str) and "ZZPH" not in next(iter(vs)) and "$" not in next(iter(vs))}
for sv in mf:
    env = {k: v for k, v in (sv.get("environment") or {}).items() if k not in shared}
    if env:
        sv["environment"] = env
    else:
        sv.pop("environment", None)
    sv["env_file"] = ["./coolify/mainflux.env"]
os.makedirs(os.path.join(d, "coolify"), exist_ok=True)
with open(os.path.join(d, "coolify", "mainflux.env"), "w", encoding="utf-8", newline="\n") as f:
    f.write("# Generated by gen-coolify.py: non-secret settings shared by the Mainflux services\n")
    for k in sorted(shared):
        f.write(f"{k}={shared[k]}\n")
with open(os.path.join(d, "coolify", "init-dbs.sh"), "w", encoding="utf-8", newline="\n") as f:
    f.write("""#!/bin/sh
# Creates one database per Mainflux service in the shared Postgres (runs on first start only).
set -e
for db in $MF_DATABASES; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \\"$db\\""
done
""")

text = yaml.safe_dump(c, sort_keys=False, width=1000, default_flow_style=False)
text = re.sub(r" null$", "", text, flags=re.M)
text = text.replace("SERVICE_FQDN_NGINX_80: null", "SERVICE_FQDN_NGINX_80:")
text = re.sub(r"ZZPH\[(.+?)\]ZZPH", lambda m: "${" + m.group(1) + "}", text)
header = (
    "# Generated for Coolify from docker/docker-compose.yml + docker/.env.\n"
    "# - Host ports removed (Traefik routes the domain to nginx:80); MQTT 1883/8883 (via mqtt-proxy) and CoAP 5683 stay published.\n"
    "# - Secrets come from Coolify magic variables (SERVICE_PASSWORD_*); SMTP settings via MF_EMAIL_* env vars.\n"
    "# Regenerate with: python docker/gen-coolify.py .  (from repo root)\n"
)
open(os.path.join(d, "docker-compose.coolify.yml"), "w", encoding="utf-8", newline="\n").write(header + text)
print("services:", len(c["services"]), "bytes:", len(header + text))
