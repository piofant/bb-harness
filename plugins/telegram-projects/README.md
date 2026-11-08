# Telegram Projects for BB

> Fork of [VKirill/bb-plugin-telegram-projects](https://github.com/VKirill/bb-plugin-telegram-projects) (MIT, © Kirill Vechkasov). Added here: one-chat layout without topics, a pinned chat, quiet reports, photos and files from Telegram, Markdown → Telegram HTML. The optional SMS companion (`companion/aivech`) is not included — take it from upstream; docs that mention it describe the upstream setup.

```
bb plugin install ./plugins/telegram-projects --yes
```

Settings worth setting first: **Публичный адрес BB** (`appUrl`, the https URL links in Telegram point to; defaults to BB's own public URL when BB knows it, otherwise the placeholder `https://bb.example.com` — replace it, or Telegram links will not open), bot token (Connection tab or Env Catalog key `tokenEnv`, default `TELEGRAM_BOT_TOKEN`), `layout` (`topics` / `single`).

Telegram Projects connects BB projects and conversations to a private Telegram bot. It creates a topic for each project, sends configured agent and Lane Pilot task events, and lets the owner continue BB threads with text, voice, photos and files.

## Quick start

1. Install the plugin in BB and open **Telegram** in the sidebar.
2. Create a Telegram bot with @BotFather, enable threaded topics, and enter its token in the plugin's **Connection** tab (or use Env Catalog).
3. Enable project sync in **General**, pair the Telegram owner using the displayed link or `/start <code>`, then choose **Create topics**.
4. Open a project topic and use `/menu` to start or connect a BB conversation.

See [Usage](docs/usage.md) for the Telegram interaction guide, [Overview](docs/overview.md) for capabilities, and [Deployment](docs/deployment.md) for setup requirements.

## One chat without topics

Turn on **One chat, no topics** in **General** (setting `layout: single`) to keep everything in a single bot chat; Threaded Mode in @BotFather may stay off.

- A plain message goes to the current BB chat. Pick it with `/chats`, start one with `/new`, change project with `/project`.
- A reply to an agent's message or report card continues that agent's chat and makes it current.
- Agent reports name the project in the card. Project topics are neither created nor deleted in this layout.
- **Quiet reports** (`doneOnlyMine`) keep «agent finished» cards only for chats you led from Telegram; questions and errors always arrive.
- **One pinned chat** (`pinnedChat`) sends everything to one BB chat. A reply to another chat's card reaches that chat once and the pinned chat stays; an archived pinned chat comes back with the next message, or a new chat starts with it when the old one cannot return.
- `bb telegram-projects chat-pin <thread-id>` (with `pinnedChat`) pins the bot chat to a BB thread from the server, without a tap in Telegram. Prefer a thread in the project checkout: an archive never tears a checkout down, so the pinned chat always comes back.
- Agent replies keep their formatting: without Rich Messages their Markdown goes out as Telegram HTML (bold, italics, code, quotes, lists, links); a part Telegram cannot parse goes as plain text.

## Photos and files

A photo, document, audio file, video or video note sent to the bot reaches the BB chat as an attachment, the same way a file attached in BB's composer does; its caption is the message text. An album goes as one message. Voice notes are still transcribed into text.

- Up to 20 MB per file: Telegram hands bots nothing bigger. A bigger file cancels the message, and the bot asks for a link (Google Drive, for example) or an upload in BB.
- A caption is never a bot command: `/new` under a photo goes to the agent as words.
- An answer to an agent's question is text only; a file in reply to a question card is refused.
- An iPhone HEIC photo sent as a file arrives as a plain file: BB takes no HEIC images.

## Documentation

- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Gotchas](docs/gotchas.md)
- [Research notes](docs/research.md)
- [Roadmap](docs/roadmap.md)
