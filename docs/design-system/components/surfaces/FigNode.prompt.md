Bobi explains itself with node diagrams; this is the node.

```jsx
<FigFlow nodes={[
  { icon: <Icon name="ticket" size={18} />, title: "linear", sub: "ENG-142 · bug assigned" },
  { icon: <Icon name="code" size={18} />, title: "engineer agent", sub: "follows sdlc.yaml" },
  { icon: <Icon name="human" size={18} />, title: "human review", sub: "required", gate: true },
  { icon: <Icon name="checkCircle" size={18} />, title: "PR #488 opened", sub: "ready for your review" },
]} />
```

Every Bobi flow reads **event → agent → workflow → gate → outcome**. Keep that order; it is the product's core claim.
