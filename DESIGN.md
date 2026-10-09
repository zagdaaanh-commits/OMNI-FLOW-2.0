---
version: alpha
name: "OmniFlow Workspace"
description: "A bilingual, high-trust marketing operations workspace with a quiet monochrome shell and focused channel accents."
colors:
  primary: "#18181B"
  background: "#FFFFFF"
  surface: "#FFFFFF"
  surface-muted: "#F4F4F5"
  text: "#18181B"
  text-muted: "#71717A"
  border: "#E4E4E7"
  success: "#10B981"
  danger: "#DC2626"
  dark-background: "#0A0A0A"
  dark-surface: "#141414"
  dark-text: "#EDEDED"
typography:
  sans:
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace"
rounded:
  DEFAULT: "0.75rem"
  sm: "0.6rem"
  md: "0.8rem"
  lg: "1.4rem"
  pill: "9999px"
spacing:
  section-gap: "1.5rem"
  page-max: "48rem"
components:
  button: { }
  card: { }
  dialog: { }
  input: { }
  drawer: { }
  toast: { }
---

# OmniFlow Workspace Design System

## Overview

### Creative North Star

A calm editorial workbench: crisp paper-like light surfaces, ink-black controls, and a restrained dark studio counterpart. Channel colors identify connected services, while the application shell stays neutral so campaign content remains the focal point.

### Product context and register

- **Audience and primary job:** Growth and marketing operators who converse with an assistant, develop campaigns, connect channels, and schedule publishing.
- **Target market(s) and evidence:** Global and China-facing social teams; the product explicitly supports Meta, Instagram, TikTok, X, WeChat, Douyin, and Xiaohongshu.
- **Locale(s) and language policy:** English and Simplified Chinese switch together through the maintained `I18N` catalog. English is the markup fallback. New user-facing strings need entries in both locales.
- **Usage scene:** Repeated desktop work with responsive mobile support; chat is the primary low-density surface and campaign modals carry denser operational detail.
- **Register:** Hybrid. The chat shell is product-like and quiet; campaign result cards may use stronger brand and channel expression.
- **Memorable signature:** The centered conversational composer moves into a bottom dock after the first message without changing its component identity.
- **Restraint:** Navigation, forms, history, loading, and errors use familiar controls and subdued surfaces.
- **Anti-references:** Avoid neon cyberpunk dashboards, decorative glass layers on every element, and consumer-social visual noise; they weaken operational clarity.
- **Token ownership/runtime mapping:** This document mirrors the canonical runtime tokens and component rules in `app/static/index.html`. The `:root`, `html.dark`, shared component CSS, and inline Tailwind configuration remain the implementation source of truth.

## Colors

Light mode uses `background` and `surface` as white, `surface-muted` for selected or secondary regions, and zinc `text`/`text-muted` for hierarchy. Dark mode maps the same semantic roles to `dark-background`, `dark-surface`, and `dark-text`. Borders carry most grouping; shadows are secondary. `success` is reserved for connected, completed, or publish-ready states and `danger` for errors or destructive outcomes. Focus uses the current ink color with a visible offset. Selection is a translucent indigo wash. Forced-colors mode restores the platform scrollbar palette.

## Typography

Inter leads the cross-script system stack, with Apple and system fallbacks. Body copy is sentence case and generally 0.8–0.95rem; section labels may use compact uppercase tracking. Assistant and user messages preserve whitespace with comfortable line height and a readable maximum measure. IDs and technical status text may use the mono stack. Chinese and English labels remain concise; italics are not used to distinguish state.

## Layout

The application shell has a stable 56px top bar, one document-level content scroller, and a 48rem chat measure. The drawer is `min(288px, 86vw)` and overlays content on narrow viewports. The empty-state composer is centered; active chat moves the same composer to a stable bottom dock. Dialogs cap their height to the visual viewport and own internal scrolling. Spacing uses the implemented 0.5–1.5rem rhythm rather than a generated scale.

## Elevation & Depth

Hierarchy comes first from tonal surfaces and one-pixel borders. The header, composer, drawer, modal scrim, dialogs, and toast viewport follow the centralized z-index tokens. Shadows belong to floating composers, drawers, menus, and dialogs; routine rows and history items remain flat. Dark mode reduces white opacity to prevent glowing borders.

## Shapes

Controls use `sm` or `DEFAULT` radii, panels and dialogs use `lg`, and compact icon or mode controls may use `pill`. Chat bubbles deliberately exaggerate their outer radius and tighten the speaker-facing corner. Material Symbols Outlined use an unfilled, medium-weight treatment unless a status icon requires emphasis.

## Components

### Foundational visual states

Interactive controls provide default, hover, focus-visible, pressed, selected, disabled, and busy states. Selected history uses `aria-current`; mode uses `aria-pressed`. Busy chat changes only the send button to a spinner and keeps the textarea enabled. Success and error feedback use semantic color plus text. The default app loading indicator is the Material Symbols `progress_activity` spinner; list loading reserves its row footprint.

### Buttons and actions

Solid ink buttons are primary; bordered surface buttons are secondary; icon buttons retain accessible names. Busy actions keep their geometry. Destructive actions must be visually separated and explicitly labeled. Campaign mutation is never inferred from a normal chat submission: users must switch the mode control or invoke **+ New Campaign**.

### Navigation and data display

The top bar remains visible above chat content and owns theme/language/account utilities. The drawer groups **New prompt**, recent conversations, campaigns, scheduling, and integrations. Recent conversation buttons expose the active row with `aria-current="page"` and truncate only the visual title.

### Forms and overlays

Fields use persistent visible labels, app-owned validation messages, and focus rings. Textareas do not resize manually and auto-grow to a 200px cap. The low-risk channel credential selector intentionally uses the native Select owner; platform popup geometry and interaction are accepted. Drawers and dialogs use a scrim, constrained height, Escape-capable app behavior where implemented, and reachable actions. Toasts sit above dialogs and never replace inline recovery for blocking errors.

### Iconography

Material Symbols Outlined is the canonical icon family. Common sizes are 14–24px with optical centering. Icons accompany text for mode changes, navigation, destructive actions, and unfamiliar operations; icon-only utilities require `title` or `aria-label`.

### Motion

Feedback transitions run 100–150ms; drawer and content entrance motion runs about 220ms with ease/ease-out. Motion communicates loading, opening, selection, or newly arrived content. It must be interruptible, and reduced-motion preferences suppress nonessential transforms and repeated animation.

### Content and data visualization

Product voice is concise, direct, and operational. Actions use verbs such as Send, Connect, Publish, Schedule, and Create. Dates and numeric campaign metrics should remain locale-aware. Color never carries campaign status alone; labels or icons accompany it.

## Do's and Don'ts

- **Do:** Keep chat history immediate with local cache, then reconcile against the authenticated database.
- **Do:** Reuse the same composer, theme tokens, drawer rows, and loading language across chat states.
- **Don't:** Hide the global header or theme control based on campaign state.
- **Don't:** create campaigns, drafts, or sidebar entries from ordinary chat-mode questions.
