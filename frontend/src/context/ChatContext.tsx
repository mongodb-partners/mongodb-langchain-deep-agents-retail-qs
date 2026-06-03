import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useReducer,
  useRef,
  useState,
} from 'react';
import {
  fetchCart,
  fetchFiles,
  fetchLatestThread,
  fetchMessages,
  streamChat,
  streamResumeInterrupt,
  submitFeedback as apiSubmitFeedback,
  type CartResponse,
  type InterruptEvent,
  type PlanSnapshot,
  type SavedFile,
  type StoredMessage,
} from '../api/client';

function genId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export const FEEDBACK_HINT =
  'Tip: rate a reply with 👍/👎, or type /feedback [up|down] <comment>.';

// Parse `/feedback [up|down|+1|-1|👍|👎] <comment>`. A leading score token is
// optional; without one the whole body is the comment and score defaults to 👍
// (so `/feedback Good job!` reads naturally rather than eating "Good").
export function parseFeedbackCommand(raw: string): { score: number; comment: string } {
  const body = raw.replace(/^\/feedback\b/i, '').trim();
  const sp = body.indexOf(' ');
  const first = (sp === -1 ? body : body.slice(0, sp)).toLowerCase();
  const rest = (sp === -1 ? '' : body.slice(sp + 1)).trim();
  if (first === 'down' || first === '-1' || first === '👎') return { score: 0, comment: rest };
  if (first === 'up' || first === '+1' || first === '👍') return { score: 1, comment: rest };
  return { score: 1, comment: body };
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'error' | 'system';
  content: string;
  timestamp: Date;
  // Per-turn correlation id (assistant messages) — the run_id feedback
  // attaches to. Set from the `correlation` SSE frame.
  runId?: string;
  // Which thumbs rating the user gave this assistant turn, if any.
  feedback?: 'up' | 'down';
}

interface ChatContextValue {
  messages: Message[];
  isLoading: boolean;
  userId: string;
  plan: PlanSnapshot;
  model: string;
  // short label of the tool the agent is currently running
  // (e.g. "researcher", "knowledge_base_search"). Empty when the agent
  // is between tool calls or generating final tokens. Wired from
  // `event: status` SSE frames so the UI can show the agent is alive
  // during long tool-bound turns.
  activeTool: string;
  // VFS files the agent wrote for the current conversation. Refreshed via
  // GET /api/files after each turn finishes (onDone) — proves the agent
  // persisted artifacts to S3 + MongoDB VFS.
  files: SavedFile[];
  // the conversation's current cart, refreshed via GET /api/cart
  // after each turn finishes (onDone). Null until the first refresh.
  cart: CartResponse | null;
  // a pending human-in-the-loop checkout pause awaiting the
  // shopper's decision. Null when no checkout approval is in flight.
  pendingInterrupt: InterruptEvent | null;
  // Floating chat panel open-state, lifted here so preset cards and the FAB
  // can both drive it.
  open: boolean;
  setOpen: (o: boolean) => void;
  setUserId: (u: string) => void;
  setModel: (m: string) => void;
  sendMessage: (message: string, opts?: { newThread?: boolean }) => void;
  newConversation: () => void;
  // resume a paused checkout with the shopper's decision. Each
  // streams the agent's follow-up into the current assistant turn.
  approveCheckout: () => void;
  editCheckout: (action: { name: string; args: Record<string, unknown> }) => void;
  rejectCheckout: (message?: string) => void;
  // 1 = 👍, 0 = 👎 for the assistant message with `messageId`. Returns false
  // if it has no run id yet or the POST failed.
  submitFeedback: (messageId: string, score: number, comment?: string) => Promise<boolean>;
}

interface State {
  messages: Message[];
  isLoading: boolean;
  userId: string;
  streamingId: string | null;
  plan: PlanSnapshot;
  threadSub: string;
  model: string;
  activeTool: string;
  files: SavedFile[];
  cart: CartResponse | null;
  pendingInterrupt: InterruptEvent | null;
}

type Action =
  | { type: 'SET_USER_ID'; userId: string }
  | { type: 'SET_MODEL'; model: string }
  | { type: 'ADD_USER_MSG'; msg: Message }
  | { type: 'START_ASSISTANT'; id: string }
  | { type: 'APPEND_TOKEN'; id: string; token: string }
  | { type: 'FINALIZE' }
  | { type: 'REPLACE_WITH_ERROR'; id: string; detail: string }
  | { type: 'SET_PLAN'; plan: PlanSnapshot }
  | { type: 'SET_ACTIVE_TOOL'; tool: string }
  | { type: 'SET_FILES'; files: SavedFile[] }
  | { type: 'SET_CART'; cart: CartResponse }
  | { type: 'SET_INTERRUPT'; interrupt: InterruptEvent }
  | { type: 'CLEAR_INTERRUPT' }
  | { type: 'RESTORE'; threadSub: string; messages: Message[] }
  | { type: 'CLEAR'; threadSub: string }
  | { type: 'ADD_MSG'; msg: Message }
  | { type: 'SET_RUN_ID'; id: string; runId: string }
  | { type: 'SET_FEEDBACK'; id: string; feedback: 'up' | 'down' };

const EMPTY_PLAN: PlanSnapshot = { todos: [], updated_at: null };

/**
 * Project a persisted agent-log transcript down to the human/assistant turns the
 * chat renders. Drops tool/system frames and empty-content `ai` turns (pure
 * tool-call steps) so the restored thread reads like the live one.
 */
function mapStored(stored: StoredMessage[]): Message[] {
  const out: Message[] = [];
  for (const m of stored) {
    const content = (m.content || '').trim();
    if (m.type === 'human') {
      out.push({ id: genId(), role: 'user', content, timestamp: new Date() });
    } else if (m.type === 'ai' && content) {
      out.push({ id: genId(), role: 'assistant', content, timestamp: new Date() });
    }
  }
  return out;
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'SET_USER_ID':
      return { ...state, userId: action.userId };
    case 'SET_MODEL':
      return { ...state, model: action.model };
    case 'ADD_USER_MSG':
      return {
        ...state,
        messages: [...state.messages, action.msg],
        isLoading: true,
      };
    case 'START_ASSISTANT':
      return {
        ...state,
        // An assistant turn is in flight — keep the loading banner on. For a
        // /chat send ADD_USER_MSG already set this; for a checkout resume (no
        // user message) this is where loading turns on.
        isLoading: true,
        streamingId: action.id,
        messages: [
          ...state.messages,
          { id: action.id, role: 'assistant', content: '', timestamp: new Date() },
        ],
      };
    case 'APPEND_TOKEN':
      return {
        ...state,
        // Tokens flowing means the model is producing user-visible
        // text — clear any tool-running banner.
        activeTool: '',
        messages: state.messages.map((m) =>
          m.id === action.id ? { ...m, content: m.content + action.token } : m,
        ),
      };
    case 'FINALIZE':
      return { ...state, isLoading: false, streamingId: null, activeTool: '' };
    case 'REPLACE_WITH_ERROR':
      return {
        ...state,
        isLoading: false,
        streamingId: null,
        activeTool: '',
        messages: state.messages.map((m) =>
          m.id === action.id
            ? { ...m, role: 'error', content: action.detail }
            : m,
        ),
      };
    case 'SET_PLAN':
      return { ...state, plan: action.plan };
    case 'SET_ACTIVE_TOOL':
      return { ...state, activeTool: action.tool };
    case 'SET_FILES':
      return { ...state, files: action.files };
    case 'SET_CART':
      return { ...state, cart: action.cart };
    case 'SET_INTERRUPT':
      return { ...state, pendingInterrupt: action.interrupt };
    case 'CLEAR_INTERRUPT':
      return { ...state, pendingInterrupt: null };
    case 'ADD_MSG':
      return { ...state, messages: [...state.messages, action.msg] };
    case 'SET_RUN_ID':
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.id === action.id ? { ...m, runId: action.runId } : m,
        ),
      };
    case 'SET_FEEDBACK':
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.id === action.id ? { ...m, feedback: action.feedback } : m,
        ),
      };
    case 'RESTORE':
      return {
        ...state,
        messages: action.messages,
        isLoading: false,
        streamingId: null,
        plan: EMPTY_PLAN,
        threadSub: action.threadSub,
        activeTool: '',
        files: [],
        cart: null,
        pendingInterrupt: null,
      };
    case 'CLEAR':
      return {
        ...state,
        messages: [],
        isLoading: false,
        streamingId: null,
        plan: EMPTY_PLAN,
        threadSub: action.threadSub,
        activeTool: '',
        files: [],
        cart: null,
        pendingInterrupt: null,
      };
    default:
      return state;
  }
}

const ChatContext = createContext<ChatContextValue | null>(null);

const initialState: State = {
  messages: [],
  isLoading: false,
  userId: 'demo-user',
  streamingId: null,
  plan: EMPTY_PLAN,
  threadSub: genId(),
  // Empty model ⇒ omit the field on /chat so the backend uses its default.
  // Header populates this from /api/models on mount.
  model: '',
  activeTool: '',
  files: [],
  cart: null,
  pendingInterrupt: null,
};

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  // Mirror state into a ref so the stable feedback callbacks always read the
  // latest messages without re-creating on every token.
  const stateRef = useRef(state);
  stateRef.current = state;
  const [open, setOpen] = useState(false);
  const loadingRef = useRef(false);
  // Holds the in-flight stream's AbortController so a new conversation or an
  // unmount can cancel it — otherwise the fetch leaks and stale onToken/onPlan
  // callbacks bleed the old conversation's content into the new one.
  const streamRef = useRef<AbortController | null>(null);
  // Tracks an in-flight "restore last conversation" run so a user action
  // (clicking New, sending a message) can cancel it before it clobbers the
  // freshly-started conversation.
  const pendingRestoreRef = useRef<{ cancelled: boolean } | null>(null);
  // Mirror the pending checkout interrupt so the stable resume callbacks read
  // the latest composite thread_id without re-creating on every state change.
  const pendingInterruptRef = useRef<InterruptEvent | null>(null);
  pendingInterruptRef.current = state.pendingInterrupt;

  // Abort any in-flight stream when the provider unmounts.
  useEffect(() => () => streamRef.current?.abort(), []);

  // Rehydrate the user's most recent conversation whenever the user id changes
  // (and on first mount). Clicking "New" only changes threadSub — not userId —
  // so it never retriggers this; that is the "unless the user clicks New"
  // escape hatch. The Header writes userId on every keystroke, so we debounce
  // and let the value settle before hitting the backend.
  useEffect(() => {
    const userId = state.userId;
    if (!userId) return;
    const token = { cancelled: false };
    pendingRestoreRef.current = token;
    const timer = setTimeout(async () => {
      const sub = await fetchLatestThread(userId);
      // A New/send during the lookup cancels the restore so we don't overwrite
      // the conversation the user just started.
      if (token.cancelled) return;
      // Switching user is a deliberate context switch — drop any live stream.
      streamRef.current?.abort();
      streamRef.current = null;
      loadingRef.current = false;
      if (!sub) {
        // No prior conversation for this user — start clean.
        dispatch({ type: 'CLEAR', threadSub: genId() });
        return;
      }
      const stored = await fetchMessages(userId, sub);
      if (token.cancelled) return;
      dispatch({ type: 'RESTORE', threadSub: sub, messages: mapStored(stored) });
      const files = await fetchFiles(userId, sub);
      if (token.cancelled) return;
      dispatch({ type: 'SET_FILES', files });
      // also rehydrate the cart this conversation built.
      const cart = await fetchCart(userId, sub);
      if (token.cancelled) return;
      dispatch({ type: 'SET_CART', cart });
    }, 400);
    return () => {
      token.cancelled = true;
      clearTimeout(timer);
    };
  }, [state.userId]);

  const setUserId = useCallback((u: string) => {
    dispatch({ type: 'SET_USER_ID', userId: u });
  }, []);

  const setModel = useCallback((m: string) => {
    dispatch({ type: 'SET_MODEL', model: m });
  }, []);

  const newConversation = useCallback(() => {
    // Cancel any in-flight restore so it can't repopulate the cleared chat.
    if (pendingRestoreRef.current) pendingRestoreRef.current.cancelled = true;
    streamRef.current?.abort();
    streamRef.current = null;
    loadingRef.current = false;
    dispatch({ type: 'CLEAR', threadSub: genId() });
  }, []);

  const submitFeedback = useCallback(
    async (messageId: string, score: number, comment?: string): Promise<boolean> => {
      const st = stateRef.current;
      const msg = st.messages.find((m) => m.id === messageId);
      if (!msg?.runId || !st.userId) return false; // nothing to key on
      try {
        await apiSubmitFeedback({
          run_id: msg.runId,
          score,
          comment: comment?.trim() ? comment.trim() : undefined,
          user_id: st.userId,
        });
        dispatch({ type: 'SET_FEEDBACK', id: messageId, feedback: score >= 1 ? 'up' : 'down' });
        return true;
      } catch {
        return false;
      }
    },
    [],
  );

  // `/feedback [up|down] <comment>` — rate the most recent assistant reply
  // instead of sending the text to the agent.
  const runFeedbackCommand = useCallback(
    async (raw: string) => {
      const { score, comment } = parseFeedbackCommand(raw);
      const target = [...stateRef.current.messages]
        .reverse()
        .find((m) => m.role === 'assistant' && m.runId);
      if (!target) {
        dispatch({
          type: 'ADD_MSG',
          msg: {
            id: genId(),
            role: 'system',
            timestamp: new Date(),
            content: `No assistant reply to rate yet. Ask something first. ${FEEDBACK_HINT}`,
          },
        });
        return;
      }
      const ok = await submitFeedback(target.id, score, comment);
      const thumb = score >= 1 ? '👍' : '👎';
      dispatch({
        type: 'ADD_MSG',
        msg: {
          id: genId(),
          role: 'system',
          timestamp: new Date(),
          content: ok
            ? `✓ Recorded ${thumb} feedback for the last reply${comment ? ` — “${comment}”` : ''}.`
            : 'Could not record feedback — the reply has no run id yet, or the request failed.',
        },
      });
    },
    [submitFeedback],
  );

  // Build the SSE handler set shared by the /chat send and the
  // /interrupts/resume flow: tokens append to `assistantId`, plan/status/
  // correlation/interrupt dispatch as usual, and onDone finalizes + refreshes
  // files and the cart for `{userId, sub}`. The same `ctrl` guards late
  // callbacks from a superseded run. Resume passes the same shape — its only
  // extra is clearing the pending interrupt, done in the callback wrapper.
  const buildStreamHandlers = useCallback(
    (opts: {
      assistantId: string;
      ctrl: AbortController;
      userId: string;
      sub: string;
      onDoneExtra?: () => void;
    }) => {
      const { assistantId, ctrl, userId, sub, onDoneExtra } = opts;
      return {
        onToken: (tok: string) => {
          if (ctrl.signal.aborted) return;
          dispatch({ type: 'APPEND_TOKEN', id: assistantId, token: tok });
        },
        onCorrelation: (runId: string) => {
          if (ctrl.signal.aborted) return;
          dispatch({ type: 'SET_RUN_ID', id: assistantId, runId });
        },
        onPlan: (plan: PlanSnapshot) => {
          if (ctrl.signal.aborted) return;
          dispatch({ type: 'SET_PLAN', plan });
        },
        onStatus: (status: { phase: 'tool_start' | 'tool_end'; name: string }) => {
          if (ctrl.signal.aborted) return;
          // Show "<tool> running" while the agent is mid-tool-call.
          // tool_end clears the banner; the next on_chat_model_stream
          // will also clear it via APPEND_TOKEN above.
          dispatch({
            type: 'SET_ACTIVE_TOOL',
            tool: status.phase === 'tool_start' ? status.name : '',
          });
        },
        onInterrupt: (ev: InterruptEvent) => {
          if (ctrl.signal.aborted) return;
          // agent paused for checkout approval. NON-terminal — a
          // `done` frame still follows. Surface the HITL card.
          dispatch({ type: 'SET_INTERRUPT', interrupt: ev });
        },
        onDone: () => {
          if (ctrl.signal.aborted) return;
          dispatch({ type: 'FINALIZE' });
          loadingRef.current = false;
          onDoneExtra?.();
          // Refresh the "Files Saved" strip: the agent may have written VFS
          // artifacts to S3 + MongoDB during this turn. Scoped by the same
          // {user, sub} the backend used for the checkpointer / VFS.
          fetchFiles(userId, sub).then((files) => {
            if (ctrl.signal.aborted) return;
            dispatch({ type: 'SET_FILES', files });
          });
          // refresh the cart — the turn may have added/optimized it.
          fetchCart(userId, sub).then((cart) => {
            if (ctrl.signal.aborted) return;
            dispatch({ type: 'SET_CART', cart });
          });
        },
        onError: (detail: string) => {
          if (ctrl.signal.aborted) return;
          dispatch({ type: 'REPLACE_WITH_ERROR', id: assistantId, detail });
          loadingRef.current = false;
        },
      };
    },
    [],
  );

  const sendMessage = useCallback(
    (message: string, opts?: { newThread?: boolean }) => {
      const trimmed = message.trim();
      if (!trimmed || loadingRef.current) return;
      if (!state.userId) return;

      // Slash-command: rate the most recent reply instead of sending to /chat.
      if (/^\/feedback\b/i.test(trimmed)) {
        void runFeedbackCommand(trimmed);
        return;
      }
      // Cancel any in-flight restore so it can't overwrite this new turn.
      if (pendingRestoreRef.current) pendingRestoreRef.current.cancelled = true;
      loadingRef.current = true;

      // Cancel any prior in-flight stream and arm a fresh controller for this
      // send so late callbacks from a superseded run can be ignored.
      streamRef.current?.abort();
      const ctrl = new AbortController();
      streamRef.current = ctrl;

      // `newThread` (preset launches) mints a fresh conversation
      // atomically so this turn is a first-turn — the server only serves the
      // response cache on a fresh conversation, so presets always replay
      // instantly. We use the local `sub` (NOT state.threadSub, which is a
      // stale closure until the CLEAR re-renders) for the request below.
      const sub = opts?.newThread ? genId() : state.threadSub;
      if (opts?.newThread) {
        dispatch({ type: 'CLEAR', threadSub: sub });
      }

      const userMsg: Message = {
        id: genId(),
        role: 'user',
        content: trimmed,
        timestamp: new Date(),
      };
      dispatch({ type: 'ADD_USER_MSG', msg: userMsg });

      const assistantId = genId();
      dispatch({ type: 'START_ASSISTANT', id: assistantId });

      // Backend scopes checkpointer / mirror / VFS by `{user}:{sub}` — `sub`
      // resolved above (a fresh id for preset launches, else the current
      // conversation), so "New Conversation" starts with an empty plan
      // namespace instead of inheriting the previous turn's todos.

      // plan updates arrive as `plan` SSE events from the chat
      // stream — no more 1.5s /plans polling. The backend emits a
      // diffed plan frame every time the planner's todo state changes.
      streamChat(
        {
          user_id: state.userId,
          thread_id: sub,
          message: trimmed,
          ...(state.model ? { model: state.model } : {}),
        },
        buildStreamHandlers({ assistantId, ctrl, userId: state.userId, sub }),
        ctrl.signal,
      );
    },
    [state.userId, state.threadSub, state.model, runFeedbackCommand, buildStreamHandlers],
  );

  // resume a paused checkout with the shopper's decision. Streams the
  // agent's follow-up into a fresh assistant turn, then clears the pending
  // interrupt and refreshes the cart + files. `thread_id` is the COMPOSITE
  // value echoed in the interrupt frame — sent back verbatim. A follow-up
  // `interrupt` during the resume re-arms the HITL card via onInterrupt.
  const resumeCheckout = useCallback(
    (
      decision: 'approve' | 'edit' | 'reject',
      extra?: {
        edited_action?: { name: string; args: Record<string, unknown> };
        message?: string;
      },
    ) => {
      const interrupt = pendingInterruptRef.current;
      if (!interrupt || loadingRef.current) return;
      if (!state.userId) return;

      loadingRef.current = true;
      streamRef.current?.abort();
      const ctrl = new AbortController();
      streamRef.current = ctrl;

      const sub = state.threadSub;

      // Clear the card immediately so the buttons can't be double-fired; the
      // FINALIZE/SET_INTERRUPT below will re-arm it if the agent pauses again.
      dispatch({ type: 'CLEAR_INTERRUPT' });

      const assistantId = genId();
      dispatch({ type: 'START_ASSISTANT', id: assistantId });

      streamResumeInterrupt(
        {
          thread_id: interrupt.thread_id,
          decision,
          ...(extra?.edited_action ? { edited_action: extra.edited_action } : {}),
          ...(extra?.message ? { message: extra.message } : {}),
        },
        buildStreamHandlers({ assistantId, ctrl, userId: state.userId, sub }),
        ctrl.signal,
      );
    },
    [state.userId, state.threadSub, buildStreamHandlers],
  );

  const approveCheckout = useCallback(() => resumeCheckout('approve'), [resumeCheckout]);
  const editCheckout = useCallback(
    (action: { name: string; args: Record<string, unknown> }) =>
      resumeCheckout('edit', { edited_action: action }),
    [resumeCheckout],
  );
  const rejectCheckout = useCallback(
    (message?: string) => resumeCheckout('reject', message ? { message } : undefined),
    [resumeCheckout],
  );

  return (
    <ChatContext.Provider
      value={{
        messages: state.messages,
        isLoading: state.isLoading,
        userId: state.userId,
        plan: state.plan,
        model: state.model,
        activeTool: state.activeTool,
        files: state.files,
        cart: state.cart,
        pendingInterrupt: state.pendingInterrupt,
        open,
        setOpen,
        setUserId,
        setModel,
        sendMessage,
        newConversation,
        approveCheckout,
        editCheckout,
        rejectCheckout,
        submitFeedback,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error('useChat must be used within ChatProvider');
  return ctx;
}
