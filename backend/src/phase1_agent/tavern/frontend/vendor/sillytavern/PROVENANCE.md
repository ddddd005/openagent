# Isolated Tavern Frontend: Source And Modifications

Recorded and modified: 2026-10-08, Asia/Shanghai.
Gemini visible-summary disclosure modified: 2026-10-09, Asia/Shanghai.

## Upstream Identity

- Project: SillyTavern, https://github.com/SillyTavern/SillyTavern.
- Local readonly checkout: `step1/SillyTavern`, branch `release`.
- Source commit: `06bde939fb1e9c4c8d8641d810f0a916b5bce127`.
- This is the inspected local revision, not a claim about the newest upstream.
- Upstream license: GNU Affero General Public License v3. The complete,
  unmodified upstream `LICENSE` is retained beside this document.
- Original authorship remains with SillyTavern and its contributors.
  Existing license and library copyright headers are retained.

## Copied And Adapted Material

`COPY-MANIFEST.json` identifies source files, inclusive source-line ranges,
source-file SHA-256 hashes and delivered-file SHA-256 hashes. Its paths are
relative to this isolated frontend directory.

1. `index.html`: the actual `message_template` structure (avatar wrapper,
   message block, name, copy action, body), `sheld/chat/form_sheld/send_form`,
   textarea and send-area layout are copied and adapted from `public/index.html`.
   The template is now a native HTML template. Native accessible buttons replace
   clickable divs; Lucide replaces the original icon font. Only copy remains
   in the initial message toolbar. P4 restores the isolated branch action,
   visible only for a real completed-round checkpoint and confirmed before
   invoking the platform consumer fork operation.
2. `styles/chat.css`: selected upstream chat-shell, form, message, avatar, text
   and textarea rules retain the original selector/layout structure. Variables
   are local, non-chat rules/imports are omitted, dimensions are responsive and
   ordinary-flow layout replaces draggable absolute placement. No stylesheet
   is loaded into the generic OpenAgent frontend document.
3. `chat.js`: upstream `addOneMessage`, `updateMessageElement` and
   `scrollChatToBottom` insertion, avatar/name/body, last-message and scheduled
   scrolling fragments are adapted to native DOM methods. The Markdown
   conversion/sanitization flow is retained with a narrow HTML allowlist.
   Rolecards, macros, regex hooks, reasoning, media, swipes, message edits,
   context exclusion and ST globals are removed. Input text is never rewritten.
4. `assets/user-default.png` and `assets/assistant.png` are unmodified local ST
   `public/img/user-default.png` and `public/img/ai4.png`. No separate asset
   copyright notice was found in those binary files. Their repository origin is
   recorded, not a separate claim about each artwork's licensing status.

`adapter.js` and `entry.js` are package-local OpenAgent adaptations. They reuse
the shared public GraphChat client, not the ST application or server.

The Gemini addition uses a separate closed `thinking-summaries` information
channel and an independent, initially collapsed disclosure. It shows only the
provider's visible summary as text; opaque signatures and original provider
parts are never imported into the message body or copied text. No ST reasoning
parser, signature fallback or model execution path was introduced.

## Closed Runtime Dependencies

All runtime distributions are copied locally, without modifications:

- Showdown `2.1.0`, MIT, `dist/showdown.min.js`.
- DOMPurify `3.4.16`, Apache-2.0 / MPL-2.0, `dist/purify.min.js`; both original
  license files are retained. This pin is independent of the ST package lock.
- Lucide `0.577.0`, ISC, `dist/umd/lucide.min.js`; its original license also
  includes Feather provenance.

Exact npm tarball identities, integrity hashes, distribution hashes and
license files are in `COPY-MANIFEST.json`. No CDN or `node_modules` path is
used by the shipped page. The only shared host script is
`/static/graph-chat-core.js`.

## Transport And State Boundary

The page uses same-origin consumer discovery, queries, commands and receipt
reads only. It has a distinct local journal/draft namespace and accepts
only the exact `TAVERN_CHAT_DISPLAY@1` public output contract and `TEXT@2`
referenced bodies. The first version explicitly requires one TEXT input
and one public tavern display root.

There is no ST initialization, ST API, chat JSONL, provider API, rolecard,
extension, model-setting or effective-context editing.
Chat messages are immutable public display facts; drafts and unknown sends
are never inserted as backend facts. Existing declared runtime controls
remain platform operations with the original request/reconciliation rules.

P4 fork support reads the restricted consumer checkpoint projection and maps
each record through its actual TEXT artifact producer session/chain identity.
A user message and multiple Agent display entries from the same completed
round share one checkpoint. No message-array truncation is performed.
Confirmation identifies the complete round and its source definition revision.
The platform creates the child; the parent remains unchanged. Only checkpoints
within the current workflow identity are supported in this first consumer UI,
including older revisions and inherited ancestor rounds.

Unknown fork outcomes retain the original parent, candidate, CAS fields and
idempotency key in the isolated local journal. Reopen and reconciliation never
repeat the POST; only a matched original receipt permits adopting the actual
child session. Candidate/read generations, definition identities and pending
locks reject stale or unrelated responses. This does not add parent rewind,
candidate selection, context editing or ST chat-file storage.

## Notice And Delivery Boundary

`legal.html` is linked from the page and makes the retained license, manifest
and the modified shell sources available through same-origin URLs.
This notice records attribution and available local sources. It does not
declare the host's overall license, a license compatibility result, an
independent plugin distribution, or completion of a legal compliance review.
External distribution or network access still requires a separate review of
the complete corresponding source, combination and notice obligations.
