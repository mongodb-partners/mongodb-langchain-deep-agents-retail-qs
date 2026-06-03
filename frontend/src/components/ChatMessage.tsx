import React, { useCallback, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Message } from '../context/ChatContext';
import { useChat } from '../context/ChatContext';

interface Props {
  message: Message;
}

/**
 * Guard against an UNCLOSED ``` code fence in the assistant's text. The agent
 * sometimes echoes a query in a fence and forgets to close it (e.g.
 * "``` db.products.aggregate(...)"), which makes react-markdown render the
 * entire rest of the message — headings, tables, prose — as one raw code
 * block. If the fence count is odd, drop the last unmatched fence marker so the
 * following markdown renders normally.
 */
function balanceCodeFences(md: string): string {
  const fences = md.match(/```/g);
  if (!fences || fences.length % 2 === 0) return md;
  const i = md.lastIndexOf('```');
  return md.slice(0, i) + md.slice(i + 3);
}

const userBubbleStyle: React.CSSProperties = {
  maxWidth: '86%',
  padding: '9px 12px',
  borderRadius: 'var(--radius-md) var(--radius-md) 4px var(--radius-md)',
  background: 'var(--green-tint-12)',
  border: '1px solid var(--green-border)',
  color: 'var(--text)',
  fontSize: 14,
  lineHeight: 1.55,
  alignSelf: 'flex-end',
  wordBreak: 'break-word',
  whiteSpace: 'pre-wrap',
};

const assistantBubbleStyle: React.CSSProperties = {
  maxWidth: '92%',
  padding: '10px 14px',
  borderRadius: 'var(--radius-md) var(--radius-md) var(--radius-md) 4px',
  background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border)',
  color: 'var(--text)',
  fontSize: 14,
  lineHeight: 1.7,
  alignSelf: 'flex-start',
  wordBreak: 'break-word',
};

const errorBubbleStyle: React.CSSProperties = {
  ...assistantBubbleStyle,
  background: 'var(--danger-tint)',
  border: '1px solid rgba(239,68,68,0.4)',
  color: 'var(--danger)',
};

const roleTagStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '1.5px',
  marginBottom: 5,
  color: 'var(--text-secondary)',
};

const systemNoteStyle: React.CSSProperties = {
  alignSelf: 'center',
  fontSize: 12,
  color: 'var(--text-secondary)',
  background: 'rgba(255,255,255,0.04)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-pill)',
  padding: '6px 12px',
  margin: '2px 0',
  maxWidth: '85%',
  textAlign: 'center',
  lineHeight: 1.5,
};

const feedbackRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  marginTop: 6,
  paddingLeft: 2,
};

const feedbackThanksStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-secondary)',
  marginLeft: 4,
};

function feedbackBtnStyle(active: boolean): React.CSSProperties {
  return {
    cursor: 'pointer',
    borderRadius: 'var(--radius-sm)',
    padding: '2px 8px',
    fontSize: 14,
    lineHeight: 1.2,
    border: active ? '1px solid var(--spring-green)' : '1px solid var(--border)',
    background: active ? 'var(--green-tint)' : 'transparent',
  };
}

const ChatMessage: React.FC<Props> = React.memo(({ message }) => {
  const { submitFeedback } = useChat();
  const [busy, setBusy] = useState(false);

  if (message.role === 'system') {
    return <div style={systemNoteStyle}>{message.content}</div>;
  }

  const isUser = message.role === 'user';
  const isError = message.role === 'error';

  const bubbleStyle = isError
    ? errorBubbleStyle
    : isUser
      ? userBubbleStyle
      : assistantBubbleStyle;

  // Thumbs appear only once the turn's run id is known and it has real text
  // (not the streaming "…" placeholder).
  const canRate =
    message.role === 'assistant' &&
    !!message.runId &&
    !!message.content &&
    message.content.trim() !== '…';

  const rate = async (score: number) => {
    if (busy) return;
    setBusy(true);
    try {
      await submitFeedback(message.id, score);
    } finally {
      setBusy(false);
    }
  };

  // Keep code-block rendering plain (no rehype-raw / dangerouslySetInnerHTML).
  const renderCode = useCallback(
    (props: { className?: string; children?: React.ReactNode }) => {
      const { className, children } = props;
      return <code className={className}>{children}</code>;
    },
    [],
  );

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: isUser ? 'flex-end' : 'flex-start',
      }}
    >
      {!isUser && (
        <div style={roleTagStyle}>{isError ? 'Error' : 'Assistant'}</div>
      )}
      <div style={bubbleStyle}>
        {isUser ? (
          <div>{message.content}</div>
        ) : (
          <div className="chat-markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{ code: renderCode }}
            >
              {balanceCodeFences(message.content || (isError ? '' : '…'))}
            </ReactMarkdown>
          </div>
        )}
      </div>
      {canRate && (
        <div style={feedbackRowStyle}>
          <button
            type="button"
            aria-label="Good response"
            title="Good response"
            disabled={busy}
            onClick={() => rate(1)}
            style={feedbackBtnStyle(message.feedback === 'up')}
          >
            👍
          </button>
          <button
            type="button"
            aria-label="Bad response"
            title="Bad response"
            disabled={busy}
            onClick={() => rate(0)}
            style={feedbackBtnStyle(message.feedback === 'down')}
          >
            👎
          </button>
          {message.feedback && (
            <span style={feedbackThanksStyle}>Thanks for the feedback!</span>
          )}
        </div>
      )}
    </div>
  );
});

ChatMessage.displayName = 'ChatMessage';
export default ChatMessage;
