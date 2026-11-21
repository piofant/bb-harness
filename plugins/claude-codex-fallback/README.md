# Conversation Fallback for BB

Automatic provider recovery using ordinary native BB threads. The original Claude thread keeps its title and history. Its visible child **Продолжение · Codex** (then Gemini) appears in the same sidebar branch. Each opens as an ordinary BB chat; the continuation's native Parent link opens the source history. BB keeps providers immutable; no BB patch is used. Default chain: Claude → Codex gpt-6.2-sol → Antigravity gemini-3.8-flash, high reasoning, original permissions.

## Install

Requires BB 0.50.1+ and Plugin SDK 0.6.157+. Set up the Codex provider first; the Gemini hop also requires the Antigravity provider (`acp-antigravity`). Recovery is opt-in per thread, not enabled globally by installing the plugin.

```sh
cd plugins/claude-codex-fallback
npm ci --include=dev
bb plugin build .
bb plugin install . --yes
```

## Use

Select **Автопереход** in a thread header and connect once. The plugin opens the native chat. When the provider fails while that chat is open, the app follows the newly created continuation automatically. Opening an older source later keeps its history open; a small **Открыть продолжение →** link leads to the active thread. Active continuations have no extra plugin banner. Use the Autotransition navigation page to list connections and enable/disable recovery.

Earlier hidden continuations are made visible once after upgrade, only when plugin ownership and root metadata match. User-customized child titles are preserved. Old plugin-chat deep links now open the native continuation. The previous stacked unified-screen UI is retired.

CLI: `bb conversation-fallback enable <thread-id>`, `status`, `list`, `recover`, `disable` (JSON output).

Recovery waits for the built-in Provider Retry decision. Auth/verification, billing, exhausted rate limits and provider connection/internal failures qualify. Policy, context limits and unknown failures stop for inspection. The plugin does not repair authentication, read credentials, or bypass account verification.

Original permissions must be supported by the destination; they are never widened. Pending approvals/background work block recovery. History transfer is bounded to the recent 500 selected events / 100000 text characters and 12 historical attachments; the entire source remains available by reference. This is not identical provider-internal context. An interrupted action may already have taken effect: the continuation is told to inspect before repeating it.

Queued messages are copied with their contents, attachments, order and schedule. Original rows are retained in the superseded native thread as a recovery archive, with dispatch postponed to year 9999 before copying (the original schedule is kept in SQLite and applied to the copy); do not edit/re-send those originals after transfer. The plugin never deletes user queue rows. Grouped messages and differing per-message permission modes currently stop automatic transfer for review. Ambiguous create results stop rather than blindly retry. A failed handoff can be inspected and retried with **Повторить переход**, which reconciles the operation metadata.

**Disable** stops future handoffs; the current continuation remains active. Superseded originals remain held while this plugin is running, even if recovery is disabled, to avoid replaying archived queue rows. Originals already parked by transfer remain scheduled in year 9999 if the plugin is uninstalled. Before uninstalling, inspect blocked/untransferred queues; the dispatch hook disappears on uninstall. To manually restore an archived original, first ensure its destination copy was not sent, then change its schedule in BB. The plugin never deletes it on your behalf.

## Updates

Installed outside BB's versioned app directory, using only the public SDK. Ordinary BB updates do not overwrite plugin sources/state. Compatibility across future breaking SDK changes is not guaranteed: after updating BB run the test/build commands and perform a synthetic recovery check. Validated on BB 0.50.1 / SDK 0.6.157 with 14 backend tests, 20 frontend tests, typecheck and a live Claude-failure → Codex continuation check. No custom BB binary is required.

## Develop

`npm install --include=dev`

`npm run test:backend`

`npm run test:app`

`npm run typecheck`

`bb plugin build .`

Local install: `bb plugin install . --yes`.

SQLite state is owned by BB plugin storage. Logs contain thread IDs, provider/model, outcome and timing, not prompt bodies or credentials.
