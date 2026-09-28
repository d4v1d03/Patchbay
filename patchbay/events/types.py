EV_STATUS = "session.status"  # {status, error?}
EV_SANDBOX_READY = "sandbox.ready"  # {container_id, repo_url, reused}
EV_USER_MESSAGE = "user.message"  # {content}
EV_MESSAGE = "agent.message"  # {content}
EV_TOOL_CALL = "agent.tool_call"  # {call_id, name, arguments}
EV_TOOL_RESULT = (
    "agent.tool_result"  # {call_id, name, output, exit_code, duration_ms, truncated, is_error}
)
EV_ASK_USER = "agent.ask_user"  # {question}
EV_USAGE = "llm.usage"  # {step, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
#                           model, latency_ms, role?}
EV_ERROR = "session.error"  # {message, recoverable}
EV_CONTEXT = "context.compacted"  # {elided, before_tokens, after_tokens}
EV_DIFF = "diff.updated"  # {files, insertions, deletions}
EV_REVIEW = "agent.review"  # {approve, issues, rejections, error}
