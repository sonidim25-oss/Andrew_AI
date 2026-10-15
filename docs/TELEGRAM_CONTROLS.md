# Telegram controls

## Deployment status

The command keyboard is implemented in source and awaits installation. The running controller last verified on 2026-10-15 uses `f410a21`, which predates these buttons. No automated tests or live Telegram checks were run for this UI change.

## Owner menu

After installation, send `/start`, `/menu` or any existing command. Andrew's replies include a persistent, compact [Telegram reply keyboard](https://core.telegram.org/bots/api#replykeyboardmarkup):

| Left button | Right button |
|---|---|
| 📊 Status | 💰 Budget |
| 🌐 Browsing history | 📖 Diary |
| 📋 Activity | 🔄 Retry messages |
| ☀️ Wake up | 🌙 Sleep |
| ⏸ Pause | ⏰ Set wake time |
| ❓ Help | 👋 Start |

Buttons use the existing commands and owner/private-chat checks. They do not invoke the model. Wake up clears owner pause; Sleep and Pause retain their existing behavior. Start opens the menu without waking Andrew. Retry messages retains its existing duplicate-delivery semantics.

Set wake time asks for a local 24-hour time, such as `09:00`, using Andrew's configured timezone. The prompt lasts ten minutes and survives a controller restart. Invalid time input asks again. `/cancel` or any command button exits time entry; otherwise ordinary text is treated as the time response until the prompt expires. The prompt itself does not change Andrew's wake state. Setting a time preserves an existing owner pause.

Typed slash commands remain supported, including history searches/deletion and diary dates/retries. Help lists their syntax. Telegram clients can hide the keyboard; use the keyboard icon to reopen it.

## Install

Run on the VPS:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/enable-browser-runtime.sh
```

The existing installer revalidates the runtime and binds its acceptance proof to the new controller files before restarting the controller. A plain copy/restart would leave the proof stale. Existing memory, ledger, pause and schedule state are preserved by the installer. After successful installation, send `/menu` in Telegram to display the buttons.
