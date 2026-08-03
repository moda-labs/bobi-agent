// The four control-plane views. Each composes the published components; none of
// them re-style a primitive.
const { useState } = React;

/* ---------------- queue ---------------- */
function QueueScreen({ onOpenGate }) {
  const [sel, setSel] = useState("e1");
  const [tab, setTab] = useState("all");
  const rows = KIT.events.filter((e) =>
    tab === "all" ? true : tab === "live" ? e.state === "live" : tab === "gates" ? !!e.gate : e.state === "done");
  return (
    <>
      <TopBar title="Queue" sub="Every webhook, message, and schedule lands here. The director dispatches from this one list."
        activeTab={tab} onTab={setTab}
        tabs={[
          { id: "all", label: "all", count: KIT.events.length },
          { id: "live", label: "live", count: KIT.events.filter((e) => e.state === "live").length },
          { id: "gates", label: "gates", count: KIT.events.filter((e) => e.gate).length },
          { id: "done", label: "done", count: KIT.events.filter((e) => e.state === "done").length },
        ]} />
      <div style={{ padding: "20px 28px 30px", display: "flex", flexDirection: "column", gap: 20 }}>
        <Toolbar placeholder="Search events" keyHint="F"
          tools={<IconButton label="Filter" icon={<Icon name="sliders" size={16} />} />}
          primary={<Button variant="primary" size="md">New event</Button>} />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(126px, 1fr))", gap: 12 }}>
          <MetricTile label="In queue" value={KIT.events.length} delta="+3 today" deltaTone="up" />
          <MetricTile label="Awaiting a human" value={KIT.events.filter((e) => e.gate).length} accent />
          <MetricTile label="Resolved today" value="12" />
          <MetricTile label="Median run" value="4.2" unit="min" delta="−0.8 vs last wk" deltaTone="up" />
        </div>
        <Panel label="Events" right={<span className="bobi-tnum" style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>{rows.length} shown</span>}>
          <EventRowHeader />
          {rows.map((e) => (
            <EventRow key={e.id} icon={<Icon name={e.icon} size={17} />} title={e.title} source={e.source}
              agent={e.agent} workflow={e.workflow} time={e.time} selected={sel === e.id}
              status={<StatusBadge state={e.state} />}
              onClick={() => { setSel(e.id); if (e.gate) onOpenGate(e.id); }} />
          ))}
          {rows.length === 0 && (
            <p style={{ padding: "28px 18px", textAlign: "center", fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-secondary)" }}>
              Nothing here. The team is watching 2 monitors.
            </p>
          )}
        </Panel>
        <p style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>
          ↳ click a row with a <span style={{ color: "var(--bobi-clay)" }}>gate</span> state to open its approval
        </p>
      </div>
    </>
  );
}

/* ---------------- approvals ---------------- */
function GatesScreen({ decisions, onDecide }) {
  const gated = KIT.events.filter((e) => e.gate);
  return (
    <>
      <TopBar title="Approvals" sub="Runs stopped at a gate. Nothing here proceeds until a person decides."
        right={<Badge tone="clay" dot pill caps={false}>{gated.filter((e) => !decisions[e.id]).length} pending</Badge>} />
      <div style={{ padding: "22px 28px 30px", display: "flex", flexDirection: "column", gap: 18, maxWidth: 780 }}>
        {gated.map((e) => (
          <GateApproval key={e.id} title={e.gate.title} workflow={e.workflow} step={e.gate.step} detail={e.gate.detail}
            decided={decisions[e.id]} decidedBy="you" expiresIn={e.time.replace(" ago", "")}
            onApprove={() => onDecide(e.id, "approved")} onReject={() => onDecide(e.id, "rejected")}>
            <CodeCard title={e.source} tag="Pending" tagAccent>
              {e.gate.body.map((l, i) => (
                <React.Fragment key={i}>
                  <Tok kind={l[0]}>{l[1]}</Tok>{i < e.gate.body.length - 1 && <br />}
                </React.Fragment>
              ))}
            </CodeCard>
          </GateApproval>
        ))}
      </div>
    </>
  );
}

/* ---------------- agents ---------------- */
function AgentsScreen() {
  return (
    <>
      <TopBar title="Agents" sub="A director plus the agents this team needs. Each runs in its own session, in parallel."
        right={<Button variant="ghost" size="sm">Edit roles</Button>} />
      <div style={{ padding: "22px 28px 30px" }}>
        <SectionLabel action={<span className="bobi-tnum" style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>{KIT.agents.length} roles</span>}>Team</SectionLabel>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(290px, 1fr))", gap: 14 }}>
          {KIT.agents.map((a) => (
            <div key={a.name} className="bobi-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
                <span style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 40, height: 40, border: "1px solid var(--border-card)", borderRadius: "var(--radius-lg)", background: "var(--surface-inset)" }}>
                    <Icon name={a.icon} size={21} />
                  </span>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 13.5, fontWeight: 500, color: "var(--bobi-ink)" }}>{a.name}</span>
                </span>
                <StatusBadge state={a.state} />
              </div>
              <p style={{ marginTop: 14, fontSize: 13.5, lineHeight: 1.6 }}>{a.role}</p>
              <div style={{ height: 1, margin: "14px 0 11px", background: "var(--border-hairline)" }}></div>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-secondary)" }}>
                <span>{a.model}</span><span className="bobi-tnum">{a.sessions} sessions</span>
              </div>
            </div>
          ))}
        </div>
        <Panel label="Overnight memory consolidation" style={{ marginTop: 24 }}
          right={<Badge tone="accent" pill caps={false}>☾ nightly</Badge>}>
          <div style={{ padding: 20, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 14 }}>
            <FigNode icon={<Icon name="sessions" size={18} />} title="the team runs" sub="sharper than yesterday" />
            <FigNode icon={<Icon name="tray" size={18} />} title="day ends" sub="23:59 · transcripts in" />
            <FigNode icon={<Icon name="moon" size={18} />} title="memory consolidation" sub="02:00 · sessions → policy.md" gate />
            <FigNode icon={<Icon name="sunrise" size={18} />} title="agents wake" sub="06:59 · policy loaded" />
          </div>
        </Panel>
      </div>
    </>
  );
}

/* ---------------- config ---------------- */
function ConfigScreen() {
  const [file, setFile] = useState("workflows/support-triage.yaml");
  const [monitors, setMonitors] = useState({ stale: true, morning: true });
  return (
    <>
      <TopBar title="Config" sub="An agent is its files. Everything the team does is declared here and versioned like code."
        right={<><Badge tone="accent" dot pill caps={false}>on the clock</Badge><Button variant="ghost" size="sm">Redeploy</Button></>} />
      <div style={{ padding: "22px 28px 30px", display: "grid", gridTemplateColumns: "296px minmax(0, 1fr)", gap: 20, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div>
            <SectionLabel>Files</SectionLabel>
            <FileTree root="my-agent/" status="● on the clock" statusAccent rows={KIT.files} current={file} onSelect={setFile} />
          </div>
          <Panel label="Monitors" pad={18}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <Switch checked={monitors.morning} onChange={(v) => setMonitors({ ...monitors, morning: v })} label="morning-review" sub="cron 07:00" />
              <Switch checked={monitors.stale} onChange={(v) => setMonitors({ ...monitors, stale: v })} label="stale-pr-check" sub="every 1h · nudge after 48h" />
            </div>
          </Panel>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <KitPane name={file} />
          <Panel label="Runtime settings" pad={18}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 14 }}>
              <Input label="Agent name" defaultValue="my-agent" hint="lowercase, no spaces" />
              <Select label="Runtime" options={["claude-code", "openai-codex"]} hint="runs on your existing plan" />
              <Select label="Approvals" options={[{ label: "inline (in thread)", value: "inline" }, { label: "control plane", value: "plane" }]} />
            </div>
          </Panel>
          <Terminal>
            <TermCmd>bobi agent my-agent start</TermCmd><br />
            <TermOut>team online · director + 3 agents · 2 monitors watching</TermOut><br />
            <TermCmd>bobi ask "what shipped yesterday?"</TermCmd><br />
            <TermOut tone="ok">→ 3 PRs merged, 1 rollback approved, 12 tickets resolved.</TermOut><br />
            <TermOut>09:41 first report posted to #eng</TermOut>
          </Terminal>
        </div>
      </div>
    </>
  );
}

Object.assign(window, { QueueScreen, GatesScreen, AgentsScreen, ConfigScreen });
