# Antigravity CLI Use Cases

## Use Case Categories

### 1. Pre-Implementation Research

Before implementing a new feature, use Antigravity when route resolution (`agents.<target>.tool`) points to `antigravity`, or when `tool: auto` selects research.

```bash
# General research
agy -p "Research best practices for implementing OAuth2 in Python.
Include:
- Recommended libraries (compare authlib vs python-oauth2 vs others)
- Security considerations
- Common pitfalls to avoid
- Example implementations

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null

# Framework-specific research
agy -p "Research FastAPI authentication patterns in 2025.
Focus on:
- JWT vs session-based auth
- Dependency injection patterns
- Testing strategies

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null
```

### 2. Repository-Wide Understanding

Leverage the large context window for comprehensive codebase analysis.

```bash
# Full repository analysis
agy -p "Analyze this entire codebase and provide:
1. Architecture diagram (describe in text)
2. Module dependency graph
3. Key abstractions and their purposes
4. Suggested areas for improvement

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null

# Specific aspect analysis
agy -p "Trace the data flow for user authentication:
- Entry points (API endpoints)
- Middleware processing
- Database interactions
- Response formatting

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null
```

### 3. Documentation & Web Research

```bash
# Latest documentation
agy -p "Find and summarize the latest React 19 features and migration guide from official docs

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null

# Compare libraries
agy -p "Compare these Python HTTP clients in 2025:
- httpx vs aiohttp vs requests
- Performance benchmarks
- Feature comparison
- Community activity

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null

# Troubleshooting
agy -p "Research common causes and solutions for: {error message}
Search Stack Overflow, GitHub Issues, and official docs

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> 2>/dev/null
```

### 4. Code Migration Analysis

```bash
# Framework migration
agy -p "Analyze our codebase for Django to FastAPI migration:
- Identify all Django-specific patterns used
- Map to FastAPI equivalents
- Estimate migration complexity per module
- Suggest migration order

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null

# Version upgrade
agy -p "Research breaking changes from Python 3.11 to 3.13.
Cross-reference with our codebase to identify:
- Deprecated features we use
- New features we could adopt
- Required changes

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir . 2>/dev/null
```

## When NOT to Use Antigravity

| Task                | Reason                                     | Use Instead                  |
| ------------------- | ------------------------------------------ | ---------------------------- |
| Design decisions    | Route should follow `agents.<target>.tool` | Resolve via `cli-tools.yaml` |
| Code implementation | Route should follow `agents.<target>.tool` | Resolve via `cli-tools.yaml` |
| Debugging           | Route should follow `agents.<target>.tool` | Resolve via `cli-tools.yaml` |
| Simple file edits   | Overkill                                   | Claude Code directly         |
| Running tests       | Execution task                             | Claude Code directly         |

## Output Handling

### Piping to Files

```bash
agy -p "Generate comprehensive documentation for src/auth/

IMPORTANT: Do not ask any clarifying questions." --model <antigravity.model> --add-dir src/auth 2>/dev/null > docs/auth-module.md
```

## Rate Limits

Free tier (personal Google account):

- 60 requests/minute
- 1,000 requests/day

Plan accordingly for large research tasks.
