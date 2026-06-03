import React, { useEffect, useState } from 'react';
import { fetchModels, type ModelOption } from '../api/client';
import { useChat } from '../context/ChatContext';

const headerStyle: React.CSSProperties = {
  position: 'sticky',
  top: 0,
  zIndex: 100,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  minHeight: 52,
  padding: '0 20px',
  gap: 16,
  background: 'rgba(6,10,15,0.8)',
  backdropFilter: 'blur(12px)',
  WebkitBackdropFilter: 'blur(12px)',
  borderBottom: '1px solid var(--border)',
};

const leftStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  minWidth: 0,
};

const titleWrapStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  fontFamily: 'var(--font-ui)',
  fontSize: 15,
  fontWeight: 600,
  color: 'var(--text)',
  letterSpacing: '-0.01em',
  lineHeight: 1.2,
  whiteSpace: 'nowrap',
};

const tagStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 10.5,
  color: 'var(--text-secondary)',
  letterSpacing: '0.3px',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const rightStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flexShrink: 0,
};

const badgeStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  padding: '5px 11px',
  background: 'var(--green-tint)',
  border: '1px solid var(--green-border)',
  borderRadius: 'var(--radius-pill)',
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  color: 'var(--text)',
  maxWidth: 320,
};

const selectStyle: React.CSSProperties = {
  background: 'transparent',
  color: 'var(--text)',
  border: 'none',
  outline: 'none',
  fontSize: 11,
  fontFamily: 'var(--font-mono)',
  cursor: 'pointer',
  maxWidth: 270,
};

const userWrapStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '4px 10px',
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-pill)',
};

const userLabelStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  textTransform: 'uppercase',
  letterSpacing: '1px',
  color: 'var(--text-secondary)',
};

const userInputStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  outline: 'none',
  color: 'var(--text)',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  width: 160,
};

function MongoLeaf({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={(size * 120) / 278}
      height={size}
      viewBox="0 0 120 278"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      style={{ flexShrink: 0, filter: 'drop-shadow(0 0 8px rgba(0,237,100,0.4))' }}
    >
      <path
        d="M82.3229 28.6444C71.5367 15.8469 62.2485 2.84945 60.351 0.149971C60.1512 -0.0499903 59.8515 -0.0499903 59.6518 0.149971C57.7542 2.84945 48.4661 15.8469 37.6798 28.6444C-54.9019 146.721 52.2613 226.406 52.2613 226.406L53.1601 227.006C53.959 239.303 55.9565 257 55.9565 257H59.9514H63.9463C63.9463 257 65.9438 239.403 66.7428 227.006L67.6416 226.306C67.7414 226.406 174.905 146.721 82.3229 28.6444ZM59.9514 224.606C59.9514 224.606 55.1576 220.507 53.8592 218.408V218.207L59.6518 89.6326C59.6518 89.2326 60.2511 89.2326 60.2511 89.6326L66.0436 218.207V218.408C64.7453 220.507 59.9514 224.606 59.9514 224.606Z"
        fill="#00ED64"
      />
    </svg>
  );
}

/**
 * Bedrock model badge. Reads `/api/models` to populate; selecting an
 * option updates ChatContext so the next /chat request carries the chosen
 * inference-profile id.
 */
function ModelBadge() {
  const { model, setModel } = useChat();
  const [options, setOptions] = useState<ModelOption[]>([]);
  const [defaultId, setDefaultId] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    fetchModels()
      .then((r) => {
        if (cancelled) return;
        setOptions(r.models);
        setDefaultId(r.default);
        if (!model && r.default) setModel(r.default);
      })
      .catch(() => {
        /* keep empty state on error; chat still works (server uses default) */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={badgeStyle} title="Bedrock inference profile">
      <svg width="9" height="9" viewBox="0 0 16 16" fill="var(--spring-green)">
        <circle cx="8" cy="8" r="5" />
      </svg>
      {options.length === 0 ? (
        <span
          style={{
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {defaultId || 'loading…'}
        </span>
      ) : (
        <select
          style={selectStyle}
          value={model || defaultId}
          onChange={(e) => setModel(e.target.value)}
          aria-label="Select Bedrock model"
        >
          {options.map((o) => (
            <option key={o.id} value={o.id} style={{ color: '#001E2B' }}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

export default function Header() {
  const { userId, setUserId } = useChat();
  return (
    <header style={headerStyle}>
      <div style={leftStyle}>
        <MongoLeaf size={26} />
        <div style={titleWrapStyle}>
          <span style={titleStyle}>Agent Cartsmith</span>
          <span style={tagStyle}>
            MongoDB Atlas + LangChain Deep Agents · retail shopping assistant
          </span>
        </div>
      </div>
      <div style={rightStyle}>
        <ModelBadge />
        <label style={userWrapStyle} title="User ID for per-user memory scope">
          <span style={userLabelStyle}>User</span>
          <input
            style={userInputStyle}
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="demo-user"
            aria-label="User ID"
            spellCheck={false}
          />
        </label>
      </div>
    </header>
  );
}
