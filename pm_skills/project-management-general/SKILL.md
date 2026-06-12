---
name: project-management-general
description: Guide project management conversations about project status, milestones, owners, blockers, risks, dependencies, follow-up cadence, delivery plans, and recovery actions. Use when users ask how to manage execution, unblock delayed work, clarify next steps, or structure project follow-up.
type: interactive
best_for:
  - "Diagnosing a delayed or at-risk project"
  - "Turning messy project updates into owners, milestones, risks, and next steps"
  - "Creating a follow-up plan for cross-functional delivery"
scenarios:
  - "This project is delayed; how should I follow up?"
  - "Help me summarize project risks and dependencies"
  - "What should I ask the team to clarify project status?"
---

## Purpose

Guide users through practical project management triage and follow-up. Focus on making the current state explicit, separating facts from assumptions, identifying risks and dependencies, assigning owners, and defining the next concrete action.

Use this for execution management. Do not treat it as product strategy, roadmap prioritization, or PRD writing unless the user explicitly asks for those product management workflows.

## Key Concepts

### Project Health Snapshot

Assess project status with five fields:

- Goal: what outcome the project is meant to achieve
- Current state: what is done, in progress, blocked, or not started
- Milestone: the next meaningful delivery checkpoint
- Risk: what could prevent the milestone from being reached
- Owner: who is accountable for the next action

### Risk and Dependency Separation

Risks are possible future problems. Dependencies are things the team needs from another person, team, vendor, system, or decision. Treat them separately so follow-up is precise.

Examples:

- Risk: Engineering capacity may not be enough to finish testing this week
- Dependency: Legal needs to approve the vendor terms before launch
- Blocker: The API credential is missing, so implementation cannot continue

### Recovery Plan

When a project is late or unclear, build a recovery plan:

1. Confirm the target milestone and deadline
2. Identify the critical path
3. List blockers, dependencies, and decisions needed
4. Assign one owner per next action
5. Set a follow-up cadence
6. Communicate tradeoffs if scope, timeline, or quality must change

### Anti-Patterns

- Asking "any updates?" without naming the milestone or decision needed
- Listing risks without an owner or mitigation action
- Treating optimistic dates as commitments before dependencies are resolved
- Escalating before clarifying facts, owners, and requested decision
- Mixing product scope debates with execution follow-up

## Application

When context is incomplete, ask up to three short clarification questions before giving a plan:

1. What is the target milestone or deliverable?
2. What is currently blocked, delayed, or unclear?
3. Who are the main owners or teams involved?

If the user already provided enough context, produce a concise execution plan with this structure:

Project status
- Goal:
- Current state:
- Next milestone:

Risks and dependencies
- Risk:
- Dependency:
- Blocker:

Next actions
- Action:
- Owner:
- Deadline:
- Follow-up cadence:

Recommended message
Provide a short message the user can send to the team or stakeholder. Keep it specific, neutral, and action-oriented.

## Examples

### Delayed Project

User: The launch is delayed because QA found issues and engineering says they need more time.

Good response:
First clarify the launch milestone, the severity of QA issues, and who owns the fix list. Then recommend a recovery plan: freeze non-critical scope, publish a bug triage list with owners, ask engineering for a revised date with confidence level, and set a daily 15-minute checkpoint until launch readiness is clear.

### Ambiguous Status

User: Help me ask the team for a project update.

Good response:
Ask for status by milestone, not generic updates. Request what changed since the last check-in, what is blocked, what decisions are needed, and what the owner commits to before the next checkpoint.

## Common Pitfalls

1. Vague follow-up
Use milestone-specific questions instead of broad update requests.

2. Ownerless actions
Every next step needs exactly one accountable owner, even if multiple people contribute.

3. Hidden tradeoffs
If timeline is at risk, explicitly frame the tradeoff among scope, date, quality, and resourcing.

4. Premature escalation
Escalate only after clarifying facts, impact, requested decision, and the consequence of no decision.
