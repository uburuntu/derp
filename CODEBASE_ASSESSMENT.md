# Codebase Assessment: Prompt Building & Gemini Usage

**Assessment Date**: 2025-10-30
**Focus Areas**: Prompt engineering, Gemini API integration, optimization opportunities

---

## Executive Summary

This is a well-architected Telegram bot powered by Google's Gemini API. The codebase demonstrates good software engineering practices with clean separation of concerns, proper typing, and observability. However, there are significant opportunities for optimization in prompt management, context handling, and cost control.

**Overall Architecture Quality**: ⭐⭐⭐⭐ (4/5)
**Prompt Engineering Maturity**: ⭐⭐⭐ (3/5)
**API Usage Efficiency**: ⭐⭐⭐ (3/5)

---

## Current Implementation Analysis

### 1. Prompt Building Architecture

#### System Prompt (`llm_gemini.py:215-268`)
**Location**: `derp/common/llm_gemini.py`

**Structure**:
```python
_BASE_SYSTEM_PROMPT = (
    # 580+ lines of personality definition
    "You are Derp, a helpful and conversational assistant..."
)
```

**Strengths**:
- Comprehensive personality definition
- Clear behavioral guidelines
- Well-structured sections (identity, communication, personalization)
- Natural language instructions

**Weaknesses**:
- ❌ Hardcoded as single monolithic string
- ❌ No versioning or A/B testing capability
- ❌ No prompt compression or optimization
- ❌ Same prompt for all chats/users (no personalization)
- ❌ No prompt template system
- ❌ ~580 lines consume significant token budget on every request

#### Context Building (`gemini.py:90-153`)
**Location**: `derp/handlers/gemini.py`

**Current Flow**:
```
1. Chat metadata (JSON dump)
2. Chat memory (if set, max 1024 chars)
3. Recent message history (up to 100 messages)
4. Current message
```

**Strengths**:
- Includes relevant chat context
- Uses cleaned MessageLog for history
- Structured JSON format

**Weaknesses**:
- ❌ No token counting or size limits
- ❌ Can include up to 100 messages = potentially huge context
- ❌ No intelligent message filtering or summarization
- ❌ No context window management
- ❌ Full JSON dumps include unnecessary metadata
- ❌ No context relevance scoring

**Estimated Token Usage**:
- System prompt: ~800-1000 tokens
- Chat metadata: ~50-100 tokens
- Recent history (100 msgs): ~10,000-50,000 tokens
- Current message: ~100-500 tokens
- **Total per request**: 11,000-51,500 tokens input

### 2. Gemini API Integration

#### Request Builder Pattern (`llm_gemini.py:212-410`)

**Strengths**:
- ✅ Clean builder pattern
- ✅ Method chaining for composability
- ✅ Proper separation of concerns
- ✅ Support for multiple content types (text, media, tools)

**Current Implementation**:
```python
request = (
    self.gemini.create_request()
    .with_google_search()
    .with_url_context()
    .with_text(context)
    .with_media(...)
)
```

#### API Key Management (`config.py:77-83`)

**Current Strategy**:
```python
@property
def google_api_key_iter(self) -> Iterable[str]:
    return itertools.cycle(self.google_api_keys)
```

**Strengths**:
- ✅ Round-robin rotation through multiple keys
- ✅ Simple and effective for basic load distribution

**Weaknesses**:
- ❌ No rate limit tracking per key
- ❌ No automatic retry with backoff
- ❌ No key health monitoring
- ❌ No fallback to different models on failure
- ❌ No cost tracking per key

#### Tool System (`llm_gemini.py:47-129`)

**Strengths**:
- ✅ Automatic function declaration generation
- ✅ Type hint to schema mapping
- ✅ Clean dependency injection
- ✅ Max 3 iterations for function calls

**Weaknesses**:
- ❌ Limited type support (str, int, float, bool only)
- ❌ No support for complex types (lists, dicts, enums)
- ❌ No tool usage analytics
- ❌ No conditional tool availability
- ❌ Tools always available (no context-based filtering)

### 3. Memory Management (`tools/memory.py`)

**Current Implementation**:
- Hard limit: 1024 characters
- No summarization or compression
- Manual updates by LLM

**Weaknesses**:
- ❌ Very small capacity (1KB)
- ❌ No automatic pruning of old information
- ❌ No semantic chunking or prioritization
- ❌ No memory retrieval strategies (always full memory)

---

## Critical Issues

### 🔴 High Priority

1. **Context Explosion Risk**
   - **Issue**: Up to 100 messages can be included in context
   - **Impact**: Potential 50K+ input tokens per request
   - **Cost**: At Gemini 2.0 Flash pricing (~$0.075/1M input tokens), this is $0.00375 per request with full history
   - **Risk**: Some requests may exceed model context window

2. **No Prompt Caching**
   - **Issue**: System prompt (800-1000 tokens) sent fresh every request
   - **Impact**: Wasted tokens and latency
   - **Solution Available**: Gemini supports context caching

3. **No Token/Cost Tracking**
   - **Issue**: No monitoring of actual token usage or costs
   - **Impact**: Cannot optimize without metrics
   - **Risk**: Unexpected API bills

### 🟡 Medium Priority

4. **Static System Prompt**
   - **Issue**: Same 580-line prompt for all users/chats
   - **Impact**: Missed personalization opportunities
   - **Limitation**: Cannot experiment with prompt variations

5. **No Error Recovery Strategy**
   - **Issue**: Basic exception handling without retry logic
   - **Impact**: Poor user experience during API issues
   - **Code**: `gemini.py:295-301` just shows generic error

6. **Inefficient Context Building**
   - **Issue**: Full JSON dumps with unnecessary fields
   - **Example**: `exclude_defaults=True, exclude_none=True, exclude_unset=True` helps but still verbose
   - **Impact**: Wasted tokens on metadata

### 🟢 Low Priority

7. **No Prompt Versioning**
   - **Issue**: Changes to system prompt affect all users immediately
   - **Impact**: Cannot rollback or A/B test

8. **Limited Tool Type Support**
   - **Issue**: Only basic types (str, int, float, bool)
   - **Impact**: Cannot build complex tools

---

## Valuable Improvement Ideas

### 💎 Quick Wins (High Impact, Low Effort)

#### 1. Implement Prompt Caching
**Impact**: 50-60% reduction in input tokens
**Effort**: Low
**ROI**: ⭐⭐⭐⭐⭐

```python
# Gemini supports context caching for prompts >1024 tokens
config = types.GenerateContentConfig(
    tools=gemini_tools,
    system_instruction=system_instruction,
    cached_content=cached_content_name,  # Add this
)
```

**Benefits**:
- Reduce system prompt tokens from ~1000 to ~10 per request
- Lower latency (cached prompts are faster)
- Significant cost savings

**Implementation**:
1. Create cached content with system prompt + static context
2. Reuse cache across requests for same chat
3. Refresh cache every 5-10 minutes or when updated

#### 2. Add Token Counting & Cost Tracking
**Impact**: Visibility into actual usage
**Effort**: Low
**ROI**: ⭐⭐⭐⭐

```python
# After each request
logfire.info(
    "gemini_request_complete",
    input_tokens=response.usage_metadata.prompt_token_count,
    output_tokens=response.usage_metadata.candidates_token_count,
    total_tokens=response.usage_metadata.total_token_count,
    estimated_cost=calculate_cost(response.usage_metadata),
)
```

**Benefits**:
- Identify expensive requests
- Track costs per user/chat
- Optimize based on real data

#### 3. Smart Context Windowing
**Impact**: 70-80% reduction in context tokens
**Effort**: Medium
**ROI**: ⭐⭐⭐⭐⭐

```python
async def _build_smart_context(
    message: Message,
    chat_settings: ChatSettingsResult | None,
    max_messages: int = 20,  # Reduce from 100
    max_context_tokens: int = 4000,  # Set hard limit
) -> str:
    """Build context with intelligent filtering."""

    # Priority order:
    # 1. Current message (always included)
    # 2. Chat memory (always included if set)
    # 3. Recent messages with relevance scoring
    # 4. Messages directly replied to

    context_parts = []
    estimated_tokens = 0

    # Add chat metadata (minimal)
    chat_info = {
        "id": message.chat.id,
        "type": message.chat.type,
        "title": message.chat.title,
    }
    context_parts.append(f"# CHAT\n{json.dumps(chat_info)}")
    estimated_tokens += 50

    # Add memory (always include)
    if chat_settings and chat_settings.llm_memory:
        context_parts.append(f"# MEMORY\n{chat_settings.llm_memory}")
        estimated_tokens += len(chat_settings.llm_memory) // 4

    # Add recent messages with sliding window
    recent_msgs = await select_recent_messages(
        executor, chat_id=message.chat.id, limit=max_messages
    )

    # Filter and summarize if needed
    selected_messages = []
    for msg in reversed(recent_msgs):  # Most recent first
        msg_tokens = estimate_tokens(msg.text or "")
        if estimated_tokens + msg_tokens > max_context_tokens - 500:
            break
        selected_messages.append(msg)
        estimated_tokens += msg_tokens

    if selected_messages:
        context_parts.append("# RECENT HISTORY")
        context_parts.extend(format_message_compact(m) for m in selected_messages)

    return "\n".join(context_parts)
```

**Benefits**:
- Predictable token usage
- Focus on relevant messages
- Better performance (less noise)

#### 4. Implement Rate Limit Handling
**Impact**: Better reliability
**Effort**: Low
**ROI**: ⭐⭐⭐⭐

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(RateLimitError),
)
async def execute_with_retry(self) -> GeminiResult:
    """Execute with automatic retry on rate limits."""
    try:
        return await self._execute_internal()
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            # Rotate to next key and retry
            self.client = genai.Client(api_key=next(settings.google_api_key_iter))
            raise
        raise
```

### 🚀 Medium-Term Improvements (High Impact, Medium Effort)

#### 5. Dynamic Prompt System
**Impact**: Flexibility and experimentation
**Effort**: Medium
**ROI**: ⭐⭐⭐⭐

```
derp/prompts/
├── __init__.py
├── base.py              # Base prompt components
├── personalities/       # Different bot personalities
│   ├── default.yaml
│   ├── professional.yaml
│   └── casual.yaml
├── templates/           # Prompt templates with variables
│   ├── chat_response.jinja2
│   └── image_gen.jinja2
└── versions/            # Versioned prompts for A/B testing
    ├── v1.yaml
    ├── v2.yaml
    └── experiments/
```

**Features**:
- YAML-based prompt definitions
- Jinja2 templates for dynamic injection
- Per-chat personality selection
- A/B testing framework
- Prompt version control
- Easy updates without code changes

**Example YAML**:
```yaml
# prompts/personalities/default.yaml
version: "2.0"
identity:
  name: "Derp"
  handle: "@DerpRobot"
  personality: "helpful, conversational, adaptable"

communication:
  response_length:
    default: 200
    simple: "1-3 sentences"
    complex: "prioritize concise high-signal answer"

  tone: "concise, friendly, clear"

  markdown_rules:
    - "Use **bold**, *italic*, __underline__"
    - "Lists with dashes (-) not asterisks (*)"

capabilities:
  - google_search
  - url_context
  - memory_management

# Load dynamically
prompt = PromptManager.load("default", version="2.0")
```

#### 6. Semantic Message Filtering
**Impact**: Better context quality
**Effort**: Medium
**ROI**: ⭐⭐⭐⭐

```python
from sentence_transformers import SentenceTransformer

class ContextBuilder:
    def __init__(self):
        self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

    async def build_semantic_context(
        self,
        current_message: str,
        recent_messages: list[Message],
        max_messages: int = 15,
    ) -> list[Message]:
        """Select most relevant messages using semantic similarity."""

        # Encode current message
        current_embedding = self.encoder.encode(current_message)

        # Score all messages by relevance
        scored_messages = []
        for msg in recent_messages:
            if not msg.text:
                continue

            msg_embedding = self.encoder.encode(msg.text)
            similarity = cosine_similarity(current_embedding, msg_embedding)

            # Recency boost
            recency_score = 1.0 / (1 + msg.age_in_minutes / 60)

            final_score = similarity * 0.7 + recency_score * 0.3
            scored_messages.append((msg, final_score))

        # Return top-k most relevant
        scored_messages.sort(key=lambda x: x[1], reverse=True)
        return [msg for msg, _ in scored_messages[:max_messages]]
```

**Benefits**:
- Include only relevant conversation history
- Reduce noise in context
- Better responses with less input

#### 7. Advanced Memory System
**Impact**: Better long-term context
**Effort**: Medium-High
**ROI**: ⭐⭐⭐⭐

```python
class MemoryManager:
    """Hierarchical memory system with automatic summarization."""

    def __init__(self):
        self.short_term_limit = 1024      # Current chat memory
        self.mid_term_limit = 4096        # Session summary
        self.long_term_limit = 16384      # User/chat profile

    async def update_memory(
        self,
        chat_id: int,
        new_facts: list[str],
    ) -> None:
        """Update memory with automatic compression."""

        current_memory = await self.load_memory(chat_id)

        # Add new facts
        updated_memory = current_memory + "\n".join(new_facts)

        # If over limit, compress using LLM
        if len(updated_memory) > self.short_term_limit:
            compressed = await self._compress_memory(updated_memory)
            await self.save_memory(chat_id, compressed)
        else:
            await self.save_memory(chat_id, updated_memory)

    async def _compress_memory(self, memory: str) -> str:
        """Use Gemini to compress memory while preserving key facts."""
        prompt = f"""
        Compress the following memory to under 1000 characters while
        preserving the most important facts and context:

        {memory}

        Focus on:
        - User preferences and personal information
        - Important ongoing topics
        - Key relationships and context
        - Remove redundant or outdated information
        """

        result = await self.gemini.create_request()
            .with_text(prompt)
            .with_model("gemini-2.0-flash-8b")  # Use cheaper model
            .execute()

        return result.text_parts[0][:1024]
```

**Features**:
- Automatic memory compression
- Hierarchical memory (short/mid/long term)
- Smart pruning of outdated info
- Semantic search in memory

#### 8. Response Streaming
**Impact**: Better user experience
**Effort**: Medium
**ROI**: ⭐⭐⭐

```python
async def handle_with_streaming(self) -> Any:
    """Stream responses as they're generated."""

    # Send initial "thinking" message
    status_msg = await self.event.reply("💭 Thinking...")

    # Stream the response
    accumulated_text = ""
    last_update = time.time()

    async for chunk in self.gemini.create_request().stream():
        if chunk.text:
            accumulated_text += chunk.text

            # Update message every 1 second or every 100 chars
            if time.time() - last_update > 1.0 or len(accumulated_text) % 100 == 0:
                await status_msg.edit_text(accumulated_text[:4000])
                last_update = time.time()

    # Final update with complete response
    await status_msg.edit_text(accumulated_text[:4000])
```

**Benefits**:
- Users see responses as they're generated
- Perceived latency reduction
- Better for long responses

### 🎯 Long-Term Strategic Improvements (High Impact, High Effort)

#### 9. Multi-Model Orchestration
**Impact**: Cost optimization and capability expansion
**Effort**: High
**ROI**: ⭐⭐⭐⭐

**Strategy**:
```python
class ModelRouter:
    """Route requests to optimal model based on requirements."""

    MODELS = {
        "simple": "gemini-2.0-flash-8b",      # Cheap, fast
        "standard": "gemini-2.0-flash",       # Balanced
        "complex": "gemini-2.0-pro",          # Expensive, capable
        "vision": "gemini-2.5-flash-image",   # Image understanding
    }

    async def route_request(self, message: Message) -> str:
        """Determine optimal model for request."""

        # Simple queries → cheap model
        if self._is_simple_query(message.text):
            return self.MODELS["simple"]

        # Has media → vision model
        if message.photo or message.video:
            return self.MODELS["vision"]

        # Complex reasoning needed → pro model
        if self._requires_deep_reasoning(message.text):
            return self.MODELS["complex"]

        # Default
        return self.MODELS["standard"]

    def _is_simple_query(self, text: str) -> bool:
        """Detect simple queries that don't need powerful models."""
        simple_patterns = [
            r"^what is",
            r"^define",
            r"^who is",
            r"^when did",
            r"^how many",
        ]
        return any(re.match(p, text.lower()) for p in simple_patterns)
```

**Cost Savings**:
- Route 60% of queries to Flash-8B (5x cheaper)
- Use Pro only when needed (10% of queries)
- Estimated savings: 40-50% on API costs

#### 10. Prompt Optimization Pipeline
**Impact**: Continuous improvement
**Effort**: High
**ROI**: ⭐⭐⭐⭐⭐

**Components**:
1. **A/B Testing Framework**
   ```python
   class PromptExperiment:
       def __init__(self, name: str, variants: dict[str, str]):
           self.name = name
           self.variants = variants
           self.metrics = defaultdict(list)

       async def get_variant(self, chat_id: int) -> str:
           """Assign chat to variant (consistent hashing)."""
           variant_idx = chat_id % len(self.variants)
           return list(self.variants.keys())[variant_idx]

       async def track_result(self, variant: str, metrics: dict):
           """Track success metrics for variant."""
           self.metrics[variant].append(metrics)
   ```

2. **Automated Prompt Optimization**
   - Use DSPy or similar frameworks
   - Automatically optimize prompts based on success metrics
   - Learn from user feedback (reactions, edits, complaints)

3. **Metrics to Track**:
   - Response quality (user reactions, thumbs up/down)
   - Task completion rate
   - User engagement (reply rate, conversation length)
   - Token efficiency (output tokens / input tokens)
   - Latency
   - Cost per conversation

#### 11. Context Compression with Small Models
**Impact**: Massive token savings
**Effort**: High
**ROI**: ⭐⭐⭐⭐⭐

```python
class ContextCompressor:
    """Compress large contexts using smaller models."""

    async def compress_history(
        self,
        messages: list[Message],
        target_tokens: int = 500,
    ) -> str:
        """Summarize message history to target token count."""

        # Format messages
        history_text = "\n".join(
            f"{msg.from_user.display_name}: {msg.text}"
            for msg in messages
            if msg.text
        )

        # Use cheap model for compression
        prompt = f"""
        Summarize this conversation history in {target_tokens} tokens or less.
        Focus on key topics, decisions, and context needed for continuation.

        {history_text}
        """

        result = await Gemini(api_key=settings.google_api_key).create_request()
            .with_text(prompt)
            .with_model("gemini-2.0-flash-8b")
            .execute()

        return result.text_parts[0]
```

**Benefits**:
- Compress 50-100 messages into 500 tokens
- Maintain conversation continuity
- Reduce input costs by 80-90%

#### 12. Tool Usage Analytics & Optimization
**Impact**: Better tool design
**Effort**: Medium
**ROI**: ⭐⭐⭐

```python
class ToolAnalytics:
    """Track and analyze tool usage patterns."""

    async def track_tool_call(
        self,
        tool_name: str,
        args: dict,
        result: str,
        success: bool,
        execution_time: float,
    ):
        """Log tool usage for analysis."""
        await self.db.insert_tool_usage(
            tool_name=tool_name,
            args=args,
            result=result,
            success=success,
            execution_time=execution_time,
            timestamp=datetime.now(),
        )

    async def get_tool_stats(self) -> dict:
        """Analyze tool usage patterns."""
        return {
            "most_used": await self._most_used_tools(),
            "success_rate": await self._tool_success_rates(),
            "avg_execution_time": await self._avg_execution_times(),
            "failure_patterns": await self._analyze_failures(),
        }
```

**Insights**:
- Which tools are actually useful?
- Which tools fail frequently?
- Are tool descriptions clear enough?
- Can we combine/simplify tools?

---

## Implementation Roadmap

### Phase 1: Quick Wins (Week 1-2)
- [ ] Add token counting and cost tracking
- [ ] Implement smart context windowing (reduce from 100 to 20 messages)
- [ ] Add rate limit handling with retry logic
- [ ] Set up prompt caching for system prompt

**Expected Impact**: 60-70% reduction in costs, better reliability

### Phase 2: Core Optimizations (Week 3-6)
- [ ] Build dynamic prompt system (YAML-based)
- [ ] Implement semantic message filtering
- [ ] Add response streaming
- [ ] Create advanced memory system with compression

**Expected Impact**: Better user experience, more personalization

### Phase 3: Advanced Features (Month 2-3)
- [ ] Multi-model orchestration
- [ ] A/B testing framework for prompts
- [ ] Context compression pipeline
- [ ] Tool analytics and optimization

**Expected Impact**: 40-50% additional cost savings, continuous improvement

---

## Cost Analysis & Projections

### Current Estimated Costs (per 1000 requests)

**Assumptions**:
- Model: gemini-2.0-flash
- Pricing: $0.075/1M input tokens, $0.30/1M output tokens
- Average input: 15,000 tokens (system prompt + context)
- Average output: 500 tokens

**Current Cost**:
```
Input:  1000 * 15,000 tokens * $0.075/1M = $1.125
Output: 1000 * 500 tokens * $0.30/1M = $0.150
Total:  $1.275 per 1000 requests
```

**Monthly at 10K requests/day**:
```
10,000 requests/day * 30 days * $1.275/1000 = $382.50/month
```

### Projected Costs After Optimizations

**Phase 1 Optimizations** (prompt caching + context windowing):
- Input reduced from 15,000 to 4,000 tokens
- Cached prompt reduces recurring tokens by 80%

```
Input:  1000 * 4,000 tokens * $0.075/1M = $0.300
Output: 1000 * 500 tokens * $0.30/1M = $0.150
Total:  $0.450 per 1000 requests (65% reduction)
Monthly: $135/month (saving $247.50/month)
```

**Phase 3 Optimizations** (multi-model routing):
- 60% of requests use Flash-8B (5x cheaper)
- 10% use Pro (more expensive but necessary)
- 30% use standard Flash

```
Weighted cost: $0.250 per 1000 requests (80% reduction from baseline)
Monthly: $75/month (saving $307.50/month from baseline)
```

---

## Monitoring & Success Metrics

### Key Metrics to Track

1. **Cost Metrics**
   - Cost per request
   - Cost per user/chat
   - Token usage (input/output)
   - Model distribution

2. **Performance Metrics**
   - Response latency (p50, p95, p99)
   - Cache hit rate (for prompt caching)
   - Context compression ratio
   - Function call success rate

3. **Quality Metrics**
   - User satisfaction (reactions, feedback)
   - Conversation continuation rate
   - Task completion rate
   - Error rate

4. **Operational Metrics**
   - API error rate
   - Rate limit hits
   - Key rotation effectiveness
   - System uptime

### Recommended Dashboards

**Logfire Dashboard Structure**:
```
1. Cost Overview
   - Daily/monthly spend
   - Cost per chat/user
   - Token usage trends

2. Performance
   - Latency distribution
   - Cache hit rates
   - Error rates

3. Usage Patterns
   - Requests by model
   - Tool usage frequency
   - Context sizes

4. Quality
   - User satisfaction scores
   - Conversation metrics
   - Feature adoption
```

---

## Specific Code Recommendations

### 1. Extract System Prompt to Configuration
**Current**: `llm_gemini.py:215-268`

**Recommended**:
```python
# derp/prompts/system_prompt.yaml
version: "1.0"
identity:
  name: "Derp"
  handle: "@DerpRobot"
  personality: "helpful, conversational, adaptable, context-aware, naturally opinionated"

communication:
  language: "always respond in user's language"
  markdown: "**bold**, *italic*, __underline__, ~~strikethrough~~"
  lists: "use dashes (-) only"
  response_length:
    default: "under 200 words"
    simple: "1-3 sentences"
    complex: "concise high-signal answer first"

  tone:
    - "concise, friendly, clear"
    - "match user's tone and energy"
    - "appropriate humor for jokes/sarcasm"

  style:
    - "avoid lists in casual conversation"
    - "use natural sentences and paragraphs"
    - "no generic follow-up questions"

...
```

### 2. Add Config for Context Management
**New**: `derp/config.py`

```python
class Settings(BaseSettings):
    # ... existing fields ...

    # Context management
    max_context_messages: int = 20  # Reduced from 100
    max_context_tokens: int = 4000
    enable_semantic_filtering: bool = False
    enable_context_compression: bool = True

    # Prompt optimization
    enable_prompt_caching: bool = True
    prompt_cache_ttl: int = 300  # 5 minutes

    # Cost controls
    max_cost_per_request: float = 0.01  # $0.01
    enable_model_routing: bool = True

    # Monitoring
    track_token_usage: bool = True
    track_tool_usage: bool = True
```

### 3. Refactor Context Builder
**Current**: `derp/handlers/gemini.py:90-153`

**Recommended**:
```python
# derp/common/context_builder.py

from dataclasses import dataclass
from typing import Protocol

@dataclass
class ContextConfig:
    max_messages: int = 20
    max_tokens: int = 4000
    include_memory: bool = True
    include_chat_metadata: bool = True
    compact_mode: bool = True

class ContextStrategy(Protocol):
    """Strategy for building context."""
    async def build(self, message: Message, config: ContextConfig) -> str:
        ...

class CompactContextBuilder:
    """Build minimal context with token budget."""

    async def build(
        self,
        message: Message,
        config: ContextConfig,
        chat_settings: ChatSettingsResult | None = None,
    ) -> str:
        context_parts = []
        token_budget = config.max_tokens

        # 1. Minimal chat metadata
        if config.include_chat_metadata:
            chat_info = {
                "id": message.chat.id,
                "type": message.chat.type,
            }
            if message.chat.title:
                chat_info["title"] = message.chat.title

            context_parts.append(f"# CHAT\n{json.dumps(chat_info)}")
            token_budget -= 50

        # 2. Memory (priority)
        if config.include_memory and chat_settings and chat_settings.llm_memory:
            memory_tokens = len(chat_settings.llm_memory) // 4
            if memory_tokens <= token_budget:
                context_parts.append(f"# MEMORY\n{chat_settings.llm_memory}")
                token_budget -= memory_tokens

        # 3. Recent messages (fit in remaining budget)
        messages = await self._get_relevant_messages(
            message,
            max_count=config.max_messages,
            max_tokens=token_budget - 200,  # Reserve 200 for current message
        )

        if messages:
            context_parts.append("# RECENT")
            for msg in messages:
                # Compact format: "user_name (id): text"
                context_parts.append(
                    f"{msg.from_user.display_name} ({msg.from_user.user_id}): {msg.text}"
                )

        # 4. Current message (always included)
        context_parts.append(f"# CURRENT\n{message.text or '[media]'}")

        return "\n".join(context_parts)
```

### 4. Add Token Tracking Middleware
**New**: `derp/middlewares/token_tracker.py`

```python
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

class TokenTrackingMiddleware(BaseMiddleware):
    """Track token usage for all Gemini requests."""

    async def __call__(
        self,
        handler: Callable,
        event: TelegramObject,
        data: dict,
    ):
        # Execute handler
        result = await handler(event, data)

        # Track usage if response includes metadata
        if hasattr(result, 'usage_metadata'):
            await self._track_usage(event, result.usage_metadata)

        return result

    async def _track_usage(self, event: TelegramObject, usage: Any):
        """Log token usage to database and monitoring."""
        logfire.info(
            "token_usage",
            chat_id=event.chat.id,
            user_id=event.from_user.id if event.from_user else None,
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count,
            total_tokens=usage.total_token_count,
            cost=self._calculate_cost(usage),
        )
```

---

## Testing Strategy

### Unit Tests Needed

1. **Context Builder Tests**
   ```python
   async def test_context_respects_token_limit():
       builder = CompactContextBuilder()
       context = await builder.build(message, ContextConfig(max_tokens=500))
       assert estimate_tokens(context) <= 500
   ```

2. **Prompt Template Tests**
   ```python
   def test_prompt_loads_correctly():
       prompt = PromptManager.load("default", version="1.0")
       assert "Derp" in prompt
       assert len(prompt) < 10000
   ```

3. **Cost Calculation Tests**
   ```python
   def test_cost_calculation():
       usage = UsageMetadata(input_tokens=1000, output_tokens=500)
       cost = calculate_cost(usage, model="gemini-2.0-flash")
       assert cost == 0.000225  # (1000 * 0.075 + 500 * 0.30) / 1M
   ```

### Integration Tests

1. **End-to-End Request Flow**
   ```python
   async def test_full_request_with_optimizations():
       message = create_test_message()
       response = await handler.handle(message)
       assert response.has_content
       assert response.token_count < 5000
   ```

2. **Cache Effectiveness**
   ```python
   async def test_prompt_caching():
       # First request
       response1 = await handler.handle(message1)

       # Second request (should use cache)
       response2 = await handler.handle(message2)

       assert response2.cache_hit is True
   ```

---

## Security Considerations

### Current Issues

1. **API Key Exposure Risk**
   - Keys in environment variables
   - No key rotation policy
   - No key-specific permissions

2. **Context Injection Risk**
   - User-provided content in context
   - JSON injection possible
   - Need input sanitization

### Recommendations

1. **Secure Key Management**
   ```python
   # Use cloud secret manager
   from google.cloud import secretmanager

   client = secretmanager.SecretManagerServiceClient()
   secret = client.access_secret_version(
       request={"name": f"projects/{PROJECT_ID}/secrets/gemini-api-key/versions/latest"}
   )
   ```

2. **Input Sanitization**
   ```python
   def sanitize_for_context(text: str) -> str:
       """Sanitize user input before adding to context."""
       # Remove potential prompt injection attempts
       text = text.replace("```", "'''")  # Prevent markdown escape
       text = text.replace("# SYSTEM", "# USER_INPUT")  # Prevent system injection
       return text
   ```

3. **Rate Limiting per User**
   ```python
   class RateLimiter:
       async def check_limit(self, user_id: int) -> bool:
           count = await redis.incr(f"rate_limit:{user_id}:minute")
           if count == 1:
               await redis.expire(f"rate_limit:{user_id}:minute", 60)
           return count <= 10  # Max 10 requests per minute
   ```

---

## Conclusion

This codebase demonstrates solid engineering practices and is well-positioned for optimization. The recommended improvements focus on:

1. **Cost Efficiency**: 60-80% reduction in API costs through caching, windowing, and routing
2. **Better UX**: Streaming responses, faster replies, more personalization
3. **Maintainability**: YAML-based prompts, versioning, A/B testing
4. **Observability**: Token tracking, cost monitoring, quality metrics

### Priority Actions (This Week)

1. ✅ Add token counting to all Gemini requests
2. ✅ Reduce context from 100 to 20 messages
3. ✅ Implement basic retry logic for rate limits
4. ✅ Extract system prompt to configuration file

### Quick Wins vs. Long-term ROI

**Quick Wins** (1-2 weeks, 60% cost reduction):
- Token tracking
- Context windowing
- Prompt caching

**Strategic** (2-3 months, 80% total cost reduction + better UX):
- Multi-model routing
- Semantic filtering
- A/B testing
- Advanced memory

The current implementation is production-ready but has significant optimization opportunities. With the recommended changes, you can achieve substantial cost savings while improving user experience and maintainability.
