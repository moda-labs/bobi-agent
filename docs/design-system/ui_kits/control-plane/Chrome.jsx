// Control-plane chrome: the void sidebar + a paper top bar.
const { useState } = React;

function KitSideNav({ active, onSelect, counts }) {
  return (
    <SideNav agent="my-agent" active={active} onSelect={onSelect} tone="light" footer="v0.9.2 · fly.dev"
      items={KIT.nav.map((n) => ({
        ...n,
        icon: <Icon name={n.icon} size={17} color="currentColor" />,
        expandable: n.id === "agents" || n.id === "config",
        count: counts && counts[n.id] != null ? counts[n.id] : n.count,
      }))} />
  );
}

// Thin wrapper so screens read declaratively; PageHeader owns the caps treatment.
function TopBar({ title, sub, right, breadcrumb, tabs, activeTab, onTab }) {
  return (
    <PageHeader title={title} sub={sub} actions={right}
      breadcrumb={breadcrumb || ["my-agent", String(title).toLowerCase()]}
      tabs={tabs} activeTab={activeTab} onTab={onTab} />
  );
}

// A group: sentence-case label OUTSIDE a clean white card. No tinted header bar
// competing with the content — the label belongs to the page, not the card.
function Panel({ label, right, children, pad = 0, style }) {
  return (
    <section style={style}>
      {(label || right) && <SectionLabel action={right}>{label}</SectionLabel>}
      <div className="bobi-card" style={{ overflow: "hidden" }}>
        <div style={{ padding: pad }}>{children}</div>
      </div>
    </section>
  );
}

// Renders a fixture "lines" array into CodeCard token spans.
function KitPane({ name }) {
  const pane = KIT.panes[name];
  if (!pane) return null;
  return (
    <CodeCard title={name} tag={pane.tag} tagAccent={!!pane.accent} rounded>
      {pane.lines.map((l, i) => {
        if (l[0] === "br") return <br key={i} />;
        if (l[0] === "gate") return (
          <React.Fragment key={i}>
            <GateLine><Tok kind="a">{l[1]}</Tok><Tok kind="c">{l[2]}</Tok></GateLine>
          </React.Fragment>
        );
        return <Tok key={i} kind={l[0]}>{l[1]}</Tok>;
      })}
    </CodeCard>
  );
}

Object.assign(window, { KitSideNav, TopBar, Panel, KitPane });
