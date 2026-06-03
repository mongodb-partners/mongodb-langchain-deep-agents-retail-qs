import React, { useState } from 'react';
import { useChat } from '../context/ChatContext';

export interface Preset {
  icon: string;
  text: string;
}

export const PRESETS: Preset[] = [
  {
    icon: '🍝',
    text: 'I want to make pasta for 4 tonight. Find the ingredients available in store and their current prices, and save a shopping list.',
  },
  {
    icon: '🏷️',
    text: 'Research promotion-stacking rules on coupons and save the explainer to a file.',
  },
  {
    icon: '🗓️',
    text: 'Plan a week of dinners for a family of 4 on a $150 budget using what’s on sale this week.',
  },
  {
    icon: '⭐',
    text: 'What are the Gold tier loyalty benefits, and how much can I save with my points?',
  },
  {
    icon: '🍗',
    text: 'Find a quick weeknight chicken recipe and check whether the ingredients are in stock.',
  },
  // Demo presets — loyalty, cart-building, deal optimization,
  // reorder, cross-sell, and the HITL checkout flow.
  {
    icon: '🎁',
    text: 'What are my loyalty benefits and how much are my points worth in dollars?',
  },
  {
    icon: '🛒',
    text: 'Add the ingredients for spaghetti bolognese to my cart.',
  },
  {
    icon: '💸',
    text: 'Optimize the savings on my cart — find every coupon I can stack and apply the best ones.',
  },
  {
    icon: '🔁',
    text: 'Look at my past orders and build me a reorder basket of what I buy regularly.',
  },
  {
    icon: '🧺',
    text: "What goes well with what's in my cart? Suggest complementary items.",
  },
  {
    icon: '✅',
    text: 'Check out and place my order.',
  },
];

const sectionStyle: React.CSSProperties = {
  width: '100%',
  maxWidth: 1120,
  margin: '0 auto',
  padding: '56px 24px 24px',
};

const headingStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  textTransform: 'uppercase',
  letterSpacing: '2px',
  color: 'var(--spring-green)',
  marginBottom: 6,
};

const subheadStyle: React.CSSProperties = {
  fontFamily: 'var(--font-display)',
  fontSize: '1.6rem',
  fontWeight: 500,
  color: 'var(--text)',
  marginBottom: 24,
};

const gridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 320px), 1fr))',
  gap: 20,
};

const baseCardStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  padding: '20px 20px 16px',
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-lg)',
  cursor: 'pointer',
  textAlign: 'left',
  color: 'var(--text)',
  transition: 'transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease',
};

const iconStyle: React.CSSProperties = {
  fontSize: 26,
  lineHeight: 1,
};

const cardTextStyle: React.CSSProperties = {
  fontSize: 14,
  lineHeight: 1.55,
  color: 'var(--text)',
  flex: 1,
};

const askStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  textTransform: 'uppercase',
  letterSpacing: '1px',
  color: 'var(--spring-green)',
};

function PresetCard({ preset, onAsk }: { preset: Preset; onAsk: () => void }) {
  const [hover, setHover] = useState(false);
  const style: React.CSSProperties = hover
    ? {
        ...baseCardStyle,
        transform: 'translateY(-2px)',
        borderColor: 'var(--green-border)',
        boxShadow: '0 0 0 1px rgba(0,237,100,0.15), 0 12px 30px rgba(0,0,0,0.35)',
      }
    : baseCardStyle;
  return (
    <button
      type="button"
      style={style}
      onClick={onAsk}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
    >
      <span style={iconStyle} aria-hidden="true">
        {preset.icon}
      </span>
      <span style={cardTextStyle}>{preset.text}</span>
      <span style={askStyle}>Ask →</span>
    </button>
  );
}

export default function PresetGrid() {
  const { sendMessage, setOpen } = useChat();

  const ask = (text: string) => {
    setOpen(true);
    // Launch each preset in a NEW conversation so it's a first-turn query the
    // server can serve from / store in the response cache.
    sendMessage(text, { newThread: true });
  };

  return (
    <section style={sectionStyle} id="presets">
      <div style={headingStyle}>Try a demo prompt</div>
      <div style={subheadStyle}>What can your assistant do?</div>
      <div style={gridStyle}>
        {PRESETS.map((p) => (
          <PresetCard key={p.icon} preset={p} onAsk={() => ask(p.text)} />
        ))}
      </div>
    </section>
  );
}
