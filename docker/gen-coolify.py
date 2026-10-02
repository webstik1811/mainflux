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

# MQTT/MQTTS reach nginx's stream proxy through plain TCP forwarders (TLS stays end-to-end to nginx)
net = list(c["services"]["nginx"]["networks"])[0]
for name, port in (("mqtt-proxy", 1883), ("mqtts-proxy", 8883)):
    c["services"][name] = {
        "image": "alpine/socat:1.8.0.3",
        "command": f"TCP-LISTEN:{port},fork,reuseaddr TCP:nginx:{port}",
        "restart": "on-failure",
        "depends_on": ["nginx"],
        "ports": [f"{port}:{port}"],
        "networks": [net],
    }

text = yaml.safe_dump(c, sort_keys=False, width=1000, default_flow_style=False)
text = text.replace("SERVICE_FQDN_NGINX_80: null", "SERVICE_FQDN_NGINX_80:")
text = re.sub(r"ZZPH\[(.+?)\]ZZPH", lambda m: "${" + m.group(1) + "}", text)
header = (
    "# Generated for Coolify from docker/docker-compose.yml + docker/.env.\n"
    "# - Host ports removed (Traefik routes the domain to nginx:80); only MQTT 1883/8883 and CoAP 5683 stay published.\n"
    "# - Secrets come from Coolify magic variables (SERVICE_PASSWORD_*); SMTP settings via MF_EMAIL_* env vars.\n"
    "# Regenerate with: python docker/gen-coolify.py .  (from repo root)\n"
)
open(os.path.join(d, "docker-compose.coolify.yml"), "w", encoding="utf-8", newline="\n").write(header + text)
print("services:", len(c["services"]))
