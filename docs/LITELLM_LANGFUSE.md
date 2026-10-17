# LiteLLM & Langfuse Integration Guide

This guide explains how to use LiteLLM and Langfuse with Stych for local LLM hosting and observability.

## Overview

### LiteLLM Integration
- **Default Provider**: LiteLLM is now the default LLM provider
- **Purpose**: Connect to your self-hosted LiteLLM server for private LLM inference
- **Benefits**: Full data privacy, no external API costs, supports 100+ models

### Langfuse Integration
- **Purpose**: Track all LLM calls, agent executions, and system performance
- **Features**: Token usage tracking, latency monitoring, cost estimation, debugging
- **Default**: Enabled in configuration

---

## Architecture

```
User Query
    ↓
Orchestrator Agent
    ↓
LLM Router
    ↓
├─→ LiteLLM (DEFAULT) → Your Local/Remote Server
│   ↓
│   Langfuse Tracking
│
├─→ OpenRouter (Fallback) → Cloud LLMs
│   ↓
│   Langfuse Tracking
│
└─→ Google AI (Fallback) → Google Cloud
    ↓
    Langfuse Tracking
```

---

## Quick Start

### 1. Start LiteLLM Server

Run a LiteLLM proxy (self-hosted or managed) and note its base URL, e.g.:
```bash
# http://localhost:4000
```

### 2. Configure Stych to Use LiteLLM

Edit `backend/.env`:
```bash
# LiteLLM Configuration (DEFAULT PROVIDER)
LITELLM_BASE_URL=http://localhost:4000
LITELLM_API_KEY=sk-your-litellm-key
LITELLM_DEFAULT_MODEL=gpt-4
```

### 3. Configure Langfuse

You already have Langfuse running:
```bash
# Your Langfuse containers:
# Web UI: http://localhost:3110
# Server: http://localhost:3030
```

Edit `backend/.env`:
```bash
# Langfuse Configuration
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-your-public-key
LANGFUSE_SECRET_KEY=sk-your-secret-key
LANGFUSE_HOST=http://localhost:3110
```

### 4. Install Dependencies

```bash
cd backend
pip install -e .
```

This will install:
- `litellm>=1.40.0` - LiteLLM client
- `langfuse>=2.0.0` - Langfuse tracking

### 5. Restart Stych Backend

```bash
cd backend
uvicorn main:app --reload
```

You should see:
```
✅ LiteLLM: Connected successfully
✅ Langfuse tracking initialized
✅ 1/1 LLM provider(s) ready
```

---

## Configuration Options

### LiteLLM Configuration

| Environment Variable | Description | Default | Example |
|-------------------|-------------|----------|----------|
| `LITELLM_BASE_URL` | LiteLLM server URL | `http://localhost:4000` | `http://litellm.internal:4000` |
| `LITELLM_API_KEY` | API key for LiteLLM (optional) | - | `sk-your-litellm-key` |
| `LITELLM_DEFAULT_MODEL` | Default model to use | `gpt-4` | `claude-3.5-sonnet` |

### Provider Selection

Switch providers in `backend/config/config.yaml`:

```yaml
llm:
  provider: "litellm"  # Options: litellm, openrouter, google, private
```

Or via environment variable:
```bash
LITELLM_PROVIDER=openrouter  # Override config.yaml
```

### Langfuse Configuration

| Environment Variable | Description | Required |
|-------------------|-------------|----------|
| `LANGFUSE_ENABLED` | Enable/disable tracking | Yes (default: true) |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key | Yes |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key | Yes |
| `LANGFUSE_HOST` | Langfuse server URL | Yes |

---

## What Gets Tracked?

### LLM Calls
Every LLM request is automatically tracked:
- Model used
- Provider (litellm, openrouter, google)
- Input messages
- Output response
- Token usage (prompt, completion, total)
- Latency (milliseconds)
- Session ID
- Agent name that made the call

### Agent Executions
Agent runs are tracked with:
- Agent name (e.g., "DataAnalysisAgent")
- Task description
- Input data
- Output data
- Execution time
- Session context

### Example in Langfuse Dashboard

```
Trace: llm_call
├─ Generation: litellm_generation
│  ├─ Model: gpt-4
│  ├─ Provider: litellm
│  ├─ Input: [...messages...]
│  ├─ Output: "The analysis shows..."
│  ├─ Usage: {prompt_tokens: 150, completion_tokens: 300, total_tokens: 450}
│  └─ Latency: 1234ms
└─ Metadata: {session_id: "abc-123", agent_name: "DataAnalysisAgent"}
```

---

## Testing the Integration

### Run Test Suite

```bash
cd backend
python test_litellm_langfuse.py
```

This will test:
1. ✅ LiteLLM connectivity
2. ✅ Langfuse tracking
3. ✅ Provider fallback mechanism

Expected output:
```
============================================================
Testing LiteLLM Provider
============================================================

📤 Sending request to LiteLLM...
✅ Success!
   Model: gpt-4
   Content: Hello from LiteLLM!...
   Usage: {'prompt_tokens': 15, 'completion_tokens': 8, 'total_tokens': 23}
   Provider: litellm
```

---

## Using Different Models with LiteLLM

### Available Models

LiteLLM supports 100+ models including:
- **OpenAI**: GPT-4, GPT-3.5, etc.
- **Anthropic**: Claude 3.5 Sonnet, Claude 3 Opus, etc.
- **Google**: Gemini 1.5 Pro, etc.
- **Meta**: Llama 3.1, Llama 2, etc.
- **Mistral**: Mistral Large, Mixtral, etc.
- **Local**: Any model hosted on your server

### Switch Models

Method 1: Per Request (in agent code)
```python
response = await llm_router.generate(
    LLMRequest(
        messages=[...],
        model="claude-3.5-sonnet",  # Override default
        temperature=0.7,
    )
)
```

Method 2: Change Default (in `.env`)
```bash
LITELLM_DEFAULT_MODEL=claude-3.5-sonnet
```

### Model Cost Tracking

Langfuse will track token usage for each model. You can see costs in Langfuse dashboard by configuring pricing.

---

## Provider Fallback

LiteLLM will automatically try fallback models if the primary fails:

```yaml
litellm:
  default_model: "gpt-4"
  fallback_models:
    - "claude-3.5-sonnet"
    - "gemini-1.5-pro"
    - "llama-3.1-70b"
```

Flow:
1. Try `gpt-4` → Fails
2. Try `claude-3.5-sonnet` → Success ✅

---

## Troubleshooting

### Issue: "LiteLLM: Cannot connect to..."

**Problem**: Cannot reach LiteLLM server

**Solutions**:
1. Check LiteLLM is running: `docker ps | grep litellm`
2. Verify URL in `.env`: `LITELLM_BASE_URL`
3. Test connectivity: `curl http://localhost:4000/health`
4. Check Docker network: Ensure Stych can reach LiteLLM

### Issue: "Langfuse tracking disabled"

**Problem**: Langfuse not configured

**Solutions**:
1. Check keys in `.env`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
2. Verify Langfuse is running: `docker ps | grep langfuse`
3. Check Langfuse URL: `LANGFUSE_HOST=http://localhost:3110`
4. Get keys from Langfuse UI → Settings → API Keys

### Issue: "Module not found: litellm"

**Problem**: Dependencies not installed

**Solution**:
```bash
cd backend
pip install -e .
```

### Issue: No tracking in Langfuse dashboard

**Problem**: Events not being sent

**Solutions**:
1. Check `LANGFUSE_ENABLED=true` in `.env`
2. Verify keys are correct (public/secret match)
3. Check Stych logs for "Error logging to Langfuse"
4. Manually test Langfuse connection:
   ```python
   from langfuse import Langfuse
   client = Langfuse(
       public_key="pk-...",
       secret_key="sk-...",
       host="http://localhost:3110"
   )
   print("Langfuse connected!")
   ```

---

## Performance & Cost Optimization

### Reduce Latency

1. **Use Local Models**: Faster than cloud API calls
2. **Reduce Max Tokens**: `max_tokens=2048` instead of `4096`
3. **Increase Temperature**: Higher temperature = fewer thinking tokens

### Reduce Costs (if using paid LLMs)

1. **Track Token Usage**: Langfuse dashboard shows per-model costs
2. **Use Smaller Models**: `gpt-3.5-turbo` instead of `gpt-4`
3. **Cache Responses**: Implement response caching for common queries

---

## Advanced Configuration

### Custom LiteLLM Server

Deploy your own LiteLLM server with custom models:

```bash
# litellm-config.yaml
model_list:
  - model_name: my-local-model
    litellm_params:
      model: my-model-name
      api_base: http://my-server:8080/v1
```

Then configure Stych:
```bash
LITELLM_BASE_URL=http://my-litellm-server:4000
```

### Langfuse Cloud vs Self-Hosted

**Self-Hosted (Current Setup)**:
```bash
LANGFUSE_HOST=http://localhost:3110
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
```

**Langfuse Cloud**:
```bash
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-your-cloud-key
LANGFUSE_SECRET_KEY=sk-your-cloud-key
```

---

## Monitoring & Observability

### Langfuse Dashboard Features

1. **Traces**: View all LLM calls and agent executions
2. **Usage Analytics**: Token usage per model, agent, user
3. **Latency Monitoring**: Track slow requests
4. **Cost Tracking**: Estimate LLM costs (configure pricing)
5. **Error Tracking**: See failed LLM calls
6. **Session Replay**: Replay full conversation flows

### Key Metrics to Track

- **P50 Latency**: 50th percentile request time
- **P95 Latency**: 95th percentile request time
- **Error Rate**: % of failed LLM calls
- **Cost per Session**: Average cost per user session
- **Tokens per Query**: Average tokens used

---

## Security Considerations

### LiteLLM Security

- ✅ Data stays in your infrastructure
- ✅ No data sent to external APIs
- ✅ API key optional for local deployments
- ⚠️ Secure your LiteLLM server with authentication

### Langfuse Security

- ✅ Traces contain no PII by default
- ✅ Can enable PII redaction
- ✅ Access control per user/session
- ⚠️ Use HTTPS for cloud Langfuse

### Best Practices

1. **Never commit API keys** to git
2. **Use separate keys** for dev/staging/production
3. **Rotate keys** regularly
4. **Enable audit logging** in Langfuse
5. **Monitor for anomalies** (sudden cost spikes, error rates)

---

## Migration from OpenRouter

To migrate from OpenRouter to LiteLLM:

1. ✅ Update `.env`:
   ```bash
   # OLD: OPENROUTER_API_KEY=sk-or-...
   # NEW:
   LITELLM_BASE_URL=http://your-server:4000
   LITELLM_API_KEY=sk-...
   ```

2. ✅ Update `config.yaml`:
   ```yaml
   # OLD:
   # provider: "openrouter"
   # NEW:
   provider: "litellm"
   ```

3. ✅ Restart backend
4. ✅ Verify in logs: "LiteLLM: Connected successfully"

---

## Next Steps

1. ✅ Configure LiteLLM connection
2. ✅ Configure Langfuse tracking
3. ✅ Run test suite: `python test_litellm_langfuse.py`
4. ✅ Monitor in Langfuse dashboard
5. ✅ Test with real data: upload CSV and query
6. ✅ Review token usage and optimize

---

## Support

- **LiteLLM Docs**: https://docs.litellm.ai/
- **Langfuse Docs**: https://langfuse.com/docs
- **Stych Issues**: https://github.com/your-repo/issues
