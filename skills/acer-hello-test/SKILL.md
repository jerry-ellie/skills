---
name: acer-hello-test
description: "Test skill for verifying the Hermes Skills Hub publish/install flow. Use when the user says 'acer hello test', 'test acer skill', or '測試 acer skill'. Replies with a fixed greeting, the current date/time from a bundled script, and a line read from a bundled reference file."
version: 1.0.0
author: Acer
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [Test, Acer, Skills Hub]
    related_skills: []
---

# Acer Hello Test

A minimal skill used only to test that a skill published to a GitHub repo can be found, installed, and run by Hermes Agent. It changes nothing on the computer.

## When to Use

The user says "acer hello test", "test acer skill", "測試 acer skill", or runs `/acer-hello-test`.

## Quick Reference

| Step | Command / File |
|---|---|
| Get time | `python "${HERMES_SKILL_DIR}/scripts/hello.py"` |
| Get message | `${HERMES_SKILL_DIR}/references/message.md` (line starting `MESSAGE:`) |

## Procedure

1. Run the bundled script and keep its output:

   ```bash
   python "${HERMES_SKILL_DIR}/scripts/hello.py"
   ```

   If `python` is not found, try `python3`. If both fail, use `unknown` as the time.

2. Read `${HERMES_SKILL_DIR}/references/message.md` and take the line that starts with `MESSAGE:`.

3. Reply with exactly these three lines (fill in the values):

   ```text
   Acer skill 測試成功 ✅
   時間：{output of step 1}
   訊息：{text after MESSAGE: from step 2}
   ```

## Pitfalls

- Do not install anything, change any file, or call any network service.
- If `references/message.md` is missing, say 「references 檔案沒有被安裝」 — that means the install did not copy the referenced files.

## Verification

All three lines appear and the 訊息 line reads 「Hermes 有成功安裝 references 資料夾」.
