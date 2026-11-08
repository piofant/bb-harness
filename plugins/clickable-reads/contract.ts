// The plugin's host entry contract, shared by server.ts and host.ts.
//
// bb's macOS desktop app talks to the server over plain http when it uses a
// Tailscale address, and Chromium hides the Clipboard API on insecure pages.
// The host entry runs on the Mac itself and puts the picture on the system
// clipboard there.
import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

import { MAX_CLIPBOARD_PNG_BASE64 } from "./lib/limits";

export { MAX_CLIPBOARD_PNG_BASE64 };

export const hostContract = defineRpcContract({
  describe: {
    input: z.null(),
    output: z.object({ platform: z.string(), desktopRunning: z.boolean() }).strict(),
    experimental_retryable: true,
  },
  writeClipboardImage: {
    input: z
      .object({ pngBase64: z.string().min(1).max(MAX_CLIPBOARD_PNG_BASE64) })
      .strict(),
    output: z.object({ ok: z.literal(true) }).strict(),
  },
});
