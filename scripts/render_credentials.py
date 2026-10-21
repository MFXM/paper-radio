#!/usr/bin/env python3
"""Render n8n credentials (plain JSON) from .env for `n8n import:credentials`.

n8n encrypts the data with N8N_ENCRYPTION_KEY during import. The rendered file
contains secrets: bootstrap.sh writes it to a temporary file and deletes it.
Credential IDs are fixed so the workflow JSON files can reference them.
"""
import json, os, sys

def env_file(path):
    vals = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k.strip()] = v
    return vals

E = {**env_file(sys.argv[1] if len(sys.argv) > 1 else ".env"), **{k: v for k, v in os.environ.items() if k.startswith("BOOT_")}}
g = lambda k, d="": E.get(k) or d

def cred(id_, name, type_, data):
    return {"id": id_, "name": name, "type": type_, "data": data}

creds = [
    cred("pipelinePgCred01", "Pipeline Postgres", "postgres", {
        "host": "postgres", "port": 5432, "database": g("PIPELINE_DB_NAME", "pipeline"),
        "user": g("POSTGRES_USER", "n8n"), "password": g("POSTGRES_PASSWORD"), "ssl": "disable"}),
    cred("qdrantApiCred001", "Qdrant", "qdrantApi", {"qdrantUrl": "http://qdrant:6333", "apiKey": g("QDRANT_API_KEY")}),
    cred("ollamaEmbedCred1", "Ollama (embeddings)", "ollamaApi", {"baseUrl": "http://ollama:11434"}),
    cred("llmGatewayCred01", "LLM gateway (Ollama API)", "ollamaApi", {
        "baseUrl": g("LLM_GATEWAY_URL", "http://ollama:11434").rstrip("/"), "apiKey": g("LLM_GATEWAY_API_KEY")}),
    cred("scoutLlmCred0001", "Scout LLM (OpenAI-compatible)", "openAiApi", {
        "apiKey": g("SCOUT_LLM_API_KEY"), "url": g("SCOUT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")}),
    cred("openRouterCred01", "OpenRouter (fallback)", "openRouterApi", {
        "apiKey": g("OPENROUTER_API_KEY"), "url": "https://openrouter.ai/api/v1"}),
    cred("geminiApiCred001", "Google Gemini", "googlePalmApi", {
        "host": "https://generativelanguage.googleapis.com", "apiKey": g("GEMINI_API_KEY")}),
    cred("mattermostBot001", "Mattermost bot", "mattermostApi", {
        "accessToken": g("MATTERMOST_BOT_TOKEN"), "baseUrl": g("MATTERMOST_URL").rstrip("/"), "allowUnauthorizedCerts": False}),
    cred("zoteroApiCred001", "Zotero API key", "httpHeaderAuth", {"name": "Zotero-API-Key", "value": g("ZOTERO_API_KEY")}),
    cred("n8nApiKeyCred001", "n8n API key", "httpHeaderAuth", {"name": "X-N8N-API-KEY", "value": g("N8N_API_KEY")}),
]
# Strip empty optional API keys so n8n does not send empty auth headers
for c in creds:
    if c["type"] == "ollamaApi" and not c["data"].get("apiKey"):
        c["data"].pop("apiKey", None)

missing = [c["name"] for c in creds if any(v == "" for k, v in c["data"].items() if k in ("password", "apiKey", "accessToken", "value"))]
json.dump(creds, sys.stdout, indent=2)
if missing:
    print("note: empty secret in: " + ", ".join(missing) + " (fill in later in the n8n UI or .env)", file=sys.stderr)
