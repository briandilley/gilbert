# Web Search Service

## Summary
Web search capability exposed as an AI tool. Interface-first design with Tavily Search as the default backend.

## Details

### Architecture
- `WebSearchBackend` ABC in `src/gilbert/interfaces/websearch.py` — `search(query, count)` returns `list[WebSearchResult]`
- `WebSearchService` in `src/gilbert/core/services/websearch.py` — wraps backend as Service + ToolProvider
- `TavilySearch` in `src/gilbert/integrations/tavily_search.py` — Tavily API implementation using httpx

### Tools
1. `web_search` — search the web via Tavily. Parameters: `query` (required), `count` (optional, max 10). Includes Tavily's AI-generated answer summary.
2. `fetch_url` — fetch and extract text content from a web page. Parameters: `url` (required), `include_links` (optional boolean). Uses stdlib `html.parser` for HTML-to-text (no external dependency). Strips scripts/styles, truncates at 50K chars, optionally lists links found.

### Configuration
```yaml
websearch:
  enabled: false
  backend: tavily
  settings:
    api_key: ""  # Tavily API key, inline in backend settings
```

### Credential
Credentials are inline in backend settings (`api_key`). No external CredentialService. API key sent in Tavily request body (not header).

## Related
- [AI Service](memory-ai-service.md) — discovers web_search tool via ToolProvider
- `src/gilbert/interfaces/websearch.py`
- `src/gilbert/core/services/websearch.py`
- `src/gilbert/integrations/tavily_search.py`
