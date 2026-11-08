// bb-plugin-clickable-reads — host entry, run by the bb daemon on each machine.
//
// Puts a PNG on the macOS system clipboard for the desktop app, whose page
// cannot use the Clipboard API over an insecure http address.
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import { experimental_defineHostEntry } from "@get-bb/plugin-sdk";
import { hostContract } from "./contract";

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

type Exec = (file: string, args: string[], options: { timeout: number }) => Promise<unknown>;

const execFileAsync: Exec = promisify(execFile);

export async function writeMacClipboardImage({
  pngBase64,
  tempDir,
  platform = process.platform,
  exec = execFileAsync,
}: {
  pngBase64: string;
  tempDir: string;
  platform?: string;
  exec?: Exec;
}): Promise<void> {
  if (platform !== "darwin") {
    throw new Error("Картинку в буфер можно положить только на macOS.");
  }
  const png = Buffer.from(pngBase64, "base64");
  if (png.length < PNG_SIGNATURE.length || !png.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) {
    throw new Error("Это не PNG.");
  }
  const file = join(tempDir, `clipboard-${randomUUID()}.png`);
  await writeFile(file, png);
  try {
    // The path goes in as an argument, so no text reaches the script itself.
    await exec(
      "osascript",
      [
        "-e",
        "on run argv",
        "-e",
        "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)",
        "-e",
        "end run",
        file,
      ],
      { timeout: 15_000 },
    );
  } finally {
    await rm(file, { force: true });
  }
}

/** Whether bb's desktop app runs on this Mac: tells several Macs apart. */
export async function isBbDesktopRunning(exec: Exec = execFileAsync): Promise<boolean> {
  try {
    await exec("pgrep", ["-f", "bb.app/Contents/MacOS/"], { timeout: 5_000 });
    return true;
  } catch {
    return false;
  }
}

export default experimental_defineHostEntry({
  contract: hostContract,
  handlers: {
    describe: async () => ({
      platform: process.platform,
      desktopRunning: process.platform === "darwin" ? await isBbDesktopRunning() : false,
    }),
    writeClipboardImage: async ({ pngBase64 }, context) => {
      await writeMacClipboardImage({ pngBase64, tempDir: context.experimental_paths.tempDir });
      return { ok: true as const };
    },
  },
});
