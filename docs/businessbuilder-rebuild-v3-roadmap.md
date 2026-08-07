# Business Builder AI Rebuild v3 Roadmap

## Purpose

Create one production-ready Business Builder AI codebase that keeps the existing Flask backend and integrations while rebuilding the user experience around a polished, futuristic, beginner-friendly product design.

This branch is intentionally isolated from `master` and starts from `voice-command-center-v2` so existing authentication, payments, Shopify, Canva, OpenAI chat, PWA, Dashboard, Settings, Build Center, AI Store Agent, database compatibility, agent infrastructure, and voice work remain protected.

## Non-negotiable safety rules

- Do not touch `.env` or secrets.
- Never expose API keys, Paystack keys, Shopify tokens, Canva tokens, OpenAI keys, call IDs, or private user data.
- Do not remove existing features unless explicitly approved.
- Do not break authentication, Paystack checkout, Shopify, Canva OAuth, OpenAI chat, PWA, Dashboard, Settings, Build Center, or AI Store Agent.
- Keep SQLite/PostgreSQL compatibility.
- Preserve `build_center` roadmap steps.
- Keep browser-control and real voice actions safe-disabled unless their production gates are explicitly met.
- Require approval before irreversible or external agent actions.
- No real OpenAI realtime integration tests unless explicitly authorized.
- Do not merge to `master` without review.

## Repository roles

### `businessbuilder-ai`
The production application and source of truth.

Contains:
- Flask backend
- Authentication and sessions
- Existing database models and persistence
- Paystack
- Shopify
- Canva OAuth
- OpenAI chat and agent services
- PWA
- Dashboard
- Build Center
- AI Store Agent
- Voice Command Center
- Browser/worker infrastructure

### `businessbuilder-replit-ui`
Design-reference repository only.

The exported Replit project is currently a design-canvas/mockup workspace rather than a complete production frontend. Its actual generated Business Builder mockup components must be recovered before exact visual porting.

## Product vision

Business Builder AI should feel like a modern AI command center for entrepreneurs rather than a collection of separate tools.

The core experience should center on three connected systems:

1. **Business Builder Agent** — the intelligence and action layer.
2. **Adventure Mode** — the guided, gamified business-building journey.
3. **Futuristic Voice** — a low-friction conversational control layer over the same agent and tools.

All three must use the same business state, permissions, approvals, and progress data.

## Design system

### Visual direction
- Dark premium foundation with strong contrast.
- Futuristic but professional; avoid childish sci-fi styling.
- Soft glass/panel depth where useful, not everywhere.
- High-quality typography with clear hierarchy.
- Strong spacing rhythm and consistent component sizing.
- Motion should communicate state and progress, not distract.
- Responsive from mobile to desktop.
- Accessibility: keyboard navigation, focus states, readable contrast, reduced-motion support.

### Shared components
- Global navigation and mobile navigation
- Primary/secondary/quiet buttons
- Cards and stat panels
- Mission/progress cards
- Approval cards
- AI messages and action proposals
- Status chips
- Empty states
- Loading/skeleton states
- Error and retry states
- Modal/drawer system
- Toast/notification system
- Voice orb/waveform/status panel

### Core AI visual states
- Idle
- Listening
- Understanding
- Thinking
- Confirming
- Acting
- Speaking
- Error/retry

## Phase 1 — Foundation and visual system

Goals:
- Audit current templates and static assets.
- Identify duplicated styling and page-specific visual systems.
- Define a single token system for spacing, typography, radii, borders, surfaces, shadows, and motion.
- Create shared application shell and navigation.
- Preserve current routes and backend behavior while replacing presentation incrementally.

Acceptance criteria:
- No backend route changes required for purely visual updates.
- Existing auth/session flows still work.
- Mobile and desktop navigation are consistent.
- PWA behavior remains intact.

## Phase 2 — Landing page

Build a polished public landing page with:
- Clear hero and value proposition
- Product demonstration
- How it works
- Business Builder Agent section
- Build Center section
- Shopify/store builder section
- Canva/design section
- Adventure Mode section
- Futuristic Voice section
- Pricing
- Testimonials/social proof placeholders until verified
- FAQ
- Strong signup/login calls to action

The landing page should not claim an integration or automated action is live unless the backend actually supports it.

## Phase 3 — Unified Dashboard

The dashboard becomes the user's operating home, not a link directory.

Key areas:
- Greeting and business identity
- Business health/progress
- Recommended next action
- Current Adventure Mode mission
- Recent AI Agent activity
- Approval queue
- Store/marketing/brand status
- Quick access to Build Center, Store Agent, Voice, Analytics, and Settings

## Phase 4 — Adventure Mode

Adventure Mode is a first-class workflow, not decoration.

Suggested journey:
1. Idea
2. Market research
3. Brand
4. Offer/products
5. Store
6. Marketing
7. Launch
8. Growth

Features:
- Missions
- Progress percentage
- XP/levels where useful
- Achievements tied to real product milestones
- Unlockable tools/templates
- AI companion guidance
- Optional voice narration
- Save/resume progress
- Clear connection to existing `build_center` roadmap steps

Do not create a second competing progress model if the Build Center already contains the required state; Adventure Mode should project/extend the existing roadmap data.

## Phase 5 — Business Builder Agent

The agent should understand:
- User business profile
- Current roadmap position
- Connected apps
- Existing drafts/assets
- Pending approvals
- Previous actions

Primary intents:
- Start a business
- Improve an existing business
- Research a market
- Build a brand
- Create products/offers
- Build or prepare a store
- Create marketing
- Analyse the business
- Prepare launch
- Grow the business

Agent action flow:
1. Understand request.
2. Build a plan.
3. Show proposed actions.
4. Ask for approval when an external/irreversible action is involved.
5. Execute only approved tools.
6. Report result and update progress.

## Phase 6 — Futuristic Voice

Voice is another interface to the same Business Builder Agent, not a separate assistant.

UX:
- Central animated AI core/orb
- Live waveform
- Clear listening/thinking/speaking states
- Interrupt/cancel support
- Voice navigation commands
- Spoken plan and approval summaries
- Text transcript/history only where privacy settings allow it

Architecture rules:
- Server owns API credentials.
- Never accept arbitrary call IDs or provider URLs from the browser.
- Pin allowed realtime provider endpoints.
- Preserve auth, CSRF, same-origin, admission, termination, retry, lease, and liveness gates already developed.
- Keep live voice disabled until worker liveness/heartbeat is proven.

## Phase 7 — Tool integrations

Connect agent tools incrementally rather than all at once.

### Shopify
- Read/store connection state
- Draft products/pages/collections
- Present preview and approval
- Publish only after explicit approval

### Canva
- Generate structured design briefs/prompts
- Use connected Canva flows without exposing tokens
- Keep user confirmation before creating or modifying external assets where appropriate

### Paystack
- Preserve current checkout implementation
- Never let the general agent freely initiate charges
- Keep explicit payment UI and server verification

### Browser/automation worker
- Keep disabled by default
- Restrict allowed actions and URLs
- Use queued worker model, bounded retries, leases, and audit-safe logs

## Phase 8 — Cost-control mode

Development should avoid unnecessary paid API usage.

Default development behavior:
- Mock/demo AI responses for UI flows where possible
- Browser speech APIs or prerecorded/demo states for voice UI development
- Shopify test/draft behavior
- Paystack test mode
- Canva development/test flows
- Real realtime AI only for targeted integration tests
- Browser agent disabled

Track paid-service usage before enabling anything for general users.

## Phase 9 — Quality and release gates

Before production release:
- Unit and integration tests pass
- No secrets in git history/diffs
- Authentication regression test
- Paystack regression test
- Shopify regression test
- Canva OAuth regression test
- OpenAI chat regression test
- PWA regression test
- Dashboard/Settings/Build Center/AI Store Agent regression test
- SQLite and PostgreSQL compatibility checks
- Accessibility smoke checks
- Mobile layout checks
- Worker liveness/heartbeat proof for voice
- Approval/audit flows validated
- No accidental live external actions during tests

## Branch strategy

Current protected rebuild branch:

`businessbuilder-rebuild-v3`

Recommended workflow:
1. Design/audit and shared shell.
2. Commit small, reviewable milestones.
3. Run tests after each milestone.
4. Open a draft PR only after the first coherent milestone is ready for review.
5. Never merge automatically.

## Immediate next tasks

1. Recover the missing Replit Business Builder mockup components from the latest Replit workspace or use screenshots as visual reference.
2. Audit the current landing page, Dashboard, Build Center, AI Store Agent, Settings, and Voice Command Center templates.
3. Define shared design tokens and app shell on this branch.
4. Rebuild the landing page first without changing backend contracts.
5. Review visually before moving to the authenticated product shell.
6. Then implement Dashboard → Adventure Mode → Agent UX → Voice UX → tool integration improvements.
