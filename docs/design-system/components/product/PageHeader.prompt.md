Every product page opens with one of these.

```jsx
<PageHeader
  title="Queue"
  breadcrumb={["my-agent", "queue"]}
  sub="Every webhook, message, and schedule lands here."
  actions={<><Button variant="ghost" size="sm">Filter</Button><Button variant="primary" size="sm">New event</Button></>}
  tabs={[{ id: "all", label: "all", count: 6 }, { id: "live", label: "live", count: 2 }]}
  activeTab={tab} onTab={setTab}
/>
```

Title goes in as normal text — the component lowercases it. Don't pass pre-cased strings.
