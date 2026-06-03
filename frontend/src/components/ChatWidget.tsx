import React, {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { useChat } from '../context/ChatContext';
import ChatMessage from './ChatMessage';
import type { CartResponse, InterruptEvent, SavedFile, Todo } from '../api/client';

/* ── small helpers ─────────────────────────────────────────────────────── */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatUsd(n: number): string {
  return `$${n.toFixed(2)}`;
}

const STATUS_COLOR: Record<Todo['status'], string> = {
  pending: 'var(--text-secondary)',
  in_progress: 'var(--spring-green)',
  completed: 'var(--forest-green)',
};

/* ── FAB ───────────────────────────────────────────────────────────────── */
function Fab({ open, onClick }: { open: boolean; onClick: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-label={open ? 'Close shopping assistant' : 'Open shopping assistant'}
      aria-expanded={open}
      className="fab"
      style={{
        position: 'fixed',
        right: 20,
        bottom: 20,
        zIndex: 1001,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: '12px 18px',
        borderRadius: 'var(--radius-pill)',
        border: 'none',
        background: 'var(--spring-green)',
        color: 'var(--slate-navy)',
        fontFamily: 'var(--font-mono)',
        fontSize: '0.7rem',
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '1px',
        cursor: 'pointer',
        boxShadow: '0 8px 26px rgba(0,237,100,0.35)',
        transition: 'transform 0.18s ease, box-shadow 0.18s ease',
        transform: hover ? 'translateY(-2px)' : 'none',
      }}
    >
      <span aria-hidden="true" style={{ fontSize: '0.95rem' }}>
        {open ? '✕' : '✦'}
      </span>
      <span className="fab-label">
        {open ? 'Close' : 'Ask your assistant'}
      </span>
    </button>
  );
}

/* ── Agent thinking (plan) collapsible ─────────────────────────────────── */
function PlanSection({ todos }: { todos: Todo[] }) {
  const [open, setOpen] = useState(false);
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.status === 'completed').length;
  return (
    <div
      style={{
        borderTop: '1px solid var(--border)',
        background: 'rgba(255,255,255,0.02)',
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 14px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: 'var(--text-secondary)',
        }}
        aria-expanded={open}
      >
        <span>
          Agent thinking · {done}/{todos.length}
        </span>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <ul
          style={{
            listStyle: 'none',
            margin: 0,
            padding: '0 14px 10px',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          {todos.map((t) => (
            <li
              key={t.id}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 8,
                fontSize: 12,
                color: 'var(--text)',
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  marginTop: 5,
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  flexShrink: 0,
                  background: STATUS_COLOR[t.status],
                  boxShadow:
                    t.status === 'in_progress'
                      ? '0 0 8px rgba(0,237,100,0.6)'
                      : 'none',
                }}
              />
              <span
                style={{
                  textDecoration:
                    t.status === 'completed' ? 'line-through' : 'none',
                  opacity: t.status === 'completed' ? 0.6 : 1,
                  lineHeight: 1.45,
                }}
              >
                {t.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ── Files Saved strip ─────────────────────────────────────────────────── */
function FilesSection({ files }: { files: SavedFile[] }) {
  if (files.length === 0) return null;
  return (
    <div
      style={{
        borderTop: '1px solid var(--border)',
        padding: '8px 14px',
        background: 'rgba(0,237,100,0.03)',
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: 'var(--spring-green)',
          marginBottom: 6,
        }}
      >
        Files Saved · {files.length}
      </div>
      <ul
        style={{
          listStyle: 'none',
          margin: 0,
          padding: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          maxHeight: 96,
          overflowY: 'auto',
        }}
      >
        {files.map((f) => (
          <li
            key={f.path}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 12,
              color: 'var(--text)',
            }}
          >
            <span aria-hidden="true">📎</span>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                flex: 1,
              }}
              title={f.path}
            >
              {f.path}
            </span>
            <span style={{ color: 'var(--text-secondary)', flexShrink: 0 }}>
              {formatSize(f.size)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ── Cart strip ────────────────────────────────────────────────────────── */
function CartSection({ cart }: { cart: CartResponse | null }) {
  // Render only once the cart has lines. (null = not yet loaded, [] = empty.)
  if (!cart || cart.lines.length === 0) {
    return (
      <div
        style={{
          borderTop: '1px solid var(--border)',
          padding: '8px 14px',
          background: 'rgba(255,255,255,0.02)',
        }}
      >
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            textTransform: 'uppercase',
            letterSpacing: '1px',
            color: 'var(--text-secondary)',
          }}
        >
          🛒 Cart · empty
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        borderTop: '1px solid var(--border)',
        padding: '8px 14px',
        background: 'rgba(0,237,100,0.03)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: 'var(--spring-green)',
          marginBottom: 6,
        }}
      >
        <span>🛒 Cart · {cart.lines.length}</span>
        <span style={{ color: 'var(--text)' }}>{formatUsd(cart.subtotal)}</span>
      </div>
      <ul
        style={{
          listStyle: 'none',
          margin: 0,
          padding: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 5,
          maxHeight: 132,
          overflowY: 'auto',
        }}
      >
        {cart.lines.map((line) => {
          const onSale = line.sale_price_usd != null;
          const unit = onSale ? (line.sale_price_usd as number) : line.unit_price_usd;
          return (
            <li
              key={line.product_id}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 6,
                fontSize: 12,
                color: 'var(--text)',
              }}
            >
              <span style={{ color: 'var(--text-secondary)', flexShrink: 0 }}>
                {line.qty}×
              </span>
              <span
                style={{
                  flex: 1,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={line.name}
              >
                {line.name}
              </span>
              {line.line_savings != null && line.line_savings > 0 && (
                <span
                  style={{
                    flexShrink: 0,
                    fontFamily: 'var(--font-mono)',
                    fontSize: 11,
                    color: 'var(--spring-green)',
                  }}
                >
                  −{formatUsd(line.line_savings)}
                </span>
              )}
              <span
                style={{
                  flexShrink: 0,
                  fontFamily: 'var(--font-mono)',
                  color: onSale ? 'var(--spring-green)' : 'var(--text-secondary)',
                }}
              >
                {formatUsd(unit * line.qty)}
              </span>
            </li>
          );
        })}
      </ul>
      {cart.total_savings > 0 && (
        <div
          style={{
            marginTop: 7,
            paddingTop: 6,
            borderTop: '1px solid var(--border)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--spring-green)',
          }}
        >
          <span>You save</span>
          <span>{formatUsd(cart.total_savings)}</span>
        </div>
      )}
    </div>
  );
}

/* ── Checkout HITL approval card ───────────────────────────────────────── */
const DECISION_LABEL: Record<string, string> = {
  approve: 'Approve',
  edit: 'Edit',
  reject: 'Reject',
};

function InterruptCard({
  interrupt,
  disabled,
  onApprove,
  onEdit,
  onReject,
}: {
  interrupt: InterruptEvent;
  disabled: boolean;
  onApprove: () => void;
  onEdit: (action: { name: string; args: Record<string, unknown> }) => void;
  onReject: (message?: string) => void;
}) {
  const { action, allowed_decisions } = interrupt;
  const [editing, setEditing] = useState(false);
  const [argsText, setArgsText] = useState(() => JSON.stringify(action.args, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);

  const submitEdit = () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(argsText) as Record<string, unknown>;
    } catch {
      setParseError('Invalid JSON — fix and try again.');
      return;
    }
    setParseError(null);
    onEdit({ name: action.name, args: parsed });
  };

  const handleReject = () => {
    const reason = window.prompt('Reason for rejecting (optional):') ?? undefined;
    onReject(reason && reason.trim() ? reason.trim() : undefined);
  };

  const btn = (label: string, onClick: () => void, primary: boolean): React.ReactNode => (
    <button
      key={label}
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        flex: 1,
        padding: '8px 10px',
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        textTransform: 'uppercase',
        letterSpacing: '1px',
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        borderRadius: 'var(--radius-md)',
        border: primary ? 'none' : '1px solid var(--border)',
        background: primary
          ? disabled
            ? 'rgba(255,255,255,0.06)'
            : 'var(--spring-green)'
          : 'transparent',
        color: primary
          ? disabled
            ? 'var(--text-secondary)'
            : 'var(--slate-navy)'
          : 'var(--text)',
        opacity: disabled ? 0.7 : 1,
      }}
    >
      {label}
    </button>
  );

  return (
    <div
      role="group"
      aria-label="Checkout approval"
      style={{
        margin: '10px 12px 0',
        padding: 12,
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--green-border)',
        background: 'var(--green-tint, rgba(0,237,100,0.06))',
        flexShrink: 0,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: 'var(--spring-green)',
          marginBottom: 6,
        }}
      >
        ✋ Approval needed · {action.name}
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--text)', marginBottom: 8 }}>
        {action.description}
      </div>

      {editing ? (
        <>
          <textarea
            value={argsText}
            onChange={(e) => setArgsText(e.target.value)}
            spellCheck={false}
            rows={6}
            aria-label="Edit action arguments as JSON"
            style={{
              width: '100%',
              boxSizing: 'border-box',
              padding: 8,
              background: 'var(--surface)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-sm)',
              color: 'var(--text)',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              resize: 'vertical',
              outline: 'none',
            }}
          />
          {parseError && (
            <div style={{ marginTop: 4, fontSize: 11, color: 'var(--danger)' }}>{parseError}</div>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            {btn('Submit edit', submitEdit, true)}
            {btn('Cancel', () => { setEditing(false); setParseError(null); }, false)}
          </div>
        </>
      ) : (
        <>
          {Object.keys(action.args).length > 0 && (
            <pre
              style={{
                margin: '0 0 8px',
                padding: 8,
                maxHeight: 120,
                overflow: 'auto',
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                borderRadius: 'var(--radius-sm)',
                color: 'var(--text-secondary)',
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
                lineHeight: 1.5,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {JSON.stringify(action.args, null, 2)}
            </pre>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            {allowed_decisions.map((d) => {
              const label = DECISION_LABEL[d] ?? d;
              if (d === 'approve') return btn(label, onApprove, true);
              if (d === 'edit') return btn(label, () => setEditing(true), false);
              if (d === 'reject') return btn(label, handleReject, false);
              return btn(label, onApprove, false);
            })}
          </div>
        </>
      )}
    </div>
  );
}

/* ── Panel ─────────────────────────────────────────────────────────────── */
function Panel() {
  const {
    messages,
    isLoading,
    activeTool,
    plan,
    files,
    cart,
    pendingInterrupt,
    userId,
    sendMessage,
    newConversation,
    approveCheckout,
    editCheckout,
    rejectCheckout,
    setOpen,
  } = useChat();
  const [draft, setDraft] = useState('');
  const [expanded, setExpanded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Auto-scroll to newest content as tokens stream in.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, activeTool, isLoading]);

  // Focus the input when the panel mounts (opens).
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || isLoading || !userId) return;
    sendMessage(text);
    setDraft('');
  };

  const canSend = !!draft.trim() && !isLoading && !!userId;

  return (
    <div
      role="dialog"
      aria-label="Retail Shopping Assistant"
      className="chat-panel"
      style={{
        position: 'fixed',
        right: 20,
        bottom: 78,
        zIndex: 1000,
        width: expanded
          ? 'min(900px, calc(100vw - 40px))'
          : 'min(420px, calc(100vw - 28px))',
        height: expanded
          ? 'calc(100vh - 110px)'
          : 'min(600px, calc(100vh - 120px))',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--surface-2)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        boxShadow: 'var(--shadow-panel)',
        overflow: 'hidden',
        animation: 'panel-in 0.22s ease both',
        transition: 'width 0.2s ease, height 0.2s ease',
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 14px',
          borderBottom: '1px solid var(--border)',
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontFamily: 'var(--font-mono)',
            fontSize: '0.7rem',
            textTransform: 'uppercase',
            letterSpacing: '1.2px',
            color: 'var(--spring-green)',
          }}
        >
          <span aria-hidden="true">✦</span>
          Agent Cartsmith
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button
            type="button"
            onClick={newConversation}
            style={{
              padding: '4px 10px',
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              textTransform: 'uppercase',
              letterSpacing: '1px',
              color: 'var(--text-secondary)',
              background: 'transparent',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-pill)',
              cursor: 'pointer',
            }}
            title="Start a new conversation"
          >
            New
          </button>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-label={expanded ? 'Collapse assistant' : 'Expand assistant'}
            aria-pressed={expanded}
            title={expanded ? 'Restore default size' : 'Expand to a larger view'}
            style={{
              width: 26,
              height: 26,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--text-secondary)',
              background: 'transparent',
              border: 'none',
              borderRadius: 'var(--radius-sm)',
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            {expanded ? '⤡' : '⤢'}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            aria-label="Close assistant"
            style={{
              width: 26,
              height: 26,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--text-secondary)',
              background: 'transparent',
              border: 'none',
              borderRadius: 'var(--radius-sm)',
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            ✕
          </button>
        </div>
      </div>

      {/* Messages */}
      <div
        ref={scrollRef}
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: 14,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        {messages.length === 0 ? (
          <div
            style={{
              margin: 'auto',
              textAlign: 'center',
              maxWidth: 260,
              color: 'var(--text-secondary)',
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            Ask about recipes, deals, loyalty, or your shopping list…
          </div>
        ) : (
          messages.map((m) => <ChatMessage key={m.id} message={m} />)
        )}

        {activeTool && (
          <div
            style={{
              alignSelf: 'flex-start',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              padding: '6px 10px',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              color: 'var(--spring-green)',
              background: 'var(--green-tint)',
              border: '1px solid var(--green-border)',
              borderRadius: 'var(--radius-pill)',
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: 'var(--spring-green)',
                animation: 'pulse-dot 1s ease-in-out infinite',
              }}
            />
            {activeTool} running…
          </div>
        )}
      </div>

      {/* Agent thinking (todos) */}
      <PlanSection todos={plan.todos} />

      {/* Cart */}
      <CartSection cart={cart} />

      {/* Files Saved */}
      <FilesSection files={files} />

      {/* Checkout approval (HITL) — shown above the input when the agent pauses */}
      {pendingInterrupt && (
        <InterruptCard
          interrupt={pendingInterrupt}
          disabled={isLoading}
          onApprove={approveCheckout}
          onEdit={editCheckout}
          onReject={rejectCheckout}
        />
      )}

      {/* Input */}
      <form
        onSubmit={submit}
        style={{
          display: 'flex',
          gap: 8,
          padding: 12,
          borderTop: '1px solid var(--border)',
          flexShrink: 0,
        }}
      >
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Ask about recipes, deals, or your shopping list…"
          aria-label="Message"
          spellCheck={false}
          style={{
            flex: 1,
            padding: '10px 12px',
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
            color: 'var(--text)',
            fontSize: 13,
            outline: 'none',
          }}
        />
        <button
          type="submit"
          disabled={!canSend}
          aria-label="Send message"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 42,
            background: canSend ? 'var(--spring-green)' : 'rgba(255,255,255,0.06)',
            color: canSend ? 'var(--slate-navy)' : 'var(--text-secondary)',
            border: 'none',
            borderRadius: 'var(--radius-md)',
            cursor: canSend ? 'pointer' : 'not-allowed',
            fontSize: 15,
            transition: 'background 0.15s ease',
          }}
        >
          ↑
        </button>
      </form>

      {/* Feedback hint — only meaningful once a user id is set. */}
      {userId && (
        <div
          style={{
            padding: '0 12px 10px',
            fontSize: 10.5,
            color: 'var(--text-secondary)',
            textAlign: 'center',
            flexShrink: 0,
          }}
        >
          Tip: rate replies with 👍/👎, or type{' '}
          <code style={{ fontFamily: 'var(--font-mono)' }}>/feedback up|down &lt;comment&gt;</code>.
        </div>
      )}
    </div>
  );
}

export default function ChatWidget() {
  const { open, setOpen } = useChat();

  // Esc closes the panel.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, setOpen]);

  return (
    <>
      {open && <Panel />}
      <Fab open={open} onClick={() => setOpen(!open)} />
    </>
  );
}
