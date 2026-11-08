import { existsSync, mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { isBbDesktopRunning, writeMacClipboardImage } from "../host";

const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from("rest-of-png"),
]).toString("base64");

describe("writeMacClipboardImage", () => {
  it("hands the PNG to osascript and removes the temporary file", async () => {
    const tempDir = mkdtempSync(join(tmpdir(), "clip-"));
    let seenFile = "";
    const exec = vi.fn(async (_file: string, args: string[]) => {
      seenFile = args[args.length - 1]!;
      expect(existsSync(seenFile)).toBe(true);
    });
    await writeMacClipboardImage({ pngBase64: PNG, tempDir, platform: "darwin", exec });
    // The path travels as an argument, never inside the script text.
    expect(exec).toHaveBeenCalledWith(
      "osascript",
      [
        "-e",
        "on run argv",
        "-e",
        "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)",
        "-e",
        "end run",
        expect.stringMatching(/clipboard-.+\.png$/),
      ],
      { timeout: 15_000 },
    );
    expect(existsSync(seenFile)).toBe(false);
    expect(readdirSync(tempDir)).toEqual([]);
  });

  it("removes the temporary file when osascript fails", async () => {
    const tempDir = mkdtempSync(join(tmpdir(), "clip-"));
    const exec = vi.fn(async () => {
      throw new Error("osascript failed");
    });
    await expect(
      writeMacClipboardImage({ pngBase64: PNG, tempDir, platform: "darwin", exec }),
    ).rejects.toThrow("osascript failed");
    expect(readdirSync(tempDir)).toEqual([]);
  });

  it("refuses other platforms and data that is not a PNG", async () => {
    const tempDir = mkdtempSync(join(tmpdir(), "clip-"));
    const exec = vi.fn();
    await expect(
      writeMacClipboardImage({ pngBase64: PNG, tempDir, platform: "linux", exec }),
    ).rejects.toThrow("только на macOS");
    await expect(
      writeMacClipboardImage({
        pngBase64: Buffer.from("GIF89a....").toString("base64"),
        tempDir,
        platform: "darwin",
        exec,
      }),
    ).rejects.toThrow("Это не PNG.");
    expect(exec).not.toHaveBeenCalled();
  });
});

describe("isBbDesktopRunning", () => {
  it("asks pgrep for the bb desktop app", async () => {
    const exec = vi.fn(async () => ({ stdout: "123\n" }));
    expect(await isBbDesktopRunning(exec)).toBe(true);
    expect(exec).toHaveBeenCalledWith("pgrep", ["-f", "bb.app/Contents/MacOS/"], { timeout: 5_000 });
  });

  it("is false when pgrep finds nothing or fails", async () => {
    expect(await isBbDesktopRunning(vi.fn(async () => Promise.reject(Object.assign(new Error("exit 1"), { code: 1 }))))).toBe(false);
  });
});
