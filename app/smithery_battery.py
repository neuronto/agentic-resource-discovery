"""A wider query battery for Smithery.

The shared PROBE_QUERIES battery (92 general capability terms) reached 34.6% of the
15,470 servers Smithery reports. Each `?q=` opens its own window, so more distinct
queries reach more of the catalogue. These are deliberately different in KIND from the
capability battery: vendor and platform names, file and data formats, industry verticals,
and single letters, which between them pull in servers whose descriptions share no
capability vocabulary with the originals.
"""

EXTRA = [
    # vendors and platforms
    "notion", "airtable", "jira", "confluence", "asana", "trello", "monday", "clickup",
    "hubspot", "salesforce", "zendesk", "intercom", "shopify", "stripe", "paypal",
    "quickbooks", "xero", "netsuite", "sap", "oracle", "snowflake", "databricks",
    "bigquery", "redshift", "mongodb", "redis", "elasticsearch", "supabase", "firebase",
    "cloudflare", "vercel", "netlify", "heroku", "digitalocean", "azure", "gcp",
    "openai", "anthropic", "gemini", "ollama", "huggingface", "replicate", "langchain",
    "llamaindex", "pinecone", "weaviate", "qdrant", "chroma", "milvus",
    "wordpress", "webflow", "contentful", "sanity", "strapi", "ghost",
    "figma", "canva", "adobe", "blender", "unity", "unreal",
    "zoom", "teams", "outlook", "gmail", "drive", "dropbox", "box", "onedrive",
    "jenkins", "gitlab", "bitbucket", "circleci", "terraform", "ansible", "pulumi",
    "grafana", "prometheus", "datadog", "sentry", "splunk", "elastic",
    "twilio", "sendgrid", "mailchimp", "klaviyo", "segment", "amplitude", "mixpanel",
    "posthog", "plausible", "matomo",
    # formats and data
    "csv", "json", "xml", "yaml", "markdown", "excel", "word", "powerpoint",
    "image", "screenshot", "ocr", "qr code", "barcode", "graph", "chart", "diagram",
    "geospatial", "gis", "satellite", "timeseries", "streaming", "queue", "webhook",
    "api gateway", "graphql", "grpc", "websocket", "cron", "etl", "pipeline",
    # verticals
    "insurance", "banking", "mortgage", "tax", "payroll", "procurement", "logistics",
    "warehouse", "manufacturing", "agriculture", "energy", "utilities", "telecom",
    "automotive", "aviation", "maritime", "construction", "architecture", "nonprofit",
    "government", "public sector", "library", "museum", "church", "fitness",
    "nutrition", "mental health", "pharmacy", "dentistry", "veterinary", "genomics",
    "clinical trial", "biotech", "chemistry", "physics", "astronomy", "climate",
    "weather forecast", "earthquake", "wildlife", "ocean",
    # single letters, which catch names sharing no vocabulary at all
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    # generic shapes
    "mcp server", "server", "tool", "client", "sdk", "cli", "bot", "assistant",
    "connector", "integration", "plugin", "bridge", "proxy", "gateway", "wrapper",
]
